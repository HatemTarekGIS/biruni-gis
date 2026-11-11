"""Offline maps: PMTiles read and written in plain Python, an area cut out of an archive (from the disk or over
http range requests), the desktop map drawing a pack through MBTiles with the Arabic basemap style, packs made
of layers, and a published site that carries its own MapLibre, pmtiles.js and Arabic font (nothing from a CDN),
previewed by a server that answers range requests.

Run:  engine\\bin\\python-qgis.bat tests\\test_pmtiles.py
"""
import json
import os
import random
import re
import sys
import tempfile
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer,  # noqa: E402
                       QgsVectorTileLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from osgeo import gdal, ogr  # noqa: E402

gdal.UseExceptions()

from biruni.share import offline_maps as om  # noqa: E402
from biruni.share import pages_publish as pp  # noqa: E402
from biruni.share import pmtiles as pm  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()

# ---- tile ids ---------------------------------------------------------------------------------------
check([pm.zxy_to_id(0, 0, 0), pm.zxy_to_id(1, 0, 0), pm.zxy_to_id(1, 0, 1), pm.zxy_to_id(1, 1, 1),
       pm.zxy_to_id(1, 1, 0), pm.zxy_to_id(2, 0, 0)] == [0, 1, 2, 3, 4, 5], "tile ids as the specification counts them")
rnd = random.Random(7)
pairs = []
for _ in range(2000):
    z = rnd.randint(0, 24)
    x, y = rnd.randrange(1 << z), rnd.randrange(1 << z)
    pairs.append(pm.id_to_zxy(pm.zxy_to_id(z, x, y)) == (z, x, y))
check(all(pairs), "2,000 random tiles: id and back")

# ---- layers into a pack -----------------------------------------------------------------------------
parcels = QgsVectorLayer("Polygon?crs=EPSG:32638&field=name:string&field=area:double", "القطع", "memory")
feats = []
for i in range(12):
    for j in range(12):
        x, y = 670000 + i * 60.0, 2730000 + j * 60.0
        f = QgsFeature(parcels.fields())
        f.setAttributes([f"قطعة {i * 12 + j + 1}", 2500.0])
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 50} {y},{x + 50} {y + 50},{x} {y + 50},{x} {y}))"))
        feats.append(f)
parcels.dataProvider().addFeatures(feats)
roads = QgsVectorLayer("LineString?crs=EPSG:32638&field=name:string", "Roads", "memory")
rf = QgsFeature(roads.fields())
rf.setAttributes(["طريق الملك فهد"])
rf.setGeometry(QgsGeometry.fromWkt("LINESTRING(669990 2729990,670700 2730700)"))
roads.dataProvider().addFeatures([rf])
for l in (parcels, roads):
    QgsProject.instance().addMapLayer(l)
from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling  # noqa: E402

pal = QgsPalLayerSettings()
pal.fieldName = "name"
parcels.setLabeling(QgsVectorLayerSimpleLabeling(pal))
parcels.setLabelsEnabled(True)

pack = os.path.join(root, "pack.pmtiles")
names = om.make_pack([parcels, roads], pack, 0, 16)
h = pm.info(pack)
check(set(names.values()) == {"القطع", "Roads"} and h["tile_type"] == 1 and h["min_zoom"] == 0 and h["max_zoom"] == 16,
      f"two layers into one pack, zooms 0-16 ({names}, {h['tile_type_name']})")
ds = ogr.Open(pack)
layer_names = sorted(ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount()))
n = sum(ds.GetLayer(i).GetFeatureCount() for i in range(ds.GetLayerCount()))
ds = None
check(layer_names == sorted(["القطع", "Roads"]) and n >= 145, f"the pack reads back: {layer_names}, {n} features")
w, s, e, nn = h["bounds"]
check(46.0 < w < 47.0 and 24.0 < s < 25.0 and e > w and nn > s, f"its bounds are the data's ({h['bounds']})")

