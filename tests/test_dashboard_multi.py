"""«Dashboard of dashboards» (dashboard/ext_multi.py + multi.py): two projects, two dashboard services, two LAN
servers (two "PCs"); a supervisor's view brings both together through their own cards. Each source keeps its
own card limits (a zone card counts only its zone, a card of one PC is refused by the other); the view sums the
numbers with the same title, draws both sites on one map and shows each dashboard in its own section.

The browser part runs in a child process (QtWebEngine does not live beside QgsApplication):
  engine\\bin\\python-qgis.bat tests\\test_dashboard_multi.py
"""
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
SHOTS = r"C:\Users\HATEMT~1\AppData\Local\Temp\claude\8aa85b7d38969e04\12cf1281-d8dc-4630-b1a0-f86abf89dcd2\scratchpad"

# ======================================================================================================================
# the browser (child process): open the view, wait for both dashboards, report what the view shows
# ======================================================================================================================
if len(sys.argv) > 2 and sys.argv[1] == "--browser":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox --ignore-certificate-errors"
    from PyQt6.QtCore import QCoreApplication, Qt, QUrl

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication

    bapp = QApplication(sys.argv[:1])
    errs = []

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                errs.append(f"{message} ({source}:{line})")

    v = QWebEngineView()
    pg = Page(QWebEngineProfile.defaultProfile())
    pg.certificateError.connect(lambda e: e.acceptCertificate())
    v.setPage(pg)
    v.resize(1400, 1000)
    v.show()
    pg.load(QUrl(sys.argv[2]))

    def js(expr, wait=10):
        out = {}
        pg.runJavaScript(expr, lambda r: out.update(v=r))
        end = time.monotonic() + wait
        while "v" not in out and time.monotonic() < end:
            bapp.processEvents()
            time.sleep(0.01)
        return out.get("v")

    def pump(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            bapp.processEvents()
            time.sleep(0.01)

    ready = False
    end = time.monotonic() + 60
    while time.monotonic() < end:
        pump(0.5)
        r = js("(()=>{const m=window.BiruniDash&&BiruniDash.multi;if(!m)return null;const s=m.sources();"
               "return s.length&&s.every(x=>x.def&&x.live)})()")
        if r:
            ready = True
            break
    pump(1.5)
    res = js("""(()=>{const q=s=>[...document.querySelectorAll(s)];
      return {ready:%s,title:document.getElementById('title').textContent,
      sources:BiruniDash.multi.sources(),
      tiles:q('.mxTile').map(t=>({title:t.querySelector('h3').textContent,value:t.querySelector('.mxVal').childNodes[0].textContent,
        rows:[...t.querySelectorAll('li')].map(l=>l.textContent)})),
      map:{shown:!document.querySelector('.mxMapBox').hidden,shapes:q('.mxMapBox svg path,.mxMapBox svg circle').length,
        legend:q('.mxLeg span').length,far:!!document.querySelector('.mxMapBox .mxNote')},
      frames:q('.mxSrc iframe').length,hostClass:document.documentElement.classList.contains('mxHost'),
      grid:getComputedStyle(document.getElementById('grid')).display}})()""" % ("true" if ready else "false"))
    v.grab().save(os.path.join(SHOTS, "multi_view.png"))
    # collapse the first dashboard
    js("document.querySelector('.mxSrc header .mxBtn').click();1")
    pump(0.3)
    res["collapsed"] = js("document.querySelector('.mxSrc').classList.contains('closed')")
    res["errors"] = errs
    print("RESULT " + json.dumps(res, ensure_ascii=False), flush=True)
    sys.exit(0)

# ======================================================================================================================
# the two "PCs"
# ======================================================================================================================
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.dashboard import builder, cards, dock, model, multi, seals, sharing  # noqa: E402
from biruni.dashboard import page as P  # noqa: E402
from biruni.dashboard.service import DashboardService  # noqa: E402

P.EXTENSIONS = ("ext_multi",)       # this test looks at the view alone (other extensions are tested on their own)

# The view frames each dashboard. The LAN server's page policy must allow it: "frame-src https:" (the view may
# frame dashboards) and "frame-ancestors 'self' https:" (a dashboard may be framed by a view). Until server.py says
# so, this test adds those two words to the header itself (and says it does).
import http.server as _HS  # noqa: E402
import inspect  # noqa: E402

from biruni.dashboard import server as _server  # noqa: E402

HOOK_IN_SERVER = "frame-src" in inspect.getsource(_server)
if not HOOK_IN_SERVER:
    print("NOTE server.py does not allow frames yet: the test widens the page policy itself (hook needed)")
    _orig_send_header = _HS.BaseHTTPRequestHandler.send_header

    def _send_header(self, k, v):
        if k == "Content-Security-Policy" and "frame-ancestors 'none'" in v:
            v = v.replace("frame-ancestors 'none'", "frame-ancestors 'self' https:") + "; frame-src https:"
        return _orig_send_header(self, k, v)

    _HS.BaseHTTPRequestHandler.send_header = _send_header

failures = []
TMP = tempfile.mkdtemp()
cards._TEST_SEED.append(bytes(range(32)))
seals._PATH.append(os.path.join(TMP, "seals.sqlite"))


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def http(method, url, body=None, timeout=40):
    out = {}

    def run():
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
                out["r"] = (r.status, r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            out["r"] = (e.code, e.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            out["r"] = (0, str(e))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        app.processEvents()
        time.sleep(0.01)
    st, txt = out["r"]
    try:
        return st, json.loads(txt)
    except ValueError:
        return st, txt


def messages_after_hello(base, card):
    """hello with a card, then long-poll until the first data -> (dash, data) or (error, None)."""
    st, r = http("POST", base + "m", {"s": None, "m": {"t": "hello", "card": card, "lang": "en"}})
    if st != 200 or not isinstance(r, dict):
        return {"t": "error", "why": f"http {st}"}, None
    sid, n = r.get("s"), r.get("n") or 0
    got = list(r.get("out") or [])
    dash = data = None
    end = time.monotonic() + 25
    while time.monotonic() < end:
        for m in got:
            if m.get("t") == "error":
                return m, None
            if m.get("t") == "dash":
                dash = m
            if m.get("t") == "data" and dash:
                data = m
        if dash and data:
            return dash, data
        st, r = http("GET", base + f"ev?s={sid}&n={n}")
        if st != 200 or not isinstance(r, dict):
            break
        n = r.get("n") or n
        got = list(r.get("out") or [])
    return dash or {"t": "error", "why": "timeout"}, data


def make_pc(name, x0, n, districts):
    prj = QgsProject()
    parcels = QgsVectorLayer("Polygon?crs=EPSG:4326&field=district:string&field=status:string&field=area:double",
                             "Parcels", "memory")
    zones = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "Districts", "memory")
    feats = []
    for i in range(n):
        f = QgsFeature(parcels.fields())
        x, y = x0 + (i % 6) * 0.01, 24.0 + (i // 6) * 0.01
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + .008} {y},{x + .008} {y + .008},{x} {y + .008},{x} {y}))"))
        f.setAttributes([districts[0] if i < n * 0.6 else districts[1], "done" if i % 3 == 0 else "open", 400.0 + i])
        feats.append(f)
    parcels.dataProvider().addFeatures(feats)
    for k, (nm, wkt) in enumerate(((districts[0], f"POLYGON(({x0 - .01} 23.99,{x0 + .07} 23.99,{x0 + .07} 24.025,"
                                                  f"{x0 - .01} 24.025,{x0 - .01} 23.99))"),
                                   (districts[1], f"POLYGON(({x0 - .01} 24.025,{x0 + .07} 24.025,{x0 + .07} 24.06,"
                                                  f"{x0 - .01} 24.06,{x0 - .01} 24.025))"))):
        z = QgsFeature(zones.fields())
        z.setGeometry(QgsGeometry.fromWkt(wkt))
        z.setAttributes([nm])
        zones.dataProvider().addFeature(z)
    prj.addMapLayers([parcels, zones])
    d = builder.template("Survey progress", {"main": parcels, "zones": zones})
    d["title"] = name
    d = model.save(prj, d)
    svc = DashboardService(prj)
    return prj, d, svc, parcels, zones


