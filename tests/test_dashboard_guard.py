"""«Who leaked this screenshot?» (dashboard/ext_guard.py + guard.py): ten cards see the same dashboard, each with
its own invisible mark; screenshots are attacked (half size, JPEG 60, one widget cropped, gamma, blur, a re-shot of
a scaled view, half size + JPEG) and the detector must name the right card, and find nothing in unmarked shots.
The mark must be invisible: PSNR > 45 dB against the same page without it.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_guard.py
"""
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

from PyQt6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])

import numpy as np  # noqa: E402

from biruni.dashboard import guard  # noqa: E402
from biruni.dashboard import page as P  # noqa: E402

guard._TEST_SECRET.append(b"\x07" * 32)
QUICK = "--quick" in sys.argv
failures, errors = [], []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.02)
    return False


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            errors.append(f"{message} (line {line})")
            print("JS ERROR:", message, line, flush=True)


def js(pg, expr):
    out = {}
    pg.runJavaScript(expr, lambda r: out.update(v=r))
    wait(lambda: "v" in out, 10)
    return out.get("v")


# ---- a dashboard ------------------------------------------------------------------------------------------------------
NOW = time.time()
DAY = 86400
DEF = {"v": 1, "id": "d_g", "title": "Survey progress", "theme": "biruni-dark", "columns": 12, "dates": "greg",
       "widgets": [
           {"id": "k1", "type": "kpi", "title": "Parcels surveyed", "x": 0, "y": 0, "w": 3, "h": 2},
           {"id": "g1", "type": "gauge", "title": "Daily target", "x": 3, "y": 0, "w": 3, "h": 2, "style": {"unit": "%"}},
           {"id": "s1", "type": "sparkline", "title": "Per day", "x": 6, "y": 0, "w": 3, "h": 2},
           {"id": "k2", "type": "kpi", "title": "Area (ha)", "x": 9, "y": 0, "w": 3, "h": 2},
           {"id": "l1", "type": "line", "title": "Surveyed and checked", "x": 0, "y": 2, "w": 6, "h": 4},
           {"id": "b1", "type": "bar", "title": "Parcels by district", "x": 6, "y": 2, "w": 3, "h": 4},
           {"id": "p1", "type": "pie", "title": "Land use", "x": 9, "y": 2, "w": 3, "h": 4},
           {"id": "t1", "type": "table", "title": "Latest parcels", "x": 0, "y": 6, "w": 6, "h": 4},
           {"id": "fd1", "type": "feed", "title": "Live activity", "x": 6, "y": 6, "w": 6, "h": 4}]}
DATA = {
    "k1": {"value": 1043, "prev": 1001, "delta": 42}, "k2": {"value": 142.6, "prev": 139.1, "delta": 3.5},
    "g1": {"value": 76, "min": 0, "max": 100},
    "s1": {"series": [{"name": "P", "points": [[NOW - (9 - i) * DAY, v] for i, v in
                                               enumerate([31, 40, 38, 52, 47, 60, 58, 66, 71, 69])]}], "x": "time"},
    "l1": {"series": [{"name": "Surveyed", "points": [[NOW - (13 - i) * DAY, 20 + i * 6 + (i % 3) * 4] for i in range(14)]},
                      {"name": "Checked", "points": [[NOW - (13 - i) * DAY, 12 + i * 5] for i in range(14)]}], "x": "time"},
    "b1": {"items": [{"key": k, "label": k, "value": v} for k, v in (("North", 412), ("South", 377), ("East", 254))]},
    "p1": {"items": [{"key": "r", "label": "Residential", "value": 61}, {"key": "c", "label": "Commercial", "value": 22},
                     {"key": "p", "label": "Public", "value": 12}]},
    "t1": {"columns": [{"key": "no", "label": "Parcel"}, {"key": "d", "label": "District"}, {"key": "a", "label": "Area"}],
           "rows": [[str(40 + i), ["North", "South", "East"][i % 3], 500 + i * 7.5] for i in range(8)], "total": 1043},
    "fd1": {"items": [{"ts": NOW - 40 * (i + 1), "who": n, "what": f"edited parcel {40 + i}", "layer": "Parcels"}
                      for i, n in enumerate(("Mona", "Sami", "Omar", "Lina", "Khaled"))]},
}
DASH = {"t": "dash", "def": DEF, "card": {"name": "x", "exp": NOW + DAY, "zone": False}, "pc": "Office PC", "pub": "ab" * 32}

P.EXTENSIONS = ("ext_guard",)          # this test looks at the guard alone
html = P.page_html("en", "preview")
check("guardA" in html and "guard_hello" in html, "the guard extension is in the page")

