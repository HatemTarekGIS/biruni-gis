"""The dashboard in a link (dashboard/snapshot.py + ext_link.py): a ~430-parcel map dashboard packed into the link,
its size, the signature (one changed byte is caught), a real card's snapshot through the service (zone and hidden
fields hold), and the page opening it offscreen with no transport - with WebCrypto/DecompressionStream and with
the page's own inflate and ed25519 fallbacks.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_link.py
"""
import json
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
BROWSER = "--browser" in sys.argv  # QtWebEngine dies next to a QgsApplication: the page part runs in a child process
if BROWSER:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript  # noqa: E402
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
    from PyQt6.QtWidgets import QApplication  # noqa: E402

    app = QApplication(sys.argv[:1])
else:
    from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

    app = QgsApplication([], True)
    app.initQgis()

from biruni.dashboard import cards, model, seals, service, snapshot  # noqa: E402
from biruni.dashboard import page as P  # noqa: E402

failures = []
errors = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


SEED = bytes(range(32))
cards._TEST_SEED[:] = [SEED]
seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
PUB = cards.public_hex()

if not BROWSER:
    # ---- 1. a showcase-sized dashboard (about 430 parcels on a street grid) --------------------------------------------
    random.seed(3)
    NOW = time.time()
    STATUS = ["approved", "surveyed", "review", "todo"]
    COLORS = {"approved": "#1e88e5", "surveyed": "#43a047", "review": "#fb8c00", "todo": "#9e9e9e"}
    feats = []
    fid = 0
    for bx in range(6):
        for by in range(6):
            x0, y0 = 46.66 + bx * 0.0042, 24.81 + by * 0.0036
            for i in range(4):
                for j in range(3):
                    if len(feats) >= 430:
                        break
                    w, h = 0.0009 + random.random() * 0.0002, 0.0009
                    x, y = x0 + i * 0.00095, y0 + j * 0.00095
                    st = random.choice(STATUS)
                    fid += 1
                    feats.append({"type": "Feature", "id": fid, "geometry": {"type": "Polygon", "coordinates": [[
                        [x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]},
                        "properties": {"label": f"{bx + 1}{by}-{i * 3 + j + 1:02d}", "_c": COLORS[st],
                                       "_k": {"status": st, "district": f"D{bx // 3 + 1}{by // 3 + 1}"},
                                       "area": round(w * h * 1.1e10, 1), "status": st}})
    roads = [{"type": "Feature", "id": f"r{i}", "geometry": {"type": "LineString", "coordinates": [
        [46.655, 24.806 + i * 0.0036], [46.69, 24.806 + i * 0.0036]]}, "properties": {"label": f"Street {i}"}}
        for i in range(7)]
    DEF = {"v": 1, "id": "d_show", "title": "Al-Waha plan — phase 2", "theme": "biruni-dark", "columns": 12,
           "widgets": [{"id": "k1", "type": "kpi", "title": "Parcels surveyed", "x": 0, "y": 0, "w": 3, "h": 2},
                       {"id": "m1", "type": "map", "title": "Project map", "x": 3, "y": 0, "w": 9, "h": 8},
                       {"id": "b1", "type": "bar", "title": "Parcels by district", "x": 0, "y": 2, "w": 3, "h": 3},
                       {"id": "t1", "type": "table", "title": "Latest parcels", "x": 0, "y": 8, "w": 6, "h": 4},
                       {"id": "fd", "type": "feed", "title": "Live activity", "x": 6, "y": 8, "w": 6, "h": 4}]}
    DATA = {"k1": {"value": len(feats), "prev": 410, "delta": len(feats) - 410, "seal": "K7QM-2D9X"},
            "m1": {"layers": [{"id": "parcels", "name": "Parcels", "kind": "fill",
                               "geojson": {"type": "FeatureCollection", "features": feats},
                               "legend": [{"label": k, "color": v} for k, v in COLORS.items()]},
                              {"id": "roads", "name": "Streets", "kind": "line",
                               "geojson": {"type": "FeatureCollection", "features": roads}, "legend": []}],
                   "bbox": [46.655, 24.806, 46.69, 24.834], "total": len(feats)},
            "b1": {"items": [{"key": d, "label": d, "value": sum(1 for f in feats if f["properties"]["_k"]["district"] == d)}
                             for d in ("D11", "D12", "D21", "D22")], "xf": "district"},
            "t1": {"columns": [{"key": "no", "label": "Parcel"}, {"key": "a", "label": "Area m²"}],
                   "rows": [[f["properties"]["label"], f["properties"]["area"]] for f in feats[:120]], "total": len(feats)},
            "fd": {"items": [{"ts": NOW - i * 60, "who": "Mona", "what": f"approved parcel {i}", "layer": "Parcels"}
                             for i in range(25)]}}
    DASH = {"t": "dash", "def": DEF, "card": {"name": "Supervisor", "exp": NOW + 86400, "zone": False}, "pc": "Office PC",
            "pub": PUB}

    raw_json = len(json.dumps(DATA, separators=(",", ":")))
    val = snapshot.pack(snapshot.payload_of(DASH, DATA, NOW), SEED)
    lnk = snapshot.link(val, "ar")
    print(f"showcase: {len(feats)} parcels, raw JSON {raw_json / 1024:.0f} KB -> link {len(lnk) / 1024:.1f} KB", flush=True)
    check(len(lnk) < 120 * 1024, f"a {len(feats)}-parcel map dashboard fits a link of reasonable size ({len(lnk) // 1024} KB)")
    payload, ok = snapshot.unpack(val)
    check(ok and payload["data"]["m1"]["total"] == len(feats) and payload["pub"] == PUB, "the link unpacks and verifies")
    a, b = val.split(".")
    mid = len(a) // 2
    bad_a = a[:mid] + ("A" if a[mid] != "A" else "B") + a[mid + 1:]
    try:
        _p, ok2 = snapshot.unpack(bad_a + "." + b)
    except Exception:  # noqa: BLE001 - the inflate itself may fail
        ok2 = False
    check(not ok2, "one changed character in the data is caught")
    bad_sig = b[:10] + ("A" if b[10] != "A" else "B") + b[11:]
    _p, ok3 = snapshot.unpack(a + "." + bad_sig)
    check(not ok3, "one changed character in the signature is caught")
    check(snapshot.fingerprint(PUB) == snapshot.fingerprint(PUB) and len(snapshot.fingerprint(PUB)) == 9, "key fingerprint")

    # shrinking
    small = DATA
    for st in snapshot.STEPS:
        small = snapshot.shrink(small, st)
    check(small["m1"].get("hidden") and "rows" in small["t1"] and len(small["t1"]["rows"]) <= 40,
          "the last shrinking steps leave the maps out and shorten long tables")
    simp = snapshot.shrink(DATA, "simplify")
    check(len(json.dumps(simp)) <= len(json.dumps(DATA)), "simplifying never grows the data")
    r6 = snapshot.shrink(DATA, "round6")
    c0 = r6["m1"]["layers"][0]["geojson"]["features"][0]["geometry"]["coordinates"][0][1]
    check(all(len(str(v).split(".")[-1]) <= 6 for v in c0), "coordinates rounded to 6 decimals")

    # ---- 2. a real card through the service: the zone and hidden fields hold in the snapshot ------------------------------
    prj = QgsProject.instance()
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=district:string&field=status:string&field=owner:string", "parcels",
                         "memory")
    fs = []
    for i in range(80):
        f = QgsFeature(lyr.fields())
        d = "North" if i % 2 else "South"
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({0.1 + (i % 10) * 0.08} {0.1 + (i // 10) * 0.1 + (0 if d == 'South' else 1)})"))
        f.setAttributes([d, "done" if i % 3 else "open", "SECRET-OWNER-%d" % i])
        fs.append(f)
    lyr.dataProvider().addFeatures(fs)
    prj.addMapLayer(lyr)
    D = model.blank("Survey")
    D["sources"] = {"s1": {"layer": lyr.id(), "filter": ""}}
    D["widgets"] = [{"id": "kpi", "type": "kpi", "title": "Parcels", "x": 0, "y": 0, "w": 3, "h": 2, "source": "s1",
                     "measure": {"op": "count"}},
                    {"id": "tbl", "type": "table", "title": "Rows", "x": 3, "y": 0, "w": 6, "h": 4, "source": "s1",
                     "options": {"fields": ["district", "status", "owner"]}},
                    {"id": "map", "type": "map", "title": "Map", "x": 0, "y": 4, "w": 9, "h": 5, "source": "s1",
                     "options": {"fields": ["status", "owner"]}}]
    model.save(prj, D)
    north = "POLYGON((0 1,1 1,1 2,0 2,0 1))"
    card = cards.issue(D["id"], "North supervisor", hide=["owner"], zone_wkt=north)
    svc = service.DashboardService(prj)
    res = snapshot.build(svc, card, "en", base="https://example.invalid/d/", seed=SEED)
    pl, ok = snapshot.unpack(res["value"])
    check(ok and res["size"] == len(res["link"]) and not res["steps"], "a card's snapshot builds and verifies")
    check(pl["data"]["kpi"]["value"] == 40, f"the card's zone holds in the snapshot (40 of 80; got {pl['data']['kpi'].get('value')})")
    check("SECRET-OWNER" not in json.dumps(pl, ensure_ascii=False), "hidden fields never reach the snapshot")
    check(pl["dash"]["def"].get("sources") is None, "no sources or expressions in the snapshot")
    try:
        snapshot.build(svc, card[:-4] + "AAAA", "en", seed=SEED)
        check(False, "a forged card gives no snapshot")
    except snapshot.SnapshotError as e:
        check(e.why in ("foreign", "bad card"), f"a forged card gives no snapshot ({e.why})")

    # ---- the page part, in a child process ----------------------------------------------------------------------------
    import subprocess  # noqa: E402

    cfg = {"html": P.page_html("ar", "bridge"), "val": val, "a": a, "b": b, "bad_sig": bad_sig, "bad_a": bad_a,
           "pub": PUB, "fp": snapshot.fingerprint(PUB), "nw": len(DEF["widgets"])}
    cfgp = os.path.join(tempfile.mkdtemp(), "cfg.json")
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--browser", cfgp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    for ln in r.stdout.splitlines():
        if ln.startswith(("PASS", "FAIL", "open", " ")) or "JS ERROR" in ln:
            print(ln, flush=True)
        if ln.startswith("FAIL "):
            failures.append(ln[5:])
    if r.returncode not in (0, 1) or "BROWSER DONE" not in r.stdout:
        check(False, "the page part ran to its end (exit %s)" % r.returncode)
        print(r.stderr[-2000:])
else:
    cfg = json.load(open(sys.argv[sys.argv.index("--browser") + 1], encoding="utf-8"))
    val, a, b, bad_sig, bad_a, PUB = cfg["val"], cfg["a"], cfg["b"], cfg["bad_sig"], cfg["bad_a"], cfg["pub"]
    # ---- 3. the page opens the link with no transport -------------------------------------------------------------------
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
        def __init__(self, name):
            super().__init__(QWebEngineProfile.defaultProfile())
            self.name = name

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                errors.append(f"{self.name}: {message} ({line})")
                print(f"{self.name} JS ERROR:", message, line, flush=True)


    def js(pg, expr):
        out = {}
        pg.runJavaScript(expr, lambda r: out.update(v=r))
        wait(lambda: "v" in out, 15)
        return out.get("v")


    HTML = cfg["html"]
    BASE = "https://biruni-gis.com/d/"


    def open_link(name, frag, flags=""):
        v = QWebEngineView()
        pg = Page(name)
        v.setPage(pg)
        v.resize(1300, 900)
        if flags:
            s = QWebEngineScript()
            s.setSourceCode(flags)
            s.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            s.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            pg.scripts().insert(s)
        v.show()
        t0 = time.monotonic()
        pg.setHtml(HTML, QUrl(BASE + "#" + frag))
        wait(lambda: js(pg, "window.__extLinkResult&&(window.__extLinkResult.shown||!window.__extLinkResult.opened)") is True, 30)
        return v, pg, time.monotonic() - t0


    v1, p1, t1 = open_link("ok", f"s={val}&l=ar")
    r1 = js(p1, "JSON.stringify(window.__extLinkResult)")
    print("open (WebCrypto/DecompressionStream):", r1, f"{t1:.2f}s", flush=True)
    check(r1 and json.loads(r1).get("ok") is True, "the page verifies the snapshot signature")
    check(js(p1, "document.getElementById('ext_link_bar').className") == "ext_link_bar ok", "a green 'verified' bar")
    check(js(p1, "document.querySelectorAll('#grid .w').length") == cfg["nw"], "every widget drawn from the link")
    check(js(p1, "document.querySelectorAll('#grid .map path.f').length") or 0, "the map's parcels are drawn")
    check(js(p1, "document.getElementById('connText').textContent") == "لقطة", "the state says «snapshot», not «live»")
    check(js(p1, "BiruniDash.state.sent.some(m=>m.t==='hello')") is False, "no hello is sent: no transport at all")
    bartext = js(p1, "document.getElementById('ext_link_bar').textContent") or ""
    check("Office PC" in bartext and cfg["fp"] in bartext, "the bar names the PC and the key fingerprint")
    v1.grab().save(os.path.join(tempfile.gettempdir(), "dash_link_ok.png"))

    v2, p2, _t = open_link("bad", f"s={a}.{bad_sig}&l=ar")
    r2 = json.loads(js(p2, "JSON.stringify(window.__extLinkResult)") or "{}")
    check(r2.get("ok") is False and js(p2, "document.getElementById('ext_link_bar').className") == "ext_link_bar bad",
          "a changed signature: red warning")
    v3, p3, _t = open_link("broken", f"s={bad_a}.{b}&l=ar")
    r3 = json.loads(js(p3, "JSON.stringify(window.__extLinkResult)") or "{}")
    check(r3.get("ok") is not True and js(p3, "document.getElementById('ext_link_bar').className") == "ext_link_bar bad",
          "a changed byte in the data: red (damaged or not verified)")

    v4, p4, t4 = open_link("fallback", f"s={val}&l=en", "window.__noDS=1;window.__noWebEd=1;")
    r4 = json.loads(js(p4, "JSON.stringify(window.__extLinkResult)") or "{}")
    print(f"open (own inflate + BigInt ed25519): {t4:.2f}s", flush=True)
    check(r4.get("ok") is True and js(p4, "document.querySelectorAll('#grid .w').length") == cfg["nw"],
          "the page's own inflate and ed25519 open and verify the same link")
    v5, p5, _t = open_link("fallback-bad", f"s={a}.{bad_sig}&l=en", "window.__noDS=1;window.__noWebEd=1;")
    check(json.loads(js(p5, "JSON.stringify(window.__extLinkResult)") or "{}").get("ok") is False,
          "the BigInt ed25519 refuses a changed signature")
    # inflate of every block type (stored, fixed, dynamic) against Python's zlib
    import base64  # noqa: E402
    import zlib  # noqa: E402

    samples = [b"", b"a", b"hello hello hello hello", os.urandom(3000), ("بيروني " * 400).encode("utf-8")]
    okall = True
    for lvl in (0, 1, 9):
        for smp in samples:
            c = zlib.compressobj(lvl, zlib.DEFLATED, -15)
            z = c.compress(smp) + c.flush()
            got = js(p4, "(()=>{const z=BiruniEd.b64.dec('%s');return BiruniEd.b64.enc(BiruniEd.inflateRaw(z))})()"
                     % base64.urlsafe_b64encode(z).rstrip(b"=").decode())
            if got != base64.urlsafe_b64encode(smp).rstrip(b"=").decode():
                okall = False
    check(okall, "the page's inflate matches zlib for stored, fixed and dynamic blocks")
    # a live page remembers the PC's key; a snapshot then says "the same computer"
    check(not errors, "no JavaScript errors" + ("" if not errors else ": " + "; ".join(errors[:3])))
    for v in (v1, v2, v3, v4, v5):
        v.close()

    print("BROWSER DONE", flush=True)

print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
sys.exit(1 if failures else 0)
