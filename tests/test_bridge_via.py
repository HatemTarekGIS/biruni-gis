r"""A teammate carries you: a phone whose network allows no direct channel to the PC sends through a teammate's
phone that does reach it - sealed for the PC alone, so the phone that carries cannot read it.

All on this PC, hidden: the PC's receiver (bridge_host.py), a helper phone and a guest phone (Chromium pages),
and a Nostr relay in place of the public ones - no internet needed.

Run:  engine\bin\python-qgis.bat tests\test_bridge_via.py
"""
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
RELAY_PORT = 10189
os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{RELAY_PORT}"
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox --disable-features=WebRtcHideLocalIpsWithMdns"

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])
profile = QWebEngineProfile.defaultProfile()
from biruni.field_photos import bridge  # noqa: E402
from biruni.field_photos.server import PhotoServer, clean_pos, phone_texts  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


work = tempfile.mkdtemp()
ALL = {"en": phone_texts(lambda s: s)}
page = bridge.phone_page(ALL)
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
if node:
    for name, script in (("phone page", page.split("<script>")[1].split("</script>")[0]),
                         ("receiver", bridge.receiver_page().decode().split("<script>")[1].split("</script>")[0])):
        p = os.path.join(work, "s.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        check(r.returncode == 0, f"the {name} script has no syntax error " + r.stderr[:300])
check(ALL["en"].get("via") == "Connected through a teammate's phone.", "the phone has the words for it")
check(clean_pos({"id": "a" * 16, "lat": 1, "lon": 1, "route": "peer"})["route"] == "peer", "the team list knows the carried route")


# ---- a Nostr relay on this PC ----------------------------------------------------------------------------------
class Relay:
    def __init__(self, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.subs, self.events, self.lock = {}, [], threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            c, _ = self.sock.accept()
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        f = c.makefile("rb")
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            b = f.read(1)
            if not b:
                return
            head += b
        k = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n") if ln.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(k + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        with self.lock:
            self.subs[c] = []
        try:
            while True:
                b0, b1 = f.read(2)
                n = b1 & 127
                if n == 126:
                    n = struct.unpack(">H", f.read(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", f.read(8))[0]
                mask = f.read(4) if b1 & 128 else bytes(4)
                data = bytes(x ^ mask[i % 4] for i, x in enumerate(f.read(n)))
                if b0 & 15 == 8:
                    break
                if b0 & 15 == 1:
                    self._msg(c, json.loads(data.decode("utf-8")))
        except (OSError, ValueError):
            pass
        with self.lock:
            self.subs.pop(c, None)

    def _send(self, c, obj):
        d = json.dumps(obj).encode("utf-8")
        h = bytes([0x81]) + (bytes([len(d)]) if len(d) < 126 else bytes([126]) + struct.pack(">H", len(d))
                             if len(d) < 65536 else bytes([127]) + struct.pack(">Q", len(d)))
        try:
            c.sendall(h + d)
        except OSError:
            pass

    def _msg(self, c, msg):
        if msg[0] == "REQ":
            with self.lock:
                self.subs[c].append((msg[1], set(msg[2].get("#t", []))))
        elif msg[0] == "EVENT":
            ev = msg[1]
            self.events.append(ev)
            self._send(c, ["OK", ev["id"], True, ""])
            topics = {t[1] for t in ev["tags"] if t[0] == "t"}
            with self.lock:
                targets = [(o, sid) for o, subs in self.subs.items() for sid, want in subs if want & topics]
            for o, sid in targets:
                self._send(o, ["EVENT", sid, ev])


relay = Relay(RELAY_PORT)
folder = os.path.join(work, "Field photos")
srv = PhotoServer(folder, os.path.join(work, "cert"), ALL, "en", ["pole", "sign"], 10188, ["127.0.0.1"], bind="127.0.0.1")
key = bridge.new_key()
host = bridge.BridgeHost(srv.url("127.0.0.1") + "bridge#" + bridge.host_frag(key))
evs = []


def js_log():
    return " | " + " | ".join(x for x in host.log[-5:] if "js:" in x)


def drain():
    while not srv.events.empty():
        evs.append(srv.events.get_nowait())
    return evs


check(wait(lambda: any(e[0] == "bridge" and e[1].get("type") == "relays" and e[1].get("up") for e in drain()), 60),
      "the PC's receiver reached the relay" + js_log())


class Phone(QWebEnginePage):
    def __init__(self, name, url):
        super().__init__(profile)
        self.name = name
        self.setHtml(page, QUrl(url))

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print(f"{self.name} js:", message, line, flush=True)

    def js(self, expr):
        out = {}
        self.runJavaScript(expr, lambda v: out.update(v=v))
        wait(lambda: "v" in out, 10)
        return out.get("v")


PHOTO = r"""(async()=>{const b=new Uint8Array(%d);for(let i=0;i<b.length;i+=65536)crypto.getRandomValues(b.subarray(i,i+65536));
 b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);
 const meta={id:'%s',lat:24.7136,lon:46.6753,acc:4,compass:90,t:Date.now(),kind:'pole',note:'%s',device:'test'};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});flush()})();1"""

# ---- the helper: a phone that reaches the PC directly -----------------------------------------------------------
helper = Phone("helper", f"https://helper.invalid/p/#k={key}&l=en")   # an old QR code: no pin
check(wait(lambda: helper.js("direct()") is True, 60), "the helper phone has a direct channel to the PC" + js_log())
check(helper.js("ID.pin") is None, "a link without the pin (printed before) still connects, unchecked as before")
# what the helper passes on is recorded, to see that it cannot read it
helper.js(r"""window._fwd=[];window._bin=[];const _s=RTCDataChannel.prototype.send;
RTCDataChannel.prototype.send=function(d){if(this===dc){if(typeof d==='string'){if(d.includes('"fwd'))window._fwd.push(d.slice(0,400))}
 else if(window._fwd.length&&window._fwd[window._fwd.length-1].includes('fwdb')&&window._bin.length<3)
  window._bin.push([...new Uint8Array(d.buffer||d).slice(0,4)].map(x=>x.toString(16).padStart(2,'0')).join(''))}
 return _s.call(this,d)};1""")

# ---- the guest: a phone that may not reach the PC directly (v=1) -------------------------------------------------
# first a dishonest helper: it swaps the PC's key in the hello it carries (to read the guest)
helper.js(r"""window._swap=null;crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},true,['deriveBits']).then(async k=>{
 window._swap=b64u.enc(await crypto.subtle.exportKey('raw',k.publicKey))});const _s2=RTCDataChannel.prototype.send;window._swapped=0;
RTCDataChannel.prototype.send=function(d){if(this!==dc&&window._swap&&typeof d==='string'&&d.includes('"t":"hello"')){
 d=d.replace(/"e":"[^"]+"/,'"e":"'+window._swap+'"');window._swapped++}return _s2.call(this,d)};1""")
guest = Phone("guest", f"https://guest.invalid/p/#k={key}&i={bridge.pin(key)}&l=en&v=1")
check(wait(lambda: (helper.js("window._swapped") or 0) >= 1 and (guest.js("SIG.refused||0") or 0) >= 1, 90),
      "a helper that swaps the PC's key in the hello is refused by the guest" + js_log())
check(guest.js("!!(VIA&&VIA.ready)") is False and guest.js("lineOpen()") is False, "... and the guest has no line through it")
helper.js("window._swap=null;window._noswap=1;1")
check(wait(lambda: guest.js("!!(VIA&&VIA.ready)") is True, 150), "the guest phone is carried by the helper to the PC" + js_log())
check(guest.js("direct()") is False and guest.js("lineOpen()") is True, "it has no channel of its own to the PC, and still a line")
check(wait(lambda: guest.js("KINDS.join(',')") == "pole,sign", 10), "the PC's kinds reach it through the helper")
check(guest.js("lastSt&&lastSt[0]") is True, "it shows as connected")
check(any(e[0] == "bridge" and e[1].get("type") == "phone" and e[1].get("via") for e in drain()),
      "the PC hears a phone that came through a teammate")
check(helper.js("carried.size") == 1, "the helper carries one teammate")

guest.js(PHOTO % (300000, "viaphoto-0001", "through a teammate"))
got = []
check(wait(lambda: bool(got.extend(e[1] for e in drain() if e[0] == "photo") or got), 90), "the guest's photo arrived on the PC")
rec = got[0] if got else {}
check(os.path.isfile(rec.get("photo", "")) and os.path.getsize(rec["photo"]) == 300000, "on disk, every byte (300 KB)")
check(rec.get("note") == "through a teammate" and rec.get("id") == "viaphoto-0001", "with its data")
check(wait(lambda: guest.js("sent") == 1, 15), "the guest got the PC's confirmation back through the helper")
seen = helper.js("JSON.stringify([window._fwd,window._bin])") or "[[],[]]"
fwd, bins = json.loads(seen)
check(len(fwd) > 10 and "through a teammate" not in seen and "viaphoto" not in seen and all(b != "ffd8ffe0" for b in bins) and bins,
      f"the helper passed {len(fwd)} messages on and could read none: no note, no name, no picture (first bytes {bins[:2]})")
check(key not in json.dumps(relay.events), "the relay saw neither the key nor any content")

# ---- the guest's place on the team list ---------------------------------------------------------------------------
guest.js("fix={lat:24.7136,lon:46.6753,acc:5,t:Date.now()};$('me').value='Guest';tellPos(true);1")
check(wait(lambda: any(e[0] == "pos" and e[1].get("name") == "Guest" and e[1].get("route") == "peer" for e in drain()), 20),
      "its position reaches the PC, marked as carried by a teammate")

# ---- both send at once: the helper's own photo and the guest's do not mix -----------------------------------------
guest.js(PHOTO % (220000, "viaphoto-0002", "guest again"))
helper.js(PHOTO % (260000, "helperphoto-0001", "the helper own photo"))
both = {}
check(wait(lambda: both.update({e[1]["id"]: e[1] for e in drain() if e[0] == "photo"}) or
           {"viaphoto-0002", "helperphoto-0001"} <= set(both), 90), "the helper's own photo and the guest's both arrive")
sizes = {k: os.path.getsize(v["photo"]) for k, v in both.items() if os.path.isfile(v.get("photo", ""))}
check(sizes.get("viaphoto-0002") == 220000 and sizes.get("helperphoto-0001") == 260000, f"each whole, not mixed ({sizes})")

# ---- the helper goes away ------------------------------------------------------------------------------------------
helper.js("drop();1")
check(wait(lambda: guest.js("lineOpen()") is False, 30), "when the helper leaves, the guest knows its line is gone")
check(guest.js("lastSt&&lastSt[0]") is False, "and says so (its photos stay on the phone)")

host.stop()
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
