r"""The Biruni bridge end to end on this PC, hidden: a phone page (Chromium) meets the PC's
hidden receiver (bridge_host.py, its own process) through a relay, they open a direct
WebRTC channel, and a photo goes over it into the photo folder.

The public relays are replaced by a Nostr relay on this PC (QWebSocketServer), so the test
needs no internet. Also checked: the Schnorr signatures (BIP340 test vectors) and the
encryption, in node, and the phone page built for GitHub Pages.

Run:  engine\bin\python-qgis.bat tests\test_field_photos_bridge.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
RELAY_PORT = 10199
os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{RELAY_PORT}"
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"  # nothing there: the bridge copes with a dead relay
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox --disable-features=WebRtcHideLocalIpsWithMdns"

from PyQt6.QtCore import QCoreApplication, Qt, QTimer, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])  # Chromium needs a program name
# the simulated phone runs in this process
profile = QWebEngineProfile.defaultProfile()
from biruni.field_photos import bridge  # noqa: E402
from biruni.field_photos.server import PhotoServer, all_phone_texts, phone_texts  # noqa: E402

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


# ---- the core script in node: signatures and encryption ---------------------------------------------
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
work = tempfile.mkdtemp()
if node:
    js = bridge._core() + r"""
(async()=>{const out=[];
 const t=async(sk,aux,msg)=>hex(await schnorr(BigInt('0x'+sk),unhex(msg),unhex(aux))).toUpperCase();
 out.push(await t('3','00'.repeat(32),'00'.repeat(32)));
 out.push(await t('B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF','00'.repeat(31)+'01',
  '243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89'));
 await bridgeKey(process.argv[2]);const m={sdp:'v=0\r\n'.repeat(200),ar:'جسر'};const s=await seal(m,true);
 out.push(JSON.stringify(await unseal(s))===JSON.stringify(m));
 out.push(await unseal(s.slice(0,30)+(s[30]==='A'?'B':'A')+s.slice(31))===null);
 out.push(BK.topic);console.log(JSON.stringify(out))})();
