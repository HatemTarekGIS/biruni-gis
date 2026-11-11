"""Dashboard extension "show" (page side), offscreen in QtWebEngine: the «since your last visit» card and its voice
button, the forecast widget (burn-up chart with its cone, the map coloured by finish week), the project film
(player, pending parcels, frames applied in order, closing restores), the meeting mode (following the presenter's
view, the laser dot, the presenter's page reporting its view) and the phone wall (wall mode, calibration, a shared
view shown at this phone's offset). Screenshots go to the scratchpad (show_ext_*.png).

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_show_page.py   (test_dashboard_show.py runs it too)
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

SHOTS = os.environ.get("BIRUNI_DASH_SHOTS",
                       r"C:\Users\HATEMT~1\AppData\Local\Temp\claude\8aa85b7d38969e04\12cf1281-d8dc-4630-b1a0-f86abf89dcd2\scratchpad")
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


def sq(x, y, s):
    return {"type": "Polygon", "coordinates": [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]}


DIST = ["north", "south", "east", "west"]
COL = {"done": "#2e7d32", "open": "#9e9e9e"}
parcels = []
for i in range(64):
    d = DIST[i // 16]
    st = "done" if i % 3 else "open"
    x, y = 46.70 + (i % 8) * 0.0011, 24.70 + (i // 8) * 0.0011
    parcels.append({"type": "Feature", "id": i + 1, "geometry": sq(x, y, 0.001),
                    "properties": {"id": i + 1, "_c": COL[st], "_l": f"P-{i + 1}", "_k": {"district": d}}})
NOW = time.time()
DAY = 86400
DEF = {"v": 1, "id": "d_s", "title": "مشروع تجريبي", "theme": "biruni-dark", "columns": 12, "dates": "both", "filters": [],
       "widgets": [{"id": "m1", "type": "map", "title": "الخريطة", "x": 0, "y": 0, "w": 8, "h": 8},
                   {"id": "k1", "type": "kpi", "title": "القطع المنجزة", "x": 8, "y": 0, "w": 4, "h": 2},
                   {"id": "pr", "type": "progress", "title": "حسب الحي", "x": 8, "y": 2, "w": 4, "h": 3},
                   {"id": "fc", "type": "text", "title": "موعد الانتهاء", "x": 8, "y": 5, "w": 4, "h": 5,
                    "style": {"show": "forecast", "of": "pr"}}]}
MAP = {"total": 64, "bbox": [46.6995, 24.6995, 46.7095, 24.7095], "layers": [
    {"id": "L0", "name": "Parcels", "kind": "fill", "style": {"opacity": 0.8}, "label": True,
     "legend": [{"label": "done", "color": COL["done"], "key": "done"}, {"label": "open", "color": COL["open"], "key": "open"}],
     "popup": [], "keys": ["district"], "geojson": {"type": "FeatureCollection", "features": parcels}}]}
PROG = {"xf": "district", "items": [{"key": d, "label": d.title(), "done": 8 + i * 2, "total": 16} for i, d in enumerate(DIST)]}
DATA = {"m1": MAP, "k1": {"value": 43, "prev": 40, "delta": 3}, "pr": PROG, "fc": {"text": ""}}
DASH = {"t": "dash", "def": DEF, "card": {"name": "Test", "exp": NOW + DAY, "zone": False}, "pc": "PC", "pub": "ab" * 32}


def view(size, lang="ar"):
    v = QWebEngineView()
    pg = Page(QWebEngineProfile.defaultProfile())
    v.setPage(pg)
    v.resize(*size)
    v.show()
    loaded = {}
    pg.loadFinished.connect(lambda ok: loaded.update(ok=ok))
    pg.setHtml(P.page_html(lang, "preview"), QUrl("https://dash.invalid/d/"))
    wait(lambda: "ok" in loaded, 20)
    wait(lambda: js(pg, "!!window.BiruniDash&&!!window.BiruniShow") is True, 10)
    js(pg, "window._out=[];BiruniDash.onSend=m=>window._out.push(m);BiruniDash.setTheme('biruni-dark');true")
    js(pg, f"BiruniDash.feed({json.dumps(DASH)});BiruniDash.feed({json.dumps({'t': 'data', 'rev': 1, 'widgets': DATA})});true")
    pump(1.0)
    return v, pg


def sent(pg, t):
    return js(pg, f"JSON.stringify(window._out.filter(m=>m.t==='{t}'))")


def shot(v, name):
    pump(0.5)
    v.grab().save(os.path.join(SHOTS, f"show_ext_{name}.png"))


v, pg = view((1400, 900))
check(js(pg, "!!window.BiruniShow") is True, "the extension's code runs in the page")
check(json.loads(sent(pg, "show_story")) and json.loads(sent(pg, "show_story"))[0]["lang"] == "ar",
      "on opening the page asks for the story in its language")
check(json.loads(sent(pg, "show_forecast")) == [{"t": "show_forecast", "widget": "fc"}], "it asks for the forecast widget")
check(json.loads(sent(pg, "show_meet_hello")), "it asks whether a meeting is running")
check(js(pg, "!!document.getElementById('show_meetBtn')&&!!document.getElementById('show_wallBtn')"),
      "header buttons for the meeting and the wall")

# ---- 1. the story -----------------------------------------------------------------------------------------------------
LINES = ["منذ زيارتك الأخيرة قبل يومين:", "ارتفع «القطع المنجزة» بمقدار 37 ليبلغ 137.",
         "أكثر المناطق تقدّمًا: حي الريحان، إذ زاد المُنجَز فيها بمقدار 12.", "اكتمل العمل في حي السدرة."]
js(pg, f"BiruniDash.feed({json.dumps({'t': 'show_story', 'lines': LINES, 'lang': 'ar', 'first': False}, ensure_ascii=False)});true")
pump(0.4)
check(js(pg, "document.querySelectorAll('#show_story li').length") == 3 and
      js(pg, "document.querySelector('#show_story h3 span').textContent") == "منذ زيارتك الأخيرة قبل يومين" and
      js(pg, "document.getElementById('show_story').nextElementSibling.id") == "grid",
      "the story card sits above the grid with its sentences")
check(js(pg, "document.querySelectorAll('#show_story .acts button').length") == 2, "Listen and Close buttons")
js(pg, "document.querySelector('#show_story .acts button').click();true")
pump(0.3)
check(not errors, "the Listen button works or says there is no voice, without errors")

# ---- 2. the forecast ----------------------------------------------------------------------------------------------------
series = [[NOW - (10 - d) * DAY, 20 + d * 3] for d in range(11)]
cone = [[NOW + i * DAY, 50 + i * 2, 50 + i * 3, 50 + i * 4] for i in range(5)]
FC = {"t": "show_forecast", "widget": "fc", "of": "pr", "xf": "district",
      "zones": [{"key": d, "label": d.title(), "done": 8 + i * 2, "total": 16, "rate": 0.8, "eta": NOW + (i + 1) * 9 * DAY,
                 "lo": NOW + (i + 1) * 6 * DAY, "hi": NOW + (i + 1) * 12 * DAY, "finished": i == 3}
                for i, d in enumerate(DIST)],
      "overall": {"done": 50, "total": 64, "rate": 3, "eta": NOW + 5 * DAY, "lo": NOW + 3 * DAY, "hi": NOW + 8 * DAY,
                  "series": series, "cone": cone}}
js(pg, f"BiruniDash.feed({json.dumps(FC)});true")
pump(0.4)
check(js(pg, "document.querySelectorAll('#w_fc .show-fcsvg polygon.cone').length") == 1 and
      js(pg, "document.querySelectorAll('#w_fc .show-fcz .d').length") == 4, "the forecast: burn-up with its cone, a date per zone")
check(js(pg, "getComputedStyle(document.querySelector('#w_fc .body')).display") == "none",
      "the text widget's own body is hidden under the forecast")
js(pg, "document.querySelector('#w_fc .show-fcbox .show-btn').click();true")
pump(0.3)
painted = js(pg, "[...document.querySelectorAll('#w_m1 path.f')].filter(n=>n.style.fill).length")
check(painted == 64 and js(pg, "!!document.querySelector('#w_m1 .show-fleg')"), "the map coloured by expected finish week, with a legend")
check(js(pg, "document.querySelector('#w_m1 [data-f=\"60\"]').style.fill") ==
      js(pg, "document.querySelector('#w_m1 [data-f=\"61\"]').style.fill"), "parcels of one district share their week's colour")
shot(v, "forecast")
js(pg, "document.querySelector('#w_fc .show-fcbox .show-btn').click();true")
pump(0.2)
check(js(pg, "[...document.querySelectorAll('#w_m1 path.f')].filter(n=>n.style.fill).length") == 0,
      "back to the map's own colours")

# ---- 3. the project film --------------------------------------------------------------------------------------------------
js(pg, "window._out=[];document.querySelector('#w_m1 .show-filmb').click();true")
check(json.loads(sent(pg, "show_film")) == [{"t": "show_film", "widget": "m1"}], "the film button asks the PC for the film")
frames = []
for d in range(8):
    frames.append({"day": f"2026-10-{d + 1:02d}", "t": NOW - (8 - d) * DAY,
                   "ch": [["L0", str(i + 1), COL["done"]] for i in range(d * 8, d * 8 + 8)]})
js(pg, f"BiruniDash.feed({json.dumps({'t': 'show_film', 'widget': 'm1', 'frames': frames, 'count': 64})});true")
pump(0.4)
check(js(pg, "!!document.querySelector('#w_m1 .show-player')&&!!document.querySelector('#w_m1 .show-date')"),
      "the player and the big date appear on the map")
pend = js(pg, "document.querySelectorAll('#w_m1 path.f.show-pending').length")
check(pend == 56, f"after the first day only its 8 parcels are coloured ({pend} still pending)")
js(pg, "(()=>{const r=document.querySelector('#w_m1 .show-player input[type=range]');r.value=4;r.dispatchEvent(new Event('input'));return true})()")
pump(0.3)
check(js(pg, "document.querySelectorAll('#w_m1 path.f.show-pending').length") == 24 and
      "40" in js(pg, "document.querySelector('#w_m1 .show-player span').textContent"),
      "seeking to day 5: 40 parcels done, 24 pending, the counter says 40")
js(pg, "(()=>{const r=document.querySelector('#w_m1 .show-player input[type=range]');r.value=1;r.dispatchEvent(new Event('input'));return true})()")
pump(0.2)
check(js(pg, "document.querySelectorAll('#w_m1 path.f.show-pending').length") == 48, "seeking back works")
shot(v, "film")
js(pg, "document.querySelector('#w_m1 .show-player button').click();true")
pump(2.2)
check(js(pg, "+document.querySelector('#w_m1 .show-player input[type=range]').value") >= 3, "Play advances day by day")
js(pg, "[...document.querySelectorAll('#w_m1 .show-player button')].pop().click();true")
pump(0.2)
check(js(pg, "document.querySelectorAll('#w_m1 path.f.show-pending').length") == 0 and
      not js(pg, "!!document.querySelector('#w_m1 .show-player')"), "closing the film gives the map back")
rec = js(pg, "typeof MediaRecorder!=='undefined'&&!!HTMLCanvasElement.prototype.captureStream")
print(f"   (MediaRecorder available in this browser: {rec})")

# ---- 4. the meeting: following and the laser ----------------------------------------------------------------------------
js(pg, "BiruniDash.feed({t:'show_meet_state',active:true,presenter:'م. منى',you:false});true")
pump(0.2)
check(js(pg, "!!document.getElementById('show_pill')"), "a pill says whom the page follows")
before = js(pg, "JSON.stringify(BiruniShow&&(()=>{const c=BiruniDash.widgets().m1;return c.view})())")
target = js(pg, "(()=>{const c=BiruniDash.widgets().m1,r=c.svg.getBoundingClientRect();"
                "return {cx:c.org[0]+c.view.x+c.view.w/2+40,cy:c.org[1]-(c.view.y+c.view.h/2)-30,mpp:c.view.w/r.width/2}})()")
js(pg, f"BiruniDash.feed({json.dumps({'t': 'show_meet', 'presenter': 'م. منى', 'view': dict(target, wid='m1', xf=['district', 'east'])}, ensure_ascii=False)});true")
pump(0.5)
after = js(pg, "(()=>{const c=BiruniDash.widgets().m1,r=c.svg.getBoundingClientRect();"
               "return {cx:c.org[0]+c.view.x+c.view.w/2,cy:c.org[1]-(c.view.y+c.view.h/2),mpp:c.view.w/r.width}})()")
check(after and abs(after["cx"] - target["cx"]) < 1 and abs(after["mpp"] - target["mpp"]) < 1e-6,
      f"the follower's map moves to the presenter's view (zoomed in, shifted)")
check(js(pg, "BiruniDash.state.xf&&BiruniDash.state.xf.key") == "east", "and selects what the presenter selected")
js(pg, "BiruniDash.feed({t:'show_meet_laser',wid:'m1',lon:46.704,lat:24.704});true")
pump(0.2)
check(js(pg, "!!document.querySelector('#w_m1 .show-laser')"), "the laser dot shows on the follower's map")
shot(v, "meeting")
js(pg, "document.querySelector('#show_pill button').click();true")
pump(0.2)
js(pg, f"BiruniDash.feed({json.dumps({'t': 'show_meet', 'presenter': 'x', 'view': dict(target, cx=target['cx'] + 500, wid='m1')})});true")
pump(0.4)
again = js(pg, "(()=>{const c=BiruniDash.widgets().m1;return c.org[0]+c.view.x+c.view.w/2})()")
check(abs(again - target["cx"]) < 1, "after «stop following» the page keeps its own view")
# the presenter's side: its page reports its view
js(pg, "window._out=[];BiruniDash.feed({t:'show_meet_state',active:true,presenter:'Test',you:true});true")
pump(0.5)
check(json.loads(sent(pg, "show_meet_view")), "the presenter's page sends its view")
js(pg, "BiruniDash.feed({t:'show_meet_state',active:false});true")
pump(0.2)
v.close()

# ---- 5. the phone wall ------------------------------------------------------------------------------------------------------
w, pw = view((400, 820))
js(pw, f"BiruniDash.feed({json.dumps({'t': 'show_wall', 'wall': 'w1', 'slot': 1, 'rows': 1, 'cols': 3, 'tile': [416, 0, 400, 820], 'virtual': [1232, 820], 'n': 2, 'view': None})});true")
pump(0.5)
check(js(pw, "document.body.classList.contains('show-wallmode')") and js(pw, "!!document.getElementById('show_cal')"),
      "joining the wall: wall mode and the calibration screen (screen 2 of 3)")
shot(w, "wall_calibrate")
check(js(pw, "document.querySelectorAll('#show_cal .row button').length") == 2, "it can move left or right")
js(pw, "[...document.querySelectorAll('#show_cal .show-btn')].pop().click();true")
pump(0.6)
check(not js(pw, "!!document.getElementById('show_cal')") and
      js(pw, "document.querySelector('#w_m1').getBoundingClientRect().width") == 400,
      "Start: the map fills the phone")
shared = js(pw, "(()=>{const c=BiruniDash.widgets().m1,r=c.svg.getBoundingClientRect();"
                "return {cx:c.org[0]+c.view.x+c.view.w/2,cy:c.org[1]-(c.view.y+c.view.h/2),mpp:c.view.w/r.width}})()")
js(pw, f"window._out=[];BiruniDash.feed({json.dumps({'t': 'show_wall_view', 'view': shared})});true")
pump(0.5)
mine = js(pw, "(()=>{const c=BiruniDash.widgets().m1,r=c.svg.getBoundingClientRect();"
              "return {cx:c.org[0]+c.view.x+c.view.w/2,cy:c.org[1]-(c.view.y+c.view.h/2),mpp:c.view.w/r.width}})()")
check(abs(mine["cx"] - shared["cx"] - (416 + 200 - 616) * shared["mpp"]) < 1 and abs(mine["mpp"] - shared["mpp"]) < 1e-6,
      "the middle phone shows the wall's centre at the wall's scale")
js(pw, f"BiruniDash.feed({json.dumps({'t': 'show_wall', 'wall': 'w1', 'slot': 0, 'rows': 1, 'cols': 3, 'tile': [0, 0, 400, 820], 'virtual': [1232, 820], 'n': 2, 'view': shared})});true")
pump(0.5)
left = js(pw, "(()=>{const c=BiruniDash.widgets().m1;return c.org[0]+c.view.x+c.view.w/2})()")
check(abs(left - (shared["cx"] - 416 * shared["mpp"])) < 1, "moved to the left screen: the map shifts by one screen and the gap")
shot(w, "wall")
pump(0.5)
check(not json.loads(sent(pw, "show_wall_view")), "a view applied from the wall is not echoed back")
js(pw, "BiruniDash.feed({t:'show_wall_end'});true")
pump(0.3)
check(not js(pw, "document.body.classList.contains('show-wallmode')"), "leaving the wall restores the dashboard")
w.close()

check(not errors, f"no JavaScript errors ({errors[:3]})")
print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
sys.exit(1 if failures else 0)
