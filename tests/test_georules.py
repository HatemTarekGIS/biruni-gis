r"""Spatial rules: learned from the data (no overlaps, a smallest area, the area field is the area, buildings inside
parcels, line ends meet), a save that breaks one does not happen, and the rules kept in the data itself - inside
the GeoPackage, beside the shapefile - come back in a new project and keep working there.

Run:  engine\bin\python-qgis.bat tests\test_georules.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.georules import core, guard  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(s=0.2):
    end = time.time() + s
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


root = tempfile.mkdtemp()
gpkg = os.path.join(root, "town.gpkg")


def write(mem, name, path=gpkg, driver="GPKG"):
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = driver
    o.layerName = name
    if driver == "GPKG" and os.path.exists(path):
        o.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    err = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), o)
    assert err[0] == QgsVectorFileWriter.WriterError.NoError, err


def poly(x, y, w, h):
    return QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + w} {y},{x + w} {y + h},{x} {y + h},{x} {y}))")


parcels = QgsVectorLayer("Polygon?crs=EPSG:32638&field=no:integer&field=area_m2:double", "Parcels", "memory")
buildings = QgsVectorLayer("Polygon?crs=EPSG:32638&field=use:string", "Buildings", "memory")
fs, bs = [], []
n = 0
for i in range(10):
    for j in range(10):
        n += 1
        f = QgsFeature(parcels.fields())
        x, y = i * 25.0, j * 25.0
        f.setGeometry(poly(x, y, 20, 20))
        f.setAttributes([n, 400.0 if n != 7 else 380.0])        # parcel 7's area written wrong
        fs.append(f)
        b = QgsFeature(buildings.fields())
        b.setGeometry(poly(x + 4, y + 4, 10, 10) if n != 12 else poly(x + 18, y + 4, 10, 10))  # 12 over the edge
        b.setAttributes(["house"])
        bs.append(b)
sl = QgsFeature(parcels.fields())
sl.setGeometry(poly(300, 0, 0.5, 4))                          # a sliver of 2 m²
sl.setAttributes([101, 2.0])
fs.append(sl)
parcels.dataProvider().addFeatures(fs)
buildings.dataProvider().addFeatures(bs)
write(parcels, "parcels")
write(buildings, "buildings")
roads = QgsVectorLayer("LineString?crs=EPSG:32638&field=name:string", "Roads", "memory")
rs = []
for k in range(11):          # a grid of streets: every end meets another street
    for wkt in (f"LINESTRING({k * 25 - 2.5} -2.5,{k * 25 - 2.5} 247.5)", f"LINESTRING(-2.5 {k * 25 - 2.5},247.5 {k * 25 - 2.5})"):
        r = QgsFeature(roads.fields())
        r.setGeometry(QgsGeometry.fromWkt(wkt))
        rs.append(r)
roads.dataProvider().addFeatures(rs)
shp = os.path.join(root, "roads.shp")
write(roads, "roads", shp, "ESRI Shapefile")

pl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
bl = QgsVectorLayer(gpkg + "|layername=buildings", "Buildings", "ogr")
rl = QgsVectorLayer(shp, "Roads", "ogr")
QgsProject.instance().addMapLayers([pl, bl, rl])

# ---- learning ----------------------------------------------------------------------------------------------
props = {p.rule["kind"]: p for p in core.learn(pl)}
check({"valid", "no_overlap", "min_area", "measure_field"} <= set(props), f"parcels: rules learned {sorted(props)}")
m = props.get("min_area")
check(m and len(m.exceptions) == 1 and 300 <= m.rule["params"]["min"] <= 400,
      f"… the smallest area from the data ({m and m.rule['params']}), the 2 m² sliver as the one breaking it")
a = props.get("measure_field")
check(a and a.rule["params"]["field"] == "area_m2" and len(a.exceptions) == 1,
      f"… «area_m2» is the area, parcel 7 written wrong ({a and len(a.exceptions)})")
check("inside" not in props, "… parcels are not inside buildings")
bprops = {p.rule["kind"]: p for p in core.learn(bl)}
ins = bprops.get("inside")
check(ins and ins.rule["params"]["layer"] == "Parcels" and len(ins.exceptions) == 1 and ins.holds == 0.99,
      "buildings: every building inside a parcel, with the one over the edge")
rprops = {p.rule["kind"]: p for p in core.learn(rl)}
check("ends_meet" in rprops and not rprops["ends_meet"].exceptions, "roads: line ends meet other lines")

# ---- keeping them in the data --------------------------------------------------------------------------------
g = guard.Guard(None)
g.install()
rules = [props["no_overlap"].rule, props["min_area"].rule, props["measure_field"].rule]
check(core.save(pl, rules) == "gpkg", "parcels' rules kept inside the GeoPackage")
check(core.save(bl, [ins.rule]) == "gpkg", "buildings' rules too")
check(core.save(rl, [rprops["ends_meet"].rule]) == "file" and os.path.exists(os.path.join(root, "roads" + core.SIDECAR)),
      "roads' rules in a file beside the shapefile")
for lyr in (pl, bl, rl):
    g.watch(lyr)
fresh = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels again", "ogr")
check(fresh.customProperty(core.PROP, "") == "" and core.load(fresh)["rules"][0]["kind"] == "no_overlap",
      "a new layer of the same file reads them from the file")
import sqlite3  # noqa: E402

con = sqlite3.connect(gpkg)
row = con.execute("SELECT data_type FROM gpkg_contents WHERE table_name='biruni_rules'").fetchone()
cnt = con.execute("SELECT count(*) FROM biruni_rules").fetchone()[0]
con.close()
check(row == ("attributes",) and cnt == 2, "… the GeoPackage holds them as a table of its own (2 layers)")


def saved(lyr, stop=False):
    """Save; True when it was written, False when the guard kept the edits back (they are back after)."""
    lyr.commitChanges(stop)
    held = g.stopped.get(lyr.id(), False)
    pump()
    return not held

# ---- the save that breaks a rule --------------------------------------------------------------------------------
pl.startEditing()
over = QgsFeature(pl.fields())
over.setGeometry(poly(10, 10, 20, 20))
over["no"] = 200
over["area_m2"] = 400.0
pl.addFeature(over)
before = pl.dataProvider().featureCount()
ok = saved(pl)
check(not ok and pl.dataProvider().featureCount() == before and pl.isModified() and pl.featureCount() == before + 1,
      "a parcel drawn over others: not saved, the edit still there")
why = [guard.explain(v) for v in g.last[pl.id()]]
check(any("overlaps" in w for w in why), f"… and why: {why[:2]}")
pump()
added = [f for f in pl.getFeatures() if f["no"] == 200][0]
pl.changeGeometry(added.id(), poly(500, 500, 20, 20))
check(saved(pl) and pl.dataProvider().featureCount() == before + 1, "moved clear of the others: saved")
pump()
f7 = [f for f in pl.getFeatures() if f["no"] == 1][0]
pl.changeAttributeValue(f7.id(), pl.fields().indexOf("area_m2"), 250.0)
check(not saved(pl) and [f for f in pl.dataProvider().getFeatures() if f["no"] == 1][0]["area_m2"] == 400.0
      and [f for f in pl.getFeatures() if f["no"] == 1][0]["area_m2"] == 250.0,
      "an area written that is not the parcel's: not saved, the value still in the edit")
pump()
check(g.save_anyway(pl) and [f for f in pl.dataProvider().getFeatures() if f["no"] == 1][0]["area_m2"] == 250.0,
      "… saved anyway when asked")
pump()
warn = [dict(r, level="warning") if r["kind"] == "measure_field" else r for r in rules]
core.save(pl, warn)
pl.changeAttributeValue(f7.id(), pl.fields().indexOf("area_m2"), 260.0)
check(saved(pl) and g.last[pl.id()] and g.last[pl.id()][0].key == "measure",
      "a rule that only warns: saved, with the warning")
pump()
tiny = QgsFeature(pl.fields())
tiny.setGeometry(poly(600, 600, 1, 1))
tiny["no"] = 300
tiny["area_m2"] = 1.0
pl.addFeature(tiny)
check(not saved(pl) and "under" in guard.explain(g.last[pl.id()][0]), "a sliver: not saved")
pl.rollBack()
pump()
bl.startEditing()
out = QgsFeature(bl.fields())
out.setGeometry(poly(-50, -50, 5, 5))
out["use"] = "kiosk"
bl.addFeature(out)
check(not saved(bl, True) and bl.isEditable() and bl.isModified() and g.last[bl.id()][0].key == "outside",
      "a building outside every parcel: not saved, even when stopping the edit session - it stays open with the edit")
bl.rollBack()
pump()
rl.startEditing()
dang = QgsFeature(rl.fields())
dang.setGeometry(QgsGeometry.fromWkt("LINESTRING(10 10,12 12)"))
rl.addFeature(dang)
check(not saved(rl) and g.last[rl.id()][0].key == "dangle", "a street that meets nothing: not saved")
rl.rollBack()
pump()
rl.startEditing()
spur = QgsFeature(rl.fields())
spur.setGeometry(QgsGeometry.fromWkt("LINESTRING(-2.5 10,-2.5 10.0,10 10,22.5 10)"))   # from one street to the next
rl.addFeature(spur)
check(saved(rl, True), "a street from one street to the next: saved")
pump()

# ---- big layer, small save ---------------------------------------------------------------------------------------
big = QgsVectorLayer("Polygon?crs=EPSG:32638&field=no:integer", "Big", "memory")
fb = []
for i in range(150):
    for j in range(150):
        f = QgsFeature(big.fields())
        f.setGeometry(poly(i * 10, j * 10, 9, 9))
        fb.append(f)
big.dataProvider().addFeatures(fb)
QgsProject.instance().addMapLayer(big)
core.save(big, [core.rule("no_overlap", min_area=0.01), core.rule("min_area", min=50)])
g.watch(big)
big.startEditing()
big.changeGeometry(5, poly(0, 40, 9, 9.5))
t0 = time.time()
ok = saved(big, True)
check(ok and time.time() - t0 < 2, f"one feature changed in 22 500: checked and saved in {time.time() - t0:.2f} s")

# ---- in a new project: the rules come with the data ---------------------------------------------------------------
QgsProject.instance().removeAllMapLayers()
g.uninstall()
QgsProject.instance().clear()
g2 = guard.Guard(None)
g2.install()
p2 = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")


def saved2(lyr):
    lyr.commitChanges(False)
    held = g2.stopped.get(lyr.id(), False)
    pump()
    return not held


r2 = QgsVectorLayer(shp, "Roads", "ogr")
QgsProject.instance().addMapLayers([p2, r2])
check(p2.id() in g2.watched and r2.id() in g2.watched, "opened again in a new project: both layers are guarded")
p2.startEditing()
ov = QgsFeature(p2.fields())
ov.setGeometry(poly(5, 5, 20, 20))
ov["no"] = 400
ov["area_m2"] = 400.0
p2.addFeature(ov)
check(not saved2(p2), "… and an overlap there is still not saved")
p2.rollBack()
pump()

# ---- the window -----------------------------------------------------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.georules import ui  # noqa: E402

canvas = QgsMapCanvas()


class Bar:
    def pushMessage(self, *a, **k):
        pass


class Iface:
    def mainWindow(self):
        return None

    def activeLayer(self):
        return b2

    def messageBar(self):
        return Bar()

    def mapCanvas(self):
        return canvas


class Plugin:
    iface = Iface()


b2 = QgsVectorLayer(gpkg + "|layername=buildings", "Buildings", "ogr")
QgsProject.instance().addMapLayer(b2)
guard._instance = g2
dlg = ui.open_ui(Plugin)
check(len(dlg.items()) == 1 and "Parcels" in dlg.items()[0].text(0),
      "the window shows the buildings' rule from the file: " + dlg.items()[0].text(0))
new = dlg.learn()
check(all(p.rule["id"] != ins.rule["id"] for p in new), f"learning again adds only new rules ({len(new)})")
n0 = len(QgsProject.instance().mapLayers())
found = dlg.check_now()
check(found and found[0].key == "outside" and len(QgsProject.instance().mapLayers()) == n0 + 1,
      "check now: the building over the edge, on the map")
check(dlg.keep() == "gpkg" and b2.id() in g2.watched, "kept from the window: in the GeoPackage, and guarded")

g2.uninstall()
QgsProject.instance().removeAllMapLayers()
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
