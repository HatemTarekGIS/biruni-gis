r"""Tests for the four-colour map, the map sheet scale bars, autosave and light mode.

Run:  engine\bin\python-qgis.bat tests\test_autosave_perf.py
"""
import datetime as dt
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsSettings, QgsVectorFileWriter,
                       QgsVectorLayer, Qgis)

SETTINGS_DIR = tempfile.mkdtemp()  # a profile of its own: the settings this test writes stay out of others
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import autosave, mapsheet, performance  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.four_colors import FIELD, _Styler, coloring_renderer  # noqa: E402
from biruni.history import ProjectHistory, list_versions  # noqa: E402
from biruni.project import create_folder_structure  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


check(os.path.normcase(os.path.normpath(QgsSettings().fileName())).startswith(
    os.path.normcase(os.path.normpath(SETTINGS_DIR))), f"settings go to a temporary profile ({QgsSettings().fileName()})")
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
TMP = tempfile.mkdtemp()

# ---- four colours ------------------------------------------------------------------
grid = QgsVectorLayer("Polygon?crs=EPSG:32637&field=name:string", "blocks", "memory")
feats = []
for i in range(5):
    for j in range(5):
        x, y = 500000 + i * 100, 2700000 + j * 100
        f = QgsFeature(grid.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x+100} {y},{x+100} {y+100},{x} {y+100},{x} {y}))"))
        f.setAttributes([f"b{i}{j}"])
        feats.append(f)
grid.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(grid)
res = processing.run("biruni:fourcolors", {"INPUT": grid, "MIN_COLORS": 4, "BALANCE": 0,
                                            "OUTPUT": "memory:"}, context=ctx, feedback=fb)
out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
polys = [(f[FIELD], f.geometry()) for f in out.getFeatures()]
clash = 0
neighbours = 0
for a in range(len(polys)):
    for b in range(a + 1, len(polys)):
        if polys[a][1].intersection(polys[b][1]).length() > 0:  # a shared border, not just a corner
            neighbours += 1
            clash += polys[a][0] == polys[b][0]
check(len(polys) == 25 and neighbours == 40 and clash == 0,
      f"four colours: {neighbours} bordering pairs, none share a colour")
check(1 <= res["COLOR_COUNT"] <= 4, f"at most 4 colours used ({res['COLOR_COUNT']})")
check(out.fields().indexOf("name") >= 0 and all(p[0] for p in polys), "attributes kept, every polygon coloured")

# a harder map: the wheel (a ring of 7 cells around a hub) and random Voronoi cells
from biruni.algorithms.four_colors import color_graph  # noqa: E402

wheel = {0: {1, 2, 3, 4, 5, 6, 7}}
for i in range(1, 8):
    wheel[i] = {0, 1 + i % 7, 1 + (i - 2) % 7}
cw = color_graph(wheel, 4)
check(max(cw.values()) < 4 and all(cw[u] != cw[v] for u in wheel for v in wheel[u]), "odd wheel in 4 colours")
import random  # noqa: E402

random.seed(7)
from qgis.core import QgsPointXY  # noqa: E402

pts = QgsGeometry.fromMultiPointXY([QgsPointXY(random.uniform(0, 1000), random.uniform(0, 1000))
                                    for _ in range(400)])
cells = pts.voronoiDiagram()
vor = QgsVectorLayer("Polygon?crs=EPSG:32637", "cells", "memory")
vf = []
for part in cells.asGeometryCollection():
    q = QgsFeature(vor.fields())
    q.setGeometry(part)
    vf.append(q)
vor.dataProvider().addFeatures(vf)
res_v = processing.run("biruni:fourcolors", {"INPUT": vor, "MIN_COLORS": 4, "BALANCE": 1, "OUTPUT": "memory:"},
                       context=ctx, feedback=fb)
vout = ctx.getMapLayer(res_v["OUTPUT"]) if isinstance(res_v["OUTPUT"], str) else res_v["OUTPUT"]
vg = [(f[FIELD], f.geometry()) for f in vout.getFeatures()]
bad = sum(1 for a in range(len(vg)) for b in range(a + 1, len(vg))
          if vg[a][0] == vg[b][0] and vg[a][1].intersection(vg[b][1]).length() > 1e-6)
check(len(vg) == 400 and bad == 0 and res_v["COLOR_COUNT"] <= 4,
      f"400 Voronoi cells, balanced by area: {res_v['COLOR_COUNT']} colours, {bad} clashes")

