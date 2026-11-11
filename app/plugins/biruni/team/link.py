"""A room reached from outside its network without a tunnel: a direct link between the computers (WebRTC), the
way the phones reach the computer over the Biruni bridge (field_photos/bridge.py) - nothing to download, no
account, no server of ours.

Each computer runs a hidden Chromium page (bridge_host.py) that only carries bytes: between the WebRTC channels and
one local connection (a WebSocket) to this module. The pages meet through the public relays with a meeting name
made from the room key, and the relays see only that name and sealed bytes.

  * The host's page answers every member that asks; each request a member sends comes out here and goes to the
    room's own server on this computer (hub.py) - unchanged, sealed with the room key as always.
  * A member's room client is given an address on this computer (``member_address``): its requests go over the
    link to the host and the answers come back. The room's code needs no change - an address "link:" is all.

The PC identity of the bridge (signed answers, the pin &i= in a phone's link; field_photos/bridge.py) is not used
here: the link key comes from the room key, which only members hold, and every request is sealed with the room key
as before - a stranger cannot answer in the host's place, and a member already reaches the room.

Python does the work, the page only carries: a page's own requests would be held to six at a time (a browser's
limit for one server), and every member keeps one open while it waits for news.

The messages on a channel: b"H" + id (4 bytes) + JSON (path or status, size), b"D" + id + a piece (60 KB at most),
b"E" + id. Between the page and this module: 1 byte (1 data, 2 opened, 3 closed, 4 state) + channel number (4) + data.
"""
import base64
import hashlib
import http.client
import json
import re
import secrets
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..secure import crypto

PIECE = 60000             # bytes a channel message carries (Chromium takes up to 256 KB)
PATH_RE = re.compile(r"^[a-z]{1,24}$")
WAIT_OPEN_S = 25          # a member's request waits this long for the link to open
ANSWER_S = 150            # … and this long for the host's answer
_MEMBERS = {}             # link key -> MemberLink (one per room on this computer)
_LOCK = threading.Lock()


def link_key(room_key):
    """The bridge key of a room's link (a different meeting name than the room's other uses)."""
    raw = crypto.hkdf(bytes(room_key), b"biruni-team", b"link-1")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