view = QWebEngineView()
pg = Page(QWebEngineProfile.defaultProfile())
view.setPage(pg)
view.resize(1366, 900)
view.show()
loaded = {}
pg.loadFinished.connect(lambda ok: loaded.update(ok=ok))
pg.setHtml(html, QUrl("https://dash.invalid/d/"))
wait(lambda: "ok" in loaded, 20)
wait(lambda: js(pg, "!!window.BiruniDash") is True, 10)
js(pg, "window.__sent=[];BiruniDash.onSend=m=>window.__sent.push(m);1")
js(pg, "BiruniDash.feed(%s);BiruniDash.feed(%s);1" % (
    __import__("json").dumps(DASH), __import__("json").dumps({"t": "data", "rev": 1, "ts": NOW, "widgets": DATA})))
pump(1.0)
sent = js(pg, "JSON.stringify(window.__sent)") or "[]"
check("guard_hello" in sent, "after the dashboard is built the page asks for its mark")
# freeze what moves (the live dot's pulse, the freshness text) so marked/unmarked differ by the mark only
js(pg, "(()=>{const s=document.createElement('style');s.textContent='*,*::before,*::after{animation:none!important;"
       "transition:none!important}';document.head.appendChild(s);const f=document.getElementById('fresh');"
       "if(f){f.style.visibility='hidden'}return 1})()")
pump(1.5)


def grab():
    pump(0.6)
    return view.grab().toImage().convertToFormat(QImage.Format.Format_RGB888)


def arr(q):
    return guard.image_array(q)


def to_q(a):
    a = np.ascontiguousarray(np.clip(np.rint(a), 0, 255).astype(np.uint8))
    h, w, _ = a.shape
    return QImage(a.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def jpeg(q, quality):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    q.save(buf, "JPG", quality)
    buf.close()
    out = QImage()
    out.loadFromData(ba, "JPG")
    return out.convertToFormat(QImage.Format.Format_RGB888)


def scaled(q, f):
    return q.scaled(int(q.width() * f), int(q.height() * f), Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)


def blur(q):
    a = arr(q)
    k = np.array([1, 2, 1], np.float32) / 4
    a = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 0, a)
    a = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 1, a)
    return to_q(a)


def gamma(q, g):
    return to_q(255.0 * (arr(q) / 255.0) ** g)


WRECTS = js(pg, "(()=>[...document.querySelectorAll('.w')].map(n=>{const r=n.getBoundingClientRect();"
                "return [r.left,r.top,r.width,r.height]}).filter(r=>r[2]>50&&r[3]>50))()") or []
WRECTS.sort(key=lambda r: -r[2] * r[3])
print("widgets (px):", [f"{int(r[2])}x{int(r[3])}" for r in WRECTS])


def crop_rect(q, r):
    x, y, w, h = [int(v) for v in r]
    return q.copy(x, y, w, h)


def crop(q):                      # the biggest widget (a chart)
    return crop_rect(q, WRECTS[0])


ATTACKS = [("original", lambda q: q), ("50% size", lambda q: scaled(q, 0.5)), ("JPEG 60", lambda q: jpeg(q, 60)),
           ("one widget", crop), ("gamma 1.3", lambda q: gamma(q, 1.3)), ("slight blur", blur),
           ("re-shot at 75%", lambda q: jpeg(scaled(q, 0.75), 85)),
           ("50% + JPEG 60", lambda q: jpeg(scaled(q, 0.5), 60))]

# ---- the unmarked page and its PSNR --------------------------------------------------------------------------------
plain = grab()
CARDS = [(f"c_{i:02d}", f"Card {i}") for i in range(10)]
n_cards = 3 if QUICK else 10
results = {name: [] for name, _ in ATTACKS}
timings = []
psnrs = []
for i, (cid, name) in enumerate(CARDS[:n_cards]):
    msg = guard.message(cid, b"\x07" * 32)
    js(pg, "BiruniDash.guard.clear();1")
    plain = grab()                      # the same moment without the mark (relative times move)
    js(pg, "BiruniDash.feed(%s);1" % __import__("json").dumps(msg))
    shot = grab()
    if i == 0:
        first = (cid, shot)
        check(js(pg, "document.querySelectorAll('.guardL').length") == 2, "the mark is two click-through layers")
        shot.save(os.path.join(os.environ.get("TEMP", "."), "guard_marked.png"))
        plain.save(os.path.join(os.environ.get("TEMP", "."), "guard_plain.png"))
    a, b = arr(shot), arr(plain)
    mse = float(((a - b) ** 2).mean())
    psnrs.append(10 * np.log10(255 ** 2 / mse) if mse > 0 else 99.0)
    for an, fn in ATTACKS:
        t0 = time.monotonic()
        r = guard.detect(fn(shot), CARDS, sec=b"\x07" * 32)
        timings.append(time.monotonic() - t0)
        ok = r["found"] and r["card"] == cid
        results[an].append((ok, r["score"], (r.get("second") or {}).get("score")))
        if not ok:
            print(f"  miss: {name} / {an}: {r}", flush=True)