tiles = pm.read_all(pack)
check(len(tiles) >= 17 and all(pm.id_to_zxy(pm.zxy_to_id(*k)) == k for k in tiles),
      f"every tile found through the directories ({len(tiles)} tiles)")

# ---- an area cut out ----------------------------------------------------------------------------------
cx, cy = (w + e) / 2, (s + nn) / 2
half = (w, s, cx, cy)
cut = os.path.join(root, "cut.pmtiles")
ch = pm.extract(pack, cut, half, 0, 14)
got = pm.read_all(cut)
want = {}
for z in range(0, 15):
    x0, y0, x1, y1 = pm.tiles_in(half, z)
    for k, v in tiles.items():
        if k[0] == z and x0 <= k[1] <= x1 and y0 <= k[2] <= y1:
            want[k] = v
check(got == want and ch["max_zoom"] <= 14 and len(got) < len(tiles),
      f"the area cut out: exactly its tiles, byte for byte ({len(got)} of {len(tiles)})")
ds = ogr.Open(cut)
check(ds is not None and ds.GetLayerCount() >= 1, "the cut archive opens in GDAL (a valid PMTiles file)")
ds = None

big = {}
for z in (9, 10):
    for x in range(1 << z):
        for y in range(0, 1 << z, 3):
            if len(big) >= 60000:
                break
            big[(z, x, y)] = rnd.randbytes(rnd.randint(1, 300))
bigp = os.path.join(root, "big.pmtiles")
bh = pm.write(bigp, big, tile_type=2, tile_compression=1)
r = pm.Reader(bigp)
sample = rnd.sample(sorted(big), 300)
check(bh["leaf_length"] > 0 and all(r.tile(*k) == big[k] for k in sample) and r.tile(9, 0, 1) is None,
      f"60,000 tiles: the root points to leaf directories, every tile found ({bh['leaf_length']} bytes of leaves)")
r.close()
dup = {(5, x, 0): b"same" for x in range(8)}
dh = pm.write(os.path.join(root, "dup.pmtiles"), dup, tile_type=2, tile_compression=1)
check(dh["tile_contents"] == 1 and dh["addressed_tiles"] == 8, "identical tiles kept once")

# ---- the desktop map ------------------------------------------------------------------------------------
st = om.basemap_style("desktop")
fonts = {json.dumps(l["layout"]["text-font"]) for l in st["layers"] if "text-font" in l.get("layout", {})}
icons = [l["id"] for l in st["layers"] if any(k.startswith("icon-") for k in l.get("layout", {}))]
check(fonts == {json.dumps([om.DESKTOP_FONT])} and not icons and "sprite" not in st and "glyphs" not in st,
      "the basemap style for the desktop: the Arabic font, no sprite, no glyph server")
txt = json.dumps(st, ensure_ascii=False)
check("name:ar" in txt, "its labels read the Arabic names (name:ar)")
mbt = om.mbtiles_for(pack)
vt = QgsVectorTileLayer(f"type=mbtiles&url={mbt}", "pack")
check(os.path.exists(mbt) and vt.isValid() and vt.sourceMaxZoom() == 16, f"the pack as MBTiles draws as vector tiles "
      f"(zoom {vt.sourceMinZoom()}-{vt.sourceMaxZoom()})")
mt = os.path.getmtime(mbt)
check(om.mbtiles_for(pack) == mbt and os.path.getmtime(mbt) == mt, "the MBTiles copy is made once")
warn = om.style_layer(vt)
check(vt.renderer() is not None and vt.labeling() is not None and len(vt.renderer().styles()) > 20,
      f"styled with the basemap style ({len(vt.renderer().styles())} styles, {len(warn)} converter notes)")
n0 = len(QgsProject.instance().mapLayers())
bl = om.add_basemap(pack, name="Basemap")
tree = QgsProject.instance().layerTreeRoot().children()
check(len(QgsProject.instance().mapLayers()) == n0 + 1 and tree[-1].layer() is bl and om.basemap_packs() == [pack],
      "added under the other layers, and known as a basemap pack")

