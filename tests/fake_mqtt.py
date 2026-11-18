"""Minimal MQTT-over-WebSocket broker in a thread for offline tests."""
import base64
import hashlib
import socket
import struct
import threading


def _read_exact(sock, n):
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            return None
        out.extend(chunk)
    return bytes(out)


def _varint(n):
    out = bytearray()
    while True:
        b = n & 127
        n >>= 7
        if n:
            b |= 128
        out.append(b)
        if not n:
            return bytes(out)


def _ws_frame(opcode, data):
    n = len(data)
    if n < 126:
        hdr = bytes([0x80 | opcode, n])
    elif n < 65536:
        hdr = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
    return hdr + data


class FakeMqttBroker:
    """Thread-based MQTT 3.1.1 over WebSocket broker for tests."""

    def __init__(self, host="127.0.0.1", port=0, refuse=False, split=False):
        self.refuse = refuse
        self.split = split
        self.published = []
        self.subs = {}
        self.conns = set()
        self.lock = threading.Lock()
        self._stopped = False
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/mqtt"

    def _accept(self):
        while not self._stopped:
            try:
                c, _ = self.sock.accept()
            except OSError:
                break
            with self.lock:
                self.conns.add(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _send_mqtt(self, c, pkt):
        try:
            if self.split and len(pkt) >= 2:
                mid = len(pkt) // 2
                c.sendall(_ws_frame(2, pkt[:mid]))
                c.sendall(_ws_frame(2, pkt[mid:]))
            else:
                c.sendall(_ws_frame(2, pkt))
        except OSError:
            pass

    def _serve(self, c):
        try:
            head = b""
            while not head.endswith(b"\r\n\r\n"):
                b = c.recv(1)
                if not b:
                    return
                head += b
                if len(head) > 16384:
                    return
            lines = head.decode("latin1").split("\r\n")
            hdrs = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            key = hdrs.get("sec-websocket-key", "")
            acc = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode("ascii")
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {acc}\r\n"
            )
            if "mqtt" in hdrs.get("sec-websocket-protocol", "").lower():
                resp += "Sec-WebSocket-Protocol: mqtt\r\n"
            c.sendall((resp + "\r\n").encode("ascii"))

            buf = bytearray()
            while not self._stopped:
                hdr = _read_exact(c, 2)
                if hdr is None:
                    break
                b0, b1 = hdr[0], hdr[1]
                n = b1 & 127
                if n == 126:
                    ext = _read_exact(c, 2)
                    if ext is None:
                        break
                    n = struct.unpack(">H", ext)[0]
                elif n == 127:
                    ext = _read_exact(c, 8)
                    if ext is None:
                        break
                    n = struct.unpack(">Q", ext)[0]
                mask = _read_exact(c, 4) if (b1 & 128) else b"\x00\x00\x00\x00"
                if mask is None:
                    break
                raw = _read_exact(c, n) if n else b""
                if raw is None:
                    break
                data = bytes(x ^ mask[i % 4] for i, x in enumerate(raw)) if (b1 & 128) else raw
                op = b0 & 15
                if op == 8:
                    break
                if op == 9:
                    c.sendall(_ws_frame(10, data))
                    continue
                if op not in (0, 1, 2):
                    continue
                buf.extend(data)
                if not self._drain_mqtt(c, buf):
                    break
        except OSError:
            pass
        finally:
            with self.lock:
                self.subs.pop(c, None)
                self.conns.discard(c)
            try:
                c.close()
            except OSError:
                pass

    def _drain_mqtt(self, c, buf):
        pos = 0
        while pos + 1 < len(buf):
            h0 = buf[pos]
            ptype, flags = h0 >> 4, h0 & 15
            j = pos + 1
            mul, rlen, ok = 1, 0, False
            for _ in range(4):
                if j >= len(buf):
                    break
                b = buf[j]
                j += 1
                rlen += (b & 127) * mul
                if not (b & 128):
                    ok = True
                    break
                mul *= 128
            if not ok or j + rlen > len(buf):
                break
            body = bytes(buf[j:j + rlen])
            pos = j + rlen
            if not self._handle_packet(c, ptype, flags, body):
                del buf[:pos]
                return False
        if pos:
            del buf[:pos]
        return True

    def _handle_packet(self, c, ptype, flags, body):
        if ptype == 1:  # CONNECT
            if self.refuse in (True, "conn"):
                self._send_mqtt(c, b"\x20\x02\x00\x05")
                return False
            self._send_mqtt(c, b"\x20\x02\x00\x00")
        elif ptype == 8:  # SUBSCRIBE
            if len(body) < 4:
                return False
            pid_hi, pid_lo = body[0], body[1]
            tlen = (body[2] << 8) | body[3]
            topic = body[4:4 + tlen].decode("utf-8", "replace")
            if self.refuse == "sub":
                self._send_mqtt(c, bytes([0x90, 0x03, pid_hi, pid_lo, 0x80]))
            else:
                with self.lock:
                    self.subs.setdefault(c, set()).add(topic)
                self._send_mqtt(c, bytes([0x90, 0x03, pid_hi, pid_lo, 0x00]))
        elif ptype == 3:  # PUBLISH
            if len(body) < 2:
                return False
            tlen = (body[0] << 8) | body[1]
            topic = body[2:2 + tlen].decode("utf-8", "replace")
            qos = (flags >> 1) & 3
            off = 2 + tlen
            pid_bytes = b""
            if qos > 0:
                pid_bytes = body[off:off + 2]
                off += 2
            payload = body[off:]
            with self.lock:
                self.published.append((topic, payload))
                targets = [o for o, ts in self.subs.items() if topic in ts]
            if qos == 1 and len(pid_bytes) == 2:
                self._send_mqtt(c, b"\x40\x02" + pid_bytes)
            tbytes = topic.encode("utf-8")
            pbody = struct.pack(">H", len(tbytes)) + tbytes + payload
            pkt = b"\x30" + _varint(len(pbody)) + pbody
            for o in targets:
                self._send_mqtt(o, pkt)
        elif ptype == 12:  # PINGREQ
            self._send_mqtt(c, b"\xD0\x00")
        elif ptype == 14:  # DISCONNECT
            return False
        return True

    def stop(self):
        self._stopped = True
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            conns = list(self.conns)
            self.conns.clear()
            self.subs.clear()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
