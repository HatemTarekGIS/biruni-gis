"""Minimal Biruni relay WebSocket server in a thread for offline tests."""
import base64
import hashlib
import re
import socket
import struct
import threading
import time

TOPIC_RE = re.compile(r"^/r/(biruni[0-9a-f]{40})$")
FRAME_RE = re.compile(r"^[A-Za-z0-9_-]{24,90000}$")


def _read_exact(sock, n):
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            return None
        out.extend(chunk)
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


class FakeOwnRelay:
    """Thread-based WebSocket relay matching relay/worker.js + relay/hub.js."""

    def __init__(self, host="127.0.0.1", port=0):
        self.frames = []
        self.rooms = {}
        self.conns = {}
        self.reject_code = None
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
        return f"ws://127.0.0.1:{self.port}/r/"

    def _accept(self):
        while not self._stopped:
            try:
                c, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _send_close(c, code):
        try:
            c.sendall(_ws_frame(8, struct.pack(">H", code)))
        except OSError:
            pass

    @staticmethod
    def _send_text(c, text):
        try:
            c.sendall(_ws_frame(1, text.encode("utf-8")))
        except OSError:
            pass

    def kick_all(self, code=1013):
        with self.lock:
            peers = list(self.conns.keys())
        for c in peers:
            self._send_close(c, code)
            try:
                c.close()
            except OSError:
                pass

    def _serve(self, c):
        topic = None
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
            req_line = lines[0].split(" ")
            path = req_line[1].split("?", 1)[0] if len(req_line) >= 2 else ""
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
                f"Sec-WebSocket-Accept: {acc}\r\n\r\n"
            )
            c.sendall(resp.encode("ascii"))

            m = TOPIC_RE.match(path)
            if not m:
                self._send_close(c, 1008)
                return
            if self.reject_code is not None:
                self._send_close(c, self.reject_code)
                return
            topic = m.group(1)
            now = time.monotonic()
            with self.lock:
                room = self.rooms.setdefault(topic, {"sockets": set(), "msgs": []})
                room["msgs"] = [(t, ts) for t, ts in room["msgs"] if now - ts <= 60.0]
                replay = [t for t, _ in room["msgs"]]
                room["sockets"].add(c)
                self.conns[c] = topic
            for msg in replay:
                self._send_text(c, msg)

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
                if op != 1:
                    self._send_close(c, 1008)
                    break
                text = data.decode("utf-8", "replace")
                if text == "p":
                    self._send_text(c, "q")
                    continue
                if len(text) > 90000:
                    self._send_close(c, 1009)
                    break
                if not FRAME_RE.match(text):
                    self._send_close(c, 1008)
                    break
                now = time.monotonic()
                with self.lock:
                    self.frames.append((topic, text))
                    room = self.rooms[topic]
                    room["msgs"] = [(t, ts) for t, ts in room["msgs"] if now - ts <= 60.0]
                    room["msgs"].append((text, now))
                    room["msgs"] = room["msgs"][-20:]
                    peers = [o for o in room["sockets"] if o is not c]
                for o in peers:
                    self._send_text(o, text)
        except OSError:
            pass
        finally:
            with self.lock:
                self.conns.pop(c, None)
                if topic and topic in self.rooms:
                    self.rooms[topic]["sockets"].discard(c)
            try:
                c.close()
            except OSError:
                pass

    def stop(self):
        self._stopped = True
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            conns = list(self.conns.keys())
            self.conns.clear()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
