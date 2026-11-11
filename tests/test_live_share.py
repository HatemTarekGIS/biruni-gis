r"""See my map end to end on this PC, hidden: a QGIS map with a styled layer is shared; a
viewer page (Chromium, the page published on GitHub Pages) meets the PC's hidden host
(bridge_host.py, its own process) through a relay, opens a direct WebRTC channel, gets the
map's title and layers, receives the map as an image, taps a feature and reads its
attributes; an edit on the PC makes the viewer redraw; stopping ends the host and tells
the viewer.

The public relays are replaced by a Nostr relay on this PC, so the test needs no internet.

Run:  engine\bin\python-qgis.bat tests\test_live_share.py
"""
import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
RELAY_PORT = 10195
SHARE_PORT = 10194  # not the real one: Biruni may be sharing on this PC
os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{RELAY_PORT}"
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"  # nothing there: the bridge copes with a dead relay
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox --disable-features=WebRtcHideLocalIpsWithMdns"
SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR

from qgis.PyQt.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,  # noqa: E402
                       QgsFillSymbol, QgsGeometry, QgsProject, QgsVectorLayer)

app = QgsApplication([sys.argv[0].encode()], True)  # Chromium needs a program name
app.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.live_share import server as ls_server  # noqa: E402
from biruni.live_share.share import LiveShare  # noqa: E402
from biruni.live_share.viewer import PAGE_URL, public_page, share_url  # noqa: E402

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

# ---- the pages ---------------------------------------------------------------------------------------
page = public_page()
check("__TEXTS__" not in page and "/*CORE*/" not in page and "RTCPeerConnection" in page and "bridgeKey" in page,
      "the viewer page is complete and connects over the bridge")
texts = json.loads(page.split("const ALL=", 1)[1].split(",$=", 1)[0])
check(sorted(texts) == sorted(["en", "ar", "es", "pt_BR", "fr", "tr", "fa"])
      and all(set(t) == set(texts["en"]) for t in texts.values()), "the viewer has every text in seven languages")
check("Biruni" in texts["fa"]["by"], "Persian keeps Biruni in Latin letters")
u = share_url("K" * 43, "ar")
from biruni.field_photos.bridge import pin as _pin  # noqa: E402
check(u.startswith(PAGE_URL) and u.endswith("#k=" + "K" * 43 + "&i=" + _pin("K" * 43) + "&l=ar"),
      "the key and the PC's pin are after # (never sent)")
if node:
    for name, html in (("viewer", page), ("host", ls_server.host_page().decode("utf-8"))):
        jsp = os.path.join(work, name + ".js")
        with open(jsp, "w", encoding="utf-8") as f:
            f.write(html.split("<script>")[1].split("</script>")[0])
        r = subprocess.run([node, "--check", jsp], capture_output=True, text=True)
        check(r.returncode == 0, f"the {name} script has no syntax error " + r.stderr[:300])
else:
    print("SKIP node not found")


# ---- a Nostr relay on this PC (a minimal WebSocket server in a thread) ------------------------------------
class Relay:
    def __init__(self, port):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(16)
        self.subs = {}
        self.events = []
        self.lock = threading.Lock()
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

# ---- the map on the PC: a styled polygon layer in a canvas ----------------------------------------------
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MERC = QgsCoordinateReferenceSystem("EPSG:3857")
lyr = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string&field=area:double", "Parcels", "memory")
feats = []
for i, (x, y) in enumerate(((46.670, 24.710), (46.690, 24.710))):
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 0.01} {y},{x + 0.01} {y + 0.01},{x} {y + 0.01},{x} {y}))"))
    f["name"], f["area"] = ["قطعة ١", "قطعة ٢"][i], 1000.5 + i
    feats.append(f)