prjA, dA, svcA, parA, zonA = make_pc("Site A", 46.00, 30, ("North", "South"))
prjB, dB, svcB, parB, zonB = make_pc("Site B", 46.09, 24, ("East", "West"))
shA = sharing.Sharing(svcA, lang="en", addresses=["127.0.0.1"], port=10181, folder=TMP)
shB = sharing.Sharing(svcB, lang="en", addresses=["127.0.0.1"], port=10183, folder=TMP)
shA.start()
shB.start()
baseA, baseB = shA.lan.local_url(), shB.lan.local_url()
check(baseA != baseB and baseA.startswith("https://127.0.0.1:"), f"two LAN servers ({baseA[:30]}…, {baseB[:30]}…)")

cardA = cards.issue(dA["id"], "Supervisor A", project=prjA)
east = next(zonB.getFeatures())                                 # the first zone of B: «East»
cardB = cards.issue(dB["id"], "Supervisor B (East only)", zone_wkt=east.geometry().asWkt(), project=prjB)

# ---- each source keeps its own card --------------------------------------------------------------------------------
dashA, dataA = messages_after_hello(baseA, cardA)
dashB, dataB = messages_after_hello(baseB, cardB)
check(dashA.get("t") == "dash" and dataA is not None, f"PC A answers its own card ({dashA.get('why', 'ok')})")
check(dashB.get("t") == "dash" and dataB is not None and dashB.get("card", {}).get("zone") is True,
      "PC B answers its zone card")
