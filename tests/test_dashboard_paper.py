"""The live paper (dashboard/paper.py + ext_paper.py), offscreen.

1. The marker dictionary: every code and its rotations differ from all others in >= 3 bits; markers round trip.
2. A paper is printed (PNG + PDF) and its passport is signed; a changed or foreign passport is refused.
3. The page's own tracker (ext_paper's JS, the code the phone runs) finds the markers in exact synthetic camera
   frames of the printed paper - turned, tilted, far and near, blurred, noisy, dark, one marker hidden - and the
   page's homography is checked against the truth (reprojection error in pixels at 1280 width).
4. The demo: the paper on a desk under a moving camera; the same tracker locks within a second and stays close
   to the true position; the live colours, the cross-filter and a tap on a parcel; screenshots.
"""
import base64
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

from PyQt6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QPoint, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import numpy as np  # noqa: E402

app = QApplication(sys.argv[:1])
from biruni.dashboard import cards  # noqa: E402
from biruni.dashboard import ext_paper as X  # noqa: E402
from biruni.dashboard import page as P  # noqa: E402
from biruni.dashboard import paper  # noqa: E402

cards._TEST_SEED.append(bytes(range(32)))
SHOTS = os.environ.get("BIRUNI_DASH_SHOTS",
                       r"C:\Users\HATEMT~1\AppData\Local\Temp\claude\8aa85b7d38969e04\12cf1281-d8dc-4630-b1a0-f86abf89dcd2\scratchpad")
failures, errors = [], []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.01)
    return False


# ---- a small planned neighbourhood ------------------------------------------------------------------------------------
LAT0, LON0 = 24.82, 46.672
MXm, MYm = 111320.0 * math.cos(math.radians(LAT0)), 110540.0


def ll(x, y):
    return [round(LON0 + x / MXm, 7), round(LAT0 + y / MYm, 7)]


STATUS = {"todo": "#90A4AE", "review": "#FFA726", "done": "#66BB6A", "approved": "#1E88E5"}
rnd = random.Random(5)
parcels = []
n = 0
for bx in range(-3, 3):
    for by in range(-2, 2):
        x0, y0 = bx * 150 + 12, by * 90 + 10
        for i in range(5):
            for j in range(2):
                n += 1
                xa, ya = x0 + i * 25, y0 + j * 34
                st = rnd.choice(list(STATUS))
                parcels.append({"type": "Feature", "id": n, "geometry": {"type": "Polygon", "coordinates": [[
                    ll(xa, ya), ll(xa + 25, ya), ll(xa + 25, ya + 34), ll(xa, ya + 34), ll(xa, ya)]]},
                    "properties": {"id": n, "_c": STATUS[st], "_l": str(n), "_k": {"district": "N" if by >= 0 else "S",
                                                                                    "status": st},
                                   "status": st, "area": 850}})
districts = [{"type": "Feature", "id": k, "geometry": {"type": "Polygon", "coordinates": [[
    ll(-455, y0), ll(445, y0), ll(445, y1), ll(-455, y1), ll(-455, y0)]]},
    "properties": {"id": k, "_l": name, "_k": {"district": k}}}
    for k, name, y0, y1 in (("N", "حي الشمال", 2, 180), ("S", "حي الجنوب", -180, -2))]
streets = [{"type": "Feature", "id": "r1", "geometry": {"type": "LineString", "coordinates": [ll(-470, 0), ll(460, 0)]},
            "properties": {"_l": "طريق الواحة", "_c": "#FFD54F"}}]
LAYERS = [{"id": "L0", "name": "الأحياء", "kind": "fill", "style": {"opacity": 0.05}, "legend": [], "popup": [],
           "geojson": {"type": "FeatureCollection", "features": districts}},
          {"id": "L1", "name": "القطع", "kind": "fill", "style": {"opacity": 0.6},
           "legend": [{"label": k, "color": v} for k, v in STATUS.items()],
           "popup": [{"key": "status", "label": "الحالة"}, {"key": "area", "label": "المساحة"}],
           "geojson": {"type": "FeatureCollection", "features": parcels}},
          {"id": "L2", "name": "الطرق", "kind": "line", "style": {}, "legend": [], "popup": [],
           "geojson": {"type": "FeatureCollection", "features": streets}}]