PAGE_JS = r"""
/* Biruni room link: carries bytes between the WebRTC channels and this computer's room (team/link.py) */
const HP=new URLSearchParams(location.hash.slice(1)),ROLE=HP.get('r');
const sock=new WebSocket(location.href.replace(/^http/,'ws').replace(/#.*$/,'').replace(/page$/,'ws'));sock.binaryType='arraybuffer';
const peers=new Map();let nextPeer=1,pc=null,sid=null,linking=false,backoff=3000,retry=null;
function up(t,p,data){const h=new Uint8Array(5);h[0]=t;new DataView(h.buffer).setUint32(1,p);
 if(sock.readyState===1)sock.send(data?cat(h,data):h)}
function state(o){up(4,0,enc.encode(JSON.stringify(o)))}
function carry(ch,p,onEnd){ch.binaryType='arraybuffer';const x={ch:ch,q:[],busy:false};peers.set(p,x);
 ch.onopen=()=>{up(2,p);state({peers:peers.size})};
 ch.onclose=()=>{if(peers.get(p)===x){peers.delete(p);up(3,p);state({peers:peers.size})}if(onEnd)onEnd()};
 ch.onmessage=e=>{if(typeof e.data!=='string')up(1,p,new Uint8Array(e.data))}}
async function toPeer(p,data){const x=peers.get(p);if(!x)return;x.q.push(data);if(x.busy)return;x.busy=true;
 while(x.q.length){while(x.ch.readyState==='open'&&x.ch.bufferedAmount>4194304)await sleep(15);
  if(x.ch.readyState!=='open'){x.q.length=0;break}x.ch.send(x.q.shift())}x.busy=false}
sock.onmessage=e=>{const u=new Uint8Array(e.data);if(u[0]===1)toPeer(new DataView(u.buffer).getUint32(1),u.slice(5))};
/* the host: answers each member that asks */
async function onOffer(o){if(o.t!=='offer'||!o.sid||!o.sdp)return;const me=new RTCPeerConnection({iceServers:BR.ice}),p=nextPeer++;
 const end=()=>{try{me.close()}catch(e){}};
 me.ondatachannel=e=>carry(e.channel,p,end);
 me.oniceconnectionstatechange=()=>{if(me.iceConnectionState==='failed'){state({failed:1});end()}};
 setTimeout(()=>{if(!peers.has(p))end()},45000);
 try{await me.setRemoteDescription({type:'offer',sdp:o.sdp});await me.setLocalDescription(await me.createAnswer());
  await gathered(me,3000);await SIG.send({t:'answer',sid:o.sid,sdp:me.localDescription.sdp},!!o.z)}catch(e){end()}}
/* a member: asks the host, and again whenever the link drops (another computer may hold the room by then) */
function later(){clearTimeout(retry);retry=setTimeout(connect,backoff);backoff=Math.min(backoff*2,30000)}
async function connect(){if(linking||peers.has(1))return;if(!SIG.up()){state({linking:1});return later()}
 linking=true;state({linking:1});const my=sid=rid(),me=pc=new RTCPeerConnection({iceServers:BR.ice});
 const drop=()=>{if(pc===me){pc=null;linking=false;try{me.close()}catch(e){}later()}};
 try{carry(me.createDataChannel('room',{ordered:true}),1,drop);
  me.onconnectionstatechange=()=>{if(me.connectionState==='connected'){linking=false;backoff=3000}};
  me.oniceconnectionstatechange=()=>{if(me.iceConnectionState==='failed'){state({failed:1});drop()}};
  await me.setLocalDescription(await me.createOffer());await gathered(me,3000);if(pc!==me)return;
  await SIG.send({t:'offer',sid:my,sdp:me.localDescription.sdp,z:canZ},true);
  await sleep(20000);if(pc===me&&!peers.has(1)||pc===me&&peers.get(1).ch.readyState!=='open')drop()}catch(e){drop()}}
async function onSignal(o){if(o.t==='answer'&&o.sid===sid&&pc&&pc.signalingState==='have-local-offer'){
 try{await pc.setRemoteDescription({type:'answer',sdp:o.sdp})}catch(e){}}}
sock.onopen=async()=>{await bridgeKey(HP.get('k')||'');
 SIG.start(ROLE==='h'?onOffer:onSignal,n=>{state({relays:n});if(ROLE!=='h'&&n&&!linking&&!peers.has(1)){backoff=3000;connect()}});
 if(ROLE!=='h')connect()};
"""


def page_html():
    from ..field_photos.bridge import _core

    return ("<!doctype html><html><head><meta charset='utf-8'><title>Biruni room link</title></head><body><script>"
            + _core() + PAGE_JS + "</script></body></html>").encode("utf-8")