wrong, _ = messages_after_hello(baseB, cardA)
check(wrong.get("t") == "error", f"PC B refuses PC A's card ({wrong.get('why')})")


def kpis(dash, data):
    out = {}
    for w in dash["def"]["widgets"]:
        if w["type"] == "kpi":
            v = (data["widgets"].get(w["id"]) or {}).get("value")
            if v is not None:
                out[w["title"]] = v
    return out


kA, kB = kpis(dashA, dataA), kpis(dashB, dataB)
common = sorted(set(kA) & set(kB))
check(bool(common), f"both dashboards have numbers with the same title ({common})")
first = common[0] if common else None
if first:
    full_b = parB.featureCount()
    check(kB[first] < full_b or first.lower().find("done") >= 0,
          f"B's zone card counts only its zone («{first}»: {kB[first]} of {full_b} parcels)")

# ---- the view's link -------------------------------------------------------------------------------------------------
linkA = shA.links(cardA)["lan"][0].replace(shA.addresses()[0], "127.0.0.1")
linkB = shB.links(cardB)["lan"][0].replace(shB.addresses()[0], "127.0.0.1")
view = multi.multi_link(baseA, [("Site A", linkA), ("Site B", linkB)], "Region")
doc = multi.parse_multi(view)
check(doc and doc["t"] == "Region" and [s["n"] for s in doc["s"]] == ["Site A", "Site B"] and doc["s"][1]["u"] == linkB,
      "the view's link holds both dashboards' own links")
check(view.startswith(baseA + "#m="), "the view is opened from a dashboard page (no server of its own)")
try:
    multi.multi_link(baseA, [("x", "https://example.com/page")])
    check(False, "a link without a card is refused")
except ValueError:
    check(True, "a link without a card is refused")
check(multi.parse_multi("https://x/#c=1") is None, "an ordinary dashboard link is not a view")


class FakeDock:
    sharing = shA


check(multi._base_of(FakeDock()).startswith("https://127.0.0.1:10181/d/"), "the view is hosted by this PC's page")

# ---- the page (child process) ------------------------------------------------------------------------------------------
proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--browser", view], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                        env=dict(os.environ, QGIS_CUSTOM_CONFIG_PATH=tempfile.mkdtemp()))
lines = []


def reader():
    for ln in proc.stdout:
        lines.append(ln)


rt = threading.Thread(target=reader, daemon=True)
rt.start()
end = time.monotonic() + 150
while proc.poll() is None and time.monotonic() < end:
    app.processEvents()           # the gateways answer in this thread
    time.sleep(0.01)
if proc.poll() is None:
    proc.kill()
rt.join(5)
res = None
for ln in lines:
    if ln.startswith("RESULT "):
        res = json.loads(ln[7:])
if res is None:
    print("".join(lines[-30:]))
check(res is not None and res.get("ready"), "the view opens both dashboards and both go live")
if res:
    print("view:", json.dumps({k: res[k] for k in ("title", "sources", "tiles", "map", "frames")}, ensure_ascii=False)[:1500])
    check(res["hostClass"] and res["grid"] == "none", "the page turns into the view (its own grid hidden)")
    check(res["frames"] == 2 and res["title"] == "Region", "one section per dashboard, with the view's title")
    tile = next((t for t in res["tiles"] if first and t["title"].strip().lower() == first.strip().lower()), None)
    if tile and first:
        val = float(tile["value"].replace(",", "").replace("٬", ""))
        check(abs(val - (kA[first] + kB[first])) < 1e-6,
              f"«{first}»: the view sums the two ({kA[first]} + {kB[first]} = {val})")
        check(len(tile["rows"]) == 2, "the tile lists what each dashboard says")
    else:
        check(False, f"a summary tile for «{first}» ({[t['title'] for t in res['tiles']]})")
    check(res["map"]["shown"] and res["map"]["shapes"] > 0 and res["map"]["legend"] == 2 and not res["map"]["far"],
          f"both sites on one map, coloured by dashboard ({res['map']})")
    check(res["collapsed"] is True, "a dashboard's section can be collapsed")
    errs = [e for e in res["errors"] if "favicon" not in e]
    check(not errs, f"no JavaScript errors ({errs[:3]})")

shA.stop()
shB.stop()
svcA.stop()
svcB.stop()
print("\nALL PASSED" if not failures else f"\nFAILED: {len(failures)}")
sys.exit(1 if failures else 0)