_Styler({p[0] for p in polys}).postProcessLayer(out, ctx, fb)
r = out.renderer()
check(r.type() == "categorizedSymbol" and r.classAttribute() == FIELD and
      len(r.categories()) == res["COLOR_COUNT"], "categorized style with one soft colour per colour id")
r_dark = coloring_renderer({1, 2}, dark=True)
r_light = coloring_renderer({1, 2}, dark=False)
cat_dark, cat_light = r_dark.categories(), r_light.categories()  # keep the copies alive
sym_dark, sym_light = cat_dark[0].symbol(), cat_light[0].symbol()
check(sym_dark.symbolLayer(0).strokeColor().name() != sym_light.symbolLayer(0).strokeColor().name(),
      "outlines follow the light / dark theme")

# ---- map sheet scale bars ----------------------------------------------------------
prj = QgsProject.instance()
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:32637"))
for style in ("single", "dual", "fixed"):
    opt = mapsheet.SheetOptions(title=f"Bars {style}", scalebar=style, scalebar_segment=100 if style == "fixed" else 0,
                                rtl=False, extent=grid.extent(), scale=2500)
    layout = mapsheet.build_layout(prj, opt)
    metric = layout.itemById("biruni-scalebar")
    imperial = layout.itemById("biruni-scalebar-imperial")
    if style == "single":
        check(metric is not None and imperial is None and metric.units() == Qgis.DistanceUnit.Meters,
              "single scale bar")
    elif style == "dual":
        check(metric is not None and imperial is not None and
              imperial.units() == Qgis.DistanceUnit.Feet and metric.units() == Qgis.DistanceUnit.Meters,
              "dual scale bar: metres and feet")
    else:
        check(metric is not None and metric.segmentSizeMode() == Qgis.ScaleBarSegmentSizeMode.Fixed and
              abs(metric.unitsPerSegment() - 100) < 1e-9 and metric.numberOfSegments() >= 1,
              f"fixed scale bar: 100 m segments x {metric.numberOfSegments() if metric else 0}")
    png = os.path.join(TMP, f"bars_{style}.png")
    check(mapsheet.export(layout, png) and os.path.getsize(png) > 10000, f"{style} sheet exported")
check(mapsheet.fixed_segment_length(2500, 50) in (25, 50), "automatic round segment length")
fixed_auto = mapsheet.build_layout(prj, mapsheet.SheetOptions(title="auto", scalebar="fixed", rtl=False,
                                                              extent=grid.extent(), scale=2500))
sb = fixed_auto.itemById("biruni-scalebar")
check(sb.segmentSizeMode() == Qgis.ScaleBarSegmentSizeMode.Fixed and sb.unitsPerSegment() > 0,
      f"fixed scale bar with a round value ({sb.unitsPerSegment()} m)")


# ---- autosave ---------------------------------------------------------------------
class Bar:
    def pushWarning(self, *a):
        pass

    def pushSuccess(self, *a):
        pass


class Iface:
    def messageBar(self):
        return Bar()


class Plugin:
    iface = Iface()


prj.clear()
root, qgz, gpkg = create_folder_structure(TMP, "Demo")
sites = QgsVectorLayer("Point?crs=EPSG:32637&field=name:string", "sites", "memory")
f = QgsFeature(sites.fields())
f.setGeometry(QgsGeometry.fromWkt("POINT(500050 2700050)"))
f.setAttributes(["one"])
sites.dataProvider().addFeatures([f])
prj.addMapLayer(sites)
check(prj.write(qgz), "project written")
history = ProjectHistory(Plugin())
history.install()
os.utime(qgz, (1_600_000_000, 1_600_000_000))  # the project file was saved long ago
for v in list_versions(root):
    import shutil

    shutil.rmtree(v["path"])
qgz_mtime = os.path.getmtime(qgz)
temp_dir = os.path.join(TMP, "appdata-autosave")
now = [dt.datetime(2026, 10, 28, 9, 0)]
saver = autosave.AutoSaver(Plugin(), clock=lambda: now[0], temp_dir=temp_dir)
saver.install()
saver.timer.stop()  # the test drives the clock itself
autosave.set_minutes(5)
autosave.set_reminder_minutes(15)
def same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


