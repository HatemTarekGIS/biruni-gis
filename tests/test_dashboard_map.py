"""The dashboard's map (map v2): several layers, their styles (one colour, a colour per value, shades by a number,
the layer's own QGIS renderer), labels, popup fields and cross-filter keys - with the card's zone and hidden
fields still kept; explain of one feature; the basemap the PC draws for a viewer's view; the builder's map
template and map panel.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_map.py
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsFeature, QgsField, QgsFillSymbol,  # noqa: E402
                       QgsGeometry, QgsProject, QgsRasterLayer, QgsRendererCategory, QgsVectorLayer)
from qgis.PyQt.QtCore import QEventLoop, QMetaType, QTimer  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.dashboard import basemap, builder, cards, data, mapdata, model, seals, service  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


cards._TEST_SEED[:] = [bytes(range(32))]
seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
prj = QgsProject.instance()

# ---- a small neighbourhood: 4 districts, 64 parcels, 2 roads ------------------------------------------------------
parcels = QgsVectorLayer("Polygon?crs=EPSG:4326", "Parcels", "memory")
pr = parcels.dataProvider()
pr.addAttributes([QgsField("no", QMetaType.Type.QString), QgsField("district", QMetaType.Type.QString),
                  QgsField("status", QMetaType.Type.QString), QgsField("area", QMetaType.Type.Double),
                  QgsField("owner", QMetaType.Type.QString)])
parcels.updateFields()
DIST = ["North", "South", "East", "West"]
STAT = ["done", "open", "review", "approved"]
feats = []
for i in range(64):
    x, y = 46.70 + (i % 8) * 0.001, 24.70 + (i // 8) * 0.001
    f = QgsFeature(parcels.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + .0009} {y},{x + .0009} {y + .0009},{x} {y + .0009},{x} {y}))"))
    f.setAttributes([f"P-{i}", DIST[((i // 8) // 4) * 2 + (i % 8) // 4], STAT[i % 4], 400.0 + i * 10,
                     f"SECRET-OWNER-{i}"])
    feats.append(f)
pr.addFeatures(feats)
roads = QgsVectorLayer("LineString?crs=EPSG:4326&field=name:string", "Roads", "memory")
rf = []
for n, wkt in (("Main road", "LINESTRING(46.6995 24.704,46.7085 24.704)"),
               ("Cross street", "LINESTRING(46.704 24.6995,46.704 24.7085)")):
    f = QgsFeature(roads.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    f.setAttributes([n])
    rf.append(f)
roads.dataProvider().addFeatures(rf)
# the parcels' own QGIS style: categorized by status
cats = [QgsRendererCategory(v, QgsFillSymbol.createSimple({"color": c, "outline_color": "#222222"}), v)
        for v, c in (("done", "#2e7d32"), ("open", "#c62828"), ("review", "#f9a825"), ("approved", "#1565c0"))]
parcels.setRenderer(QgsCategorizedSymbolRenderer("status", cats))
prj.addMapLayers([parcels, roads])

D = model.blank("Map")
D.update({"id": "d_map", "sources": {"s1": {"layer": parcels.id(), "filter": ""}, "s2": {"layer": roads.id(),
                                                                                         "filter": ""}},
          "filters": [{"id": "f1", "source": "s1", "field": "district", "label": "District"}],
          "widgets": [
              {"id": "m", "type": "map", "title": "Map", "x": 0, "y": 0, "w": 8, "h": 8,
               "options": {"basemap": "project", "layers": [
                   {"source": "s1", "name": "Parcels", "style": {"kind": "layer"}, "label": "no",
                    "popup": ["status", "area", "owner"]},
                   {"source": "s2", "name": "Roads", "style": {"kind": "single", "color": "#ffffff", "width": 4},
                    "label": "name"}]}},
              {"id": "m2", "type": "map", "title": "By status", "x": 0, "y": 8, "w": 6, "h": 6,
               "options": {"layers": [{"source": "s1", "style": {"kind": "categories", "field": "status"}}]}},
              {"id": "m3", "type": "map", "title": "By area", "x": 6, "y": 8, "w": 6, "h": 6,
               "options": {"layers": [{"source": "s1", "style": {"kind": "graduated", "field": "area",
                                                                  "classes": 4}}]}},
              {"id": "m4", "type": "map", "title": "Owners", "x": 0, "y": 14, "w": 6, "h": 6,
               "options": {"layers": [{"source": "s1", "style": {"kind": "categories", "field": "owner"},
                                       "label": "owner", "popup": ["owner", "no"]}]}},
              {"id": "b", "type": "bar", "title": "By district", "x": 8, "y": 0, "w": 4, "h": 4, "source": "s1",
               "measure": {"op": "count"}, "group": {"field": "district"}},
              {"id": "p", "type": "pie", "title": "By status", "x": 8, "y": 4, "w": 4, "h": 4, "source": "s1",
               "measure": {"op": "count"}, "group": {"field": "status"}},
              {"id": "flt", "type": "filter", "title": "District", "x": 8, "y": 8, "w": 4, "h": 2,
               "options": {"filter": "f1"}},
          ]})
probs = model.validate(D)
check(not probs, f"a map with several layers is a valid definition {probs}")
bad = json.loads(json.dumps(D))
bad["widgets"][0]["options"]["layers"][0]["source"] = "nope"
check(any("unknown source" in p for p in model.validate(bad)), "a map layer of an unknown source is a problem")
bad["widgets"][0]["options"]["layers"][0] = {"source": "s1", "style": {"kind": "categories"}}
check(any("needs a field" in p for p in model.validate(bad)), "a categories style needs a field")
model.save(prj, D)

comp = data.Computer(D)
res = comp.compute()
check(not res["errors"], f"no widget failed {res['errors']}")
w = res["widgets"]
m = w["m"]
check([L["kind"] for L in m["layers"]] == ["fill", "line"] and m["basemap"] is True and m["total"] == 66,
      "the map has a fill and a line layer, its basemap flag and the total")
pf = m["layers"][0]["geojson"]["features"]
check(len(pf) == 64 and all(f["geometry"]["type"] == "Polygon" for f in pf), "every parcel as a polygon in 4326")
qcol = {"done": "#2e7d32", "open": "#c62828", "review": "#f9a825", "approved": "#1565c0"}
by_no = {f["properties"]["_l"]: f["properties"] for f in pf}
check(all(by_no[f"P-{i}"]["_c"] == qcol[STAT[i % 4]] for i in range(64)),
      "style 'layer': every parcel has the colour QGIS gives it with the layer's own renderer")
check(all(by_no["P-0"].get("_o") == "#222222" for _ in [0]), "... and its outline colour")
check(sorted(x["color"] for x in m["layers"][0]["legend"]) == sorted(qcol.values()),
      "the legend is the renderer's legend")
check(set(by_no["P-5"]) >= {"id", "_c", "_l", "_k", "status", "area", "owner"} and
      by_no["P-5"]["_k"] == {"district": by_no["P-5"]["_k"]["district"], "status": "open"},
      f"a parcel carries its label, popup fields and the keys the bars/pie/filter group by ({by_no['P-5']['_k']})")
check(m["layers"][0]["popup"][0] == {"key": "status", "label": "status"} and m["layers"][0]["label"] is True,
      "the layer says which popup fields and that it has labels")
rd = m["layers"][1]
check(rd["style"]["color"] == "#ffffff" and rd["style"]["width"] == 4 and
      {f["properties"]["_l"] for f in rd["geojson"]["features"]} == {"Main road", "Cross street"},
      "the roads: one colour, their width, their names as labels")
m2 = w["m2"]["layers"][0]
leg = {x["key"]: x["color"] for x in m2["legend"]}
check(len(leg) == 4 and all(f["properties"]["_c"] == leg[f["properties"]["_k"]["status"]]
                            for f in m2["geojson"]["features"]), "style 'categories': one colour per status")
m3 = w["m3"]["layers"][0]
areas = {f["properties"]["_c"] for f in m3["geojson"]["features"]}
check(len(m3["legend"]) == 4 and len(areas) == 4 and "–" in m3["legend"][0]["label"],
      "style 'graduated': 4 classes of area, 4 shades, ranges in the legend")
check(w["b"].get("xf") == "district" and w["p"].get("xf") == "status" and w["flt"].get("xf") == "district",
      "bars, pies and filters say which field their items are (for the cross-filter)")

# ---- a card: a zone and hidden fields ----------------------------------------------------------------------------
zone = "POLYGON((46.6995 24.6995,46.7035 24.6995,46.7035 24.7085,46.6995 24.7085,46.6995 24.6995))"
card = cards.parse(cards.issue("d_map", "West half", hide=["owner"], zone_wkt=zone, project=prj), project=prj)
cres = comp.compute(card=card)
cw = cres["widgets"]
txt = json.dumps(cw, ensure_ascii=False)
check("SECRET-OWNER" not in txt, "a hidden field never leaves the PC: not as a popup field, a label or a colour")
cm = cw["m"]["layers"][0]
check(len(cm["geojson"]["features"]) == 32 and all("owner" not in f["properties"] for f in cm["geojson"]["features"]),
      f"the card's zone cuts the parcels (32 of 64) ({len(cm['geojson']['features'])})")
check([p["key"] for p in cm["popup"]] == ["status", "area"], "the hidden field is dropped from the popup list")
m4 = cw["m4"]["layers"][0]
check(m4["legend"] == [{"label": m4["name"], "color": m4["style"]["color"]}] and m4["label"] is False and
      len({f["properties"]["_c"] for f in m4["geojson"]["features"]}) == 1,
      "a style on a hidden field falls back to one colour (it says nothing about the field)")
ren_hidden = cards.parse(cards.issue("d_map", "No status", hide=["status"], project=prj), project=prj)
rh = comp.compute(card=ren_hidden)["widgets"]
check(len({f["properties"]["_c"] for f in rh["m"]["layers"][0]["geojson"]["features"]}) == 1 and
      "status" not in json.dumps(rh["m"]["layers"][0]["popup"]),
      "the layer's own renderer reads a hidden field: one colour instead")

# ---- explain one feature -------------------------------------------------------------------------------------------
fid = pf[3]["id"]
ex = comp.explain("m", f"L0:{fid}", card=None)
check(ex["count"] == 1 and ex["features"][0]["label"] == pf[3]["properties"]["_l"] and
      {x["label"] for x in ex["features"][0]["fields"]} == {"status", "area", "owner"} and len(ex["bbox"]) == 4,
      "what is behind one parcel: its label, fields and extent")
out_fid = next(f["id"] for f in pf if f["id"] not in {x["id"] for x in cm["geojson"]["features"]})
try:
    comp.explain("m", f"L0:{out_fid}", card=card)
    check(False, "a parcel outside the card's zone cannot be explained")
except data.DataError:
    check(True, "a parcel outside the card's zone cannot be explained")
ex2 = comp.explain("m", f"L0:{cm['geojson']['features'][0]['id']}", card=card)
check("SECRET-OWNER" not in json.dumps(ex2) and all(x["label"] != "owner" for x in ex2["features"][0]["fields"]),
      "explain keeps the hidden field back")

# ---- an edit: only the touched parcel changes ------------------------------------------------------------------------
parcels.startEditing()
target = pf[7]["id"]
parcels.changeAttributeValue(target, parcels.fields().indexOf("status"), "done")
parcels.commitChanges()
comp.invalidate(parcels.id(), [target])
after = {f["id"]: f["properties"]["_c"] for f in comp.compute()["widgets"]["m"]["layers"][0]["geojson"]["features"]}
check(after[target] == qcol["done"], "after an edit the parcel takes its new colour")

# ---- the basemap ---------------------------------------------------------------------------------------------------------
check(basemap.view_args({"bbox": [46.69, 24.69, 46.71, 24.71], "w": 800, "h": 600, "d": 2}) ==
      {"bbox": [46.69, 24.69, 46.71, 24.71], "pw": 1600, "ph": 1200}, "a view: its bbox and pixel size")
check(basemap.view_args({"bbox": [46.69, 24.69, 46.71, 24.71], "w": 3000, "h": 1500, "d": 2})["pw"] == 1600,
      "the image is never larger than 1600 px")
check(basemap.view_args({"bbox": [46.71, 24.69, 46.69, 24.71], "w": 800, "h": 600}) is None and
      basemap.view_args({"bbox": "x", "w": 800, "h": 600}) is None, "a bad view is refused")
check(basemap.clamp_to_zone([46.69, 24.69, 46.71, 24.71], zone)[2] < 46.71 and
      basemap.clamp_to_zone([10, 10, 11, 11], zone) is None, "the basemap stays near the card's zone")

# a raster basemap: a small GeoTIFF made here
from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

tif = os.path.join(tempfile.mkdtemp(), "ortho.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, 64, 64, 3, gdal.GDT_Byte)
ds.SetGeoTransform([46.695, 0.0002, 0, 24.712, 0, -0.0002])
from osgeo import osr  # noqa: E402

sr = osr.SpatialReference()
sr.ImportFromEPSG(4326)
ds.SetProjection(sr.ExportToWkt())
for b, v in ((1, 200), (2, 180), (3, 140)):
    ds.GetRasterBand(b).Fill(v)
ds = None
ortho = QgsRasterLayer(tif, "Ortho")
prj.addMapLayer(ortho)
check([x.name() for x in basemap.basemap_layers(prj)] == ["Ortho"],
      "only raster and vector-tile layers make the basemap (never a vector layer)")

svc = service.DashboardService(prj)
got = []
sess = svc.connect(got.append)
sess.receive({"t": "hello", "card": cards.issue("d_map", "All", project=prj)})


def wait_for(cond, ms=8000):
    loop = QEventLoop()
    t = QTimer()
    t.setInterval(50)
    t.timeout.connect(lambda: loop.quit() if cond() else None)
    t.start()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    t.stop()
    return cond()


sess.receive({"t": "basemap", "widget": "m", "bbox": [46.698, 24.698, 46.71, 24.71], "w": 400, "h": 300, "d": 1})
ok = wait_for(lambda: any(x.get("t") == "basemap" for x in got))
bm = next((x for x in got if x.get("t") == "basemap"), {})
check(ok and str(bm.get("img", "")).startswith("data:image/jpeg;base64,") and len(bm["bbox"]) == 4,
      "the PC draws the basemap for the view and sends it as a JPEG")
got.clear()
sess.receive({"t": "basemap", "widget": "m2", "bbox": [46.698, 24.698, 46.71, 24.71], "w": 400, "h": 300})
check(any(x.get("t") == "error" and x.get("why") == "no basemap" for x in got),
      "a map without the basemap option gets none")
got.clear()
sess.receive({"t": "basemap", "widget": "m", "bbox": [1, 2], "w": 400, "h": 300})
check(any(x.get("why") == "bad view" for x in got), "a bad view is answered as such")
# a viewer whose card is limited to a zone: the image is cut near it
got.clear()
zs = svc.connect(got.append)
zs.receive({"t": "hello", "card": cards.issue("d_map", "Zone", zone_wkt=zone, project=prj)})
zs.receive({"t": "basemap", "widget": "m", "bbox": [46.60, 24.60, 46.80, 24.80], "w": 400, "h": 400})
wait_for(lambda: any(x.get("t") == "basemap" for x in got))
zb = next((x for x in got if x.get("t") == "basemap"), {})
check(zb and zb["bbox"][0] > 46.69 and zb["bbox"][2] < 46.71, f"a zone card's basemap stays near its zone {zb.get('bbox')}")
# explain over the service
got.clear()
sess.receive({"t": "explain", "widget": "m", "key": f"L0:{fid}"})
check(any(x.get("t") == "explain" and x.get("count") == 1 for x in got), "explain a map feature over the service")
svc.stop()

# ---- the builder: the map template and the map panel ---------------------------------------------------------------
t = builder.template("Map dashboard")
probs = model.validate(t)
mw = next(x for x in t["widgets"] if x["type"] == "map")
check(not probs and mw["w"] == 8 and mw["h"] == 8 and mw["options"]["basemap"] == "project" and
      mw["options"]["layers"], f"the map dashboard template: a large map with its layers and the basemap {probs}")
res = data.Computer(t).compute()
check(not res["errors"], f"the map dashboard template computes {res['errors']}")
check("Map dashboard" in builder.TEMPLATES, "the template is in the list")
legacy = {"id": "w1", "type": "map", "source": "s1", "style": {"by": "status"}, "options": {"fields": ["status"]}}
dd = {"sources": {"s1": {"layer": parcels.id(), "filter": ""}}, "widgets": [legacy]}
ls = builder.map_layers_of(dd, legacy)
check(ls == [{"source": "s1", "name": "Parcels", "style": {"kind": "categories", "field": "status"}, "label": None,
              "popup": ["status"]}], "a first-shape map opens in the builder as one map layer")
dlg = builder.BuilderDialog(json.loads(json.dumps(t)), preview=False)
mid = next(x["id"] for x in dlg.d["widgets"] if x["type"] == "map")
dlg._select(mid)
pn = dlg.panel
check(pn.map_box.isVisibleTo(dlg) and not pn.data_box.isVisibleTo(dlg), "the map panel shows the map's layers")
n0 = len(pn._map_entries())
pn._map_add()
check(len(pn._map_entries()) == n0 + 1, "a layer is added to the map")
pn.ml_kind.setCurrentIndex(pn.ml_kind.findData("graduated"))
e = pn._map_entries()[pn._map_index()]
check(e["style"]["kind"] == "graduated", "the style kind is kept")
pn._map_remove()
check(len(pn._map_entries()) == n0, "a layer is removed")
pn.ml_basemap.setChecked(False)
mw2 = next(x for x in dlg.d["widgets"] if x["type"] == "map")
check(mw2["options"]["basemap"] == "none" and not model.validate(dlg.d), "the basemap switch is kept; still valid")
dlg.done(0)

print()
print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
for f in failures:
    print("  FAIL", f)
app.exitQgis()
sys.exit(1 if failures else 0)
