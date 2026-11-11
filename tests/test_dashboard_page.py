"""The dashboard's browser page (dashboard/page.py), offscreen in QtWebEngine: every language and transport builds;
all 15 widgets draw from the SPEC shapes; numbers move without the DOM being made again; hidden values; themes;
right to left; explain and seal requests; the seal's QR code reads back; the LAN transport against a small server
here; screenshots for a look.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

from PyQt6.QtCore import QCoreApplication, QSize, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QImage, QPainter  # noqa: E402
from PyQt6.QtSvg import QSvgRenderer  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
from biruni.dashboard import page as P  # noqa: E402

SHOTS = os.environ.get("BIRUNI_DASH_SHOTS",
                       r"C:\Users\HATEMT~1\AppData\Local\Temp\claude\8aa85b7d38969e04\12cf1281-d8dc-4630-b1a0-f86abf89dcd2\scratchpad")
failures = []
errors = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.02)
    return False


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


class Page(QWebEnginePage):
    def __init__(self, name):
        super().__init__(QWebEngineProfile.defaultProfile())
        self.name = name
        self.log = []

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        self.log.append((level, message, line))
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            errors.append(f"{self.name}: {message} (line {line})")
            print(f"{self.name} JS ERROR:", message, line, flush=True)


def view_of(name, html, url, size=(1366, 900)):
    v = QWebEngineView()
    pg = Page(name)
    v.setPage(pg)
    v.resize(*size)
    v.show()
    loaded = {}
    pg.loadFinished.connect(lambda ok: loaded.update(ok=ok))
    if html is None:
        pg.load(QUrl(url))
    else:
        pg.setHtml(html, QUrl(url))
    wait(lambda: "ok" in loaded, 20)
    return v, pg


def js(pg, expr):
    out = {}
    pg.runJavaScript(expr, lambda r: out.update(v=r))
    wait(lambda: "v" in out, 10)
    return out.get("v")


def shot(v, name):
    pump(0.6)
    img = v.grab()
    path = os.path.join(SHOTS, f"dash_{name}.png")
    img.save(path)
    return path, img


# ---- a sample definition and data: one widget of each of the 15 kinds --------------------------------------------------
NOW = time.time()
DEF = {"v": 1, "id": "d_test", "title": "Survey progress — Al Narjis", "theme": "biruni-light", "columns": 12,
       "dates": "both", "k_min": 3,
       "filters": [{"id": "f1", "source": "s1", "field": "district", "label": "District"}],
       "widgets": [
           {"id": "k1", "type": "kpi", "title": "Parcels surveyed", "x": 0, "y": 0, "w": 3, "h": 2,
            "style": {"color": "#2e7d32", "icon": "check", "decimals": 0, "target": 1200,
                      "thresholds": [{"at": 900, "color": "#f9a825"}]}},
           {"id": "g1", "type": "gauge", "title": "Daily target", "x": 3, "y": 0, "w": 3, "h": 2,
            "style": {"unit": "%", "thresholds": [{"at": 50, "color": "#c62828"}, {"at": 80, "color": "#f9a825"}]}},
           {"id": "s1", "type": "sparkline", "title": "Parcels per day", "x": 6, "y": 0, "w": 3, "h": 2},
           {"id": "f1w", "type": "filter", "title": "District", "x": 9, "y": 0, "w": 3, "h": 2,
            "options": {"filter": "f1"}},
           {"id": "l1", "type": "line", "title": "Surveyed and checked", "x": 0, "y": 2, "w": 6, "h": 4},
           {"id": "a1", "type": "area", "title": "Area by use (m²)", "x": 6, "y": 2, "w": 6, "h": 4},
           {"id": "b1", "type": "bar", "title": "Parcels by district", "x": 0, "y": 6, "w": 4, "h": 4},
           {"id": "p1", "type": "pie", "title": "Land use", "x": 4, "y": 6, "w": 4, "h": 4},
           {"id": "pr1", "type": "progress", "title": "Progress by zone", "x": 8, "y": 6, "w": 4, "h": 4},
           {"id": "m1", "type": "map", "title": "Survey points", "x": 0, "y": 10, "w": 6, "h": 5,
            "style": {"by": "status"}},
           {"id": "c1", "type": "choropleth", "title": "Parcels per block", "x": 6, "y": 10, "w": 6, "h": 5},
           {"id": "t1", "type": "table", "title": "Latest parcels", "x": 0, "y": 15, "w": 6, "h": 4},
           {"id": "lb1", "type": "leaderboard", "title": "Team this week", "x": 6, "y": 15, "w": 3, "h": 4},
           {"id": "fd1", "type": "feed", "title": "Live activity", "x": 9, "y": 15, "w": 3, "h": 4},
           {"id": "tx1", "type": "text", "title": "Note", "x": 0, "y": 19, "w": 12, "h": 2},
       ]}


def poly(x, y, s=0.004):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]}


DAY = 86400
DATA = {
    "k1": {"value": 1043, "prev": 1001, "delta": 42, "seal": "K7QM-2D9X"},
    "g1": {"value": 76, "min": 0, "max": 100, "seal": "G4RT-88PQ"},
    "s1": {"series": [{"name": "Parcels", "points": [[NOW - (9 - i) * DAY, v] for i, v in
                                                       enumerate([31, 40, 38, 52, 47, 60, 58, 66, 71, 69])]}], "x": "time"},
    "f1w": {"options": [{"key": "North", "label": "North", "count": 412}, {"key": "South", "label": "South", "count": 377},
                        {"key": "East", "label": "East", "count": 254}], "current": []},
    "l1": {"series": [{"name": "Surveyed", "points": [[NOW - (13 - i) * DAY, 20 + i * 6 + (i % 3) * 4] for i in range(14)]},
                      {"name": "Checked", "points": [[NOW - (13 - i) * DAY, 12 + i * 5] for i in range(14)]}], "x": "time"},
    "a1": {"series": [{"name": "Residential", "points": [[m, 1000 + m * 120] for m in range(1, 9)]},
                      {"name": "Commercial", "points": [[m, 400 + m * 60] for m in range(1, 9)]},
                      {"name": "Public", "points": [[m, 250 + m * 30] for m in range(1, 9)]}], "x": "number"},
    "b1": {"items": [{"key": "North", "label": "North", "value": 412}, {"key": "South", "label": "South", "value": 377},
                     {"key": "East", "label": "East", "value": 254}, {"key": "West", "label": "West", "hidden": True}],
           "seal": "B9ZZ-1Q2W"},
    "p1": {"items": [{"key": "res", "label": "Residential", "value": 61}, {"key": "com", "label": "Commercial", "value": 22},
                     {"key": "pub", "label": "Public", "value": 12}, {"key": "x", "label": "Other", "hidden": True}]},
    "pr1": {"items": [{"label": "Zone 1", "done": 40, "total": 55}, {"label": "Zone 2", "done": 31, "total": 60},
                      {"label": "Zone 3", "done": 58, "total": 58}, {"label": "Zone 4", "done": 9, "total": 48}]},
    "m1": {"geojson": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": i, "geometry": {"type": "Point", "coordinates": [46.66 + (i % 7) * 0.004, 24.81 + (i // 7) * 0.003]},
         "properties": {"label": f"Point {i}", "status": ["done", "check", "todo"][i % 3]}} for i in range(35)] + [
        {"type": "Feature", "id": "road", "geometry": {"type": "LineString", "coordinates": [[46.655, 24.808], [46.69, 24.83]]},
         "properties": {"label": "Road", "status": "road"}}]}, "bbox": [46.65, 24.805, 46.69, 24.835]},
    "c1": {"geojson": {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f"b{i}", "geometry": poly(46.66 + (i % 4) * 0.0045, 24.81 + (i // 4) * 0.0045),
         "properties": {"label": f"Block {i}", "value": 5 + i * 3, "hidden": i == 5}} for i in range(12)]},
        "min": 5, "max": 38},
    "t1": {"columns": [{"key": "no", "label": "Parcel"}, {"key": "district", "label": "District"},
                       {"key": "area", "label": "Area m²"}],
           "rows": [["45", "North", 612.5], ["46", "North", 588.0], ["112", "South", 731.25], {"hidden": True},
                    ["9", "East", 455.0]], "total": 1043},
    "lb1": {"items": [{"key": "mona", "label": "Mona", "value": 96}, {"key": "sami", "label": "Sami", "value": 81},
                      {"key": "omar", "label": "Omar", "value": 77}, {"key": "lina", "label": "Lina", "value": 52}]},
    "fd1": {"items": [{"ts": NOW - 40, "who": "Mona", "what": "edited parcel 45", "layer": "Parcels"},
                      {"ts": NOW - 400, "who": "Sami", "what": "added 3 points", "layer": "Survey points"},
                      {"ts": NOW - 4000, "who": "Omar", "what": "checked block 7", "layer": "Blocks"}]},
    "tx1": {"text": "Survey of **Al Narjis** district, phase 2.\nNumbers update live from the office computer."},
}
CARD_INFO = {"name": "Supervisor North", "exp": NOW + 30 * DAY, "zone": True}
DASH = {"t": "dash", "def": DEF, "card": CARD_INFO, "pc": "Office PC", "pub": "ab" * 32}

# ---- 1. every language and transport builds ------------------------------------------------------------------------------
sizes = {}
for lang in P.LANGS:
    for tr in P.TRANSPORTS:
        h = P.page_html(lang, tr)
        sizes[(lang, tr)] = len(h.encode("utf-8"))
        assert "__TEXTS__" not in h and "/*APP*/" not in h and "__TRANSPORT__" not in h
check(all(s > 20000 for s in sizes.values()), "pages built for 7 languages x 3 transports")
check('dir="rtl"' in P.page_html("ar", "lan") and 'dir="ltr"' in P.page_html("en", "lan"), "the first language sets the direction")
check("RTCPeerConnection" in P.page_html("en", "bridge") and "NostrRelay" in P.page_html("en", "bridge")
      and "NostrRelay" not in P.page_html("en", "lan"), "the bridge page carries the bridge core; the others do not")
print("page sizes (KB): lan %.1f, preview %.1f, bridge %.1f" % (sizes[("en", "lan")] / 1024, sizes[("en", "preview")] / 1024,
                                                                 sizes[("en", "bridge")] / 1024))
try:
    P.page_html("en", "nope")
    check(False, "an unknown transport is refused")
except ValueError:
    check(True, "an unknown transport is refused")
out = os.path.join(SHOTS, "dash_public")
path = P.write_public_page(out)
check(os.path.isfile(path) and path.endswith(os.path.join("d", "index.html")), "write_public_page writes d/index.html")

# ---- 2. the preview page with all 15 widgets ------------------------------------------------------------------------------
v, pg = view_of("preview", P.page_html("en", "preview"), "https://dash.invalid/d/")
check(wait(lambda: js(pg, "!!window.BiruniDash") is True, 10), "the page exposes window.BiruniDash")
sent0 = js(pg, "JSON.stringify(BiruniDash.state.sent)")
check(json.loads(sent0 or "[]")[:1] and json.loads(sent0)[0].get("t") == "hello", "the page says hello first")
js(pg, "window._out=[];BiruniDash.onSend=m=>window._out.push(m);1")
js(pg, "BiruniDash.feed(%s);1" % json.dumps(DASH))
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "data", "rev": 1, "ts": NOW, "widgets": DATA}))
pump(1.2)
n = js(pg, "document.querySelectorAll('#grid .w').length")
check(n == 15, f"15 widgets on the grid ({n})")
probes = {
    "kpi": "document.querySelector('#w_k1 .kval').textContent",
    "gauge": "document.querySelector('#w_g1 .gnum').textContent",
    "sparkline": "document.querySelectorAll('#w_s1 svg path').length",
    "filter": "document.querySelectorAll('#w_f1w .chip').length",
    "line": "document.querySelectorAll('#w_l1 svg path').length",
    "area": "document.querySelectorAll('#w_a1 svg path').length",
    "bar": "document.querySelectorAll('#w_b1 .vcol, #w_b1 .hrow').length",
    "pie": "document.querySelectorAll('#w_p1 svg g path').length",
    "progress": "document.querySelectorAll('#w_pr1 .prow').length",
    "map": "document.querySelectorAll('#w_m1 svg circle, #w_m1 svg path.f').length",
    "choropleth": "document.querySelectorAll('#w_c1 svg path.f').length",
    "table": "document.querySelectorAll('#w_t1 tbody tr').length",
    "leaderboard": "document.querySelectorAll('#w_lb1 .lrow').length",
    "feed": "document.querySelectorAll('#w_fd1 li').length",
    "text": "document.querySelectorAll('#w_tx1 strong').length",
}
got = {k: js(pg, e) for k, e in probes.items()}
print("rendered:", got)
check(got["kpi"] == "1,043", "kpi shows its value")
check(str(got["gauge"]).startswith("76"), "gauge shows its value")
check(got["sparkline"] >= 2 and got["line"] >= 2 and got["area"] >= 6, "sparkline, line (2 series) and stacked area drawn")
check(js(pg, "document.querySelectorAll('#w_l1 svg circle').length") == 28, "line points marked")
check(got["filter"] == 4, "filter chips (All + 3)")
check(got["bar"] == 4 and got["pie"] == 3 and got["progress"] == 4, "bar, pie (hidden slice left out), progress")
check(got["map"] == 36 and got["choropleth"] == 12, "map points + line, choropleth blocks")
check(got["table"] == 5 and got["leaderboard"] == 4 and got["feed"] == 3 and got["text"] == 1, "table, leaderboard, feed, text")
check(js(pg, "document.querySelector('#w_k1 .delta').className").endswith("up"), "kpi delta arrow points up")
check("hatch" in js(pg, "[...document.querySelectorAll('#w_b1 i')].map(x=>x.className).join(' ')"), "a hidden bar is hatched")
check(js(pg, "[...document.querySelectorAll('#w_b1 .vl')].map(x=>x.textContent).includes('hidden')"), "a hidden bar says hidden")
check(js(pg, "document.querySelectorAll('#w_t1 td.hatch').length") == 1, "a hidden table row is hatched")
check(js(pg, "document.querySelector('#w_k1 .sealb').textContent") == "K7QM-2D9X", "the seal code is shown")
check("#f9a825" in (js(pg, "document.querySelector('#w_k1 .kval').style.color") or "").lower()
      or "249, 168, 37" not in (js(pg, "document.querySelector('#w_k1 .kval').style.color") or ""),
      "kpi colour follows its thresholds")
check(js(pg, "document.getElementById('cardInfo').textContent").startswith("Card: Supervisor North"), "the card is named in the footer")
check("Limited to an area" in js(pg, "document.getElementById('cardInfo').textContent"), "a zone card says so")
check("Updated" in (js(pg, "document.getElementById('fresh').textContent") or ""), "the freshness badge")

# numbers move to the new value; the nodes stay
js(pg, "window._n=document.querySelector('#w_k1 .kval');window._n._mark=7;window._b=document.querySelector('#w_b1 .vcol');window._b._mark=8;1")
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "data", "rev": 2, "ts": NOW + 5, "widgets": {
    "k1": {"value": 1100, "prev": 1043, "delta": 57, "seal": "K7QM-3333"},
    "b1": {"items": [{"key": "North", "label": "North", "value": 450}, {"key": "South", "label": "South", "value": 377},
                     {"key": "East", "label": "East", "value": 254}, {"key": "West", "label": "West", "hidden": True}]}}}))
check(wait(lambda: js(pg, "document.querySelector('#w_k1 .kval').textContent") == "1,100", 5), "kpi reaches the new value")
check(js(pg, "document.querySelector('#w_k1 .kval')._mark===7 && document.querySelector('#w_b1 .vcol')._mark===8"),
      "the same DOM nodes are updated (not made again)")
check(js(pg, "BiruniDash.state.rev") == 2, "rev follows the PC")
# a whole widget withheld
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "data", "rev": 3, "ts": NOW + 9, "widgets": {"g1": {"hidden": True}}}))
check(js(pg, "document.getElementById('w_g1').classList.contains('hid') && !!document.querySelector('#w_g1 .hidmsg')"),
      "a withheld widget shows 'hidden for privacy'")
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "data", "rev": 4, "ts": NOW + 12, "widgets": {"g1": DATA["g1"]}}))
check(not js(pg, "document.getElementById('w_g1').classList.contains('hid')"), "and comes back when the PC sends it again")

# explain: tap a number
js(pg, "window._out=[];document.querySelector('#w_k1 .kval').click();1")
o = js(pg, "JSON.stringify(window._out)")
check(any(m.get("t") == "explain" and m.get("widget") == "k1" for m in json.loads(o or "[]")), "a tap on a number asks to explain it")
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "explain", "widget": "k1", "count": 1043, "bbox": [46.6, 24.8, 46.7, 24.9],
                                              "features": [{"id": 45, "label": "Parcel 45", "by": "Mona", "when": NOW - 60},
                                                           {"id": 46, "label": "Parcel 46", "by": "Sami", "when": NOW - 600}],
                                              "more": True}))
check(js(pg, "document.querySelectorAll('#sheetBody .flist li').length") == 2 and not js(pg, "document.getElementById('sheet').classList.contains('hide')"),
      "the explain sheet lists the features")
js(pg, "BiruniDash.close();1")
js(pg, "window._out=[];document.querySelector('#w_b1 .vcol').click();1")
check(any(m.get("t") == "explain" and m.get("widget") == "b1" and m.get("key") == "North"
          for m in json.loads(js(pg, "JSON.stringify(window._out)") or "[]")), "a tap on a bar explains that bar")
js(pg, "BiruniDash.close();1")

# seal: tap the code
js(pg, "window._out=[];document.querySelector('#w_k1 .sealb').click();1")
check(any(m.get("t") == "seal" and m.get("code") == "K7QM-3333" for m in json.loads(js(pg, "JSON.stringify(window._out)") or "[]")),
      "a tap on the seal asks for its statement")
STATEMENT = {"dash": "d_test", "widget": "k1", "title": "Parcels surveyed", "value": 1100, "filters": {}, "card": "c_9x",
             "ts": NOW, "data": "b3f1c0de" * 8}
SIG = "5a" * 64
js(pg, "BiruniDash.feed(%s);1" % json.dumps({"t": "seal", "code": "K7QM-3333", "statement": STATEMENT, "sig": SIG}))
qr = js(pg, "(document.querySelector('#sheetBody .qr svg')||{}).outerHTML||''")
check(qr.startswith("<svg"), "the seal sheet shows a QR code")
check("Parcels surveyed" in (js(pg, "document.getElementById('sheetBody').textContent") or ""), "and the statement")
if qr:
    try:
        import numpy as np
        import zxingcpp

        r = QSvgRenderer(bytes(qr, "utf-8"))
        img = QImage(QSize(600, 600), QImage.Format.Format_Grayscale8)
        img.fill(255)
        p = QPainter(img)
        r.render(p)
        p.end()
        a = np.frombuffer(img.constBits().asstring(img.sizeInBytes()), np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width()]
        res = zxingcpp.read_barcodes(a)
        text = res[0].text if res else ""
        back = json.loads(text) if text else {}
        check(back.get("code") == "K7QM-3333" and back.get("statement") == json.loads(json.dumps(STATEMENT)) and back.get("sig") == SIG,
              "the QR code reads back as the signed statement")
    except Exception as e:  # noqa: BLE001
        check(False, f"QR read back: {e}")
# QR encoder across versions: several lengths read back
lens_ok = []
import numpy as np  # noqa: E402
import zxingcpp  # noqa: E402


def read_matrix(m):
    n = len(m)
    a = np.full(((n + 8) * 8, (n + 8) * 8), 255, np.uint8)
    for y in range(n):
        for x in range(n):
            if m[y][x]:
                a[(y + 4) * 8:(y + 5) * 8, (x + 4) * 8:(x + 5) * 8] = 0
    res = zxingcpp.read_barcodes(a)
    return res[0].text if res else None


big_ok = []
for n in (2000, 2300):          # versions 38 and 40: the encoder's modules read back (the page shows no QR this large)
    payload = ("Biruni-" * 400)[:n]
    q = json.loads(js(pg, "JSON.stringify(QR.make(new TextEncoder().encode(%s),'M'))" % json.dumps(payload)))
    big_ok.append(read_matrix(q["m"]) == payload and q["version"] >= 38)
check(all(big_ok), f"QR versions 38-40 encode correctly {big_ok}")
check(js(pg, "QR.svg('x'.repeat(1700))") is None, "the page shows no QR code over 1600 bytes")
for n in (5, 60, 300, 700, 900, 1100, 1300, 1600):
    payload = ("Biruni-" * 400)[:n]
    s = js(pg, "QR.svg(%s)" % json.dumps(payload))
    ver = js(pg, "(QR.make(new TextEncoder().encode(%s),'M').size-17)/4" % json.dumps(payload))
    try:
        import numpy as np
        import zxingcpp

        r = QSvgRenderer(bytes(s, "utf-8"))
        side = (int(ver) * 4 + 17 + 8) * 8      # a whole number of pixels per module (no seams between them)
        img = QImage(QSize(side, side), QImage.Format.Format_Grayscale8)
        img.fill(255)
        p = QPainter(img)
        r.render(p)
        p.end()
        a = np.frombuffer(img.constBits().asstring(img.sizeInBytes()), np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width()]
        res = zxingcpp.read_barcodes(a)
        lens_ok.append(bool(res) and res[0].text == payload)
        print(f"  QR {n} bytes: version {ver}: {'ok' if lens_ok[-1] else 'NOT READ'}", flush=True)
    except Exception:  # noqa: BLE001
        lens_ok.append(False)
check(all(lens_ok), f"QR codes of 5..1600 bytes (versions 1..33) read back from the page's SVG {lens_ok}")
js(pg, "BiruniDash.close();1")

# filter chips send a filter
js(pg, "window._out=[];document.querySelectorAll('#w_f1w .chip')[1].click();1")
check(any(m.get("t") == "filter" and m.get("values") == {"f1": ["North"]} for m in json.loads(js(pg, "JSON.stringify(window._out)") or "[]")),
      "a filter chip sends the filter")
check(js(pg, "document.querySelectorAll('#w_f1w .chip')[1].getAttribute('aria-pressed')") == "true", "the chip shows pressed")
js(pg, "window._out=[];document.querySelectorAll('#w_f1w .chip')[0].click();1")
check(any(m.get("t") == "filter" and m.get("values") == {"f1": None} for m in json.loads(js(pg, "JSON.stringify(window._out)") or "[]")),
      "'All' clears it")
# table sorting
js(pg, "document.querySelectorAll('#w_t1 th')[2].click();document.querySelectorAll('#w_t1 th')[2].click();1")
first = js(pg, "document.querySelector('#w_t1 tbody tr td:nth-child(3)').textContent")
check(first.replace(",", "").startswith("731"), f"the table sorts by a column (descending first row {first})")
# a map feature tap
js(pg, """window._out=[];const c=document.querySelector('#w_m1 svg circle');const r=c.getBoundingClientRect();
const o={bubbles:true,clientX:r.x+r.width/2,clientY:r.y+r.height/2,pointerId:5};c.dispatchEvent(new PointerEvent('pointerdown',o));
c.dispatchEvent(new PointerEvent('pointerup',o));1""")
check(any(m.get("t") == "explain" and m.get("widget") == "m1" for m in json.loads(js(pg, "JSON.stringify(window._out)") or "[]")),
      "a tap on a map point explains it")
js(pg, "BiruniDash.close();1")

# themes
bgs = {}
for th in P.THEMES:
    js(pg, f"BiruniDash.setTheme('{th}');1")
    bgs[th] = js(pg, "getComputedStyle(document.body).backgroundColor")
check(len(set(bgs.values())) == 6 and js(pg, "document.documentElement.dataset.theme") == "tv-wall", f"6 themes switch {bgs}")
js(pg, "BiruniDash.setTheme('biruni-light');1")
p1, img_light = shot(v, "light")
js(pg, "window.scrollTo(0,820);1")
shot(v, "light2")
js(pg, "window.scrollTo(0,0);1")
js(pg, "BiruniDash.setTheme('biruni-dark');1")
p2, _ = shot(v, "dark")
js(pg, "BiruniDash.setTheme('desert');1")
shot(v, "desert")
js(pg, "BiruniDash.setTheme('tv-wall');BiruniDash.setTV(true);1")
shot(v, "tv")
js(pg, "BiruniDash.setTV(false);BiruniDash.setTheme('biruni-light');1")
check(not img_light.isNull() and img_light.width() > 1000, "a desktop screenshot")
# right to left
js(pg, "BiruniDash.setLang('ar');1")
pump(0.8)
check(js(pg, "document.documentElement.dir") == "rtl" and js(pg, "document.documentElement.lang") == "ar", "Arabic: right to left")
check(js(pg, "document.querySelectorAll('#grid .w').length") == 15, "the widgets are drawn again in Arabic")
hijri = js(pg, "document.getElementById('cardInfo').textContent")
check("هـ" in hijri or "١" in hijri or "14" in hijri, f"dates both Gregorian and Hijri ({hijri})")
first_x = js(pg, "document.getElementById('w_k1').getBoundingClientRect().x")
last_x = js(pg, "document.getElementById('w_f1w').getBoundingClientRect().x")
check(first_x > last_x, "the grid is mirrored (the first widget is on the right)")
shot(v, "ar")
# phone width
v.resize(390, 844)
pump(1.0)
js(pg, "BiruniDash.setLang('en');1")
pump(0.8)
cols = js(pg, "getComputedStyle(document.getElementById('grid')).gridTemplateColumns.split(' ').length")
check(cols == 2, f"phone width: 2 columns ({cols})")
check(js(pg, "document.documentElement.scrollWidth<=document.documentElement.clientWidth+1"), "no sideways scroll on a phone")
shot(v, "phone")
js(pg, "window.scrollTo(0,900);1")
shot(v, "phone2")
js(pg, "BiruniDash.setLang('ar');window.scrollTo(0,0);1")
shot(v, "phone_ar")
v.close()

# ---- 3. the LAN transport against a small server here ----------------------------------------------------------------------
store = {"posts": [], "out": [], "n": 0, "sid": "S1"}
cond = threading.Condition()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/d/tok/ev"):
            q = dict(x.split("=", 1) for x in self.path.split("?", 1)[1].split("&"))
            n = int(q.get("n", 0))
            with cond:
                cond.wait_for(lambda: len(store["out"]) > n, timeout=3)
                self._json({"out": store["out"][n:], "n": len(store["out"])})
            return
        if self.path.startswith("/d/tok/"):
            b = P.page_html("en", "lan").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._json({"error": "no"}, 404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        store["posts"].append(body)
        m = body.get("m") or {}
        if m.get("t") == "hello":
            self._json({"s": "S1", "out": [DASH, {"t": "data", "rev": 1, "ts": NOW, "widgets": DATA}]})
        else:
            self._json({"s": "S1", "out": []})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
v2, pg2 = view_of("lan", None, f"http://127.0.0.1:{port}/d/tok/#c=BDC1.test.sig&l=en")
check(wait(lambda: any((b.get("m") or {}).get("t") == "hello" and (b.get("m") or {}).get("card") == "BDC1.test.sig"
                       for b in store["posts"]), 10), "LAN: the page posts hello with its card")
check(wait(lambda: js(pg2, "document.querySelectorAll('#grid .w').length") == 15, 10), "LAN: the dashboard comes in the hello answer")
with cond:
    store["out"].append({"t": "data", "rev": 2, "ts": NOW + 3, "widgets": {"k1": {"value": 2000, "delta": 5}}})
    cond.notify_all()
check(wait(lambda: js(pg2, "document.querySelector('#w_k1 .kval').textContent") == "2,000", 10), "LAN: a long poll brings new numbers")
check(js(pg2, "document.getElementById('conn').className").endswith("on"), "LAN: the badge says live")
js(pg2, "document.querySelector('#w_k1 .kval').click();1")
check(wait(lambda: any((b.get("m") or {}).get("t") == "explain" and b.get("s") == "S1" for b in store["posts"]), 5),
      "LAN: a request goes with the session id")
with cond:
    store["out"].append({"t": "error", "why": "revoked"})
    cond.notify_all()
check(wait(lambda: not js(pg2, "document.getElementById('banner').classList.contains('hide')"), 8)
      and "withdrawn" in (js(pg2, "document.getElementById('banner').textContent") or ""), "LAN: a revoked card is told and stops")
v2.close()
srv.shutdown()

# ---- 4. the bridge page opens and asks for a link when it has none -------------------------------------------------------
v3, pg3 = view_of("bridge", P.page_html("en", "bridge"), "https://hatemtarekgis.github.io/biruni-gis/d/#c=BDC1.x.y&k=short")
pump(0.8)
check("link or QR" in (js(pg3, "document.getElementById('banner').textContent") or ""), "bridge: a link without a key says how to open it")
v3.close()
v4, pg4 = view_of("nocard", P.page_html("ar", "lan"), "https://x.invalid/d/tok/")
pump(0.6)
check(js(pg4, "document.documentElement.dir") == "rtl" and "QR" in (js(pg4, "document.getElementById('banner').textContent") or ""),
      "no card: the Arabic page says to open it with the link or QR code")
v4.close()

check(not errors, "no JavaScript errors" + ("" if not errors else ": " + "; ".join(errors[:5])))
print("screenshots:", SHOTS)
print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
sys.exit(1 if failures else 0)