rec = autosave.recovery_path(prj.fileName())
check(same(rec, os.path.join(root, "Demo.autosave.qgz")), "recovery copy sits next to the project file")
check(saver.tick() is None, "first tick only starts the clock")
f2 = QgsFeature(sites.fields())
f2.setGeometry(QgsGeometry.fromWkt("POINT(500150 2700150)"))
f2.setAttributes(["two"])
sites.dataProvider().addFeatures([f2])
prj.setTitle("changed")  # an unsaved change
check(prj.isDirty(), "the project has unsaved changes")
now[0] += dt.timedelta(minutes=4)
check(saver.tick() is None and not os.path.exists(rec), "nothing written before 5 minutes")
now[0] += dt.timedelta(minutes=1)
written = saver.tick()
check(same(written, rec) and os.path.exists(rec), "recovery copy written after 5 minutes")
check(os.path.getmtime(qgz) == qgz_mtime, "the project file itself was not touched")
check(same(prj.fileName(), qgz) and prj.isDirty(), "project keeps its file name and stays unsaved")
check(list_versions(root) == [], "no history version made by the autosave")
mem_gpkg = rec[:-4] + ".memory.gpkg"
kept = QgsVectorLayer(f"{mem_gpkg}|layername=layer_1", "k", "ogr")
check(kept.isValid() and kept.featureCount() == 2, "features of the temporary layer kept beside the copy")
del kept  # release the file
check(not os.path.exists(os.path.join(root, "Demo.autosave.gpkg")), "the copy does not look like a project folder")
check(same(autosave.check_recovery(qgz), rec), "a copy newer than the project is found")
now[0] += dt.timedelta(minutes=2)
check(saver.tick() is None, "no new copy before the next 5 minutes")

# a real save removes the copy (and the history makes its own version, as usual)
prj.write()
check(not os.path.exists(rec) and not os.path.exists(mem_gpkg), "a real save removes the recovery copy")
for _ in range(300):  # the history keeps its version once the save has finished, in the background
    QgsApplication.processEvents()
    if list_versions(root):
        break
    __import__("time").sleep(0.02)
check(len(list_versions(root)) == 1, "the real save makes one history version")

# crash, then restore
prj.setTitle("changed again")
now[0] += dt.timedelta(minutes=6)
check(same(saver.tick(), rec), "new copy after more changes")
os.utime(qgz, (1_600_000_000, 1_600_000_000))
saver.copies.clear()  # as if Biruni had crashed: the new session did not write this copy
prj.clear()
check(os.path.exists(rec), "a copy left by a crash survives closing")
saver2 = autosave.AutoSaver(Plugin(), clock=lambda: now[0], temp_dir=temp_dir)
before = len(list_versions(root))
check(saver2.restore(rec, qgz), "the copy is restored")
kept_dir = os.path.join(root, ".biruni", "before-restore")
check(os.path.isdir(kept_dir) and len(os.listdir(kept_dir)) == 1,
      "the project file was kept before the restore replaced it")
check(same(prj.fileName(), qgz) and prj.title() == "changed again" and prj.isDirty(),
      "restored into the project's own file, marked unsaved")
restored = prj.mapLayersByName("sites")
check(restored and restored[0].featureCount() == 2, "temporary layer features came back")
check(len(list_versions(root)) == before + 1 and not os.path.exists(rec),
      "the state before restoring was kept as a version; the copy is gone")

# temporary map
prj.clear()
tmp_layer = QgsVectorLayer("Point?crs=EPSG:4326&field=n:integer", "scratch", "memory")
prj.addMapLayer(tmp_layer)
saver3 = autosave.AutoSaver(Plugin(), clock=lambda: now[0], temp_dir=temp_dir)
saver3.tick()
prj.setDirty(True)
now[0] += dt.timedelta(minutes=5)
trec = saver3.tick()
check(trec and same(os.path.dirname(trec), temp_dir) and os.path.exists(trec) and prj.fileName() == "",
      "a temporary map is copied to the autosave folder and stays a temporary map")
check(same(autosave.check_recovery("", temp_dir), trec), "the temporary map's copy is found at start-up")
saver3._on_cleared()
check(not os.path.exists(trec), "closing normally removes this session's copy")

