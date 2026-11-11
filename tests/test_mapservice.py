"""A map service in one folder (mapservice/, SPEC.md): one PMTiles archive for the map, one FlatGeobuf per layer
read in pieces for identify and queries, a static OGC API - Features tree that the desktop's own OAPIF client
reads, plain {z}/{x}/{y} tiles with a TileJSON, a raster as PNG tiles, a signed manifest (tampering is caught
on the disk and over http), hidden fields that never leave the PC, the dialog, and the viewer page in a browser
(signature check with Web Crypto, queries and identify over HTTP range requests).

Run:  engine\\bin\\python-qgis.bat tests\\test_mapservice.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
SHOTS = os.environ.get("BIRUNI_SHOTS", tempfile.gettempdir())

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# =================================================================================================================
# the page, in a browser of its own (QtWebEngine runs in a process of its own: it does not mix with QGIS here)
# =================================================================================================================
def page_main(url, out_json):
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = os.environ.get("BMS_FLAGS", "--disable-gpu --no-sandbox --enable-unsafe-swiftshader")
    from PyQt6.QtCore import QEventLoop, QTimer, QUrl
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    res, logs = {}, []

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            logs.append(f"{level.name if hasattr(level, 'name') else level}: {message} ({source}:{line})")

    view = QWebEngineView()
    pg = Page(view)
    view.setPage(pg)
    view.resize(1200, 800)
    view.show()

    def pump(secs):
        loop = QEventLoop()
        QTimer.singleShot(int(secs * 1000), loop.quit)
        loop.exec()

    def js(expr, secs=30):
        box = {}
        pg.runJavaScript(expr, 0, lambda v: box.setdefault("v", v))
        t = time.time()
        while "v" not in box and time.time() - t < secs:
            app.processEvents()
            time.sleep(0.01)
        return box.get("v")

    def await_js(expr, secs=60):
        """run an async expression; the result comes back through window.__r"""
        js("window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (%s))}catch(e){window.__r=JSON.stringify({error:String(e)})}})();1" % expr)
        t = time.time()
        while time.time() - t < secs:
            v = js("window.__r")
            if v is not None:
                return json.loads(v)
            pump(0.1)
        return None

    pg.load(QUrl(url))
    t = time.time()
    while time.time() - t < 30 and not js("!!(window.BMS&&window.BMS.sig&&window.BMS.sig.filesDone)"):
        pump(0.2)
    res["sig"] = json.loads(js("JSON.stringify({state:BMS.sig.state,bad:BMS.sig.bad,checked:BMS.sig.checked,"
                               "badge:document.getElementById('badge').className,text:document.getElementById('badge').textContent})") or "null")
    t = time.time()
    while time.time() - t < 20 and not js("!!(window.BMS&&window.BMS.ready)"):
        pump(0.2)
    res["map_ready"] = bool(js("!!(window.BMS&&window.BMS.ready)"))
    res["webgl"] = js("(()=>{try{return !!document.createElement('canvas').getContext('webgl2')||!!document.createElement('canvas').getContext('webgl')}catch(e){return false}})()")
    res["dir"] = js("document.documentElement.dir")
    res["logo"] = js("!!document.querySelector('header svg.logo')")
    q = await_js("BMS.query(BMS.D.layers[0].id,'الحالة','eq','معتمدة',false,5000).then(r=>({total:r.total,read:r.read,"
                 "sample:r.found.slice(0,3).map(f=>f.properties),hidden:r.found.some(f=>'owner_id' in f.properties)}))")
    res["query"] = q
    q2 = await_js("BMS.query(BMS.D.layers[0].id,'المساحة','gt','900',false,5000).then(r=>({total:r.total,read:r.read}))")
    res["query_num"] = q2
    if res["map_ready"]:
        pump(2)
        res["identify"] = await_js("(async()=>{const L=BMS.D.layers[0],b=L.bbox;BMS.map.jumpTo({center:[(b[0]+b[2])/2,(b[1]+b[3])/2],zoom:16});"
                                   "await new Promise(r=>BMS.map.once('idle',r));const c=BMS.map.getCenter();await BMS.identifyAt(c.lng,c.lat);"
                                   "return BMS.lastPopup||null})()")
        view.grab().save(os.path.join(SHOTS, "mapservice_page.png"))
    res["logs"] = [x for x in logs if "rror" in x][:20]
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    app.quit()


if len(sys.argv) > 2 and sys.argv[1] == "--page":
    page_main(sys.argv[2], sys.argv[3])
    sys.exit(0)

# =================================================================================================================
from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsFeature, QgsFillSymbol, QgsGeometry,  # noqa: E402
                       QgsPointXY, QgsProject, QgsRasterLayer, QgsRendererCategory, QgsVectorLayer,
                       QgsVectorTileLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from osgeo import gdal, ogr  # noqa: E402

from biruni.mapservice import core  # noqa: E402
from biruni.share import offline_maps as om  # noqa: E402
from biruni.share import pmtiles as pm  # noqa: E402

core._TEST_SEED[:] = [bytes(range(32))]
root = tempfile.mkdtemp(prefix="mapservice_test_")
prj = QgsProject.instance()

# ---- layers: 2,500 parcels (Arabic fields, a field to hide, a categorized style), points, an empty line layer ----
parcels = QgsVectorLayer("Polygon?crs=EPSG:32638&field=رقم القطعة:integer&field=الحالة:string&field=المساحة:double"
                         "&field=owner_id:string", "القطع", "memory")
STATUS = ["معتمدة", "مرفوعة", "قيد المراجعة", "لم تبدأ"]
feats = []
x0, y0 = 670000.0, 2730000.0
for i in range(2500):
    r, c = divmod(i, 50)
    x, y = x0 + c * 32, y0 + r * 32
    f = QgsFeature(parcels.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 30} {y},{x + 30} {y + 30},{x} {y + 30},{x} {y}))"))
    f.setAttributes([i + 1, STATUS[i % 4], 600.0 + (i % 500), f"SECRET-{i}"])
    feats.append(f)
parcels.dataProvider().addFeatures(feats)
cats = [QgsRendererCategory(s, QgsFillSymbol.createSimple({"color": col}), s)
        for s, col in zip(STATUS, ("#1E88E5", "#43A047", "#FB8C00", "#9E9E9E"))]
parcels.setRenderer(QgsCategorizedSymbolRenderer("الحالة", cats))
wells = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "Wells", "memory")
wf = []
for i in range(30):
    f = QgsFeature(wells.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(45.0 + i * 0.001, 24.66 + (i % 5) * 0.001)))
    f.setAttributes([f"well {i}"])
    wf.append(f)
wells.dataProvider().addFeatures(wf)
empty = QgsVectorLayer("LineString?crs=EPSG:4326&field=n:string", "Empty roads", "memory")
tif = os.path.join(root, "ortho.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, 200, 200, 3, gdal.GDT_Byte)
ds.SetGeoTransform((x0, 8.0, 0, y0 + 1600, 0, -8.0))
from osgeo import osr  # noqa: E402

sr = osr.SpatialReference()
sr.ImportFromEPSG(32638)
ds.SetProjection(sr.ExportToWkt())
for b in range(1, 4):
    band = ds.GetRasterBand(b)
    band.WriteRaster(0, 0, 200, 200, bytes(((i * (b + 1)) % 256) for i in range(200 * 200)))
ds = None
ortho = QgsRasterLayer(tif, "Ortho", "gdal")
prj.addMapLayers([parcels, wells, empty, ortho])
check(ortho.isValid(), "a raster to tile")

# ---- publish ---------------------------------------------------------------------------------------------------
out = os.path.join(root, "svc")
steps = []
t0 = time.time()
info = core.publish([parcels, wells, empty], out, "مخطط الواحة — خدمة خرائط",
                    {"page_size": 1000, "xyz": True, "raster": ortho, "hide": {"القطع": ["owner_id"]},
                     "signer": "مكتب الاختبار", "description": "test"}, progress=lambda p, t="": steps.append(p))
print("   published in %.1f s: %d files, %.1f KB" % (time.time() - t0, len(info["files"]),
                                                      sum(s for _p, s in info["files"]) / 1024))
ids = [l["id"] for l in info["layers"]]
check(ids == ["layer1", "wells"], "layer ids are address-safe (Arabic name -> layer1): %s" % ids)
check(any("Empty roads" in w for w in info["warnings"]), "an empty layer is reported, not published")
check(steps and steps[-1] == 100, "progress reaches 100")
names = {p for p, _s in info["files"]}
for need in ("index.html", "service.json", "manifest.json", "manifest.sig", "data/map.pmtiles", "data/layer1.fgb",
             "data/wells.fgb", "data/raster.pmtiles", "ogc/index.html", "ogc/index.json", "ogc/api/index.html",
             "ogc/conformance/index.html", "ogc/collections/index.html", "ogc/collections/layer1/index.html",
             "ogc/collections/layer1/items", "ogc/collections/layer1/items-3.json", "ogc/about.html",
             "tiles.json", "lib/maplibre-gl.mjs", "lib/pmtiles.js", "lib/flatgeobuf.js", "lib/LICENSE-flatgeobuf.txt",
             ".nojekyll", "README.md"):
    check(need in names, "written: " + need)
check(sum(1 for p in names if p.endswith(".pmtiles")) == 2, "the map is ONE vector archive (plus the raster)")

# ---- the one PMTiles archive: every layer inside ---------------------------------------------------------------
rd = pm.Reader(os.path.join(out, "data", "map.pmtiles"))
meta = rd.metadata() if callable(getattr(rd, "metadata", None)) else getattr(rd, "metadata", {})
vl = sorted(v["id"] for v in (meta or {}).get("vector_layers", []))
check(vl == ["layer1", "wells"], "map.pmtiles carries both layers as source-layers (%s)" % vl)
vt = QgsVectorTileLayer("type=xyz&url=file:///" + out.replace("\\", "/") + "/xyz/{z}/{x}/{y}.pbf&zmax=14&zmin=0", "xyz")
check(vt.isValid(), "the plain {z}/{x}/{y}.pbf tiles open as a vector tile layer")
some = []
for r_, _d, fs in os.walk(os.path.join(out, "xyz")):
    some += [os.path.join(r_, f) for f in fs if f.endswith(".pbf")]
check(some and all(open(p, "rb").read(2) != b"\x1f\x8b" for p in some[:20]), "xyz tiles are not gzipped (static hosts send them as they are)")
tj = json.load(open(os.path.join(out, "tiles.json"), encoding="utf-8"))
check(tj["tilejson"] == "3.0.0" and tj["tiles"] == ["xyz/{z}/{x}/{y}.pbf"] and len(tj["vector_layers"]) == 2, "TileJSON 3.0.0")
rr = pm.Reader(os.path.join(out, "data", "raster.pmtiles"))
hdr = rr.header if not callable(getattr(rr, "header", None)) else rr.header()
check(hdr["tile_type"] == 2 and hdr["max_zoom"] >= 14 and hdr["min_zoom"] < hdr["max_zoom"], "the raster: PNG tiles in PMTiles (z %s-%s)" % (hdr["min_zoom"], hdr["max_zoom"]))

# ---- FlatGeobuf: the whole layer, its spatial index, no hidden field -----------------------------------------------
fg = ogr.Open(os.path.join(out, "data", "layer1.fgb"))
lyr = fg.GetLayer(0)
fnames = [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())]
check(lyr.GetFeatureCount() == 2500, "layer1.fgb holds all 2,500 parcels")
check("owner_id" not in fnames and "الحالة" in fnames and "_fid" in fnames, "FGB fields: Arabic names kept, the hidden field left out (%s)" % fnames)
check(lyr.TestCapability(ogr.OLCFastSpatialFilter), "FGB has its spatial index (read in pieces)")
fg = None

# ---- static OGC API - Features ---------------------------------------------------------------------------------
land = json.load(open(os.path.join(out, "ogc", "index.json"), encoding="utf-8"))
rels = {l["rel"]: l["href"] for l in land["links"]}
check(rels.get("conformance") == "conformance/" and rels.get("data") == "collections" and rels.get("service-desc") == "api/",
      "landing page links (relative until the address is known): %s" % rels)
p1 = json.load(open(os.path.join(out, "ogc", "collections", "layer1", "items"), encoding="utf-8"))
p3 = json.load(open(os.path.join(out, "ogc", "collections", "layer1", "items-3.json"), encoding="utf-8"))
check(p1["numberMatched"] == 2500 and p1["numberReturned"] == 1000 and p3["numberReturned"] == 500, "items paged by 1,000")
check(any(l["rel"] == "next" and l["href"].endswith("collections/layer1/items-2.json") for l in p1["links"])
      and not any(l["rel"] == "next" for l in p3["links"]), "pages chained by next links")
check(all("owner_id" not in f["properties"] for f in p1["features"]), "OGC items: the hidden field never leaves the PC")
raw = open(os.path.join(out, "data", "map.pmtiles"), "rb").read() + open(os.path.join(out, "index.html"), "rb").read()
check(b"SECRET-" not in raw, "the hidden values are nowhere in the tiles or the page")

# ---- the signature on the disk --------------------------------------------------------------------------------
v = core.verify(out)
check(v["ok"] and v["signature_ok"] and v["by_me"] and v["signer"] == "مكتب الاختبار" and v["checked"] == len(names) - 2,
      "the folder verifies: signed by this PC, %d files" % v["checked"])
check(info["link_suffix"] == "#fp=" + info["pub"][:16], "the link pins the key (#fp=)")
bad = os.path.join(root, "tampered")
shutil.copytree(out, bad)
with open(os.path.join(bad, "ogc", "collections", "layer1", "items-2.json"), "r+b") as f:
    f.seek(200)
    f.write(b"9")
v2 = core.verify(bad)
check(not v2["ok"] and v2["signature_ok"] and v2["bad"] == ["ogc/collections/layer1/items-2.json"], "a changed file is caught: %s" % v2["bad"])
man = open(os.path.join(bad, "manifest.json"), "rb").read().replace(b"\xd9\x85\xd9\x83\xd8\xaa\xd8\xa8", b"\xd9\x85\xd9\x83\xd8\xaa\xd9\x88")
open(os.path.join(bad, "manifest.json"), "wb").write(man)
check(not core.verify(bad)["signature_ok"], "a changed manifest breaks the signature")
check(not core.verify(out, expected_pub="00" * 32)["ok"], "another key than the expected one is refused")

# ---- over http (range requests, like GitHub Pages) -----------------------------------------------------------------
url = om.serve(out)
req = urllib.request.Request(url + "data/layer1.fgb", headers={"Range": "bytes=0-99"})
with urllib.request.urlopen(req, timeout=10) as r:
    check(r.status == 206 and len(r.read()) == 100, "the FGB is served in pieces (206)")
vh = core.verify(url)
check(vh["ok"], "the service verifies over http too (%d files)" % vh["checked"])
badurl = om.serve(bad)
check(not core.verify(badurl)["ok"], "... and a tampered copy is refused over http")

# ---- the desktop's own OGC API client reads the static tree --------------------------------------------------------
# GIS programs need absolute links: the folder is pointed at its address (here: the preview) and signed again
nre = core.rebase(out, url)
land = json.load(open(os.path.join(out, "ogc", "index.html"), encoding="utf-8"))
p1 = json.load(open(os.path.join(out, "ogc", "collections", "layer1", "items"), encoding="utf-8"))
nxt = [l["href"] for l in p1["links"] if l["rel"] == "next"]
check(nre > 5 and nxt == [url + "ogc/collections/layer1/items-2.json"]
      and {l["rel"]: l["href"] for l in land["links"]}["data"] == url + "ogc/collections", "rebase: the links point at the address (%s)" % nxt)
check(core.verify(url)["ok"], "... and the folder is signed again")
oapif = QgsVectorLayer(f"url='{url}ogc/' typename='layer1'", "oapif", "OAPIF")
check(oapif.isValid(), "QGIS's OAPIF client opens the static service")
if oapif.isValid():
    n = sum(1 for _ in oapif.getFeatures())
    flds = [f.name() for f in oapif.fields()]
    check(n == 2500, "... and follows the pages to all 2,500 features (%d)" % n)
    check("الحالة" in flds and "owner_id" not in flds, "... with the Arabic fields (%s)" % flds[:6])

# ---- the dialog (offscreen) and the check ------------------------------------------------------------------------
from biruni.mapservice import dialog as msd  # noqa: E402


class FakePlugin:
    iface = None


dlg = msd.MapServiceDialog(FakePlugin())
for i in range(dlg.layers.count()):
    it = dlg.layers.item(i)
    from qgis.PyQt.QtCore import Qt  # noqa: E402
    it.setCheckState(Qt.CheckState.Checked if it.text() == "Wells" else Qt.CheckState.Unchecked)
dlg.folder.edit.setText(os.path.join(root, "dlg"))
dlg.title.setText("Wells service")
di = dlg.run(quiet=True)
check(di is not None and [l["id"] for l in di["layers"]] == ["wells"] and dlg.done_label.text(), "the dialog publishes a service")
check(dlg.preview(open_browser=False).startswith("http://127.0.0.1:") and "#fp=" in dlg.preview(open_browser=False),
      "Preview gives the local address with the key pin")
ci = msd.check_ui(None, os.path.join(root, "dlg"))
check(ci["ok"] and "valid" in msd.check_text(ci), "Check a map service: valid")

# ---- the viewer page in a browser ----------------------------------------------------------------------------------
res_path = os.path.join(root, "page.json")
proc = subprocess.run([sys.executable, os.path.abspath(__file__), "--page", url + info["link_suffix"], res_path],
                      capture_output=True, text=True, timeout=240)
if proc.returncode != 0 or not os.path.exists(res_path):
    print(proc.stdout[-2000:], proc.stderr[-2000:])
res = json.load(open(res_path, encoding="utf-8")) if os.path.exists(res_path) else {}
print("   page:", json.dumps({k: v for k, v in res.items() if k != "logs"}, ensure_ascii=False)[:900])
for line in res.get("logs", []):
    print("   console:", line)
sig = res.get("sig") or {}
check(sig.get("state") == "ok" and not sig.get("bad") and sig.get("checked", 0) >= 10 and sig.get("badge") == "ok",
      "the page checks the signature with Web Crypto (%s, %s files)" % (sig.get("state"), sig.get("checked")))
check(res.get("dir") == "ltr" or res.get("dir") == "rtl", "the page sets its direction")
check(res.get("logo"), "the page carries the Biruni logo")
q = res.get("query") or {}
check(q.get("total") == 625 and q.get("read") == 2500 and not q.get("hidden"),
      "a query on a field reads the FGB over range requests: 625 of 2,500 (%s)" % q)
qn = res.get("query_num") or {}
check(qn.get("total") == 5 * 199, "a numeric query (area > 900): %s" % qn)
if res.get("map_ready"):
    idf = res.get("identify") or {}
    check(bool(idf) and "الحالة" in idf and "owner_id" not in idf, "identify reads the full feature from the FGB: %s" % idf)
else:
    print("   (no WebGL in this offscreen browser: the map itself is not drawn here; webgl=%s)" % res.get("webgl"))
pg_tamper = os.path.join(root, "page2.json")
subprocess.run([sys.executable, os.path.abspath(__file__), "--page", badurl, pg_tamper], capture_output=True, text=True,
               timeout=240)
r2 = json.load(open(pg_tamper, encoding="utf-8")) if os.path.exists(pg_tamper) else {}
check((r2.get("sig") or {}).get("badge") == "bad", "the page of a tampered copy turns red (%s)" % (r2.get("sig") or {}).get("state"))
r3p = os.path.join(root, "page3.json")
subprocess.run([sys.executable, os.path.abspath(__file__), "--page", url + "#fp=00112233445566ff", r3p], capture_output=True,
               text=True, timeout=240)
r3 = json.load(open(r3p, encoding="utf-8")) if os.path.exists(r3p) else {}
check((r3.get("sig") or {}).get("state") == "otherkey", "a link pinned to another key is flagged")

om.stop_all()
shutil.rmtree(root, ignore_errors=True)
print("ALL PASSED" if not failures else "FAILED: %d" % len(failures))
sys.exit(1 if failures else 0)