# ---- the preview server ---------------------------------------------------------------------------------
site = os.path.join(root, "site")
res = pp.publish_site([parcels, roads], site, "موقع", mode="tiles", basemap_pack=pack, max_zoom=14)
url = om.serve(site)
req = urllib.request.Request(url + "data/basemap.pmtiles", headers={"Range": "bytes=0-126"})
with urllib.request.urlopen(req, timeout=10) as resp:
    head = resp.read()
    status, crange = resp.status, resp.headers.get("Content-Range")
check(status == 206 and len(head) == 127 and pm.parse_header(head)["tile_type"] == 1 and crange.startswith("bytes 0-126/"),
      f"the preview answers a range request: 206, {crange}")
with urllib.request.urlopen(url + "lib/maplibre-gl.mjs", timeout=10) as resp:
    mjs_type = resp.headers.get("Content-Type")
check(mjs_type.startswith("text/javascript"), f"the module is served as JavaScript ({mjs_type})")
remote = pm.Reader(url + "data/" + [p for p, _ in res["files"] if p.startswith("data/") and "basemap" not in p][0].split("/", 1)[1])
local = pm.Reader(os.path.join(site, [p for p, _ in res["files"] if p.startswith("data/") and "basemap" not in p][0]))
some = list(local.entries())[:20]
same = all(remote.src.read(remote.header["data_offset"] + off, ln) ==
           local.src.read(local.header["data_offset"] + off, ln) for _t, off, ln in some)
check(same and remote.src.requests >= 2, f"an archive read over http, in pieces ({remote.src.requests} requests)")
remote.close()
local.close()
rc = os.path.join(root, "remote-cut.pmtiles")
pm.extract(url + "data/basemap.pmtiles", rc, half, 0, 14)
lc = os.path.join(root, "local-cut.pmtiles")
pm.extract(os.path.join(site, "data", "basemap.pmtiles"), lc, half, 0, 14)
check(pm.read_all(rc) == pm.read_all(lc), "cutting over http gives the same tiles as from the disk")

# ---- the site ---------------------------------------------------------------------------------------------
page = open(os.path.join(site, "index.html"), encoding="utf-8").read()
refs = re.findall(r'(?:src|href)="([^"]+)"', page) + re.findall(r'import [^;]* from "([^"]+)"', page)
check(res["mode"] == "tiles" and refs and all(not r.startswith(("http:", "https:", "//")) for r in refs)
      and "unpkg" not in page and "cdn" not in page.lower(), f"the page loads only local files: {refs}")
libs = ["lib/maplibre-gl.mjs", "lib/maplibre-gl-worker.mjs", "lib/maplibre-gl.css", "lib/pmtiles.js",
        "lib/fonts/NotoSansArabic-Regular.ttf", "lib/fonts/OFL.txt", "lib/LICENSE-maplibre.txt",
        "lib/LICENSE-pmtiles.txt", "lib/LICENSE-basemap-style.txt", "data/basemap.pmtiles"]
check(all(os.path.exists(os.path.join(site, *p.split("/"))) for p in libs), "MapLibre, its worker, pmtiles.js, the "
      "Arabic font, the licences and the basemap are in the site")
m = re.search(r"const D = (\{.*?\});\n", page, re.S)
D = json.loads(m.group(1).replace("<\\/", "</")) if m else {}
labels = [l for L in D.get("layers", []) for l in L["layers"] if l["type"] == "symbol"]
check(labels and all(l["layout"]["text-font"] == [om.FONT_NAME] and l["layout"]["text-field"] == ["get", "_l"]
                     for l in labels), f"labels drawn in the bundled Arabic font ({len(labels)} label layers)")
ff = D.get("fontFaces", {}).get(om.FONT_NAME, [{}])[0]
check(ff.get("url") == "lib/fonts/NotoSansArabic-Regular.ttf" and "U+0600-06FF" in ff.get("unicode-range", []),
      "the font is declared for the Arabic blocks")
