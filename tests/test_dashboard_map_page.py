"""The dashboard page's map (map v2), offscreen in QtWebEngine: several styled layers drawn, the legend and its
layer switches, labels that never overlap, a scale bar, the basemap asked for and placed under the layers,
a tap that opens a popup and asks "what is behind it", the cross-filter between a bar chart and the map,
and a parcel that changes colour with a pulse.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_map_page.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
from biruni.dashboard import page as P  # noqa: E402

failures, errors = [], []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(sec):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


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


# ---- data: 4 districts of 16 parcels, 2 roads, 3 points -------------------------------------------------------------
def sq(x, y, s):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]}


COL = {"done": "#2e7d32", "open": "#c62828"}
parcels = []
for i in range(64):
    d = ["north", "south", "east", "west"][i // 16]
    st = "done" if i % 3 else "open"
    x, y = 46.70 + (i % 8) * 0.0011, 24.70 + (i // 8) * 0.0011
    parcels.append({"type": "Feature", "id": i + 1, "geometry": sq(x, y, 0.001),
                    "properties": {"id": i + 1, "_c": COL[st], "_l": f"P-{i + 1}", "_k": {"district": d, "status": st},
                                   "status": st, "area": 500 + i}})
roads = [{"type": "Feature", "id": "r1", "geometry": {"type": "LineString", "coordinates": [[46.6995, 24.7044],
                                                                                           [46.7095, 24.7044]]},
          "properties": {"id": "r1", "_l": "Main road", "_c": "#ffd54f"}}]
pts = [{"type": "Feature", "id": f"p{i}", "geometry": {"type": "Point", "coordinates": [46.701 + i * 0.002, 24.703]},
        "properties": {"id": f"p{i}", "_c": "#6d4c41", "_l": f"Mark {i}"}} for i in range(3)]
DEF = {"v": 1, "id": "d_m", "title": "Map", "theme": "biruni-light", "columns": 12, "dates": "greg", "filters": [],
       "widgets": [{"id": "m1", "type": "map", "title": "Parcels", "x": 0, "y": 0, "w": 8, "h": 8,
                    "options": {"basemap": "project"}},
                   {"id": "b1", "type": "bar", "title": "By district", "x": 8, "y": 0, "w": 4, "h": 4}]}
MAP = {"basemap": True, "total": 68, "bbox": [46.6995, 24.6995, 46.7095, 24.7095], "layers": [
    {"id": "L0", "name": "Parcels", "kind": "fill", "style": {"opacity": 0.7}, "label": True,
     "legend": [{"label": "done", "color": COL["done"]}, {"label": "open", "color": COL["open"]}],
     "popup": [{"key": "status", "label": "Status"}, {"key": "area", "label": "Area"}],
     "geojson": {"type": "FeatureCollection", "features": parcels}},
    {"id": "L1", "name": "Roads", "kind": "line", "style": {"color": "#ffd54f", "width": 5}, "label": True,
     "legend": [{"label": "Road", "color": "#ffd54f"}], "popup": [], "geojson": {"type": "FeatureCollection",
                                                                                  "features": roads}},
    {"id": "L2", "name": "Marks", "kind": "point", "style": {"size": 7}, "label": True, "legend": [], "popup": [],
     "geojson": {"type": "FeatureCollection", "features": pts}}]}
BAR = {"xf": "district", "items": [{"key": k, "label": k.title(), "value": 16} for k in ("north", "south", "east",
                                                                                          "west")]}
DASH = {"t": "dash", "def": DEF, "card": {"name": "Test", "exp": time.time() + 86400, "zone": False}, "pc": "PC",
        "pub": "ab" * 32}

v = QWebEngineView()
pg = Page(QWebEngineProfile.defaultProfile())
v.setPage(pg)
v.resize(1400, 900)
v.show()
loaded = {}
pg.loadFinished.connect(lambda ok: loaded.update(ok=ok))
pg.setHtml(P.page_html("en", "preview"), QUrl("https://dash.invalid/d/"))
wait(lambda: "ok" in loaded, 20)
check(wait(lambda: js(pg, "!!window.BiruniDash") is True, 10), "the page is up")
js(pg, "window._out=[];BiruniDash.onSend=m=>window._out.push(m);true")
js(pg, f"BiruniDash.feed({json.dumps(DASH)});BiruniDash.feed({json.dumps({'t': 'data', 'rev': 1, 'widgets': {'m1': MAP, 'b1': BAR}})});true")
pump(1.2)
check(js(pg, "document.querySelectorAll('#w_m1 [data-l=L0] path.f').length") == 64 and
      js(pg, "document.querySelectorAll('#w_m1 [data-l=L1] path.f').length") == 1 and
      js(pg, "document.querySelectorAll('#w_m1 [data-l=L2] circle.f').length") == 3,
      "three layers drawn: 64 parcels, a road (with its casing), 3 points")
check(js(pg, "document.querySelector('#w_m1 [data-f=\"2\"]').getAttribute('fill')") == COL["done"],
      "each feature has the colour the PC gave it")
order = js(pg, "[...document.querySelectorAll('#w_m1 .gl > g')].map(g=>g.dataset.l).join(',')")
check(order == "L0,L1,L2", f"fills under lines under points ({order})")
check(js(pg, "document.querySelectorAll('#w_m1 .gleg .ly').length") == 3 and
      js(pg, "document.querySelectorAll('#w_m1 .gleg .it').length") == 3, "the legend: the layers and their classes")
# labels: some shown, none overlapping
lab = json.loads(js(pg, """JSON.stringify([...document.querySelectorAll('#w_m1 .lab text')].map(t=>{const r=t.getBoundingClientRect();return [r.left,r.top,r.right,r.bottom]}))""") or "[]")


def overlap(a, b):
    return not (a[2] <= b[0] + 0.5 or b[2] <= a[0] + 0.5 or a[3] <= b[1] + 0.5 or b[3] <= a[1] + 0.5)


check(len(lab) >= 3 and not any(overlap(a, b) for i, a in enumerate(lab) for b in lab[i + 1:]),
      f"labels are drawn and never overlap ({len(lab)})")
check(bool(js(pg, "document.querySelector('#w_m1 .mscale span').textContent")) and
      js(pg, "!!document.querySelector('#w_m1 svg.mnorth')"), "a scale bar and a north arrow")
# the basemap
check(wait(lambda: any(m.get("t") == "basemap" for m in json.loads(js(pg, "JSON.stringify(window._out)") or "[]")), 3),
      "the page asks the PC for the basemap of its view")
bm = next(m for m in json.loads(js(pg, "JSON.stringify(window._out)")) if m.get("t") == "basemap")
check(bm["widget"] == "m1" and len(bm["bbox"]) == 4 and bm["w"] > 100 and bm["h"] > 100, "... with its bbox and size")
PIX = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
js(pg, f"BiruniDash.feed({json.dumps({'t': 'basemap', 'widget': 'm1', 'bbox': bm['bbox'], 'img': PIX})});true")
pump(0.6)
check(js(pg, "document.querySelectorAll('#w_m1 image.bm').length") == 1 and
      js(pg, "document.querySelector('#w_m1 .bmg').nextElementSibling.classList.contains('gl')"),
      "the basemap image is placed under the layers")
# a layer switched off in the legend
js(pg, "document.querySelectorAll('#w_m1 .gleg .lyh input')[1].click();true")
check(js(pg, "document.querySelector('#w_m1 [data-l=L1]').style.display") == "none", "the legend switches a layer off")
js(pg, "document.querySelectorAll('#w_m1 .gleg .lyh input')[1].click();true")
# cross-filter: a bar selects its parcels on the map
js(pg, "document.querySelector('#w_b1 [data-k=south]').click();true")
pump(0.3)
check(js(pg, "document.querySelectorAll('#w_m1 [data-l=L0] .xoff').length") == 48 and
      js(pg, "document.querySelectorAll('#w_m1 [data-l=L0] .xon').length") == 16 and
      js(pg, "document.getElementById('xpill').classList.contains('on')"),
      "a bar tapped: its 16 parcels light up on the map, the others dim, the selection shows at the top")
check(not any(m.get("t") == "explain" for m in json.loads(js(pg, "JSON.stringify(window._out)"))),
      "the first tap selects (no question to the PC)")
js(pg, "document.querySelector('#w_b1 [data-k=south]').click();true")
check(any(m.get("t") == "explain" and m.get("widget") == "b1" for m in json.loads(js(pg, "JSON.stringify(window._out)"))),
      "a second tap on the selected bar asks what is behind it")
js(pg, "BiruniDash.close();document.getElementById('xpill').click();true")
check(js(pg, "document.querySelectorAll('#w_m1 .xoff').length") == 0, "the selection is cleared from the top")
# a tap on a parcel: popup, highlight, selection of its district
js(pg, "window._out=[];true")
tap = """(function(){const n=document.querySelector('#w_m1 [data-f="20"]');const r=n.getBoundingClientRect();const c=document.querySelector('#w_m1 .map svg');
const o={clientX:r.left+r.width/2,clientY:r.top+r.height/2,bubbles:true,pointerId:1};n.dispatchEvent(new PointerEvent('pointerdown',o));c.dispatchEvent(new PointerEvent('pointerup',o));return true})()"""
js(pg, tap)
pump(0.3)
check(not js(pg, "document.querySelector('#w_m1 .mpop').classList.contains('hide')") and
      "Status" in (js(pg, "document.querySelector('#w_m1 .mpop').textContent") or "") and
      js(pg, "document.querySelectorAll('#w_m1 .hl path').length") == 1,
      "a tap on a parcel: its popup with the chosen fields, and a highlight")
check(js(pg, "BiruniDash.state.xf&&BiruniDash.state.xf.field") == "district" and
      js(pg, "document.querySelector('#w_b1 [data-k=south]').classList.contains('xsel')"),
      "... and its district is selected in the bar chart")
js(pg, "[...document.querySelectorAll('#w_m1 .mpop .acts button')].pop().click();true")
check(any(m.get("t") == "explain" and m.get("key") == "L0:20" for m in json.loads(js(pg, "JSON.stringify(window._out)"))),
      "'What is behind it' asks the PC about that feature (L0:20)")
js(pg, "BiruniDash.close();true")
# a parcel changes colour: a pulse, the same node
js(pg, "window._n=document.querySelector('#w_m1 [data-f=\"1\"]');true")
MAP["layers"][0]["geojson"]["features"][0]["properties"]["_c"] = COL["done"]
js(pg, f"BiruniDash.feed({json.dumps({'t': 'data', 'rev': 2, 'widgets': {'m1': MAP}})});true")
pump(0.2)
check(js(pg, "window._n===document.querySelector('#w_m1 [data-f=\"1\"]')&&window._n.classList.contains('pulse')&&window._n.getAttribute('fill')==='#2e7d32'"),
      "a feature that changes colour keeps its node and pulses")
# zoom with the wheel: parcel numbers appear
before = js(pg, "document.querySelectorAll('#w_m1 .lab text').length")
js(pg, """(function(){const c=document.querySelector('#w_m1 .map svg');const r=c.getBoundingClientRect();
for(let i=0;i<6;i++)c.dispatchEvent(new WheelEvent('wheel',{deltaY:-100,clientX:r.left+r.width/2,clientY:r.top+r.height/2,bubbles:true,cancelable:true}));return true})()""")
pump(0.8)
after = js(pg, "document.querySelectorAll('#w_m1 .lab text').length")
check(after > before or after >= 10, f"zooming in shows more labels ({before} → {after})")
# a first-shape map still draws
OLD = {"geojson": {"type": "FeatureCollection", "features": [
    {"type": "Feature", "id": 1, "geometry": {"type": "Point", "coordinates": [46.7, 24.7]}, "properties": {"status": "a"}},
    {"type": "Feature", "id": 2, "geometry": {"type": "Point", "coordinates": [46.71, 24.71]}, "properties": {"status": "b"}}]},
    "bbox": [46.7, 24.7, 46.71, 24.71], "total": 2}
DEF2 = dict(DEF, widgets=[{"id": "m9", "type": "map", "title": "Old", "x": 0, "y": 0, "w": 6, "h": 5, "style": {"by": "status"}}])
js(pg, f"BiruniDash.feed({json.dumps(dict(DASH, **{'def': DEF2}))});BiruniDash.feed({json.dumps({'t': 'data', 'rev': 3, 'widgets': {'m9': OLD}})});true")
pump(0.6)
check(js(pg, "document.querySelectorAll('#w_m9 circle.f').length") == 2 and
      js(pg, "document.querySelectorAll('#w_m9 .gleg .it').length") == 2, "a map of the first shape still draws, with its legend " + str(js(pg, "[document.querySelectorAll('#w_m9 circle.f').length,document.querySelectorAll('#w_m9 .gleg .it').length,document.querySelectorAll('#w_m9 .gleg').length]")))
check(not errors, f"no JavaScript errors {errors}")

print()
print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
for f in failures:
    print("  FAIL", f)
sys.exit(1 if failures else 0)