# ---- a WebSocket, only the little the page needs ------------------------------------------------------------------
class _Socket:
    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile
        self.lock = threading.Lock()
        self.closed = False

    def send(self, data):
        n = len(data)
        head = bytes([0x82]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                else bytes([127]) + struct.pack(">Q", n))
        with self.lock:
            if self.closed:
                return False
            try:
                self.wfile.write(head + data)
                self.wfile.flush()
                return True
            except OSError:
                self.closed = True
                return False

    def receive(self):
        """One whole message (bytes), or None when the page went away."""
        try:
            return self._receive()
        except (OSError, ValueError, struct.error):
            return None

    def _receive(self):
        parts = []
        while True:
            head = self.rfile.read(2)
            if len(head) < 2:
                return None
            fin, op, n = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self.rfile.read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self.rfile.read(8))[0]
            mask = self.rfile.read(4) if head[1] & 0x80 else b"\0\0\0\0"
            data = self.rfile.read(n)
            if len(data) < n:
                return None
            if n and mask != b"\0\0\0\0":   # the whole block at once (a byte at a time took seconds a snapshot)
                key = (mask * (n // 4 + 1))[:n]
                data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
            if op == 8:
                return None
            if op == 9:   # ping
                with self.lock:
                    try:
                        self.wfile.write(bytes([0x8A, len(data)]) + bytes(data[:125]))
                    except OSError:
                        return None
                continue
            if op in (0, 1, 2):
                parts.append(bytes(data))
                if fin:
                    return b"".join(parts)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # a page or a room client that went away
        import sys

        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class _Link:
    """The local server of one link: the hidden page, its WebSocket, and (a member) the room client's requests."""

    role = "h"

    def __init__(self, key):
        self.key = key
        self.token = secrets.token_urlsafe(16)
        self.sock = None
        self.state = {}
        self.peers = set()
        self._incoming = {}       # (peer, id) -> [head, pieces]
        self.host = None
        link = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _mine(self):
                bits = self.path.split("?")[0].strip("/").split("/")
                if not bits or not secrets.compare_digest(bits[0], link.token) or self.client_address[0] != "127.0.0.1":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return bits[1:]

            def do_GET(self):  # noqa: N802
                rest = self._mine()
                if rest is None:
                    return
                if rest == ["page"]:
                    body = page_html()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif rest == ["ws"] and self.headers.get("Upgrade", "").lower() == "websocket":
                    accept = base64.b64encode(hashlib.sha1((self.headers.get("Sec-WebSocket-Key", "") +
                                                            "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept.decode())
                    self.end_headers()
                    self.wfile.flush()
                    self.close_connection = True
                    link._serve(_Socket(self.rfile, self.wfile))
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_POST(self):  # noqa: N802 - a member's room client
                rest = self._mine()
                if rest is None:
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n > 0 else b""
                if len(rest) == 2 and rest[0] == "r" and PATH_RE.match(rest[1]) and link.role == "m":
                    status, out = link.request(rest[1], body)
                else:
                    status, out = 404, b""
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def start_page(self):
        from ..field_photos.bridge import BridgeHost

        self.host = BridgeHost(f"http://127.0.0.1:{self.port}/{self.token}/page#k={self.key}&r={self.role}")

    def stop(self):
        if self.host is not None:
            self.host.stop()
            self.host = None
        try:
            self.server.shutdown()
            self.server.server_close()
        except OSError:
            pass
        if self.sock is not None:
            self.sock.closed = True

    def running(self):
        return self.host is not None and self.host.running()

    # ---- the page ----------------------------------------------------------------------------------------------
    def _serve(self, sock):
        self.sock = sock
        while True:
            msg = sock.receive()
            if msg is None:
                break
            if len(msg) < 5:
                continue
            kind, peer, data = msg[0], int.from_bytes(msg[1:5], "big"), msg[5:]
            if kind == 1:
                self._piece(peer, data)
            elif kind == 2:
                self.peers.add(peer)
                self._opened(peer)
            elif kind == 3:
                self.peers.discard(peer)
                self._closed(peer)
            elif kind == 4:
                try:
                    self.state.update(json.loads(data.decode("utf-8")))
                except ValueError:
                    pass
        if self.sock is sock:
            self.sock = None
            for p in list(self.peers):
                self.peers.discard(p)
                self._closed(p)

    def send(self, peer, head, mid, body):
        """One message to a peer: its head (path or status), then the body in pieces."""
        sock = self.sock
        if sock is None:
            return False
        pre = bytes([1]) + peer.to_bytes(4, "big")
        tag = mid.to_bytes(4, "big")
        ok = sock.send(pre + b"H" + tag + json.dumps(dict(head, size=len(body))).encode("utf-8"))
        for i in range(0, len(body), PIECE):
            ok = ok and sock.send(pre + b"D" + tag + body[i:i + PIECE])
        return ok and sock.send(pre + b"E" + tag)

    def _piece(self, peer, data):
        if len(data) < 5:
            return
        kind, mid, rest = data[:1], int.from_bytes(data[1:5], "big"), data[5:]
        k = (peer, mid)
        if kind == b"H":
            try:
                head = json.loads(rest.decode("utf-8"))
            except ValueError:
                return
            if isinstance(head, dict) and 0 <= int(head.get("size", 0)) <= 80 * 2**20:
                self._incoming[k] = [head, []]
        elif kind == b"D" and k in self._incoming:
            self._incoming[k][1].append(rest)
        elif kind == b"E" and k in self._incoming:
            head, pieces = self._incoming.pop(k)
            body = b"".join(pieces)
            if len(body) == int(head.get("size", -1)):
                self._message(peer, mid, head, body)

    def _opened(self, peer):
        pass

    def _closed(self, peer):
        for k in [k for k in self._incoming if k[0] == peer]:
            self._incoming.pop(k, None)

    def _message(self, peer, mid, head, body):
        raise NotImplementedError


class HostLink(_Link):
    """The host's side: each request a member sends goes to the room's server on this computer, unchanged."""

    role = "h"

    def __init__(self, key, hub_port):
        super().__init__(key)
        self.hub_port = hub_port
        self.start_page()

    def _message(self, peer, mid, head, body):
        path = str(head.get("path", ""))
        if not PATH_RE.match(path):
            return
        threading.Thread(target=self._forward, args=(peer, mid, path, body), daemon=True).start()

    def _forward(self, peer, mid, path, body):
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.hub_port, timeout=ANSWER_S)
            c.request("POST", "/" + path, body=body, headers={"Content-Type": "application/octet-stream"})
            r = c.getresponse()
            status, out = r.status, r.read()
            c.close()
        except (OSError, http.client.HTTPException):
            status, out = 503, b""
        self.send(peer, {"status": status}, mid, out)

    def members(self):
        return len(self.peers)


class MemberLink(_Link):
    """A member's side: the room client's requests go over the link to whichever computer holds the room."""

    role = "m"

    def __init__(self, key):
        super().__init__(key)
        self._next = 0
        self._wait = {}          # id -> [event, status, body]
        self._lock = threading.Lock()
        self._open = threading.Event()
        self.start_page()

    @property
    def address(self):
        return f"http://127.0.0.1:{self.port}/{self.token}/r"

    def _opened(self, peer):
        if peer == 1:
            self._open.set()

    def _closed(self, peer):
        super()._closed(peer)
        if peer == 1:
            self._open.clear()
            with self._lock:   # the link dropped: what waited is answered now, so the room client tries again
                waiting, self._wait = self._wait, {}
            for w in waiting.values():
                w[1], w[2] = 502, b""
                w[0].set()

    def request(self, path, body):
        if not self._open.wait(WAIT_OPEN_S):
            return 504, b"no link to the room yet"
        with self._lock:
            self._next = (self._next + 1) % 2**31
            mid = self._next
            w = self._wait[mid] = [threading.Event(), 504, b""]
        if not self.send(1, {"path": path}, mid, body):
            with self._lock:
                self._wait.pop(mid, None)
            return 502, b""
        if not w[0].wait(ANSWER_S):
            with self._lock:
                self._wait.pop(mid, None)
            return 504, b"the room did not answer"
        return w[1], w[2]

    def _message(self, peer, mid, head, body):
        with self._lock:
            w = self._wait.pop(mid, None)
        if w is not None:
            w[1], w[2] = int(head.get("status", 502)), body
            w[0].set()


def member_address(room_key):
    """The address a member's room client uses for a host reached over the link ("link:")."""
    key = link_key(room_key)
    with _LOCK:
        m = _MEMBERS.get(key)
        if m is None or not m.running():
            if m is not None:
                m.stop()
            m = _MEMBERS[key] = MemberLink(key)
        return m.address


def stop_member(room_key):
    with _LOCK:
        m = _MEMBERS.pop(link_key(room_key), None)
    if m is not None:
        m.stop()


def wait_member_open(room_key, seconds):
    """Test helper and the join window: True once this computer's link to the room is open."""
    m = _MEMBERS.get(link_key(room_key))
    return m is not None and m._open.wait(seconds)

