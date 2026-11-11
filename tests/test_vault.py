r"""The Vault («الخزانة», vault/): sections and counts of a real project folder, Arabic and English search, card facts,
badges, cached pictures, add to map, quick look without touching the project, "where is this used", duplicates, the
Today strip, renaming / deleting, folder watching only while shown, nothing done before the pane opens, and the pane
drawn off screen (pictures for a look: Arabic RTL and English, light and dark, cards and list).

Run:  engine\bin\python-qgis.bat tests\test_vault.py
Pictures go to %BIRUNI_SHOTS% (else a temporary folder, printed at the end).
"""
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsFields,  # noqa: E402
                       QgsGeometry, QgsLayerTreeModel, QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize,
                       QgsMimeDataUtils, QgsPointXY, QgsPrintLayout, QgsProject, QgsRectangle, QgsUnitTypes,
                       QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
QgsApplication.bookmarkManager().initialize(os.path.join(SETTINGS_DIR, "bookmarks.xml"))

from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QEventLoop, QMetaType, Qt, QTimer  # noqa: E402
from qgis.PyQt.QtGui import QColor, QFont, QImage  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMainWindow  # noqa: E402

from biruni import i18n  # noqa: E402
from biruni.theme import TOKENS, app_qss, build_palette  # noqa: E402

failures = []
SHOTS = os.environ.get("BIRUNI_SHOTS") or tempfile.mkdtemp(prefix="vault_shots_")
os.makedirs(SHOTS, exist_ok=True)
WORK = tempfile.mkdtemp(prefix="vault_test_")
QApplication.setFont(QFont("Segoe UI", 9))


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout_ms=15000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# ---- a fake Biruni around the pane ---------------------------------------------------------------------------
class Bar:
    def __init__(self):
        self.messages = []

    def pushMessage(self, *a):  # noqa: N802
        self.messages.append(a)

    def pushWarning(self, *a):  # noqa: N802
        self.messages.append(a)


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.bar = Bar()
        self.active = None
        self.opened_layouts = []

    def mainWindow(self):  # noqa: N802
        return self.mw

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return self.bar

    def activeLayer(self):  # noqa: N802
        return self.active

    def openLayoutDesigner(self, layout):  # noqa: N802
        self.opened_layouts.append(layout.name())


class Theme:
    def __init__(self):
        self.dark = False
        self.fns = []

    def tokens(self):
        return TOKENS["dark" if self.dark else "light"]

    def is_dark(self):
        return self.dark

    def on_change(self, fn):
        self.fns.append(fn)


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.toolbox = None
        self.runner = None
        self.project = None


# ---- a project folder with a bit of everything ---------------------------------------------------------------
from biruni.project import create_folder_structure  # noqa: E402

ROOT, QGZ, PROJECT_GPKG = create_folder_structure(WORK, "Riyadh survey")
DATA = os.path.join(ROOT, "data")
UTM = QgsCoordinateReferenceSystem("EPSG:32638")
WGS = QgsCoordinateReferenceSystem("EPSG:4326")
X0, Y0 = 670000.0, 2730000.0


def write_layer(path, geom_type, crs, fields, rows, layer_name=None, driver="GPKG", append=False):
    fl = QgsFields()
    for name, t in fields:
        fl.append(QgsField(name, t))
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.fileEncoding = "UTF-8"
    if layer_name:
        opts.layerName = layer_name
    if append:
        opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    w = QgsVectorFileWriter.create(path, fl, geom_type, crs, QgsProject.instance().transformContext(), opts)
    for geom, attrs in rows:
        f = QgsFeature(fl)
        f.setGeometry(geom)
        f.setAttributes(attrs)
        w.addFeature(f)
    del w


S = QMetaType.Type.QString
D = QMetaType.Type.Double
SURVEY = os.path.join(DATA, "survey.gpkg")
squares = [QgsGeometry.fromRect(QgsRectangle(X0 + i * 60, Y0, X0 + i * 60 + 50, Y0 + 40)) for i in range(4)]
write_layer(SURVEY, QgsWkbTypes.Type.Polygon, UTM, [("name", S), ("owner_name", S), ("area", D)],
            [(g, [n, o, 2000.0]) for g, n, o in zip(squares, ["قطعة 1", "قطعة 2", "", "قطعة 4"],
                                                   ["أحمد", "سارة", "محمد", "ليلى"])], "parcels")