# ---- 1. markers -------------------------------------------------------------------------------------------------------
D = paper.DICT
rots = [[c, paper.rot(c), paper.rot(paper.rot(c)), paper.rot(paper.rot(paper.rot(c)))] for c in D]
dmin = min(bin(a ^ b).count("1") for i, ra in enumerate(rots) for j, rb in enumerate(rots)
           for a in (ra if i != j else ra[:1]) for b in (rb if i != j else rb[1:]))
check(len(D) >= 32 and dmin >= 3, "the marker dictionary: %d codes, rotations apart by >= %d bits" % (len(D), dmin))
g = paper.marker_cells(5)
check(all(g[0]) and all(g[5]) and all(r[0] and r[5] for r in g), "a marker has a whole black border")
code = 0
for r in range(4):
    for c in range(4):
        code |= g[r + 1][c + 1] << (15 - (r * 4 + c))
check(code == D[5], "marker cells round trip to their code")

# ---- 2. printing and the passport -------------------------------------------------------------------------------------
box = paper.bbox_of(LAYERS)
pp = paper.make_passport("d_t", "m1", "مخطط الاختبار — المرحلة الأولى", box, "A4", "landscape", code=11)
check(paper.verify_passport(pp), "a passport is signed and checks out")
check(paper.verify_passport(pp, cards.public_hex()), "... with this PC's dashboard key")
forged = json.loads(json.dumps(pp))
forged["merc"][0] += 50
check(not paper.verify_passport(forged), "a passport whose map extent was changed is refused")
other = paper.make_passport("d_t", "m1", "x", box, seed=bytes(range(1, 33)))
check(not paper.verify_passport(other, cards.public_hex()), "a paper signed by another computer is refused")
fx = pp["frame"]
e = pp["merc"]
check(abs((e[2] - e[0]) / (fx[2] - fx[0]) - (e[3] - e[1]) / (fx[3] - fx[1])) < 1e-6 * (e[2] - e[0]),
      "the printed map has the same scale across and down")
PPM = 5.0
img = paper.render_png(pp, LAYERS, os.path.join(SHOTS, "paper_sample.png"), PPM)
check(img.width() == round(297 * PPM) and img.height() == round(210 * PPM), "the paper renders at its size")
pdf = os.path.join(SHOTS, "paper_sample.pdf")
paper.render_pdf(pp, LAYERS, pdf)
check(os.path.getsize(pdf) > 5000 and open(pdf, "rb").read(5) == b"%PDF-", "the paper is written as a PDF")
tok = paper.qr_token(pp)
check(tok.startswith("BLP1.%s.11." % pp["id"]) and len(tok) < 140, "the QR token is short (%d chars)" % len(tok))

# ---- 3. the page's tracker on exact synthetic frames ----------------------------------------------------------------
_gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
_bpl = _gray.bytesPerLine()
PAPER = np.frombuffer(_gray.constBits().asstring(_bpl * _gray.height()), np.uint8).reshape(
    _gray.height(), _bpl)[:, :_gray.width()].astype(np.float32)


def rotm(th, ph, ps):
    cz, sz, cx, sx, cy, sy = math.cos(th), math.sin(th), math.cos(ph), math.sin(ph), math.cos(ps), math.sin(ps)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    return Rz @ Rx @ Ry


def cam_h(th, tilt, yaw, scale, W=1280, H=720, shift=(0, 0)):
    """Page millimetres -> image pixels of a pinhole camera looking at the paper."""
    w, h = 297.0, 210.0
    R = rotm(math.radians(th), math.radians(tilt), math.radians(yaw))
    Z = 600.0
    f = scale * W * Z / w
    M = np.array([[f * R[0, 0] + W / 2 * R[2, 0], f * R[0, 1] + W / 2 * R[2, 1], f * shift[0] + W / 2 * Z],
                  [f * R[1, 0] + H / 2 * R[2, 0], f * R[1, 1] + H / 2 * R[2, 1], f * shift[1] + H / 2 * Z],
                  [R[2, 0], R[2, 1], Z]])
    return M @ np.array([[1, 0, -w / 2], [0, 1, -h / 2], [0, 0, 1]])