"""
    p = os.path.join(work, "core.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write(js)
    key0 = bridge.new_key()
    r = subprocess.run([node, p, key0], capture_output=True, text=True, encoding="utf-8")
    out = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    check(len(out) == 5, "the bridge core runs " + r.stderr[-200:])
    if len(out) == 5:
        check(out[0] == "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F38"
                        "2D2CE5EBEEE8FDB2172F477DF4900D310536C0", "Schnorr signature: BIP340 vector 0")
        check(out[1] == "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B0912"
                        "92BFF4EA897EFCB639EA871CFA95F6DE339E4B0A", "Schnorr signature: BIP340 vector 1")
        check(out[2] is True and out[3] is True, "messages are sealed and opened; a changed one is refused")
        check(out[4].startswith("biruni") and key0 not in out[4] and len(out[4]) == 46,
              "the relays see a meeting name, never the key")
else:
    print("SKIP node not found")

# ---- the key and the QR address --------------------------------------------------------------------
k = bridge.new_key()
check(len(k) == 43 and bridge.new_key() != k, "a new random key each time")
u = bridge.phone_url(k, "ar")
check(u.startswith(bridge.PAGE_URL) and u.split("#", 1)[1] == f"k={k}&i={bridge.pin(k)}&l=ar",
      "the key and the PC's pin are after # (never sent)")
check(len(bridge.pin(k)) == 22 and bridge.pin(k) == bridge.identity(k)[1] and bridge.pin(bridge.new_key()) != bridge.pin(k),
      "each key has its own identity, and the same key keeps its pin")

# ---- the page published on GitHub Pages ---------------------------------------------------------------
ALL = dict(all_phone_texts(), en=phone_texts(lambda s: s))
ALL["ar"] = dict(ALL.get("ar") or phone_texts(lambda s: s), title="بيروني - صور ميدانية")
page = bridge.phone_page(ALL)
check("__TEXTS__" not in page and "__KINDS__" not in page and "/*SEND{*/" not in page and "/*BOOT*/" not in page,
      "the phone page is complete")
check("fetch('photo?m=" not in page and "RTCPeerConnection" in page, "the phone page sends over the bridge")
if node:
    jsp = os.path.join(work, "page.js")
    with open(jsp, "w", encoding="utf-8") as f:
        f.write(page.split("<script>")[1].split("</script>")[0])
    r = subprocess.run([node, "--check", jsp], capture_output=True, text=True)
    check(r.returncode == 0, "the phone page script has no syntax error " + r.stderr[:300])
    with open(jsp, "w", encoding="utf-8") as f:
        f.write(bridge.receiver_page().decode().split("<script>")[1].split("</script>")[0])
    r = subprocess.run([node, "--check", jsp], capture_output=True, text=True)
    check(r.returncode == 0, "the receiver script has no syntax error " + r.stderr[:300])


# ---- a Nostr relay on this PC (a minimal WebSocket server in a thread) ----------------------------------
import base64  # noqa: E402
import hashlib  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402


class Relay:
    def __init__(self, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.subs = {}  # connection -> [(sub id, topics)]
        self.events = []
        self.lock = threading.Lock()
        check(True, "the test relay listens")
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
        k = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n")
             if ln.lower().startswith(b"sec-websocket-key")][0]
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
                op = b0 & 15
                if op == 8:
                    break
                if op == 1:
                    self._msg(c, json.loads(data.decode("utf-8")))
        except (OSError, ValueError):
            pass
        with self.lock:
            self.subs.pop(c, None)

    def _send(self, c, obj):
        d = json.dumps(obj).encode("utf-8")
        h = bytes([0x81]) + (bytes([len(d)]) if len(d) < 126 else
                             bytes([126]) + struct.pack(">H", len(d)) if len(d) < 65536 else
                             bytes([127]) + struct.pack(">Q", len(d)))
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

# ---- the PC: photo server on 127.0.0.1 only + the hidden receiver process -------------------------------
folder = os.path.join(work, "Field photos")
texts = {"en": dict(phone_texts(lambda s: s), title="TITLE FROM THE PC")}
FORM = [{"label": "رقم العمود", "type": "text", "req": True},
        {"label": "الحالة", "type": "choice", "req": True, "choices": ["سليم", "تالف"]},
        {"label": "مشاكل", "type": "multi", "choices": ["صدأ", "ميلان", "مطفي"]},
        {"label": "الارتفاع", "type": "number", "min": 0, "max": 40},
        {"label": "عدد الكشافات", "type": "integer", "min": 1, "max": 8},
        {"label": "آمن؟", "type": "yesno"}, {"label": "المسّاح", "type": "text", "keep": True}]
srv = PhotoServer(folder, os.path.join(work, "cert"), texts, "en", ["عمود", "لوحة"], 10198, ["127.0.0.1"],
                  bind="127.0.0.1", form=FORM)
NAMES = [f["name"] for f in srv.store.form]
key = bridge.new_key()
host = bridge.BridgeHost(srv.url("127.0.0.1") + "bridge#" + bridge.host_frag(key))
check(host.sandboxed, "the hidden receiver runs inside Chromium's sandbox (scripts/patch_engine.py placed its libraries)")
evs = []


def js_log():
    return " | " + " | ".join(x for x in host.log[-5:] if "js:" in x)


def drain():
    while not srv.events.empty():
        evs.append(srv.events.get_nowait())
    return evs


check(wait(lambda: any(e[0] == "bridge" and e[1].get("type") == "relays" and e[1].get("up") for e in drain()), 60),
      "the hidden receiver started and reached the relay ".replace("","") + js_log())

# ---- the phone ----------------------------------------------------------------------------------------------


CSP_REFUSED = []


class Phone(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print("phone js:", message, line, flush=True)
        if "Content Security Policy" in message:
            CSP_REFUSED.append(message)


csp_page = bridge.with_csp(page)   # as published (scripts/build_bridge_page.py)
check('http-equiv="Content-Security-Policy"' in csp_page and "'unsafe-inline'" not in csp_page
      and f"ws://127.0.0.1:{RELAY_PORT}" in csp_page.split("connect-src", 1)[1].split(";", 1)[0],
      "the published page carries a CSP: its own script only, the relays only")
phone = Phone(profile)
phone.setHtml(csp_page, QUrl(f"https://phone.invalid/p/#k={key}&i={bridge.pin(key)}&l=ar"))
result = {}


def js(expr):
    result.pop("v", None)
    phone.runJavaScript(expr, lambda v: result.update(v=v))
    wait(lambda: "v" in result, 10)
    return result.get("v")


check(wait(lambda: js("document.documentElement&&document.documentElement.dir") == "rtl", 20), "the phone page opens in Arabic, right to left")
check(js("T.title") == "بيروني - صور ميدانية", "the phone has its texts before it ever connects")
check(wait(lambda: js("!!(dc&&dc.readyState==='open')") is True, 60),
      "phone and PC opened a direct channel ".replace("","") + js_log())
check(wait(lambda: js("KINDS.join(',')") == "عمود,لوحة", 10), "the PC's kinds reach the phone")
check(js("ID.pin") == bridge.pin(key) and not js("SIG.refused||0"), "the phone checks the PC's pin and took its signed answer")
FORGE = r"""window._f=null;(async()=>{const r={};const before=SIG.refused||0;let called=0;const on=SIG.on;
 SIG.on=o=>{if(String(o.sid).startsWith('forged'))called++;else on(o)};
 await SIG.arrived(await seal({t:'answer',sid:'forged1',sdp:'v=0',n:rid(),ts:Date.now()}));
 const kp=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},true,['sign','verify']);
 const pub=b64u.enc(await crypto.subtle.exportKey('raw',kp.publicKey));
 const sig=b64u.enc(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},kp.privateKey,enc.encode('BRA1|forged2|v=0')));
 await SIG.arrived(await seal({t:'answer',sid:'forged2',sdp:'v=0',ip:pub,is:sig,n:rid(),ts:Date.now()}));
 const sk=await crypto.subtle.importKey('pkcs8',b64u.dec('__SK__'),{name:'ECDSA',namedCurve:'P-256'},true,['sign']);
 const j=await crypto.subtle.exportKey('jwk',sk);const rp=b64u.enc(cat(new Uint8Array([4]),b64u.dec(j.x),b64u.dec(j.y)));
 const s3=b64u.enc(await crypto.subtle.sign({name:'ECDSA',hash:'SHA-256'},sk,enc.encode('BRA1|other|v=0')));
 r.wrongText=await IDOK(rp,s3,'BRA1|forged3|v=0');r.rightText=await IDOK(rp,s3,'BRA1|other|v=0');
 SIG.on=on;r.called=called;r.refused=(SIG.refused||0)-before;window._f=JSON.stringify(r)})();1"""
js(FORGE.replace("__SK__", bridge.identity(key)[0]))
check(wait(lambda: bool(js("window._f")), 10), "the forgery checks ran")
forged = json.loads(js("window._f") or "{}")
check(forged.get("called") == 0 and forged.get("refused") == 2,
      "an unsigned answer and one signed by another key are refused when the link has the pin (%s)" % forged)
check(forged.get("wrongText") is False and forged.get("rightText") is True,
      "the PC's signature is bound to the call it answers (another call's signature is refused)")
check(js("LANG") == "ar", "the language from the code stays (the PC's own is only a default)")
check(js("setLang('en');[document.documentElement.dir,T.start,$('go').textContent].join('|')") == "ltr|Start|Start",
      "the phone can switch language on the page")
check(js("setLang('ar');document.documentElement.dir") == "rtl", "and back to Arabic, right to left")
check(any(e[0] == "bridge" and e[1].get("type") == "phone" for e in drain()), "the PC hears a phone connected")
relayed = json.dumps(relay.events)
check(key not in relayed and "TITLE FROM THE PC" not in relayed and "v=0" not in relayed,
      "the relay saw neither the key nor any readable content")

js(r"""(async()=>{const b=new Uint8Array(300000);for(let i=0;i<b.length;i+=65536)crypto.getRandomValues(b.subarray(i,i+65536));b.set([0xff,0xd8,0xff,0xe0]);
 b.set([0xff,0xd9],b.length-2);const meta={id:'bridgephoto-0001',lat:24.7136,lon:46.6753,acc:4,compass:90,
 t:Date.now(),kind:'عمود',note:'عبر الجسر',device:'test'};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});await flush()})();1""")
got = []
check(wait(lambda: bool(got.extend(e[1] for e in drain() if e[0] == "photo") or got), 60), "the photo arrived")
rec = got[0] if got else {}
check(os.path.isfile(rec.get("photo", "")) and os.path.getsize(rec["photo"]) == 300000,
      "on disk, every byte (300 KB in 19 pieces)")
check(rec.get("note") == "عبر الجسر" and rec.get("kind") == "عمود" and rec.get("compass") == 90, "with its data")
check(wait(lambda: js("sent") == 1, 10), "the phone got the confirmation")
js("window._left=-1;all().then(a=>{window._left=a.length});1")
check(wait(lambda: js("window._left") == 0, 10), "and took it off its waiting list")
check(len(relay.events) <= 6, f"only a few small messages went through the relay ({len(relay.events)})")

# ---- the form: designed on the PC, asked on the phone, checked on both --------------------------------------
check(wait(lambda: js("FORM.map(f=>f.name).join(',')") == ",".join(NAMES), 10), "the PC's form reaches the phone")
check(js("document.querySelectorAll('#form .fld').length") == 7, "every question is on the page")
check(js("document.querySelectorAll('#form .req').length") == 2, "required questions are marked")
check(js("answers()") is None and js("document.querySelectorAll('#form .fld.bad').length") == 2,
      "the camera does not open while required questions are empty - they are marked")
n_pole, n_state, n_prob, n_h, n_lamps, n_safe, n_who = NAMES
js(f"vals={{'{n_pole}':'P-17','{n_state}':'تالف','{n_h}':'٤٠٫٥'}};drawForm();1")
check(js("answers()") is None and js(f"document.querySelector('.fld[data-name=\"{n_h}\"]').classList.contains('bad')"),
      "a number over its limit is refused (Arabic digits read)")
js(f"vals['{n_h}']='١٢٫٥';vals['{n_lamps}']='2.5';drawForm();1")
check(js("answers()") is None, "a whole number with a fraction is refused")
js(f"$('form').querySelectorAll('.fld')[2].querySelectorAll('button')[0].click();"
   f"$('form').querySelectorAll('.fld')[2].querySelectorAll('button')[2].click();"
   f"$('form').querySelectorAll('.fld')[5].querySelectorAll('button')[1].click();"
   f"vals['{n_lamps}']='3';vals['{n_who}']='حاتم';1")
got_answers = js("JSON.stringify(answers())")
check(json.loads(got_answers or "null") == {n_pole: "P-17", n_state: "تالف", n_prob: ["صدأ", "مطفي"], n_h: 12.5,
                                            n_lamps: 3, n_safe: False, n_who: "حاتم"},
      "the answers are read from taps and boxes: " + str(got_answers))
js("afterShot();1")
check(js("JSON.stringify(vals)") == json.dumps({n_who: "حاتم"}, ensure_ascii=False, separators=(",", ":")),
      "after a photo only the kept answer stays")
js(r"""(async()=>{const b=new Uint8Array(2000);b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);
 const meta={id:'bridgephoto-0002',lat:24.7136,lon:46.6753,acc:4,t:Date.now(),device:'test',f:"""
   + got_answers + r"""};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});await flush()})();1""")
got2 = []
check(wait(lambda: bool(got2.extend(e[1] for e in drain() if e[0] == "photo" and e[1]["id"] == "bridgephoto-0002")
                        or got2), 60), "the photo with answers arrived")
check((got2[0] if got2 else {}).get("fields") == {n_pole: "P-17", n_state: "تالف", n_prob: "صدأ; مطفي", n_h: 12.5,
                                                  n_lamps: 3, n_safe: False, n_who: "حاتم"},
      "the answers are kept with the photo: " + str(got2[0].get("fields") if got2 else None))

# ---- receipts for Google Drive: the PC names, signed, the photos it holds as full originals --------------------
srv.drive_clean = "auto"
js("window._dg=null;window._dgok=null;(()=>{const f=fromPc;window._fromPc0=f;"
   "window.fromPc=m=>{if(m&&m.t==='dgot'){window._dg=m;IDOK(m.ip,m.is,'BRDG1|'+m.nonce+'|'+m.clean+'|'+m.ids.join(','))"
   ".then(v=>window._dgok=v)}else f(m)}})();"
   "lineText(JSON.stringify({t:'dhave',nonce:'n-receipt-1',ids:['bridgephoto-0002','never-sent',7]}));1")
check(wait(lambda: js("window._dgok") is not None, 15)
      and js("JSON.stringify([window._dg.nonce,window._dg.clean,window._dg.ids,window._dgok])")
      == '["n-receipt-1","auto",["bridgephoto-0002"],true]',
      f"asked over the line, the PC names only what it holds, with the office's choice, signed ({js('JSON.stringify(window._dg)')})")
js("window.fromPc=window._fromPc0;1")
srv.drive_clean = "manual"

# ---- the team and the guide over the bridge, the averaged position -------------------------------------------
js("fix={lat:24.7136,lon:46.6753,acc:5,alt:null,heading:null,t:Date.now()};$('me').value='Ali';tellPos(true);1")
check(wait(lambda: any(e[0] == "pos" and e[1]["name"] == "Ali" for e in drain()), 15),
      "the phone's position reaches the PC over the direct channel")
ring = [[46.6750, 24.7130], [46.6760, 24.7130], [46.6760, 24.7140], [46.6750, 24.7140], [46.6750, 24.7130]]
srv.set_guide([{"k": "poly", "n": "قطعة 12", "c": [ring]}, {"k": "pt", "n": "P-17", "c": [46.6800, 24.7136]}])
check(wait(lambda: js("GUIDE.v") == 1, 15), "the PC's guide reaches the phone by itself")
check(js("$('guideCard').classList.contains('hide')") is False and "قطعة 12" in (js("$('gtext').textContent") or ""),
      "the phone shows the guide: " + str(js("$('gtext').textContent")))
inside = js("(()=>{const n=nearest(proj(fix.lat,fix.lon));return [n.inside,Math.round(n.d)].join(',')})()")
check(inside == "true,30", "inside the plot, 30 m from its nearest (west) edge: " + str(inside))
js("fix={lat:24.7136,lon:46.6790,acc:5,t:Date.now()};1")
outside = js("(()=>{const n=nearest(proj(fix.lat,fix.lon));return [n.inside,n.it.n,Math.round(n.d)].join(',')})()")
check(outside == "false,P-17,101", "outside, the nearest target and its distance: " + str(outside))
avg = js("""JSON.stringify((()=>{const pts=[];for(let i=0;i<30;i++)pts.push({lat:24.7136+(i%2?1:-1)*0.000005,
 lon:46.6753+(i%3-1)*0.000005,acc:4});pts.push({lat:24.7140,lon:46.6760,acc:40});pts.push({lat:24.7136,lon:46.6800,acc:4});
 const r=average(pts);return [r.n,Math.round(r.sd*10)/10,Math.round(meters(r,{lat:24.7136,lon:46.6753})*10)/10]})())""")
check(json.loads(avg or "null") and json.loads(avg)[0] == 30 and json.loads(avg)[2] < 0.5,
      "averaging leaves out a poor reading and a jump: " + str(avg))

# ---- GPS mode (the PC's GPS panel over the bridge): every position, with speed and height -------------------
drain().clear()
js("GPSMODE=true;room=null;indoorFix=null;$('share').checked=false;lastPos=null;"
   "fix={lat:24.7140,lon:46.6760,acc:4,alt:612,heading:90,speed:3,t:Date.now()};tellPos();1")
gps_pos = []
check(wait(lambda: bool(gps_pos.extend(e[1] for e in drain() if e[0] == "pos" and e[1].get("speed") == 3)
                        or gps_pos), 15),
      "in GPS mode the phone sends its position even with sharing off, with its speed")
if gps_pos:
    from biruni.gps.browser import fix_from_json  # noqa: E402

    gfix = fix_from_json(gps_pos[0])
    check(abs(gfix.lat - 24.714) < 1e-9 and gfix.alt == 612 and abs(gfix.speed_kmh - 10.8) < 1e-6
          and gfix.course == 90, "the GPS panel reads it as a fix (position, height, speed in km/h, direction)")
js("GPSMODE=false;$('share').checked=true;1")

# ---- inside a building: the room code is the position -----------------------------------------------------------
from biruni.field_photos.server import indoor_payload  # noqa: E402

room_ring = [[46.67500, 24.71300], [46.67506, 24.71300], [46.67506, 24.71305], [46.67500, 24.71305], [46.67500, 24.71300]]
srv.set_indoor(indoor_payload([{"id": 1, "name": "الدور الأول"}],
                              [{"id": "L1-101", "lv": 1, "n": "مكتب", "num": "101", "cat": "office", "c": [room_ring],
                                "p": [46.67503, 24.713025], "r": 4}]))
check(wait(lambda: js("INDOOR&&INDOOR.units.length") == 1, 15), "the indoor map reaches the phone")
check(js("$('roomRow').classList.contains('hide')") is False, "the phone offers to scan a room code")
js("roomFromText('https://biruni-gis.com/r/#u=L1-101&n=101&l=1');1")
check(js("[room&&room.id,indoorFix.level,indoorFix.unit,$('shot').disabled].join(',')") == "L1-101,1,L1-101,false",
      "a scanned room code places the phone in that room, without GPS")
check("الدور الأول" in (js("$('roomName').textContent") or ""), "and names the room and its level")
js("roomFromText('L9-999');1")
check(js("room.id") == "L1-101", "a code not in the map changes nothing")
tap = js("tapRoom(rplan.W/2+20,rplan.H/2);[indoorFix.acc,indoorFix.lon>46.67503].join(',')")
check(tap == "1.5,true", "a tap on the room's plan moves the position inside the room: " + str(tap))
check(js("shotFix().unit") == "L1-101", "the next photo is taken in the room")
js(r"""(async()=>{const b=new Uint8Array(2000);b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);const p=shotFix();
 const meta={id:'bridgephoto-0003',lat:p.lat,lon:p.lon,acc:p.acc,t:Date.now(),device:'test',level:p.level,unit:p.unit};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});await flush()})();1""")
got3 = []
check(wait(lambda: bool(got3.extend(e[1] for e in drain() if e[0] == "photo" and e[1]["id"] == "bridgephoto-0003")
                        or got3), 60) and got3[0].get("unit") == "L1-101" and got3[0].get("level") == 1,
      "the photo arrives with its level and room")
js("localStorage.setItem('biruni-room',JSON.stringify({u:'L1-101',t:Date.now()}));setRoom(null);roomFromStore();1")
check(js("room&&room.id") == "L1-101", "a code read by the camera app (the room page) is picked up by the photo page")

# ---- waiting photos are sealed on the phone + "X photos waiting" counter + incognito warning -------------------
js(r"""(async()=>{window._qCheck=null;
 const b=new Uint8Array(2400);b.set([0xff,0xd8,0xff,0xe0]);
 const secret=new TextEncoder().encode('secret-offline-note-77');b.set(secret,100);b.set([0xff,0xd9],b.length-2);
 const meta={id:'bridgephoto-0004',lat:24.7155,lon:46.6788,acc:3,t:Date.now(),note:'secret-offline-note-77',device:'test'};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});
 setLang('en');await count();
 const enWait=$('waitPill').textContent,enHide=$('waitPill').classList.contains('hide');
 setLang('ar');await count();
 const arWait=$('waitPill').textContent,arBox=$('lWait').textContent;
 setLang('en');await count();
 const items=await all(),it=items.find(x=>x.id==='bridgephoto-0004');
 const raw=new Uint8Array(await it.blob.arrayBuffer());
 const rawStr=new TextDecoder('latin1').decode(raw);
 window._qCheck={sealed:it.meta.sealed,metaKeys:Object.keys(it.meta).sort().join(','),
  firstByte:raw[0],hasJpeg:raw[0]===0xff&&raw[1]===0xd8,hasNote:rawStr.includes('secret-offline-note-77'),
  enWait:enWait,enHide:enHide,arWait:arWait,arBox:arBox}})();1""")
check(wait(lambda: js("!!window._qCheck") is True, 15), "waiting photo inspected in phone storage")
qc = js("window._qCheck") or {}
check(qc.get("sealed") == 1 and qc.get("metaKeys") == "id,sealed,t" and qc.get("firstByte") == 1
      and not qc.get("hasJpeg") and not qc.get("hasNote"),
      f"waiting photo on the phone is encrypted with the PC's key (no readable JPEG, note or coordinates: {qc})")
check(qc.get("enWait") == "1 photos waiting" and not qc.get("enHide")
      and qc.get("arWait") == "1 صور مستنية" and qc.get("arBox") == "1 صور مستنية",
      f"the page shows the clear waiting counter in English and Arabic ({qc.get('enWait')} / {qc.get('arWait')})")
js("flush();1")
got4 = []
check(wait(lambda: bool(got4.extend(e[1] for e in drain() if e[0] == "photo" and e[1]["id"] == "bridgephoto-0004")
                        or got4), 60) and got4[0].get("note") == "secret-offline-note-77"
      and abs(got4[0].get("lat", 0) - 24.7155) < 1e-6,
      "when the connection is back, the PC decrypts and saves the waiting photo with its note and coordinates")
check(wait(lambda: js("$('waitPill').classList.contains('hide')") is True, 10),
      "the waiting counter hides again once all photos reach the PC")

# ---- Google Drive carries the original: over the open line only the preview goes; the original replaces it later --
js(r"""(async()=>{window._df=driveFirst;driveFirst=()=>true;
 const b=new Uint8Array(40000);for(let i=0;i<b.length;i++)b[i]=i%251;b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);
 const meta={id:'bridgephoto-0010',lat:24.716,lon:46.679,acc:3,t:Date.now(),note:'drive first',device:'test'};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});await flush()})();1""")
got5 = []
check(wait(lambda: bool(got5.extend(e[1] for e in drain() if e[0] == "photo" and e[1]["id"] == "bridgephoto-0010")
                        or got5), 60) and bool(got5[0].get("preview"))
      and os.path.getsize(got5[0].get("photo", "")) < 40000,
      "with Google Drive first: the line sends only the preview, and the PC shows it at once")


def p10():
    js("all().then(a=>{const x=a.find(y=>y.id==='bridgephoto-0010');window._p10=x?(x.prevSent?'prev':'wait'):'gone'});1")
    wait(lambda: False, 0.2)
    return js("window._p10")


check(wait(lambda: p10() == "prev", 10), "the phone keeps the original for Google Drive, marked 'preview sent'")
js("flush();1")
wait(lambda: False, 3)
check(len([e for e in drain() if e[0] == "photo" and e[1]["id"] == "bridgephoto-0010"]) == 1,
      "the next round sends nothing more over the line")
js("driveFirst=window._df;flush();1")
check(wait(lambda: any(e[0] == "photo" and e[1]["id"] == "bridgephoto-0010" and not e[1].get("preview")
                       and os.path.getsize(e[1].get("photo", "")) == 40000 for e in drain()), 60),
      "when Drive cannot carry it, the line sends the original, and it replaces the preview")
check(wait(lambda: p10() == "gone", 10), "… and the phone lets it go")

js("window.BIRUNI_INCOGNITO=true;checkPrivate();setLang('ar');1")
check(wait(lambda: js("!$('privWarn').classList.contains('hide')&&$('privWarn').textContent.includes('التصفح الخفي')") is True, 5),
      "private browsing mode shows a clear warning banner")
js("window.BIRUNI_INCOGNITO=false;checkPrivate();setLang('en');1")
check(wait(lambda: js("$('privWarn').classList.contains('hide')") is True, 5),
      "normal browsing mode hides the private browsing warning")

# ---- direct WebRTC blocked (CGNAT): no hang on 'connecting', encrypted preview over relays, original waits -----
js(r"""(() => {
 BR.ice = []; BR.directMs = 1200; BR.paceMs = 10; BR.chunkSize = 700;
 window._blockP2P = true;
 const scrubSdp = s => String(s || '').replace(/a=candidate:(\S+) (\d+) udp (\d+) \S+ \d+ typ host/gi,
  'a=candidate:$1 $2 udp $3 192.0.2.1 9 typ host');
 const origSigSend = SIG.send.bind(SIG);
 SIG.send = async (obj, z) => {
  if (window._blockP2P && obj && obj.t === 'offer' && obj.sdp) obj = Object.assign({}, obj, {sdp: scrubSdp(obj.sdp)});
  return await origSigSend(obj, z);
 };
 window._origRTC = window.RTCPeerConnection;
 window.RTCPeerConnection = function(cfg) {
  const p = new window._origRTC({iceServers: []});
  const origSetRemote = p.setRemoteDescription.bind(p);
  p.setRemoteDescription = async (desc) => {
   const sdp = window._blockP2P ? scrubSdp(desc.sdp) : desc.sdp;
   return await origSetRemote({type: desc.type, sdp: sdp});
  };
  return p;
 };
 drop(); RELAY = false; connect();
})();1""")
check(wait(lambda: js("linking === false && RELAY === true && lastState === 'relayMode'"), 15),
      "when direct WebRTC cannot open, the page does not hang on 'connecting' and switches to relay mode")

js(r"""(async () => {
 window._relayTest = null;
 const calls = [];
 const fakeRelay = (name, accept) => {
  const r = Object.create(NostrRelay.prototype);
  r.url = name; r.up = true; r.fails = 0; r.backoffUntil = 0; r.oks = new Map(); r.notices = [];
  r.sendWait = async (ev) => {
   calls.push(name);
   if (!accept) { r.fails++; r.backoffUntil = Date.now() + 20000; return false; }
   return true;
  };
  return r;
 };
 const realRelays = SIG.relays;
 const rBad1 = fakeRelay('bad1', false), rBad2 = fakeRelay('bad2', false),
       rGood1 = fakeRelay('good1', true), rGood2 = fakeRelay('good2', true);
 SIG.relays = [rBad1, rBad2, rGood1, rGood2];
 const ok1 = await SIG.sendChunk({t: 'noop'});
 const firstBatch = calls.slice(); calls.length = 0;
 const ok2 = await SIG.sendChunk({t: 'noop'});
 const secondBatch = calls.slice();
 SIG.relays = realRelays;
 window._relayTest = {ok1, ok2, firstBatch, secondBatch, bad1BackedOff: rBad1.backoffUntil > Date.now()};
})();1""")
check(wait(lambda: js("!!window._relayTest") is True, 10), "relay etiquette & failover checked")
rt = js("window._relayTest") or {}
check(rt.get("ok1") is True and rt.get("ok2") is True and rt.get("bad1BackedOff") is True
      and rt.get("secondBatch") == ["good1", "good2"],
      f"each chunk goes to at most 2 healthy relays, and rejecting relays back off and fail over ({rt})")

# 3. Out-of-order and duplicate chunks + preview saved on PC while encrypted original stays on phone:
js(r"""(async () => {
 window._prevDone = null;
 const realSendChunk = SIG.sendChunk.bind(SIG);
 const bufChunks = [];
 SIG.sendChunk = async (obj) => {
  if (obj && obj.t === 'rchunk' && obj.id === 'bridgephoto-0005') {
   bufChunks.push(obj);
   if (bufChunks.length === obj.k) {
    const order = [bufChunks[obj.k - 1], bufChunks[0], bufChunks[obj.k - 1], ...bufChunks.slice(1, -1)];
    for (const c of order) await realSendChunk(Object.assign({}, c));
   }
   return true;
  }
  return await realSendChunk(obj);
 };
 const b = new Uint8Array(8000);
 b.set([0xff, 0xd8, 0xff, 0xe0]);
 const noteBytes = new TextEncoder().encode('relay-preview-note-55');
 b.set(noteBytes, 60);
 b.set([0xff, 0xd9], b.length - 2);
 const meta = {id: 'bridgephoto-0005', lat: 24.7201, lon: 46.6802, acc: 4, t: Date.now(), note: 'relay-preview-note-55', device: 'test'};
 await put({id: meta.id, blob: new Blob([b], {type: 'image/jpeg'}), meta: meta, room: ROOM});
 await flush();
 SIG.sendChunk = realSendChunk;
 setLang('en'); await count();
 const enPill = $('waitPill').textContent;
 setLang('ar'); await count();
 const arPill = $('waitPill').textContent;
 setLang('en'); await count();
 const left = await all(), it = left.find(x => x.id === 'bridgephoto-0005');
 window._prevDone = {leftCount: left.length, prevSent: !!(it && it.prevSent), hasOrigBlob: !!(it && it.blob && it.blob.size > 4000),
  enPill: enPill, arPill: arPill};
})();1""")
got5_prev = []
check(wait(lambda: bool(got5_prev.extend(e[1] for e in drain() if e[0] == "photo" and e[1]["id"] == "bridgephoto-0005")
                        or got5_prev), 30),
      "out-of-order and duplicated encrypted chunks reassemble on the PC and save the preview")
check(wait(lambda: js("!!window._prevDone") is True, 15), "phone processed the signed preview ack")
pd = js("window._prevDone") or {}
rec5_prev = srv.store.records.get("bridgephoto-0005") or {}
prev_size = os.path.getsize(rec5_prev["photo"]) if rec5_prev.get("photo") and os.path.isfile(rec5_prev["photo"]) else 0
check(rec5_prev.get("preview") is True and 0 < prev_size < 8000 and rec5_prev.get("note") == "relay-preview-note-55",
      f"PC saved the preview marked 'preview': True ({prev_size} bytes < 8000 original)")
check(pd.get("leftCount") == 1 and pd.get("prevSent") is True and pd.get("hasOrigBlob") is True,
      f"phone kept the encrypted original waiting for Wi-Fi ({pd})")
check(pd.get("enPill") == "1 arrived as preview, original waiting for Wi-Fi"
      and pd.get("arPill") == "1 وصلت مصغّرة، الأصل بانتظار الواي فاي",
      f"page shows '1 arrived as preview, original waiting for Wi-Fi' in English and Arabic ({pd.get('enPill')} / {pd.get('arPill')})")

# 4. Signed ack bound to (id, kind, nonce): a forged, wrong-kind or wrong-nonce ack never deletes the original
js(r"""(async () => {
 window._ackGuard = null;
 const beforeRefused = SIG.refused || 0;
 acks.set('bridgephoto-0005', {kind: 'original', nonce: 'real-nonce-1', resolve: () => { window._ackForged = true; }, reject: () => {}});
 await handleAck({id: 'bridgephoto-0005', kind: 'preview', nonce: 'real-nonce-1', status: 200, ip: 'bad', is: 'bad'});
 await handleAck({id: 'bridgephoto-0005', kind: 'original', nonce: 'wrong-nonce', status: 200, ip: 'bad', is: 'bad'});
 await handleAck({id: 'bridgephoto-0005', kind: 'original', nonce: 'real-nonce-1', status: 200, ip: 'bad', is: 'bad'});
 acks.delete('bridgephoto-0005');
 const left = await all();
 window._ackGuard = {refusedDelta: (SIG.refused || 0) - beforeRefused, forgedAccepted: !!window._ackForged, stillThere: left.length === 1};
})();1""")
check(wait(lambda: js("!!window._ackGuard") is True, 10), "ack guard checked")
ag = js("window._ackGuard") or {}
check(ag.get("refusedDelta") == 3 and not ag.get("forgedAccepted") and ag.get("stillThere") is True,
      f"ack with wrong kind, wrong nonce or bad signature is refused and never deletes the original ({ag})")

# 5. When the PC is unreachable, new photos stay encrypted on the phone
js(r"""(async () => {
 window._pcOffCheck = null;
 RELAY = false;
 const b = new Uint8Array(3000); b.set([0xff, 0xd8, 0xff, 0xe0]); b.set([0xff, 0xd9], b.length - 2);
 const meta = {id: 'bridgephoto-0006', lat: 24.721, lon: 46.681, acc: 5, t: Date.now(), note: 'while-pc-off', device: 'test'};
 await put({id: meta.id, blob: new Blob([b], {type: 'image/jpeg'}), meta: meta, room: ROOM});
 const left = await all(), it = left.find(x => x.id === 'bridgephoto-0006');
 window._pcOffCheck = {count: left.length, sealed: it && it.meta && it.meta.sealed};
 await del('bridgephoto-0006');
})();1""")
check(wait(lambda: js("!!window._pcOffCheck") is True, 10), "offline storage while PC unreachable checked")
pco = js("window._pcOffCheck") or {}
check(pco.get("count") == 2 and pco.get("sealed") == 1,
      f"when the PC is unreachable, photos stay encrypted on the phone ({pco})")

# 6. Direct WebRTC restored (Wi-Fi): the full original is sent, replaces the preview in the same record, and is then deleted
js("window._blockP2P = false; window.RTCPeerConnection = window._origRTC; drop(); connect(); 1")
got5_orig = []
check(wait(lambda: bool(got5_orig.extend(e[1] for e in drain()
                        if e[0] == "photo" and e[1]["id"] == "bridgephoto-0005" and not e[1].get("preview"))
                        or got5_orig), 30),
      "once direct WebRTC opens, the full original arrives at the PC")
rec5_orig = srv.store.records.get("bridgephoto-0005") or {}
orig_size = os.path.getsize(rec5_orig["photo"]) if rec5_orig.get("photo") and os.path.isfile(rec5_orig["photo"]) else 0
check(rec5_orig.get("preview") is False and orig_size == 8000,
      f"the original replaced the preview in the same record (preview={rec5_orig.get('preview')}, size={orig_size} bytes)")
check(wait(lambda: js("(async()=>{window._afterOrig=(await all()).length})();window._afterOrig") == 0, 10),
      "after the signed original ack, the phone deletes the original from its waiting queue")

# ---- the receiver page is for this PC only -------------------------------------------------------------
import http.client  # noqa: E402
import ssl  # noqa: E402

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
c = http.client.HTTPSConnection("127.0.0.1", 10198, context=ctx, timeout=10)
c.request("GET", "/wrong/bridge")
check(c.getresponse().status == 404, "the receiver page needs the token")
c.close()

check(not CSP_REFUSED, "the whole trip ran under the page's CSP, nothing refused " + " | ".join(CSP_REFUSED)[:400])
js("(()=>{const s=document.createElement('script');s.textContent='window._injected=1';document.head.append(s);return 1})()")
check(wait(lambda: CSP_REFUSED, 5) and js("window._injected===1") is False, "… and a script slipped into the page is refused")
host.stop()
check(wait(lambda: not host.running(), 10), "the hidden receiver ends when the photos stop")
srv.stop()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