write_layer(SURVEY, QgsWkbTypes.Type.Point, WGS, [("well_no", S)],
            [(QgsGeometry.fromPointXY(QgsPointXY(46.7 + i * 0.01, 24.7)), [f"W{i}"]) for i in range(5)], "wells",
            append=True)
ROADS = os.path.join(DATA, "roads.shp")
write_layer(ROADS, QgsWkbTypes.Type.LineString, UTM, [("road_name", S)],
            [(QgsGeometry.fromPolylineXY([QgsPointXY(X0, Y0 - 20 + i * 30), QgsPointXY(X0 + 300, Y0 + i * 25)]),
              [f"Road {i}"]) for i in range(3)], driver="ESRI Shapefile")
NOCRS = os.path.join(DATA, "old_points.shp")
write_layer(NOCRS, QgsWkbTypes.Type.Point, QgsCoordinateReferenceSystem(), [("id", S)],
            [(QgsGeometry.fromPointXY(QgsPointXY(X0 + i * 10, Y0 + i * 7)), [str(i)]) for i in range(6)],
            driver="ESRI Shapefile")
for ext in (".prj", ".cpg"):
    if os.path.exists(NOCRS[:-4] + ext) and ext == ".prj":
        os.remove(NOCRS[:-4] + ext)

from osgeo import gdal, ogr, osr  # noqa: E402