lyr.dataProvider().addFeatures(feats)
lyr.updateExtents()
lyr.setFieldAlias(0, "Parcel name")
lyr.renderer().setSymbol(QgsFillSymbol.createSimple({"color": "220,30,30,255", "outline_color": "0,0,0,255"}))
QgsProject.instance().addMapLayer(lyr)
QgsProject.instance().setTitle("TITLE OF THE MAP")
canvas = QgsMapCanvas()
canvas.resize(800, 600)
canvas.setDestinationCrs(MERC)
canvas.setLayers([lyr])
xf = QgsCoordinateTransform(WGS84, MERC, QgsProject.instance())
view = xf.transformBoundingBox(lyr.extent())
view.grow(500)
canvas.setExtent(view)
canvas.setCanvasColor(Qt.GlobalColor.white)

# ---- share it ------------------------------------------------------------------------------------------------
share = LiveShare(canvas, os.path.join(work, "cert"), attributes=True, port=SHARE_PORT)
share.start()
key = share.key
check(len(key) == 43, "a new key for this share")
evs = []


def drain():
    evs.extend(share.poll())
    return evs


def js_log():
    return " | " + " | ".join(x for x in (share.host.log[-5:] if share.host else []) if "js:" in x)


check(wait(lambda: any(e[0] == "host" and e[1].get("type") == "relays" and e[1].get("up") for e in drain()), 60),
      "the hidden host started and reached the relay" + js_log())