# reminder for edits waiting too long
prj.clear()
edit_layer = QgsVectorLayer(f"{gpkg}|layername=biruni_project", "info", "ogr")
prj.addMapLayer(edit_layer)
edit_layer.startEditing()
nf = QgsFeature(edit_layer.fields())
nf.setAttribute("key", "k")
edit_layer.addFeature(nf)
t = dt.datetime(2026, 10, 28, 12, 0)
saver4 = autosave.AutoSaver(None, clock=lambda: t)
check(saver4._remind_edits(t, prj) == [], "edits just started: no reminder")
check(saver4._remind_edits(t + dt.timedelta(minutes=14), prj) == [], "no reminder before 15 minutes")
due = saver4._remind_edits(t + dt.timedelta(minutes=15), prj)
check([lyr.name() for lyr in due] == ["info"], "reminder after 15 minutes of unsaved edits")
check(saver4._remind_edits(t + dt.timedelta(minutes=20), prj) == [] and
      len(saver4._remind_edits(t + dt.timedelta(minutes=30), prj)) == 1, "then once every 15 minutes")
check(edit_layer.isModified(), "edits are never saved without asking")
edit_layer.rollBack()
check(saver4._remind_edits(t + dt.timedelta(minutes=31), prj) == [] and not saver4.edit_since,
      "nothing to remind once the edits are gone")
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

menu = QMenu()
autosave.fill_menu(menu)
acts = menu.actions()
checked = [a for a in acts if a.isChecked()]
acts[3].trigger()  # every 10 minutes
check(len(acts) == len(autosave.CHOICES) and len(checked) == 1 and autosave.minutes() == 10,
      "autosave menu: one choice checked, picking one changes the setting")
autosave.set_minutes(0)
check(autosave.minutes() == 0, "autosave can be switched off")
history.unload()
saver.uninstall()

# ---- light mode --------------------------------------------------------------------
s = QgsSettings()
s.setValue("qgis/map_update_interval", 333)
s.remove("qgis/enable_anti_aliasing")
keys = list(performance.light_settings())
before = {k: (s.contains(k), s.value(k)) for k in keys}
performance.apply(None, True)
check(performance.is_on() and s.value("qgis/enable_anti_aliasing", type=bool) is False and
      s.value("qgis/map_update_interval", type=int) == 500, "light mode applied")
performance.apply(None, True)  # twice: the user's own values are still the ones kept
performance.apply(None, False)
after = {k: (s.contains(k), s.value(k)) for k in keys}
check(after == before and not performance.is_on(), "light mode off restores every setting exactly")
check(not s.contains("qgis/enable_anti_aliasing"), "a setting that did not exist is removed again")

shp = os.path.join(TMP, "roads.shp")
roads = QgsVectorLayer("LineString?crs=EPSG:32637&field=n:integer", "roads", "memory")
rf = QgsFeature(roads.fields())
rf.setGeometry(QgsGeometry.fromWkt("LINESTRING(500000 2700000, 500100 2700100)"))
rf.setAttributes([1])
roads.dataProvider().addFeatures([rf])
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "ESRI Shapefile"
QgsVectorFileWriter.writeAsVectorFormatV3(roads, shp, QgsProject.instance().transformContext(), opts)
for ext in (".qix", ".sbn", ".sbx"):
    if os.path.exists(shp[:-4] + ext):
        os.remove(shp[:-4] + ext)
shp_layer = QgsVectorLayer(shp, "roads", "ogr")
rep = performance.optimise_layers([shp_layer], big_layer=0)
check("roads" in rep["indexed"] and os.path.exists(shp[:-4] + ".qix"), "spatial index added to a shapefile")
check(rep["big"] == [("roads", 1)], "big layers are listed")
rep2 = performance.optimise_layers([QgsVectorLayer(shp, "roads", "ogr")])
check(rep2["indexed"] == [], "an existing index is left alone")

from osgeo import gdal  # noqa: E402

tif = os.path.join(TMP, "big.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, 5000, 300, 1, gdal.GDT_Byte)
ds.SetGeoTransform((500000, 1, 0, 2700300, 0, -1))
ds = None
from qgis.core import QgsRasterLayer  # noqa: E402

rl = QgsRasterLayer(tif, "big")
check(performance.optimise_layers([rl])["need_overviews"] == ["big"], "big raster without overviews flagged")
check(performance.optimise_layers([rl], build_raster_overviews=True)["overviews"] == ["big"] and
      os.path.exists(tif + ".ovr"), "overviews built beside the raster")
check(performance.optimise_layers([QgsRasterLayer(tif, "big")])["need_overviews"] == [], "then not flagged again")
check("roads" in performance.report_text(rep), "readable report")
info = performance.system_check(TMP)
check(info["cores"] >= 1 and isinstance(info["suggest"], bool) and info["ram_gb"], f"system check {info}")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