bm = D.get("basemap") or {}
check(bm.get("file") == "data/basemap.pmtiles" and all(l.get("source", "basemap") == "basemap" for l in bm["layers"])
      and "OpenStreetMap" in bm.get("attribution", "") and D.get("online") is False,
      "the basemap is in the site, with the OpenStreetMap attribution, and nothing is fetched online")
ds = ogr.Open(os.path.join(site, D["layers"][0]["file"]))
lyr0 = ds.GetLayer(0)
lyr0.SetNextByIndex(0)
f0 = lyr0.GetNextFeature()
check(f0 is not None and f0.GetField("_l") and f0.GetField("_l").startswith("قطعة"), f"the labels are in the tiles "
      f"({f0.GetField('_l') if f0 else None})")
ds = None
web = om.basemap_style("web")
check(web.get("font-faces", {}).get(om.FONT_NAME) and all("id" in l and "type" in l for l in web["layers"])
      and all(l.get("layout", {}).get("text-font", [om.FONT_NAME]) == [om.FONT_NAME] for l in web["layers"]),
      f"the web basemap style is whole ({len(web['layers'])} layers, the Arabic font everywhere)")
plain = os.path.join(root, "site2")
r2 = pp.publish_site([parcels], plain, "Tiles", mode="tiles")
p2 = open(os.path.join(plain, "index.html"), encoding="utf-8").read()
D2 = json.loads(re.search(r"const D = (\{.*?\});\n", p2, re.S).group(1))
check(D2["basemap"] is None and D2["online"] is True and not os.path.exists(os.path.join(plain, "data", "basemap.pmtiles")),
      "without a pack: the online OpenStreetMap tiles as before, no basemap file")
# ---- the buttons (an archive on the preview server stands in for the planet build) ------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class Bar:
    def __init__(self):
        self.said = []

    def pushMessage(self, title, text, *a, **k):
        self.said.append(text)


class Tree:
    def selectedLayers(self):
        return [parcels]


class Iface:
    def __init__(self):
        self.w, self.bar = QWidget(), Bar()

    def mainWindow(self):
        return self.w

    def messageBar(self):
        return self.bar

    def layerTreeView(self):
        return Tree()


class Plugin:
    iface = Iface()


from biruni.share import offline_maps_ui as ui  # noqa: E402

plug = Plugin()
dlg = ui.basemap_ui(plug)
dlg.url.setText(url + "data/basemap.pmtiles")
dlg.maxz.setValue(14)
p = dlg.measure(half)
check(p is not None and len(p.tiles) == len(got) and "MB" in dlg.out.text(),
      f"how big an area is, before downloading: {dlg.out.text()}")
n1 = len(QgsProject.instance().mapLayers())
lyr = dlg.download(os.path.join(root, "downloaded"), half)
check(lyr is not None and os.path.exists(os.path.join(root, "downloaded.pmtiles"))
      and len(QgsProject.instance().mapLayers()) == n1 + 1, "the area downloaded and added: " + dlg.out.text()[:60])
dlg.url.setText(url + "nothing.pmtiles")
check(dlg.measure(half) is None and "404" in dlg.out.text(), "a wrong address: said so (" + dlg.out.text() + ")")
check(dlg.open_file(os.path.join(site, "index.html")) is None and "PMTiles" in dlg.out.text(),
      "not a PMTiles file: said so")
pk = ui.pack_ui(plug)
names2 = pk.make(os.path.join(root, "from-dialog"))
check(names2 == {"القطع": "القطع"} and "Saved" in pk.out.text(), "a pack from the chosen layers: " + pk.out.text())
check(ui.preview_ui(plug, site, open_browser=False) == url and url in plug.iface.bar.said[-1],
      "the preview of a site: its address on this computer")
om.stop_all()

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
QgsProject.instance().clear()
app.exitQgis()
sys.exit(1 if failures else 0)