# ---- the viewer -------------------------------------------------------------------------------------------------
class Viewer(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print("viewer js:", message, line, flush=True)


profile = QWebEngineProfile.defaultProfile()
viewer = Viewer(profile)
viewer.setHtml(page, QUrl(f"https://viewer.invalid/s/#k={key}&i={_pin(key)}&l=ar"))
result = {}


def js(expr):
    result.pop("v", None)
    viewer.runJavaScript(expr, lambda v: result.update(v=v))
    wait(lambda: "v" in result, 10)
    return result.get("v")


check(wait(lambda: js("document.documentElement.dir") == "rtl", 20), "the viewer opens in Arabic, right to left")
check(js("T.title") == "خريطة مباشرة", "with its texts before it connects")
check(wait(lambda: js("live()") is True, 60), "viewer and PC opened a direct channel" + js_log())
check(wait(lambda: js("INFO&&INFO.title") == "TITLE OF THE MAP", 15), "the viewer got the map's title")
check(js("JSON.stringify(INFO.layers)") == '["Parcels"]' and js("$('title').textContent") == "TITLE OF THE MAP",
      "and its layers, shown in the header")
home = js("JSON.stringify(INFO.extent)")
ce = canvas.extent()
check(home is not None and all(abs(a - b) < 1 for a, b in zip(json.loads(home), (ce.xMinimum(), ce.yMinimum(),
                                                                                 ce.xMaximum(), ce.yMaximum()))),
      "the viewer starts at the PC's view: " + str(home))
check(wait(lambda: js("!!LAST") is True, 40), "the map arrived as an image" + js_log())
last = json.loads(js("JSON.stringify({id:LAST.id,w:LAST.w,h:LAST.h,pw:LAST.pw,ph:LAST.ph,size:LAST.size,type:LAST.type})")
                  or "{}")
check(last.get("type") == "image/png" and last.get("w") == last.get("pw") and last.get("h") == last.get("ph")
      and last.get("w", 0) >= 100, f"a PNG of the size asked for: {last}")
check(js("$('wait').classList.contains('hide')&&ST") == "connected", "the viewer shows the map, connected")

PX = r"""window._px=null;(async()=>{const b=await createImageBitmap(LAST.blob);const c=new OffscreenCanvas(b.width,b.height),
 g=c.getContext('2d');g.drawImage(b,0,0);const d=g.getImageData(0,0,b.width,b.height).data;let red=0;const seen=new Set();
 for(let i=0;i<d.length;i+=4){if(d[i]>180&&d[i+1]<90&&d[i+2]<90)red++;if(seen.size<64)seen.add(d[i]+','+d[i+1]+','+d[i+2])}
 window._px={red:red,colors:seen.size,id:LAST.id}})();1"""


def pixels():
    js(PX)
    wait(lambda: js("!!window._px") is True, 10)
    return json.loads(js("JSON.stringify(window._px)") or "{}")


px1 = pixels()
check(px1.get("red", 0) > 500 and px1.get("colors", 0) > 2, f"the image shows the layer in its own style: {px1}")
check(wait(lambda: share.viewers == 1 or bool(drain()) and share.viewers == 1, 10), "the PC counts one viewer")
check(any(e[0] == "host" and e[1].get("type") == "viewer" for e in drain()), "the PC hears a viewer joined")

# ---- a tap on a parcel: its attributes -------------------------------------------------------------------------
c = feats[0].geometry().centroid()
c.transform(xf)
cx, cy = c.asPoint().x(), c.asPoint().y()
js(f"IDENT=null;identify(toScreen({cx},{cy}));1")
check(wait(lambda: js("!!IDENT") is True, 15), "the tap was answered")
ident = json.loads(js("JSON.stringify(IDENT)") or "{}")
f0 = (ident.get("features") or [{}])[0]
check(len(ident.get("features") or []) == 1 and f0.get("layer") == "Parcels"
      and ["Parcel name", "قطعة ١"] in f0.get("attrs", []) and ["area", "1000.5"] in f0.get("attrs", []),
      f"the tapped parcel's attributes, with the alias: {ident}")
check(js("!$('identSheet').classList.contains('hide')&&$('identBody').textContent.includes('قطعة ١')") is True,
      "shown in the details sheet")
js(f"IDENT=null;identify(toScreen({cx + 5000},{cy + 5000}));1")
check(wait(lambda: js("!!IDENT") is True, 15) and js("IDENT.features.length") == 0, "a tap on nothing finds nothing")

relayed = json.dumps(relay.events, ensure_ascii=False)
check(key not in relayed and "TITLE OF THE MAP" not in relayed and "Parcels" not in relayed and "v=0" not in relayed
      and "قطعة" not in relayed, f"the relay saw neither the key nor any readable content ({len(relay.events)} messages)")

# ---- an edit on the PC: the viewer redraws -------------------------------------------------------------------
shown = last["id"]
lyr.startEditing()
nf = QgsFeature(lyr.fields())
nf.setGeometry(QgsGeometry.fromWkt("POLYGON((46.670 24.722,46.700 24.722,46.700 24.726,46.670 24.726,46.670 24.722))"))
nf["name"] = "جديدة"
lyr.addFeature(nf)
check(wait(lambda: (js("CHANGED") or 0) >= 1, 20), "the edit reached the viewer as a change")
check(wait(lambda: (js("LAST.id") or 0) > shown, 20), "and the viewer drew its view again")
px2 = pixels()
check(px2.get("red", 0) > px1.get("red", 0) * 1.2, f"the new parcel is on the new image: {px1['red']} -> {px2.get('red')}")
lyr.rollBack()

# ---- details off: the PC answers no attributes -----------------------------------------------------------------
share.set_attributes(False)
js("IDENT=null;INFO.attrs=true;" f"identify(toScreen({cx},{cy}));1")
check(wait(lambda: js("!!IDENT") is True, 15) and js("IDENT.off") is True and js("IDENT.features.length") == 0,
      "with feature details off the PC sends none")
share.set_attributes(True)

# ---- the share server is for this PC's hidden page only, with the token ---------------------------------------
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def get(path):
    conn = http.client.HTTPSConnection("127.0.0.1", SHARE_PORT, context=ctx, timeout=10)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


check(get("/wrong/info")[0] == 404 and get("/wrong/host")[0] == 404, "the share needs the token")
tok = share.server.token
check(get(f"/{tok}/render?x0=0&y0=0&x1=10&y1=10&w=4000&h=100&d=1")[0] == 400, "an image too big is refused")
check(get(f"/{tok}/render?x0=10&y0=0&x1=0&y1=10&w=100&h=100&d=1")[0] == 400, "a wrong extent is refused")
check(get(f"/{tok}/host")[0] == 200, "the host page is served to this PC with the token")
got = {}
threading.Thread(target=lambda: got.update(r=get(f"/{tok}/render?x0={ce.xMinimum()}&y0={ce.yMinimum()}"
                                                f"&x1={ce.xMaximum()}&y1={ce.yMaximum()}&w=300&h=200&d=2"))).start()
wait(lambda: "r" in got, 30)
st, png = got.get("r", (0, b""))
check(st == 200 and png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (600, 400),
      "a sharp image for a high-density screen (twice the pixels)")

# ---- stop: the host ends and the viewer is told ----------------------------------------------------------------
host = share.host
t0 = time.monotonic()
share.stop()
print(f"stop took {time.monotonic() - t0:.2f}s", flush=True)
check(wait(lambda: not host.running(), 10), "the hidden host ends when sharing stops")
check(wait(lambda: js("ST") == "ended", 15), "the viewer shows that sharing ended")
check(js("live()") is False and js("$('st').textContent") == texts["ar"]["ended"], "disconnected, with the reason")
try:
    get(f"/{tok}/info")
    closed = False
except OSError:
    closed = True
check(closed, "the share server is closed")

# ---- the panel: start, the link and QR code, a viewer counted, stop --------------------------------------------
from qgis.PyQt.QtWidgets import QApplication, QMainWindow  # noqa: E402

from biruni.live_share import dock as ld  # noqa: E402
from biruni.secure import door as _door  # noqa: E402
_door.check_door = lambda u, **k: {"state": "ok", "page": _door.rel_of(u), "why": "", "version": "test"}  # the live site is checked in test_door.py

ls_server.SHARE_PORT = SHARE_PORT  # the panel takes the port from here when it starts


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.setCentralWidget(canvas)

    def mainWindow(self):  # noqa: N802
        return self.mw

    def mapCanvas(self):  # noqa: N802
        return canvas


class Theme:
    def tokens(self):
        return {k: "#888888" for k in ("text", "text_dim", "accent", "panel", "border", "accent_text",
                                       "accent_hover", "mid", "icon")}


class Activity:
    def __init__(self):
        self.logged = []

    def log(self, *a):
        self.logged.append(a)


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.activity = Activity()
        self.toolbox = None


plugin = Plugin()
d = ld.open_dock(plugin)
check(d is ld.open_dock(plugin), "one panel per window")
check(d.qr_card.isHidden() and d.attrs.isChecked(), "nothing shared yet; feature details on by default")
d.start()
check(d.share is not None and d.share.port == SHARE_PORT, "the panel shares")
check(d.qr.pixmap() is not None and not d.qr.pixmap().isNull(), "the QR code is shown")
check(d.link.startswith(PAGE_URL) and d.link.split("#k=")[1][:43] == d.share.key and d.share.key != key,
      "the link holds this share's own new key")
d._copy()
check(QApplication.clipboard().text() == d.link, "Copy link puts it on the clipboard")
check(plugin.activity.logged and plugin.activity.logged[0][0] == "live_share", "the start is in the activity log")
check(wait(lambda: d._read_events() or "Ready" in d.state.text(), 60), "the panel says it is ready: " + d.state.text())
viewer2 = Viewer(profile)
viewer2.setHtml(page, QUrl(f"https://viewer.invalid/s/#k={d.share.key}&l=en"))
check(wait(lambda: d._read_events() or d.share.viewers == 1, 60) and "1" in d.viewers.text(),
      "the panel counts the viewer: " + d.viewers.text())
d.attrs.setChecked(False)
check(d.share.maps.attributes is False, "turning feature details off applies at once")
d.attrs.setChecked(True)
host2 = d.share.host
d.toggle()
check(d.share is None and not host2.running() and d.qr_card.isHidden(), "Stop sharing ends everything")
ld.close_dock(plugin)

QgsProject.instance().removeAllMapLayers()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
os._exit(1 if failures else 0)  # the web engine and QGIS together do not always tear down cleanly