js(pg, "BiruniDash.guard.clear();1")
check(js(pg, "document.querySelectorAll('.guardL').length") == 0, "the mark can be taken off (tests)")

# ---- every widget on its own (how small a crop can still be traced) -------------------------------------------------
per_widget = []
for r in WRECTS:
    q = crop_rect(first[1], r)
    d = guard.detect(q, CARDS, sec=b"\x07" * 32)
    per_widget.append((int(r[2]), int(r[3]), d["found"] and d["card"] == first[0], d["score"],
                       d["found"] and d["card"] != first[0]))

# ---- false alarms on unmarked shots --------------------------------------------------------------------------------
fp = []
for an, fn in ATTACKS:
    r = guard.detect(fn(plain), CARDS, sec=b"\x07" * 32)
    fp.append((an, r["found"], r["score"]))
# another unmarked page: light theme, different layout width
js(pg, "BiruniDash.setTheme('biruni-light');1")
view.resize(1100, 820)
plain2 = grab()
for an, fn in ATTACKS:
    r = guard.detect(fn(plain2), CARDS, sec=b"\x07" * 32)
    fp.append((an + " (light)", r["found"], r["score"]))

# ---- light theme marked ---------------------------------------------------------------------------------------------
light_ok = []
light_psnr = []
for cid, name in CARDS[:3]:
    js(pg, "BiruniDash.guard.clear();1")
    p0 = grab()
    js(pg, "BiruniDash.feed(%s);1" % __import__("json").dumps(guard.message(cid, b"\x07" * 32)))
    s = grab()
    mse = float(((arr(s) - arr(p0)) ** 2).mean())
    light_psnr.append(10 * np.log10(255 ** 2 / mse) if mse > 0 else 99.0)
    for an, fn in (("original", lambda q: q), ("50% + JPEG 60", lambda q: jpeg(scaled(q, 0.5), 60))):
        r = guard.detect(fn(s), CARDS, sec=b"\x07" * 32)
        light_ok.append(r["found"] and r["card"] == cid)

# ---- the table --------------------------------------------------------------------------------------------------------
print("\nattack               identified   mean score   mean runner-up")
for an, _ in ATTACKS:
    rs = results[an]
    hit = sum(1 for r in rs if r[0])
    sc = np.mean([r[1] for r in rs])
    sec2 = np.mean([r[2] for r in rs if r[2] is not None])
    print(f"{an:20s} {hit:3d}/{len(rs):<3d}       {sc:6.1f}       {sec2:6.1f}")
print("\nunmarked shots: found =", sum(1 for f in fp if f[1]), "of", len(fp), "; max score %.1f" % max(f[2] for f in fp))
print("PSNR marked vs unmarked: min %.1f dB, mean %.1f dB (light theme: min %.1f dB)"
      % (min(psnrs), float(np.mean(psnrs)), min(light_psnr)))
print("one widget at a time (card 0):")
for w, h, ok, sc, _wrong in per_widget:
    print(f"   {w:4d}x{h:<4d} {'identified' if ok else 'not traced':12s} score {sc}")
print("detect time: mean %.1f s, max %.1f s" % (float(np.mean(timings)), max(timings)))

check(min(psnrs) > 45.0, f"the mark is invisible: PSNR {min(psnrs):.1f} dB > 45")
total = sum(len(v) for v in results.values())
hits = sum(1 for v in results.values() for r in v if r[0])
check(hits / total >= 0.9, f"the right card is named in {hits}/{total} attacked screenshots")
for an in ("original", "50% size", "JPEG 60", "one widget", "50% + JPEG 60"):
    rs = results[an]
    check(all(r[0] for r in rs), f"«{an}»: every card is identified")
check(not any(f[1] for f in fp), f"no card is named in an unmarked screenshot ({len(fp)} tries)")
check(all(light_ok), "light theme: the mark is found too")
check(min(light_psnr) > 45.0, f"light theme: still invisible (PSNR {min(light_psnr):.1f} dB)")
big = [ok for w, h, ok, _s, _x in per_widget if w * h >= 150000]
check(bool(big) and all(big), f"every widget of at least 150 000 px on its own is traced ({sum(big)}/{len(big)})")
check(not any(x for *_r, x in per_widget), "a crop of one widget never names a wrong card")
check(not errors, f"no JavaScript errors ({errors[:2]})")
view.close()
print("\nALL PASSED" if not failures else f"\nFAILED: {len(failures)}")
sys.exit(1 if failures else 0)
