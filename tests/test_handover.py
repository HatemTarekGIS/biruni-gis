r"""Without internet: a phone hands its waiting photos to a teammate's phone - two QR codes, then a direct link -
sealed for the computer, so the teammate carries them and cannot open them; whichever phone reaches the computer
first delivers them, and the computer keeps one copy.

Checked: the QR codes the phone draws (against segno, every mask), the short code that carries the link, the
whole hand-over between two phone pages (hidden Chromium, no internet, no relay), and the delivery to the computer.

Run:  engine\bin\python-qgis.bat tests\test_handover.py
"""
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
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
from biruni.field_photos.server import PAGE, PhotoServer, phone_texts, render_page  # noqa: E402

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
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)


def run_node(script):
    p = os.path.join(work, "t.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write(script)
    r = subprocess.run([node, p], capture_output=True, text=True, encoding="utf-8")
    return r


# ---- 1. the scripts, the QR codes, the short link code ------------------------------------------------------------
texts = {"en": phone_texts(lambda s: s)}
srv = PhotoServer(os.path.join(work, "photos"), os.path.join(work, "cert"), texts, "en", [], 10178, ["127.0.0.1"],
                  bind="127.0.0.1")
local_page = render_page(texts, "en", [], [], srv.handover.public).decode("utf-8")
bridge_page = bridge.phone_page(texts)
check("__PCKEY__" not in local_page and srv.handover.public in local_page and "__PCKEY__" not in bridge_page,
      "the computer's key is in the page it serves (and the bridge page learns it when it connects)")
check(len(srv.handover.public) == 87 and json.loads(srv.bridge_config)["pk"] == srv.handover.public,
      "… and it goes to the bridge's receiver")
again = PhotoServer(os.path.join(work, "photos2"), os.path.join(work, "cert"), texts, "en", [], 10177, ["127.0.0.1"],
                    bind="127.0.0.1")
check(again.handover.public == srv.handover.public, "the key is kept: the same computer has the same key next time")
again.stop()
if node:
    for name, html in (("local page", local_page), ("bridge page", bridge_page)):
        js = html.split("<script>")[1].split("</script>")[0]
        p = os.path.join(work, "s.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        check(r.returncode == 0, f"the {name} script has no syntax error " + r.stderr[:300])
    qr = PAGE.split("/*QR{*/")[1].split("/*}QR*/")[0]
    import segno  # noqa: E402
    import segno.encoder as segno_encoder  # noqa: E402

    # segno writes one more zero byte when the data already ends on a byte boundary (always, in byte mode); the
    # standard and the reference implementations add none, as the phone does - readers take both. Made standard
    # here, segno checks all the rest: error correction, interleaving, placement, masks, format and version.
    segno_encoder.write_padding_bits = lambda buff, version, length: buff.extend([0] * ((8 - length % 8) % 8))

    samples = ["B1o|a3Fz|pQ7+Wn2kz0ZbY1mR9tLx8vC4|" + "x" * 43 + "|192.168.43.1_51234,10.0.0.7_60001",
               "hello", "B1a|" + "y" * 150, "0" * 7, "ممر", "q" * 20, "w" * 55, "e" * 200, "r" * 260]
    cases = [(t, k) for t in samples for k in range(8)]
    r = run_node(qr + "\nconsole.log(JSON.stringify(" + json.dumps(cases, ensure_ascii=False) +
                 ".map(([t,k])=>QRM(t,k).map(r=>r.map(x=>x?1:0).join('')))))")
    got = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    bad = []
    for (t, k), mat in zip(cases, got):
        q = segno.make(t, error="m", mode="byte", mask=k, micro=False, boost_error=False, encoding="utf-8")
        want = ["".join("1" if c else "0" for c in row) for row in q.matrix_iter(scale=1, border=0)]
        if mat != want:
            bad.append(f"{t[:12]}… mask {k} (v{q.version})")
    check(len(got) == len(cases) and not bad, f"the phone's QR codes are module for module those of segno, "
          f"{len(cases)} codes, versions {sorted({segno.make(t, error='m', mode='byte', micro=False, boost_error=False, encoding='utf-8').version for t in samples})} {bad[:3]}")
    r = run_node(qr + "\nconsole.log(JSON.stringify([0,1].map(()=>QRM('B1o|'+'z'.repeat(140)).length)))")
    check(r.returncode == 0 and json.loads(r.stdout)[0] <= 49, f"a link code fits a small code ({json.loads(r.stdout or '[0]')[0]} modules a side)")
    hand = PAGE.split("/*HAND{*/")[1].split("/*}HAND*/")[0]
    sdp = ("v=0\r\no=- 46117 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\nm=application 9 UDP/DTLS/SCTP "
           "webrtc-datachannel\r\nc=IN IP4 0.0.0.0\r\na=candidate:1467250027 1 udp 2122260223 192.168.43.12 51234 typ host "
           "generation 0 network-id 1\r\na=candidate:2 1 udp 2122194687 fe80::1 51235 typ host generation 0\r\n"
           "a=candidate:3 1 tcp 1518280447 192.168.43.12 9 typ host tcptype active\r\na=ice-ufrag:Fq3z\r\n"
           "a=ice-pwd:Wq9b2T7nVd1kLp0zXc8vRs5y\r\na=ice-options:trickle\r\na=fingerprint:sha-256 "
           + ":".join(f"{i * 7 % 256:02X}" for i in range(32)) + "\r\na=setup:actpass\r\na=mid:0\r\na=sctp-port:5000\r\n")
    r = run_node("const $=()=>null;" + hand.split("/*QR{*/")[0] + "\nconst c=sdpCode(" + json.dumps(sdp) + ",'o'),d=codeSdp(c);"
                 "console.log(JSON.stringify([c,d.type,d.sdp,codeSdp('nonsense'),codeSdp('B1x|a|b|c|')]))")
    out = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    if len(out) == 5:
        code, kind, back = out[0], out[1], out[2]
        check(code.startswith("B1o|Fq3z|Wq9b2T7nVd1kLp0zXc8vRs5y|") and code.endswith("|192.168.43.12_51234") and len(code) < 110,
              f"the link fits {len(code)} characters: names, the fingerprint and the address on this network ({code[:40]}…)")
        check(kind == "offer" and "a=fingerprint:sha-256 " + ":".join(f"{i * 7 % 256:02X}" for i in range(32)) in back
              and "192.168.43.12 51234 typ host" in back and "a=setup:actpass" in back, "… and gives back a whole offer")
        check(out[3] is None and out[4] is None, "anything else is not taken for a code")
    else:
        check(False, "the link code runs " + r.stderr[-300:])
else:
    print("SKIP node not found")


# ---- 2. two phones, no internet: one hands its photos to the other ------------------------------------------------
class Phone(QWebEnginePage):
    def __init__(self, name, html):
        super().__init__(profile)
        self.name = name
        self.setHtml(html, QUrl(f"https://{name}.invalid/p/"))

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print(f"{self.name} js:", message, line, flush=True)

    def js(self, expr):
        out = {}
        self.runJavaScript(expr, lambda v: out.update(v=v))
        wait(lambda: "v" in out, 10)
        return out.get("v")


giver = Phone("giver", local_page)
taker = Phone("taker", render_page(texts, "en", [], [], "").decode("utf-8"))
for p in (giver, taker):
    check(wait(lambda p=p: p.js("!!db&&typeof handGive") == "function", 20), f"the {p.name}'s page is ready")
    p.js("window.HAND_TEST=true;start=()=>{};1")
PHOTO = r"""(async()=>{const b=new Uint8Array(%d);for(let i=0;i<b.length;i+=65536)crypto.getRandomValues(b.subarray(i,i+65536));
 b.set([0xff,0xd8,0xff,0xe0]);b.set([0xff,0xd9],b.length-2);
 const meta={id:'%s',lat:24.7136,lon:46.6753,acc:4,compass:90,t:Date.now(),kind:'pole',note:'%s',device:'test'};
 await put({id:meta.id,blob:new Blob([b],{type:'image/jpeg'}),meta:meta,room:ROOM});window._put=(window._put||0)+1})();1"""
giver.js(PHOTO % (420000, "handphoto-0001", "secret note one"))
giver.js(PHOTO % (180000, "handphoto-0002", "secret note two"))
check(wait(lambda: giver.js("window._put") == 2, 10), "the giver has two photos waiting (the computer is not reachable)")

# a phone that never met the computer cannot seal: it says so
taker.js(PHOTO % (1000, "takerown-0001", "the taker own"))
wait(lambda: taker.js("window._put") == 1, 10)
taker.js("handGive();1")
check(wait(lambda: taker.js("$('toast').textContent") == texts["en"]["handNoKey"], 10), "a phone that never met the computer says it cannot seal")
taker.js("del('takerown-0001');1")

giver.js("handGive();1")
check(wait(lambda: (giver.js("window._handCode||''") or "").startswith("B1o|"), 20), "the giver shows its code")
offer = giver.js("window._handCode")
check(giver.js("!$('handQr').classList.contains('hide')&&$('handQr').width>200") is True and
      giver.js("$('handStep').textContent") == texts["en"]["handGive1"], f"… as a QR code with what to do ({len(offer)} characters)")
taker.js("handTake();1")
check(wait(lambda: taker.js("$('handStep').textContent") == texts["en"]["handTake1"], 10), "the taker is asked to scan it")
check(taker.js("handRead('a wrong code')") is not None and wait(lambda: taker.js("$('toast').textContent") == texts["en"]["handBad"], 5),
      "a code that is not a hand-over code is refused")
taker.js(f"handRead({json.dumps(offer)});1")
check(wait(lambda: (taker.js("window._handCode||''") or "").startswith("B1a|"), 20), "the taker reads it and shows its own code")
answer = taker.js("window._handCode")
giver.js(f"handRead({json.dumps(answer)});1")
check(wait(lambda: giver.js("HAND===null") is True and taker.js("HAND===null") is True, 60),
      "the giver reads it back: the two phones link directly and the photos go over")
check(giver.js("$('toast').textContent") == texts["en"]["handGave"].replace("{n}", "2"), "the giver: " + str(giver.js("$('toast').textContent")))
check(taker.js("$('toast').textContent") == texts["en"]["handTook"].replace("{n}", "2"), "the taker: " + str(taker.js("$('toast').textContent")))
taker.js("all().then(async a=>{window._c=await Promise.all(a.filter(x=>x.carried).map(async x=>({id:x.id,meta:x.meta,"
         "b:H64.enc(await x.blob.arrayBuffer())})))});1")
wait(lambda: taker.js("!!window._c") is True, 10)
carried = taker.js("window._c") or []
check(len(carried) == 2 and all(c["meta"].get("sealed") == 1 for c in carried), "the taker carries two sealed photos")
import base64  # noqa: E402

blobs = {c["id"]: base64.urlsafe_b64decode(c["b"] + "=" * (-len(c["b"]) % 4)) for c in carried}
check(all(b[:1] == b"\x01" and b"\xff\xd8\xff\xe0" not in b[:200] and b"secret note" not in b for b in blobs.values()),
      "it cannot open them: no picture, no note in what it holds")
check(wait(lambda: taker.js("$('handInfo').textContent") == texts["en"]["handCarry"].replace("{n}", "2"), 10),
      "it says it is carrying two photos for teammates")
check(giver.js("(async()=>{window._g=(await all()).length})();1") == 1 and wait(lambda: giver.js("window._g") == 2, 5),
      "the giver keeps its photos too, until one of the two reaches the computer")

# ---- 3. the taker reaches the computer first ----------------------------------------------------------------------
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def post(server, meta, body):
    base = urllib.parse.urlsplit(server.url("127.0.0.1"))
    c = http.client.HTTPSConnection("127.0.0.1", base.port, context=ctx, timeout=20)
    c.request("POST", base.path + "photo?m=" + urllib.parse.quote(json.dumps(meta)), body=body,
              headers={"Content-Type": "application/octet-stream"})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


codes = [post(srv, c["meta"], blobs[c["id"]]) for c in carried]
check(codes == [200, 200], f"the computer opens what the taker carried ({codes})")
recs = {r["id"]: r for r in srv.store.records.values()}
check(set(recs) == {"handphoto-0001", "handphoto-0002"} and recs["handphoto-0001"].get("note") == "secret note one"
      and os.path.getsize(recs["handphoto-0001"]["photo"]) == 420000, "the photos are on the computer whole, with their notes")
# the giver comes back later with its own copies: the computer keeps one
giver.js("all().then(async a=>{window._own=await Promise.all(a.map(async x=>({meta:x.meta,b:H64.enc(await x.blob.arrayBuffer())})))});1")
wait(lambda: giver.js("!!window._own") is True, 10)
own = giver.js("window._own") or []
codes = [post(srv, o["meta"], base64.urlsafe_b64decode(o["b"] + "=" * (-len(o["b"]) % 4))) for o in own]
check(codes == [200, 200] and len(srv.store.records) == 2, "the giver's own copies arrive later: still one copy each on the computer")
# another computer cannot open them
other = PhotoServer(os.path.join(work, "other"), os.path.join(work, "other-cert"), texts, "en", [], 10176, ["127.0.0.1"],
                    bind="127.0.0.1")
check(other.handover.public != srv.handover.public and post(other, carried[0]["meta"], blobs[carried[0]["id"]]) == 400
      and not other.store.records, "another computer cannot open them (and the phone drops them there)")
other.stop()
check(post(srv, {"id": "x", "sealed": 1, "t": 1}, b"\x01" + os.urandom(120)) == 400, "damaged bytes are refused")

# ---- 4. the page opens without internet (GitHub Pages) -------------------------------------------------------------
out = os.path.join(work, "site")
r = subprocess.run([sys.executable, os.path.join(HERE, "..", "scripts", "build_bridge_page.py"), out], capture_output=True, text=True)
sw = open(os.path.join(out, "p", "sw.js"), encoding="utf-8").read() if os.path.isfile(os.path.join(out, "p", "sw.js")) else ""
check("caches.match" in sw and "serviceWorker.register('sw.js')" in bridge_page,
      "the phone page keeps itself on the phone, to open in the field without internet")
if node and sw:
    p = os.path.join(work, "sw.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write(sw)
    check(subprocess.run([node, "--check", p]).returncode == 0, "… its script has no syntax error")

srv.stop()
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