def warp(Hm, W=1280, H=720, seed=0):
    """The exact picture: every pixel of the frame looks up the paper through the inverse homography."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    Hi = np.linalg.inv(Hm)
    u, v = xx + 0.5, yy + 0.5
    den = Hi[2, 0] * u + Hi[2, 1] * v + Hi[2, 2]
    mx = (Hi[0, 0] * u + Hi[0, 1] * v + Hi[0, 2]) / den
    my = (Hi[1, 0] * u + Hi[1, 1] * v + Hi[1, 2]) / den
    px, py = mx * PPM - 0.5, my * PPM - 0.5
    ph, pw = PAPER.shape
    inside = (px >= 0) & (py >= 0) & (px < pw - 1) & (py < ph - 1)
    x0 = np.clip(px.astype(np.int32), 0, pw - 2)
    y0 = np.clip(py.astype(np.int32), 0, ph - 2)
    fx, fy = np.clip(px - x0, 0, 1), np.clip(py - y0, 0, 1)
    val = ((PAPER[y0, x0] * (1 - fx) + PAPER[y0, x0 + 1] * fx) * (1 - fy)
           + (PAPER[y0 + 1, x0] * (1 - fx) + PAPER[y0 + 1, x0 + 1] * fx) * fy)
    desk = 70 + 25 * np.sin(xx / 37.0 + np.sin(yy / 53.0) * 2) + rng.normal(0, 6, (H, W))
    out = np.where(inside, val * 0.93, desk)
    return out


def blur(a, k=1):
    out = a.copy()
    for _ in range(k):
        out = (out + np.roll(out, 1, 0) + np.roll(out, -1, 0) + np.roll(out, 1, 1) + np.roll(out, -1, 1)) / 5
    return out


def png_b64(a):
    a = np.clip(a, 0, 255).astype(np.uint8)
    h, w = a.shape
    q = QImage(a.tobytes(), w, h, w, QImage.Format.Format_Grayscale8).copy()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    q.save(buf, "PNG")
    buf.close()
    return base64.b64encode(bytes(ba)).decode("ascii")


class Page(QWebEnginePage):
    def __init__(self):
        super().__init__(QWebEngineProfile.defaultProfile())

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            errors.append(f"{message} (line {line})")
            print("JS ERROR:", message, line, flush=True)


def view_of(html, size=(1280, 760)):
    v = QWebEngineView()
    pg = Page()
    v.setPage(pg)
    v.resize(*size)
    v.show()
    done = {}
    pg.loadFinished.connect(lambda ok: done.update(ok=ok))
    pg.setHtml(html, QUrl("https://dash.invalid/d/"))
    wait(lambda: "ok" in done, 20)
    return v, pg


def js(pg, expr, secs=60):
    out = {}
    pg.runJavaScript(expr, lambda r: out.update(v=r))
    wait(lambda: "v" in out, secs)
    return out.get("v")


html = P.page_html("ar", "preview")
check("paperView" in html and "detectorFactory" in html, "the page carries the live paper")
v, pg = view_of(html)
check(wait(lambda: js(pg, "!!(window.BiruniDash&&BiruniDash.paper)") is True, 10), "the page exposes BiruniDash.paper")
check(js(pg, "JSON.stringify(BiruniDash.paper.dict)") == json.dumps(D, separators=(",", ":")),
      "the page's dictionary is paper.py's")
lay = js(pg, "JSON.stringify(BiruniDash.paper.layout(297,210,11))")
mk, fr = paper.layout(297.0, 210.0, 11)
check(lay and json.loads(lay)["frame"] == fr and len(json.loads(lay)["m"]) == len(mk), "the page lays out a paper as paper.py does")
js(pg, "window.__PP=" + json.dumps(pp) + ";1")

GRID = [(fx[0] + (fx[2] - fx[0]) * i / 9, fx[1] + (fx[3] - fx[1]) * j / 6) for i in range(10) for j in range(7)]


def reproj(Hm, Hest):
    errs = []
    for x, y in GRID:
        a = Hm @ np.array([x, y, 1.0])
        b = Hest @ np.array([x, y, 1.0])
        errs.append(math.hypot(a[0] / a[2] - b[0] / b[2], a[1] / a[2] - b[1] / b[2]))
    return float(np.median(errs)), float(max(errs))


CASES = [("straight", 0, 0, 0, 0.75, 0, 0, 1, None),
         ("turned 30", 30, 10, 0, 0.7, 0, 3, 1, None),
         ("turned 60", 60, 15, 5, 0.6, 0, 3, 1, None),
         ("tilted 45", 8, 45, 0, 0.75, 0, 3, 1, None),
         ("tilted 35 and yawed", -12, 35, 18, 0.7, 1, 4, 1, None),
         ("far (scale 0.35)", 10, 20, 0, 0.35, 0, 3, 1, None),
         ("near (scale 1.0)", 4, 12, 0, 1.0, 0, 3, 1, None),
         ("blurred", 15, 25, 0, 0.7, 2, 3, 1, None),
         ("noisy", 15, 25, 0, 0.7, 0, 14, 1, None),
         ("low light", 15, 25, 0, 0.7, 1, 5, 0.32, None),
         ("one marker hidden", 15, 25, 0, 0.7, 0, 3, 1, 2),
         ("two markers hidden", 20, 30, 5, 0.65, 1, 4, 1, (0, 5))]
results = []
for name, th, tilt, yaw, sc, bl, noise, light, hide in CASES:
    Hm = cam_h(th, tilt, yaw, sc)
    a = warp(Hm, seed=len(results))
    if bl:
        a = blur(a, bl)
    if hide is not None:
        for hid in (hide if isinstance(hide, tuple) else (hide,)):
            m_ = next(m for m in pp["m"] if m[0] == hid)
            corners = [(m_[1] - 3, m_[2] - 3), (m_[1] + m_[3] + 3, m_[2] - 3), (m_[1] + m_[3] + 3, m_[2] + m_[3] + 3),
                       (m_[1] - 3, m_[2] + m_[3] + 3)]
            pts = [Hm @ np.array([x, y, 1.0]) for x, y in corners]
            xs = [p[0] / p[2] for p in pts]
            ys = [p[1] / p[2] for p in pts]
            a[max(0, int(min(ys))):int(max(ys)) + 1, max(0, int(min(xs))):int(max(xs)) + 1] = 150  # a thumb
    a = a * light + np.random.default_rng(7).normal(0, noise, a.shape)
    b64 = png_b64(a)
    t0 = time.monotonic()
    js(pg, """window.__R=null;(()=>{const im=new Image();im.onload=()=>{
      const c=document.createElement('canvas');c.width=im.width;c.height=im.height;const x=c.getContext('2d',{willReadFrequently:true});
      x.drawImage(im,0,0);const d=x.getImageData(0,0,c.width,c.height);const t=performance.now();
      const ms=BiruniDash.paper.detect(d.data,c.width,c.height,BiruniDash.paper.dict);const dt=performance.now()-t;
      const ps=BiruniDash.paper.pose(ms,window.__PP);window.__R=JSON.stringify({n:ms.length,ids:ms.map(m=>m.id),H:ps?ps.H:null,used:ps?ps.n:0,rms:ps?ps.rms:null,ms:dt})};
      im.src='data:image/png;base64,%s';return 1})()""" % b64)
    wait(lambda: js(pg, "!!window.__R") is True, 30)
    r = js(pg, "window.__R")
    try:
        r = json.loads(r)
    except (TypeError, ValueError):
        r = {"n": 0, "H": None}
    if r.get("H"):
        med, mx = reproj(Hm, np.array(r["H"]).reshape(3, 3))
    else:
        med, mx = float("inf"), float("inf")
    results.append((name, r.get("n"), r.get("used"), med, mx, r.get("ms")))
    print("   %-24s markers %s used %s  error median %.2f px, max %.2f px  detect %.0f ms" %
          (name, r.get("n"), r.get("used"), med, mx, r.get("ms") or -1), flush=True)
    if name in ("tilted 45", "low light", "one marker hidden"):
        QImage(np.clip(a, 0, 255).astype(np.uint8).tobytes(), 1280, 720, 1280, QImage.Format.Format_Grayscale8).copy().save(
            os.path.join(SHOTS, "paper_frame_%s.png" % name.replace(" ", "_")))
for name, nm, used, med, mx, ms in results:
    check(med < 3.0 and mx < 6.0, "tracked: %s (median %.2f px, max %.2f px over the map)" % (name, med, mx))
allmed = [r[3] for r in results]
print("   all cases: median of medians %.2f px, worst median %.2f px" % (float(np.median(allmed)), max(allmed)))
check(js(pg, """(()=>{const c=document.createElement('canvas');c.width=640;c.height=360;const x=c.getContext('2d');x.fillStyle='#777';
   x.fillRect(0,0,640,360);for(let i=0;i<40;i++){x.fillStyle=i%2?'#111':'#eee';x.fillRect(20+i*14,40+(i%5)*50,12,30)}
   const d=x.getImageData(0,0,640,360);return BiruniDash.paper.detect(d.data,640,360,BiruniDash.paper.dict).length})()""") == 0,
      "no markers are found in a picture without any")
v.close()

# ---- 4. the demo ------------------------------------------------------------------------------------------------------
NOW = time.time()
DEF = {"v": 1, "id": "d_t", "title": "مخطط الاختبار — المرحلة الأولى", "theme": "biruni-dark", "columns": 12, "dates": "both",
       "k_min": 0, "filters": [],
       "widgets": [{"id": "m1", "type": "map", "title": "الخريطة", "x": 0, "y": 0, "w": 8, "h": 7},
                   {"id": "k1", "type": "kpi", "title": "القطع المعتمدة", "x": 8, "y": 0, "w": 4, "h": 2,
                    "style": {"color": "#43a047", "decimals": 0}},
                   {"id": "b1", "type": "bar", "title": "حسب الحي", "x": 8, "y": 2, "w": 4, "h": 4}]}
DATA = {"m1": {"layers": LAYERS, "bbox": box, "total": len(parcels)}, "k1": {"value": 42},
        "b1": {"xf": "district", "items": [{"key": "N", "label": "حي الشمال", "value": 20},
                                           {"key": "S", "label": "حي الجنوب", "value": 22}]}}
v, pg = view_of(html, (1280, 760))
wait(lambda: js(pg, "!!window.BiruniDash") is True, 10)
js(pg, "BiruniDash.onSend=m=>{if(m&&m.t==='paper_list')setTimeout(()=>BiruniDash.feed({t:'paper_list',papers:[window.__PP]}),30)};"
       "window.__PP=%s;BiruniDash.feed(%s);BiruniDash.feed(%s);1" % (
           json.dumps(pp), json.dumps({"t": "dash", "def": DEF, "card": {"name": "t", "exp": NOW + 9e5, "zone": False},
                                       "pc": "PC", "pub": pp["pub"]}),
           json.dumps({"t": "data", "rev": 1, "ts": NOW, "widgets": DATA})))
check(wait(lambda: js(pg, "!!document.getElementById('paperBtn')") is True, 5), "a dashboard with a map gets the Live paper button")
# the frame rate this offscreen browser gives an empty page (no GPU here: a phone is many times faster)
js(pg, "window.__raf=0;(function f(){window.__raf++;if(window.__raf<1e6)requestAnimationFrame(f)})();1")
pump(1.5)
base_fps = (js(pg, "window.__raf") or 0) / 1.5
print("   this offscreen browser draws an empty page at %.0f frames/s" % base_fps)
js(pg, "window.__raf=1e7;document.getElementById('paperBtn').click();1")
check(wait(lambda: js(pg, "BiruniDash.paper.state.open&&!!BiruniDash.paper.view.demo") is True, 5),
      "without a camera the live paper opens the demo")
check(wait(lambda: js(pg, "BiruniDash.paper.state.papers.length") == 1, 3), "the PC's papers arrive (paper_list)")
locked = wait(lambda: js(pg, "!!BiruniDash.paper.state.firstLock") is True, 6)
lock_ms = js(pg, "BiruniDash.paper.state.firstLock?BiruniDash.paper.state.firstLock-BiruniDash.paper.view.demoStart:-1")
lock_fr = js(pg, "BiruniDash.paper.state.lockFrame||-1")
check(locked and lock_fr is not None and 0 < lock_fr <= 2,
      "the demo locks on the first frames it analyses (frame %s, %.0f ms here)" % (lock_fr, lock_ms or -1))
pump(5)
print("   timings ms:", js(pg, "JSON.stringify(BiruniDash.paper.state.prof)"), "worker:", js(pg, "String(BiruniDash.paper.view.worker&&true)"))
st = json.loads(js(pg, "JSON.stringify({err:BiruniDash.paper.state.err,fps:BiruniDash.paper.state.fps,tr:BiruniDash.paper.state.stats,"
                       "used:BiruniDash.paper.state.used,rms:BiruniDash.paper.state.rms,code:(BiruniDash.paper.state.paper||{}).code})"))
print("   demo: error %.2f px (smoothed, vs the true camera), %s fps, tracked %s of %s analysed frames, %s markers, rms %.2f" %
      (st["err"] or -1, st["fps"], st["tr"]["tracked"], st["tr"]["frames"], st["used"], st["rms"] or -1), flush=True)
check(st["err"] is not None and st["err"] < 4.0, "the overlay stays on the paper in the demo (%.2f px)" % (st["err"] or -1))
check(st["tr"]["frames"] > 0 and st["tr"]["tracked"] / st["tr"]["frames"] > 0.9, "the demo keeps the lock (%d/%d frames)" %
      (st["tr"]["tracked"], st["tr"]["frames"]))
check(st["code"] == 11, "the paper is told apart by its code marker")
v.grab().save(os.path.join(SHOTS, "show_paper_demo.png"))

# ---- 4b. a shaking hand: jitter on the screen and the match of the live layer, per display -----------------------------
# 150 frames at 30 fps of a hand-held camera: tremor at 8-12 Hz (±3-8 px, ±0.5-1.5°), a slow drift, a little tilt,
# motion blur and sensor noise. "before" is the display as it was (the frame as captured, the layer at that frame's
# pose); "stab" the steady paper; "ar" the stabilised augmented-reality view. jitter_layer: how much the live layer
# moves on the screen from frame to frame; jitter_paper: how much the paper itself moves; err: live layer vs paper.
SHAKE = json.loads(js(pg, "JSON.stringify(BiruniDash.paper.measure({n:150,blur:.6,noise:.10}))", 240))
PAN = json.loads(js(pg, "JSON.stringify(BiruniDash.paper.measure({n:120,pan:true,amp:0}))", 240))
print("   shaking hand, %d frames (%d lost), %.0f ms:" % (SHAKE["frames"], SHAKE["lost"], SHAKE["ms"]))
print("   %-8s %14s %14s %10s %10s" % ("display", "layer jitter", "paper jitter", "error", "max error"))
for m in ("before", "stab", "ar"):
    r = SHAKE[m]
    print("   %-8s %11.3f px %11.3f px %7.3f px %7.3f px" % (m, r["jitter_layer"], r["jitter_paper"], r["err"], r["err_max"]))
print("   deliberate pan: the stabilised view follows %d frame(s) behind; error ar %.3f px" % (PAN["lag_frames"], PAN["ar"]["err"]))
B, Sb, A = SHAKE["before"], SHAKE["stab"], SHAKE["ar"]
check(SHAKE["lost"] <= 3, "the shaking paper stays tracked (%d of %d frames lost)" % (SHAKE["lost"], SHAKE["frames"]))
check(Sb["jitter_layer"] < 1e-6, "steady paper: the live layer does not move on the screen")
check(Sb["jitter_paper"] < 0.5, "steady paper: the paper itself stands still (%.3f px from frame to frame)" % Sb["jitter_paper"])
check(Sb["err"] < 1.0, "steady paper: the live layer sits on the paper (%.3f px)" % Sb["err"])
check(A["jitter_layer"] * 4 <= B["jitter_layer"], "augmented reality: the tremor is cut at least 4x (%.2f -> %.2f px)" %
      (B["jitter_layer"], A["jitter_layer"]))
check(A["err"] < 1.0, "augmented reality: the live layer stays on the stabilised paper (%.3f px)" % A["err"])
check(PAN["lag_frames"] <= 1, "augmented reality follows a deliberate move within one frame (%d)" % PAN["lag_frames"])
# both displays on the live demo, with the shaking hand
js(pg, "BiruniDash.paper.view.demo.cam=BiruniDash.paper.shakeCam(1);BiruniDash.paper.setMode('stab');1")
pump(3)
check(js(pg, "!!BiruniDash.paper.view.bgOv&&BiruniDash.paper.view.mode==='stab'") is True, "the steady paper is shown once locked")
print("   the picture is warped by:", js(pg, "BiruniDash.paper.view.warpBy"))
v.grab().save(os.path.join(SHOTS, "paper_stab_steady.png"))
# the steady paper is a mosaic: a corner the camera does not see is never black (last seen picture, or the print)
dark = js(pg, """(()=>{const V=BiruniDash.paper.view,p=BiruniDash.paper.state.paper;if(!V.mos||!V.bg)return -1;const x=V.bg.getContext('2d'),w=p.page[0],h=p.page[1];
  let n=0;for(const q of [[4,4],[w-4,4],[w-4,h-4],[4,h-4],[w/2,4],[w/2,h-4]]){const B=V.bgOv,z=B[6]*q[0]+B[7]*q[1]+B[8],X=(B[0]*q[0]+B[1]*q[1]+B[2])/z,Y=(B[3]*q[0]+B[4]*q[1]+B[5])/z;
   const d=x.getImageData(Math.round(X),Math.round(Y),1,1).data;if(d[0]+d[1]+d[2]<120)n++}return n})()""")
check(dark == 0, "steady paper: no corner of the sheet is black (%s dark)" % dark)
js(pg, "BiruniDash.paper.setFrozen(true);1")
pump(0.6)
js(pg, "(()=>{const d=JSON.parse(JSON.stringify(BiruniDash.state.data.m1));const f=d.layers[1].geojson.features;for(let i=0;i<9;i++)f[i*5+1].properties._c='#E53935';"
       "BiruniDash.feed({t:'data',rev:5,ts:Date.now()/1000,widgets:{m1:d,k1:{value:61}}});return 1})()")
pump(0.5)
check(js(pg, "BiruniDash.paper.view.frozen&&document.getElementById('paperFreezeBtn').classList.contains('on')") is True
      and js(pg, "Object.keys(BiruniDash.paper.state.pulse).length") >= 6, "Hold freezes the picture and the live layer keeps updating")
v.grab().save(os.path.join(SHOTS, "paper_stab_held.png"))
js(pg, "BiruniDash.paper.setFrozen(false);BiruniDash.paper.setMode('ar');1")
pump(3)
v.grab().save(os.path.join(SHOTS, "paper_stab_ar.png"))
js(pg, "BiruniDash.paper.view.demo.cam=null;1")
# a parcel changes: it pulses
js(pg, "(()=>{const d=JSON.parse(JSON.stringify(BiruniDash.state.data.m1));const f=d.layers[1].geojson.features;for(let i=0;i<12;i++)f[i*3].properties._c='#1E88E5';"
       "BiruniDash.feed({t:'data',rev:2,ts:Date.now()/1000,widgets:{m1:d,k1:{value:54}}});return 1})()")
pump(0.4)
check(js(pg, "Object.keys(BiruniDash.paper.state.pulse).length") >= 6, "parcels that changed colour pulse on the paper")
check(js(pg, "document.getElementById('paperKpis').textContent.indexOf('54')>=0") is True, "the paper's header shows the live numbers")
v.grab().save(os.path.join(SHOTS, "show_paper_pulse.png"))
js(pg, "BiruniDash.setXF('district','N');1")
pump(0.8)
v.grab().save(os.path.join(SHOTS, "show_paper_xf.png"))
js(pg, "document.getElementById('xpill').click();1")
# tap a parcel: hold the demo camera still, let the lock settle, then tap the projected centre of a parcel
js(pg, "BiruniDash.paper.view.demo.freeze=5;1")
pump(2.5)
pt = js(pg, """(()=>{const S=BiruniDash.paper.state,V=BiruniDash.paper.view;const it=S.feat.filter(x=>x.kind==='fill'&&!x.faint)[25];
  return BiruniDash.paper.toScreen(it.cx,it.cy)})()""")
if pt:
    QTest.mouseClick(v.focusProxy(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(pt[0]), int(pt[1])))
check(wait(lambda: js(pg, "!document.getElementById('paperPop').classList.contains('hide')") is True, 3),
      "a tap on a parcel of the paper shows its details")
pump(0.3)
v.grab().save(os.path.join(SHOTS, "show_paper_pop.png"))
js(pg, "BiruniDash.paper.close();1")
check(js(pg, "document.getElementById('paperView').classList.contains('hide')") is True, "the live paper closes")
v.close()
# phone width
v, pg = view_of(html, (400, 860))
wait(lambda: js(pg, "!!window.BiruniDash") is True, 10)
js(pg, "BiruniDash.onSend=m=>{if(m&&m.t==='paper_list')setTimeout(()=>BiruniDash.feed({t:'paper_list',papers:[window.__PP]}),30)};"
       "window.__PP=%s;BiruniDash.feed(%s);BiruniDash.feed(%s);BiruniDash.paper.open(true);1" % (
           json.dumps(pp), json.dumps({"t": "dash", "def": DEF, "card": None, "pc": "PC", "pub": pp["pub"]}),
           json.dumps({"t": "data", "rev": 1, "ts": NOW, "widgets": DATA})))
check(wait(lambda: js(pg, "!!BiruniDash.paper.state.firstLock") is True, 8), "the demo locks at phone width (portrait)")
pump(3)
v.grab().save(os.path.join(SHOTS, "show_paper_phone.png"))
js(pg, "BiruniDash.paper.view.demo.cam=BiruniDash.paper.shakeCam(1);BiruniDash.paper.setMode('stab');1")
pump(3)
check(js(pg, "BiruniDash.paper.view.mode==='stab'&&!!BiruniDash.paper.view.bgOv&&BiruniDash.paper.view.cv.height>BiruniDash.paper.view.cv.width") is True,
      "the steady paper fills a portrait phone screen")
v.grab().save(os.path.join(SHOTS, "paper_stab_phone.png"))
v.close()
# a page without a map: no button, no errors
v, pg = view_of(html)
wait(lambda: js(pg, "!!window.BiruniDash") is True, 10)
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "dash", "def": {"v": 1, "id": "x", "title": "x", "widgets": [
    {"id": "k1", "type": "kpi", "title": "k", "x": 0, "y": 0, "w": 3, "h": 2}]}, "card": None, "pc": "", "pub": ""}))
pump(0.5)
check(js(pg, "!document.getElementById('paperBtn')") is True, "a dashboard without a map has no Live paper button")
v.close()

# ---- 5. the PC answers paper_list with its own papers -------------------------------------------------------------------
if X.handler is not None:
    sent = []

    class _S:
        card = None
        definition = None

        def _out(self, m):
            sent.append(m)

    class _Svc:
        project = None

        def _def_of(self, s):
            return {"id": "d_t"}

    orig = paper.papers_for
    paper.papers_for = lambda dash, project=None: [pp] if dash == "d_t" else []
    try:
        from biruni.dashboard import service as SV

        SV.HANDLERS["paper_list"](_Svc(), _S(), {"t": "paper_list"})
        check(sent and sent[-1]["t"] == "paper_list" and sent[-1]["papers"][0]["id"] == pp["id"],
              "the PC answers paper_list with the dashboard's papers")
        s2 = _S()
        s2.card = {"widgets": ["k1"]}
        SV.HANDLERS["paper_list"](_Svc(), s2, {"t": "paper_list"})
        check(sent[-1]["papers"] == [], "a card without the map widget gets no papers")
    finally:
        paper.papers_for = orig
check(X.ACTIONS and X.ACTIONS[0][0] == "paper_print", "the panel gets a Print a live paper action")
langs = ("en", "ar", "es", "fr", "pt_BR", "tr", "fa")
check(all(set(X.TEXTS[l]) == set(X.TEXTS["en"]) for l in langs), "the page texts exist in all 7 languages")
check(all("بيروني" not in X.TEXTS[l]["paper_search"] for l in langs if l != "ar"), "Biruni is not translated outside Arabic")

check(not errors, "no JavaScript errors" + ("" if not errors else ": " + "; ".join(errors[:4])))
print("ALL PASSED" if not failures else "FAILED: %d" % len(failures), flush=True)
sys.exit(1 if failures else 0)