gdal.UseExceptions()
DEM = os.path.join(DATA, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(DEM, 80, 60, 1, gdal.GDT_Float32)
ds.SetGeoTransform((X0, 5, 0, Y0 + 300, 0, -5))
srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
ds.SetProjection(srs.ExportToWkt())
import numpy  # noqa: E402

yy, xx = numpy.mgrid[0:60, 0:80]
ds.GetRasterBand(1).WriteArray((numpy.sin(xx / 9.0) * numpy.cos(yy / 7.0) * 40 + 600).astype("float32"))
ds = None
DXF = os.path.join(ROOT, "plan.dxf")
dxf = ogr.GetDriverByName("DXF").CreateDataSource(DXF)
dl = dxf.CreateLayer("entities")
for i in range(4):
    f = ogr.Feature(dl.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(f"LINESTRING ({i * 10} 0, {i * 10 + 30} 40, {i * 10 + 60} 0)"))
    dl.CreateFeature(f)
dxf = None
CSV = os.path.join(DATA, "wells_list.csv")
with open(CSV, "w", encoding="utf-8", newline="") as fh:
    fh.write("well,depth\nW1,30\nW2,45\nW3,52\n")
FAV = os.path.join(WORK, "My GIS library")
os.makedirs(FAV)
shutil.copy(CSV, os.path.join(FAV, "wells copy.csv"))  # a duplicate outside the project
shutil.copy(DEM, os.path.join(FAV, "dem_backup.tif"))
INDOOR = os.path.join(ROOT, "outputs", "Building A", "indoor.gpkg")
os.makedirs(os.path.dirname(INDOOR))
write_layer(INDOOR, QgsWkbTypes.Type.Polygon, UTM, [("name", S)], [(squares[0], ["Lobby"])], "units")
with open(os.path.join(ROOT, "exports", "report.pdf"), "wb") as fh:
    fh.write(b"%PDF-1.4\n%fake\n")
# field photos (a folder inside the project, with its GeoPackage and three photos of today)
PHOTOS = os.path.join(ROOT, "Field photos")
os.makedirs(PHOTOS)
write_layer(os.path.join(PHOTOS, "field_photos.gpkg"), QgsWkbTypes.Type.Point, WGS, [("photo", S), ("taken", S)],
            [(QgsGeometry.fromPointXY(QgsPointXY(46.71, 24.70)), ["p1.jpg", "2026-10-30"])], "field_photos")
for i, col in enumerate(("#E5484D", "#0E7C74", "#D97706")):
    img = QImage(320, 240, QImage.Format.Format_RGB32)
    img.fill(QColor(col))
    img.save(os.path.join(PHOTOS, f"p{i + 1}.jpg"), "JPG")
from qgis.core import QgsSettings  # noqa: E402

QgsSettings().setValue("biruni/field-photos/folder", PHOTOS)
# lots of small files (the scan must stay in the background)
MANY = os.path.join(ROOT, "exports", "pages")
os.makedirs(MANY)
for i in range(1500):
    with open(os.path.join(MANY, f"page_{i:04d}.txt"), "w") as fh:
        fh.write("x")

# ---- nothing before the pane opens ---------------------------------------------------------------------------
t0 = time.perf_counter()
import biruni.vault as vault  # noqa: E402

t_import = (time.perf_counter() - t0) * 1000
plugin = Plugin()
undo = vault.install(plugin)
heavy = [m for m in ("biruni.vault.dock", "biruni.vault.scan", "biruni.vault.thumbs", "biruni.vault.worker",
                     "biruni.vault.catalog") if m in sys.modules]
check(not heavy, f"start-up: nothing of the Vault is loaded before it is opened ({heavy or 'none'}; "
                 f"import {t_import:.1f} ms)")

# ---- the project ---------------------------------------------------------------------------------------------
prj = QgsProject.instance()
prj.setCrs(UTM)
parcels = QgsVectorLayer(f"{SURVEY}|layername=parcels", "قطع الأراضي", "ogr")
wells = QgsVectorLayer(f"{SURVEY}|layername=wells", "Wells", "ogr")
roads = QgsVectorLayer(ROADS, "Roads", "ogr")
gone = QgsVectorLayer(ROADS, "Old survey", "ogr")
prj.addMapLayers([parcels, wells, roads, gone])
gone.setDataSource(os.path.join(WORK, "deleted", "old_survey.shp"), "Old survey", "ogr")
check(prj.mapLayer(gone.id()) is not None, "a layer whose file is missing stays in the project")
recent = QgsSettings().value(vault.RECENT_KEY, [], type=list)
check(any(os.path.normcase(r) == os.path.normcase(ROADS) for r in recent),
      "recent data: files added to the map are remembered (one cheap signal)")
check("biruni.vault.dock" not in sys.modules, "remembering recent data did not build the pane")
md = parcels.metadata()
md.setAbstract("Land parcels surveyed in the north district")
parcels.setMetadata(md)
# a charter on the parcels (name is required: one parcel has none)
from biruni.charter import model as cm  # noqa: E402

charter = cm.new_charter()
charter["fields"] = [cm.field_spec("name", required=True, alias_ar="اسم القطعة", alias_en="Parcel name"),
                     cm.field_spec("owner_name", alias_ar="المالك", alias_en="Owner")]
cm.write_charter(parcels, charter)
# a print layout and a map theme
lay = QgsPrintLayout(prj)
lay.initializeDefaults()
lay.setName("Site plan")
mp = QgsLayoutItemMap(lay)
lay.addLayoutItem(mp)
mp.attemptMove(QgsLayoutPoint(10, 10, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
mp.attemptResize(QgsLayoutSize(277, 190, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
mp.setExtent(QgsRectangle(X0 - 50, Y0 - 60, X0 + 350, Y0 + 140))
mp.setFrameEnabled(True)
prj.layoutManager().addLayout(lay)
root = prj.layerTreeRoot()
for node in root.findLayers():
    node.setItemVisibilityChecked(node.layerId() in (parcels.id(), roads.id()))
themes = prj.mapThemeCollection()
themes.insert("Survey only", themes.createThemeFromCurrentState(root, QgsLayerTreeModel(root)))
for node in root.findLayers():
    node.setItemVisibilityChecked(True)
# Map Stops
from biruni.stops.model import PROJECT, Frame, store_for  # noqa: E402

store = store_for(plugin, prj)
s1 = store.add("North gate", Frame(X0 + 100, Y0 + 20, 400, 250, 0, UTM), scope=PROJECT, note="بوابة الشمال")
s2 = store.add("Water tank", Frame(X0 + 200, Y0 + 60, 300, 200, 0, UTM), scope=PROJECT, group="Site visits")
img = QImage(160, 100, QImage.Format.Format_RGB32)
img.fill(QColor("#3B82F6"))
from biruni.stops.model import image_to_bytes  # noqa: E402

store.update(s1.id, thumb=image_to_bytes(img))
# a style file and a sealed delivery
roads.saveNamedStyle(os.path.join(DATA, "roads_style.qml"))
from biruni.seal.core import seal_file  # noqa: E402

seal_file(DEM, "Test office", certificate=False)
prj.write(QGZ)
check(os.path.exists(QGZ), "the test project is saved as a project folder")

# ---- open ----------------------------------------------------------------------------------------------------
plugin.iface.mw.resize(1300, 900)
plugin.iface.mw.show()
t0 = time.perf_counter()
d = vault.open_dock(plugin)
t_open = (time.perf_counter() - t0) * 1000
check(t_open < 1500, f"the pane opens quickly ({t_open:.0f} ms, folders read in the background)")
check(d.worker.isRunning() or d.worker.ran >= 0, "the folder scan runs in the worker thread")
# the interface keeps breathing while the folder (1,500+ files) is read
gaps, last = [], [time.perf_counter()]


def beat():
    now = time.perf_counter()
    gaps.append((now - last[0]) * 1000)
    last[0] = now


hb = QTimer()
hb.setInterval(5)
hb.timeout.connect(beat)
hb.start()


def settled():
    return d._files is not None and d._photo_facts is not None and d.worker.pending() == 0 and not d._main


wait_until(settled, 30000)
wait(400)
wait_until(settled, 30000)
hb.stop()
worst = max(gaps) if gaps else 0
check(worst < 400, f"the interface never stops while folders are read (longest pause {worst:.0f} ms)")
check(d.worker.ran > 0, f"the worker did the reading ({d.worker.ran} jobs)")


def sec(key):
    return d.find(key=key)


def kids(key):
    e = sec(key)
    return [c for c in (e.children if e else []) if c.row == "item"]


project = sec("sec:project")
groups = {g.kind: g for g in project.children}
check({"map", "layout", "stop", "charter", "photos", "indoor", "style", "sealed"} <= set(groups),
      f"This project: maps, layouts, stops, charters, photos, indoor, styles, sealed ({sorted(groups)})")
main_map = d.find(key="map:main")
check(main_map is not None and main_map.facts.get("layer_count") == 4, "the main map lists its 4 layers")
check(any(c.kind == "theme" and c.title == "Survey only" for c in kids("grp:map")), "map themes are listed")
check([c.title for c in kids("grp:layout")] == ["Site plan"], "layouts are listed")
check(len(kids("grp:stop")) == 2, "the two Map Stops are listed")
check(kids("grp:stop")[0].thumb is not None, "a stop shows its picture")
check([c.title for c in kids("grp:charter")] == ["قطع الأراضي"], "layers with a Data Charter are listed")
photos = kids("grp:photos")
check(len(photos) == 1 and photos[0].facts.get("count") == 3 and photos[0].facts.get("points") == 1,
      "field photos: 3 photos and their points layer")
check(len(kids("grp:indoor")) == 1, "the indoor map is found in the project folder")
check(any(c.title == "roads_style.qml" for c in kids("grp:style")), "style files are listed")
check(any(c.title == "dem.tif" for c in kids("grp:sealed")), "sealed files are listed")
check(len(kids("grp:folder:vector")) == 2, f"project folder: 2 vector files ({[c.title for c in kids('grp:folder:vector')]})")
check(len(kids("grp:folder:raster")) == 1, "project folder: 1 raster")
check([c.title for c in kids("grp:folder:cad")] == ["plan.dxf"], "project folder: the DXF drawing")
check([c.title for c in kids("grp:folder:table")] == ["wells_list.csv"], "project folder: the CSV table")
docs = kids("grp:folder:document")
check(len(docs) >= 1500 and not any(c.title.endswith(".jpg") for c in docs),
      f"documents listed ({len(docs)}), the field photos are not repeated there")
check(not any(c.title.endswith((".seal.json", ".seal.png", ".shx", ".dbf", ".prj")) for g in ("vector", "raster")
              for c in kids(f"grp:folder:{g}")), "sidecar and seal files are never listed alone")
dbs = kids("sec:db")
db_names = [c.title for c in dbs]
check("Riyadh survey.gpkg" in db_names and "survey.gpkg" in db_names,
      f"databases: the project database and the GeoPackage in the folder ({db_names})")

# lazy: the GeoPackage lists its two layers when opened
survey = d.find(path=SURVEY, title="survey.gpkg")
it = d._item_of[id(survey)]
d.tree.expand(d.proxy.mapFromSource(it.index()))
wait_until(lambda: survey.children and survey.children[0].row == "item", 8000)
check(sorted(c.title for c in survey.children) == ["parcels", "wells"], "a GeoPackage opens to its two layers")
wait_until(settled, 15000)
wells_e = next(c for c in survey.children if c.title == "wells")
check(any(b[0] == "Not projected" for b in wells_e.badges), "badge: a layer in degrees is 'Not projected'")
check(wells_e.facts.get("count") == 5, "the GeoPackage's layer shows its feature count")

# ---- card facts and badges -----------------------------------------------------------------------------------
roads_e = d.find(path=ROADS, title="roads.shp")
check(roads_e.facts.get("layers", [{}])[0].get("count") == 3, "card: feature count of the shapefile")
check(roads_e.facts.get("layers", [{}])[0].get("crs") == "EPSG:32638", "card: its coordinate system")
check((roads_e.facts.get("size") or 0) > 0, "card: its size (all its parts)")
from biruni.vault.dock import facts_line  # noqa: E402

line = facts_line(roads_e)
check("3 features" in line and "EPSG:32638" in line and ("KB" in line or " B" in line), f"card line: {line}")
check(any(b[0] == "On the map" for b in roads_e.badges), "badge: the file is on the map")
nocrs_e = d.find(path=NOCRS, title="old_points.shp")
check(any(b == ("No CRS!", "bad") for b in nocrs_e.badges), "badge: a shapefile without a .prj has 'No CRS!'")
gone_e = d.find(key=f"layer:{gone.id()}")
check(gone_e is not None and any(b == ("Missing file!", "bad") for b in gone_e.badges),
      "badge: a map layer whose file is gone shows 'Missing file!'")
dem_e = d.find(path=DEM, title="dem.tif")
check(any(b == ("Sealed", "sealed") for b in dem_e.badges), "badge: sealed ✓")
check(dem_e.facts.get("width") == 80 and dem_e.facts.get("crs") == "EPSG:32638", "raster card: size in pixels, CRS")
charter_e = kids("grp:charter")[0]
check(charter_e.facts.get("score") == 75 and any(b[0] == "Health 75%" for b in charter_e.badges),
      f"Data Charter card: its health score (3 of 4 parcels named) {charter_e.badges}")
parcels_e = d.find(key=f"layer:{parcels.id()}")
check(any(b[0].startswith("Charter 75") for b in parcels_e.badges), "a map layer shows its charter health")
dxf_e = d.find(path=DXF, title="plan.dxf")
check(dxf_e.facts.get("layers", [{}])[0].get("count") == 4, "the DXF drawing's entities are counted")

# ---- pictures: drawn once, cached on disk, reused ------------------------------------------------------------
check(roads_e.thumb is not None and dem_e.thumb is not None, "vector and raster cards have a picture")
key = roads_e.data.get("thumb_key")
check(key and os.path.exists(d.cache.path(key)), "the picture is cached on disk (profile cache folder)")
check(d.cache.folder.startswith(SETTINGS_DIR), "the cache lives in the profile")
check(kids("grp:layout")[0].thumb is not None, "the layout card shows its page")
check(photos[0].strip and len(photos[0].strip) == 3, "field photos: a strip of the latest photos")
made = d.cache.made
d._pix.clear()
d.refresh()
wait_until(settled, 20000)
wait(200)
roads_e = d.find(path=ROADS, title="roads.shp")
check(roads_e.thumb is not None and d.cache.made == made and d.cache.reused > 0,
      f"pictures are reused, not drawn again (made {made} → {d.cache.made}, reused {d.cache.reused})")
os.utime(ROADS, (time.time() + 5, time.time() + 5))
check(__import__("biruni.vault.thumbs", fromlist=["x"]).file_key(ROADS, "", "x") !=
      __import__("biruni.vault.thumbs", fromlist=["x"]).file_key(ROADS, "", "y") and key !=
      __import__("biruni.vault.thumbs", fromlist=["x"]).file_key(ROADS, roads_e.table, "#0D9488"),
      "a changed file gets a new picture (key = path + date)")

# ---- search --------------------------------------------------------------------------------------------------
def search(text):
    d.search.setText(text)
    d._apply_search()
    return [e.title for e in d.visible_entries()]


hits = search("roads")
check("roads.shp" in hits and "Roads" in hits and "dem.tif" not in hits, f"search in English: {hits[:6]}")
hits = search("الاراضي")
check("قطع الأراضي" in hits, f"search in Arabic, without hamza: {hits[:6]}")
hits = search("owner_name")
check("قطع الأراضي" in hits, "search finds field names")
hits = search("بوابه الشمال")
check("North gate" in hits, "search finds a stop by its Arabic note (ة/ه)")
hits = search("north district")
check("قطع الأراضي" in hits, "search finds the layer's notes (metadata)")
hits = search("Not projected")
check("wells" in hits, f"search finds badges {hits}")
search("")
check(len(d.visible_entries()) > 20, "clearing the search shows everything again")

# ---- drag to the map -----------------------------------------------------------------------------------------
it = d._item_of[id(roads_e)]
mime = d.model.mimeData([it.index()])
uris = QgsMimeDataUtils.decodeUriList(mime) if mime is not None else []
check(len(uris) == 1 and uris[0].providerKey == "ogr" and uris[0].uri.endswith("roads.shp"),
      "drag: the card carries a layer the map and the Layers panel accept")
check(bool(it.flags() & Qt.ItemFlag.ItemIsDragEnabled), "data cards can be dragged")

# ---- add to map / quick look ---------------------------------------------------------------------------------
n = len(prj.mapLayers())
lyr = d.add_to_map(dem_e)
check(lyr is not None and len(prj.mapLayers()) == n + 1, "add to map")
n = len(prj.mapLayers())
ql = d.quick_look(nocrs_e, show=False)
check(ql.layer is not None and ql.layer.isValid() and ql.layer.featureCount() == 6,
      "quick look opens the file on its own small map")
check(len(prj.mapLayers()) == n and not ql.in_project(), "quick look does not add anything to the project")
ql.resize(560, 600)
ql.show()
app.processEvents()
ql.canvas.zoomToFullExtent()
ql.canvas.refresh()
wait(100)
ql.canvas.waitWhileRendering()
app.processEvents()
ql_png = os.path.join(SHOTS, "quick_look_en_light.png")
ql.grab().save(ql_png)
ql.close()

# ---- where is this used --------------------------------------------------------------------------------------
survey_parcels = next(c for c in survey.children if c.title == "parcels")
used = d.where_used(survey_parcels)
check(("layer", "قطع الأراضي") in used and ("layout", "Site plan") in used and ("theme", "Survey only") in used,
      f"where is this used: layer, layout, map theme ({used})")
used = d.where_used(d.find(path=DXF, title="plan.dxf"))
check(used == [], "a file not on the map is used nowhere")
dlg, _u = d.show_where_used(survey_parcels, show=False)
props = d.properties(roads_e, show=False)
check("road_name" in props.info.toPlainText() and "EPSG:32638" in props.info.toPlainText(),
      "properties card: fields and coordinate system")

# ---- duplicates ----------------------------------------------------------------------------------------------
csv_e = d.find(path=CSV, title="wells_list.csv")
QgsSettings().setValue("biruni/vault/folders", [FAV])
found = []
d.find_duplicates(csv_e, done=found.append, show=False)
wait_until(lambda: found, 10000)
check(found and [os.path.basename(p) for p in found[0]] == ["wells copy.csv"],
      f"find duplicates: the copy in a favourite folder ({found})")

# ---- seal ---------------------------------------------------------------------------------------------------
sealed = []
d.seal(csv_e, ask=False, done=sealed.append)
wait_until(lambda: sealed, 15000)
check(sealed and isinstance(sealed[0], dict) and os.path.exists(CSV + ".seal.json"), "seal a file from the Vault")
check(any(b == ("Sealed", "sealed") for b in csv_e.badges), "the card shows 'Sealed' at once")
res = d.check_seal(csv_e)
check(isinstance(res, dict) and res.get("ok"), f"check the seal: the file was not changed ({res})")

# ---- Today ---------------------------------------------------------------------------------------------------
d.rebuild()
today = {k: txt for k, _i, txt in d.today_items()}
check("grp:photos" in today and "3" in today["grp:photos"], f"Today: new photos ({today})")
check("grp:stop" in today and "2" in today["grp:stop"], "Today: new stops")
check("sec:folder" in today, "Today: new files")
check(len(d.today.chips) == len(today), "the Today strip shows a chip for each")
d.reveal("grp:stop")
check(d.tree.currentIndex().data(vault.dock.ENTRY_ROLE).key == "grp:stop", "a Today chip opens its group")

# ---- folders section -----------------------------------------------------------------------------------------
wait_until(settled, 15000)  # (sealing wrote files: the watched folder was read again)
FAV_KEY = f"fav:{os.path.normcase(os.path.abspath(FAV))}"
fav = d.find(key=FAV_KEY)
check(fav is not None, "favourite folders are listed")
d.tree.expand(d.proxy.mapFromSource(d._item_of[id(fav)].index()))


def fav_kids():
    e = d.find(key=FAV_KEY)
    return [c.title for c in e.children if c.row == "item"] if e else []


wait_until(lambda: fav_kids() and settled(), 8000)
check(sorted(fav_kids()) == ["dem_backup.tif", "wells copy.csv"], f"a folder opens to its data ({fav_kids()})")
check(d.find(key="grp:recent") is not None, "recent data is listed")
# ---- rename / delete ----------------------------------------------------------------------------------------
wait_until(settled, 15000)
nocrs_e = d.find(path=NOCRS, title="old_points.shp")
new = d.rename(nocrs_e, "older_points")
check(new and os.path.exists(new) and os.path.exists(new[:-4] + ".dbf") and not os.path.exists(NOCRS),
      f"rename: a shapefile is renamed with all its parts {plugin.iface.bar.messages[-1:]}")
check(d.rename(roads_e, "x") is None, "rename: refused for a file on the map")
wait_until(settled, 15000)
e = d.find(path=new, title="older_points.shp")
check(e is not None, "the renamed file is listed after the rescan")
gone_files = d.delete(e, ask=False, trash=False)
check(gone_files and not any(os.path.exists(f) for f in gone_files), "delete: the file and its parts")
wait_until(settled, 15000)
check(d.find(path=new) is None, "the deleted file is no longer listed")

# ---- watching only while shown ------------------------------------------------------------------------------
check(len(d.watched()) >= 2, f"the project folder is watched while the Vault is shown ({len(d.watched())})")
d.hide()
app.processEvents()
check(d.watched() == [], "nothing is watched while the Vault is hidden")
with open(os.path.join(DATA, "new_notes.csv"), "w") as fh:
    fh.write("a\n1\n")
scans = d.stats["scans"]
wait(300)
check(d.stats["scans"] == scans, "a hidden Vault does not read anything")
d.show()
app.processEvents()
check(len(d.watched()) >= 2, "watching starts again when shown")

# ---- pictures for a look ------------------------------------------------------------------------------------
I18N = os.environ.get("VAULT_I18N_JSON") or os.path.join(
    os.environ.get("TEMP", tempfile.gettempdir()), "claude", "c--Users-HatemTarek-Desktop-DXF-to-SHP-App-HatemTarek",
    "12cf1281-d8dc-4630-b1a0-f86abf89dcd2", "scratchpad", "i18n7", "vault.json")


def load_arabic():
    table = {}
    base = os.path.join(HERE, "..", "app", "plugins", "biruni", "i18n", "ar.json")
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            table.update(json.load(fh))
    if os.path.exists(I18N):
        with open(I18N, encoding="utf-8") as fh:
            table.update({k: v[0] for k, v in json.load(fh).items()})
    return table
shots = [ql_png]
d.setFloating(True)
wait_until(settled, 15000)
for lang_code, dark, mode in (("en", False, "cards"), ("en", True, "cards"), ("ar", False, "cards"),
                              ("ar", True, "cards"), ("en", False, "list"), ("ar", True, "list")):
    rtl = lang_code == "ar"
    i18n._strings = load_arabic() if rtl else {}
    i18n._lang = lang_code
    direction = Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight
    QApplication.setLayoutDirection(direction)
    d.setLayoutDirection(direction)
    plugin.theme.dark = dark
    app.setPalette(build_palette(plugin.theme.tokens()))
    app.setStyleSheet(app_qss(plugin.theme.tokens()))
    d.setWindowTitle(i18n.tr("Vault"))
    d.search.setPlaceholderText(i18n.tr("Search names, fields, notes…"))
    d.refresh_theme()
    d.set_mode(mode)
    d.rebuild()
    d.resize(400, 1100)
    app.processEvents()
    wait_until(settled, 15000)
    wait(120)
    tag = f"{lang_code}_{'dark' if dark else 'light'}_{mode}"
    path = os.path.join(SHOTS, f"vault_{tag}.png")
    pm = d.grab()
    pm.save(path)
    shots.append(path)
    check(not pm.isNull() and pm.width() >= 380, f"drawn: {tag} ({pm.width()}×{pm.height()})")
    if mode == "cards" and not dark:  # the lower part (folder, databases, folders)
        d.tree.verticalScrollBar().setValue(d.tree.verticalScrollBar().maximum() // 2 + 600)
        app.processEvents()
        path = os.path.join(SHOTS, f"vault_{tag}_lower.png")
        d.grab().save(path)
        shots.append(path)
        d.tree.verticalScrollBar().setValue(0)
    if lang_code == "ar" and not dark and mode == "cards":
        p2 = d.properties(d.find(path=ROADS, title="roads.shp"), show=False)
        p2.setLayoutDirection(direction)
        p2.resize(480, 520)
        path = os.path.join(SHOTS, "properties_ar_light.png")
        p2.grab().save(path)
        shots.append(path)
        hits = search("طرق") or search("roads")
        search("")
i18n._strings = {}
i18n._lang = "en"
QApplication.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
plugin.theme.dark = False

# ---- right-click menus ---------------------------------------------------------------------------------------
wait_until(settled, 15000)
texts = [a.text() for a in d.build_menu(d.find(path=ROADS, title="roads.shp")).actions()]
check({"Add to map", "Quick look", "Open its folder", "Copy path", "Properties", "Where is this used?",
       "Find duplicates", "Data Charter…", "Rename…", "Delete…"} <= set(texts), f"a file's menu ({texts})")
texts = [a.text() for a in d.build_menu(kids("grp:stop")[0]).actions()]
check("Go to this stop" in texts, "a stop's menu")
texts = [a.text() for a in d.build_menu(kids("grp:layout")[0]).actions()]
check("Open layout" in texts, "a layout's menu")
d.default_action(kids("grp:layout")[0])
check(plugin.iface.opened_layouts == ["Site plan"], "double-click on a layout opens it")
worker = d.worker
# ---- unload --------------------------------------------------------------------------------------------------
undo()
check("biruni.vault.dock" in sys.modules and not vault.dock._DOCK, "unload removes the pane")
check(not worker.isRunning(), "the worker thread stopped")

print("\npictures:", SHOTS)
for s in shots:
    print("  ", s)
print("\nALL PASS" if not failures else f"\n{len(failures)} FAILED:\n  " + "\n  ".join(failures))
sys.exit(1 if failures else 0)
