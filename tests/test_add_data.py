r"""The Add Data window, off screen: sample files made with the engine (a GeoPackage with two tables, a shapefile, a
GeoJSON, CSVs with lat/lon and with Arabic X/Y headers, GeoTIFFs, a ZIP holding a shapefile, a GPX and a DXF, an
unreadable text file) are each recognised with the right kind; the GeoPackage gives two cards; Add puts the right
layers (valid, CSV points at the right place, points on top) in the project; unreadable files are not added; a big
folder is read without blocking the window; paste and drop; web maps, services and the classic window; Arabic.

Run:  engine\bin\python-qgis.bat tests\test_add_data.py
"""
import os
import shutil
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature,  # noqa: E402
                       QgsGeometry, QgsLayerTreeLayer, QgsProject, QgsSettings, QgsVectorFileWriter, QgsVectorLayer,
                       QgsWkbTypes)
from qgis.gui import QgsLayerTreeMapCanvasBridge, QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QEventLoop, QMimeData, QTimer, QUrl  # noqa: E402
from qgis.PyQt.QtGui import QAction  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMainWindow  # noqa: E402

failures = []
BASEMAPS = [("osm", "OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19, "©"),
            ("topo", "OpenTopoMap", "https://tile.opentopomap.org/{z}/{x}/{y}.png", 17, "©")]


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=50):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_idle(dlg, limit=60.0):
    end = time.monotonic() + limit
    while dlg.busy() and time.monotonic() < end:
        pump(30)
    pump(30)
    return not dlg.busy()


class Bar:
    def __init__(self):
        self.notices = []

    def _push(self, title, text, *a):
        self.notices.append(text)

    pushInfo = pushSuccess = pushWarning = pushMessage = _push


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.mw.resize(1200, 800)
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.bridge = QgsLayerTreeMapCanvasBridge(QgsProject.instance().layerTreeRoot(), self.canvas)
        self.bar = Bar()
        self.pages = []
        self.projects = []
        self.classic = QAction("Data Source Manager", self.mw)
        self.classic.setObjectName("mActionDataSourceManager")
        self.classic_hits = []
        self.classic.triggered.connect(lambda *_: self.classic_hits.append(1))
        self.mw.addAction(self.classic)

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def openDataSourceManagerPage(self, name):
        self.pages.append(name)

    def addProject(self, path):
        self.projects.append(path)
        return True


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = None
        self.runner = None
        self.basemaps_added = []

    def add_basemap(self, key, zoom_world=False):
        self.basemaps_added.append(key)
        from qgis.core import QgsRasterLayer

        return QgsRasterLayer("", key, "wms")  # a stand-in (no network here)


# ---- sample data made with the engine ----------------------------------------------------------------------------
work = tempfile.mkdtemp(prefix="biruni_add_data_")
ctx = QgsCoordinateTransformContext()


def write(layer, path, driver, layer_name=None, append=False):
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = driver
    if layer_name:
        o.layerName = layer_name
    if append:
        o.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
    err = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, ctx, o)
    assert err[0] == QgsVectorFileWriter.WriterError.NoError, err


def memory(geom, crs, wkts):
    vl = QgsVectorLayer(f"{geom}?crs={crs}&field=id:integer&field=name:string", "m", "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(vl.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes([i + 1, f"n{i}"])
        feats.append(f)
    vl.dataProvider().addFeatures(feats)
    return vl


gpkg = os.path.join(work, "city.gpkg")
write(memory("Point", "EPSG:4326", ["Point(46.6 24.7)", "Point(46.7 24.8)", "Point(46.8 24.9)"]), gpkg, "GPKG",
      "wells")
write(memory("Polygon", "EPSG:4326", ["Polygon((46 24, 47 24, 47 25, 46 24))"]), gpkg, "GPKG", "districts",
      append=True)
shp = os.path.join(work, "roads.shp")
write(memory("LineString", "EPSG:32638", ["LineString(500000 2700000, 500100 2700100)",
                                          "LineString(500100 2700100, 500300 2700100)"]), shp, "ESRI Shapefile")
geojson = os.path.join(work, "stops.geojson")
write(memory("Point", "EPSG:4326", ["Point(31.2 30.0)"]), geojson, "GeoJSON")
csv_ll = os.path.join(work, "shops.csv")
with open(csv_ll, "w", encoding="utf-8", newline="") as f:
    f.write("name;Latitude;Longitude\nA;24.71;46.61\nB;24.82;46.72\nC;24.93;46.83\n")
csv_ar = os.path.join(work, "نقاط_الرفع.csv")
with open(csv_ar, "w", encoding="utf-8-sig", newline="") as f:
    f.write("رقم النقطة,الشرقيات,الشماليات,المنسوب\n1,500100.5,2700200.25,612.4\n2,500300,2700400,615\n")
csv_wkt = os.path.join(work, "parcels_wkt.csv")
with open(csv_wkt, "w", encoding="utf-8", newline="") as f:
    f.write('id,WKT\n1,"POLYGON((0 0,10 0,10 10,0 0))"\n2,"POLYGON((20 0,30 0,30 10,20 0))"\n')
csv_plain = os.path.join(work, "owners.csv")
with open(csv_plain, "w", encoding="utf-8", newline="") as f:
    f.write("owner,parcel\nAli,12\nSara,14\n")
from osgeo import gdal, osr  # noqa: E402

gdal.UseExceptions()
srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
dem = os.path.join(work, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(dem, 20, 10, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000, 10, 0, 2700200, 0, -10))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).Fill(612.5)
ds = None
photo = os.path.join(work, "ortho.tif")
ds = gdal.GetDriverByName("GTiff").Create(photo, 16, 16, 3, gdal.GDT_Byte)
ds.SetGeoTransform((500000, 1, 0, 2700016, 0, -1))
ds.SetProjection(srs.ExportToWkt())
ds = None
zpath = os.path.join(work, "delivery.zip")
with zipfile.ZipFile(zpath, "w") as zf:
    for e in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        if os.path.exists(shp[:-4] + e):
            zf.write(shp[:-4] + e, "data/blocks" + e)
gpx = os.path.join(work, "walk.gpx")
with open(gpx, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
            '<wpt lat="24.7" lon="46.6"><name>a</name></wpt>'
            '<trk><name>t</name><trkseg><trkpt lat="24.7" lon="46.6"/><trkpt lat="24.71" lon="46.61"/></trkseg>'
            '</trk></gpx>')
dxf = os.path.join(work, "plan.dxf")
_cad = QgsVectorLayer("LineString?crs=EPSG:32638", "c", "memory")
_f = QgsFeature(_cad.fields())
_f.setGeometry(QgsGeometry.fromWkt("LineString(0 0, 10 10)"))
_cad.dataProvider().addFeatures([_f])
write(_cad, dxf, "DXF")
junk = os.path.join(work, "readme.txt")
with open(junk, "w", encoding="utf-8") as f:
    f.write("hello world, this is only a note\n")
qgz = os.path.join(work, "old_map.qgz")
with open(qgz, "wb") as f:
    f.write(b"PK\x05\x06" + b"\0" * 18)

from biruni import add_data  # noqa: E402

check("biruni.add_data.dialog" not in sys.modules and "biruni.add_data.detect" not in sys.modules,
      "importing the package loads nothing else")
from biruni.add_data import detect  # noqa: E402
from biruni.add_data.dialog import facts, sources_from_mime  # noqa: E402

# ---- detection on its own (what the background thread does) ------------------------------------------------------
kinds = {}
for p in (gpkg, shp, geojson, csv_ll, csv_ar, csv_wkt, csv_plain, dem, photo, zpath, gpx, dxf, junk, qgz):
    kinds[p] = detect.detect(p, "EPSG:32638")
check(sorted(i["kind"] for i in kinds[gpkg]) == ["areas", "points"], "GeoPackage: two tables, points and areas")
check({i["name"] for i in kinds[gpkg]} == {"wells", "districts"}, "GeoPackage cards are named after the tables")
check(all(i["checked"] for i in kinds[gpkg]), "both tables ticked by default")
wells = next(i for i in kinds[gpkg] if i["name"] == "wells")
check(wells["count"] == 3 and "4326" in wells["crs"], f"wells: 3 features, WGS 84 ({wells['count']}, {wells['crs']})")
check([i["kind"] for i in kinds[shp]] == ["lines"] and kinds[shp][0]["name"] == "roads", "shapefile: lines 'roads'")
check("32638" in kinds[shp][0]["crs"], f"shapefile CRS read ({kinds[shp][0]['crs']})")
check(kinds[shp][0]["size"] and kinds[shp][0]["size"] > 100, "shapefile size counts its parts")
check([i["kind"] for i in kinds[geojson]] == ["points"], "GeoJSON: points")
ll = kinds[csv_ll][0]
check(ll["kind"] == "points" and ll["xy"] == ("Longitude", "Latitude") and ll["crs"] == "EPSG:4326"
      and ll.get("crs_guessed"), "CSV lat/lon: points, X=Longitude Y=Latitude, WGS 84 guessed")
ar = kinds[csv_ar][0]
check(ar["kind"] == "points" and ar["xy"] == ("الشرقيات", "الشماليات"), "CSV with Arabic شرقيات / شماليات headers")
check(ar["crs"] == "EPSG:32638" and ar.get("crs_from_project"), "projected values: the map's CRS, and it says so")
check(any("check" in s for s in facts(ar, "WGS 84 / UTM zone 38N")), "the card asks to check the coordinate system")
check(kinds[csv_wkt][0]["kind"] == "areas" and kinds[csv_wkt][0].get("wkt") == "WKT", "CSV with a WKT column: areas")
check(kinds[csv_plain][0]["kind"] == "table", "CSV without coordinates: a table")
check(kinds[dem][0]["kind"] == "elevation", "one-band float GeoTIFF: elevation")
check(kinds[photo][0]["kind"] == "image", "three-band GeoTIFF: image")
check(kinds[dem][0].get("pixels") == (20, 10), "raster size read")
z = kinds[zpath]
check(len(z) == 1 and z[0]["kind"] == "lines" and z[0]["container"] == "archive" and z[0]["name"] == "blocks",
      f"ZIP: the shapefile inside is found ({[(i['kind'], i['name'], i['container']) for i in z]})")
check(any("delivery.zip" in s for s in facts(z[0])), "the card says it is inside the archive")
g = kinds[gpx]
check(g and all(i["kind"] == "gps" for i in g), "GPX: GPS track cards")
check({i["name"] for i in g if i["checked"]} >= {"waypoints", "tracks"}, "GPX: waypoints and tracks ticked")
check(not any(i["checked"] for i in g if i["table"] in ("route_points", "track_points", "routes")),
      "GPX: empty and per-point tables not ticked")
check(kinds[dxf] and all(i["kind"] == "cad" for i in kinds[dxf]), "DXF: CAD drawing")
check(kinds[junk][0]["kind"] == "unknown" and not kinds[junk][0]["readable"], "a plain note: cannot be read")
check(kinds[qgz][0]["kind"] == "project" and not kinds[qgz][0]["checked"], "a project file: offered to open")
check(detect.detect(os.path.join(work, "nope.shp"))[0]["note"] == "missing", "a missing file")
check(detect.detect("https://tile.openstreetmap.org/{z}/{x}/{y}.png")[0]["kind"] == "web", "XYZ address: web map")
check(detect.detect("https://h/arcgis/rest/services/Roads/FeatureServer/0")[0]["provider"] == "arcgisfeatureserver",
      "ArcGIS feature layer address")
svc = detect.detect("https://h/geoserver/wms?service=WMS&request=GetCapabilities")[0]
check(svc["kind"] == "service" and svc["page"] == "wms", "WMS address: a service with its page")
for n, expect in (("خط الطول", "lon"), ("دائرة العرض", "lat"), ("الشرقيات", "x"), ("شماليات", "y"),
                  ("Easting", "x"), ("POINT_Y", "y"), ("lng", "lon")):
    table = {"lon": detect.LON_NAMES, "lat": detect.LAT_NAMES, "x": detect.X_NAMES, "y": detect.Y_NAMES}[expect]
    check(detect._match(n, table), f"column «{n}» is {expect}")
check(not detect._match("name", detect.X_NAMES), "an ordinary column is not a coordinate")

# ---- the window -----------------------------------------------------------------------------------------------
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:32638"))
plugin = Plugin()
from biruni.vault import RECENT_KEY  # noqa: E402

QgsSettings().setValue(RECENT_KEY, [shp, os.path.join(work, "gone.shp"), gpkg] + [f"{work}/long_name_{i:02d}_of_a_survey_file.shp" for i in range(12)])
for i in range(12):
    shutil.copy(shp, os.path.join(work, f"long_name_{i:02d}_of_a_survey_file.shp"))
QgsSettings().setValue("biruni/add_data/browse_dir", work)
dlg = add_data.open_dialog(plugin)
check(len(dlg.recent_buttons) == 8 and dlg.recent_buttons[0].toolTip() == shp, "Recent: the last 8 files still there")
check(dlg.minimumSizeHint().width() <= 1000, f"recent files do not widen the window ({dlg.minimumSizeHint().width()})")
check(dlg.isVisible() and add_data.open_dialog(plugin) is dlg, "one window, brought forward when asked again")
check(not dlg.add_button.isEnabled(), "Add is off while the list is empty")
t0 = time.monotonic()
dlg.add_paths([gpkg, shp, geojson, csv_ll, csv_ar, dem, zpath, junk])
check(time.monotonic() - t0 < 0.5, "adding files returns at once (read in the background)")
check(dlg.busy() and not dlg.progress.isHidden(), "progress shows while reading")
check(wait_idle(dlg), "all read")
cards = {(c.item["path"], c.item["name"]): c for c in dlg.cards}
check(len(dlg.cards) == 9, f"9 cards (the GeoPackage gives two) - {len(dlg.cards)}")
check(sum(1 for c in dlg.cards if c.item["path"] == gpkg) == 2, "two GeoPackage cards")
junk_card = next(c for c in dlg.cards if c.item["path"] == junk)
check(not junk_card.check.isEnabled() and not junk_card.check.isChecked(), "the unreadable card is greyed")
check(junk_card.property("off") == "true", "and looks off")
check(dlg.add_button.text() == "Add 8 layers" and dlg.add_button.isEnabled(), f"Add 8 layers ({dlg.add_button.text()})")
dlg.add_paths([shp])
check(len(dlg.cards) == 9 and not dlg.busy(), "the same file twice is not read again")
dlg.set_all(False)
check(not dlg.add_button.isEnabled(), "select none: Add is off")
dlg.set_all(True)
layers = dlg.add_checked(keep_open=True)
check(len(layers) == 8 and all(lyr.isValid() for lyr in layers), f"8 valid layers added ({len(layers)})")
names = {lyr.name() for lyr in project.mapLayers().values()}
check(names >= {"wells", "districts", "roads", "stops", "shops", "نقاط_الرفع", "dem", "blocks"},
      f"the right names ({sorted(names)})")
check("readme" not in names, "the unreadable file is not added")
by = {lyr.name(): lyr for lyr in project.mapLayers().values()}
for name, lyr in by.items():
    check(lyr.dataProvider() is not None and lyr.isValid() and lyr.source(), f"«{name}» has a valid source")
shops = by["shops"]
pts = sorted((round(f.geometry().asPoint().x(), 2), round(f.geometry().asPoint().y(), 2)) for f in shops.getFeatures())
check(pts == [(46.61, 24.71), (46.72, 24.82), (46.83, 24.93)] and shops.crs().authid() == "EPSG:4326",
      f"lat/lon CSV points at the right place in WGS 84 ({pts})")
survey = by["نقاط_الرفع"]
pts = sorted((f.geometry().asPoint().x(), f.geometry().asPoint().y()) for f in survey.getFeatures())
check(pts == [(500100.5, 2700200.25), (500300.0, 2700400.0)] and survey.crs().authid() == "EPSG:32638",
      f"Arabic X/Y CSV points at the right place in the map's CRS ({pts})")
check(QgsWkbTypes.geometryType(by["districts"].wkbType()) == QgsWkbTypes.GeometryType.PolygonGeometry
      if hasattr(QgsWkbTypes, "GeometryType") and hasattr(QgsWkbTypes.GeometryType, "PolygonGeometry")
      else by["districts"].geometryType().name == "Polygon", "districts are areas")
check(by["blocks"].featureCount() == 2 and "/vsizip/" in by["blocks"].source(), "the ZIP layer reads through /vsizip/")
order = [n.layer().name() for n in project.layerTreeRoot().children() if isinstance(n, QgsLayerTreeLayer)]
check(order.index("wells") < order.index("roads") < order.index("districts") < order.index("dem"),
      f"points on top, then lines, areas, images ({order})")
check(plugin.iface.bar.notices and plugin.iface.bar.notices[-1] == "8 layers added", "summary in the message bar")
check(len(dlg.cards) == 1 and dlg.cards[0].item["path"] == junk, "added cards leave the list; the unreadable stays")
check(dlg.isVisible(), "keep open: still open")

# ---- a big folder does not block ----------------------------------------------------------------------------------
folder = os.path.join(work, "survey_folder")
os.makedirs(folder)
for i in range(120):
    with open(os.path.join(folder, f"p{i:03d}.csv"), "w", encoding="utf-8") as f:
        f.write("x,y,z\n" + "\n".join(f"{500000 + k},{2700000 + k},{k}" for k in range(50)) + "\n")
write(memory("LineString", "EPSG:32638", ["LineString(0 0, 1 1)"]), os.path.join(folder, "edge.shp"), "ESRI Shapefile")
for side in ("notes.pdf", "photo.jpg", "edge.qpj"):
    with open(os.path.join(folder, side), "w") as f:
        f.write("x")
os.makedirs(os.path.join(folder, "sub"))
ticks = []
timer = QTimer()
timer.timeout.connect(lambda: ticks.append(time.monotonic()))
timer.start(10)
dlg.clear()
t0 = time.monotonic()
dlg.add_paths([folder])
check(time.monotonic() - t0 < 0.3, "a folder returns at once")
check(wait_idle(dlg, 120), "the folder is read")
spent = time.monotonic() - t0
timer.stop()
gaps = [b - a for a, b in zip(ticks, ticks[1:])]
check(len(ticks) > 5 and max(gaps or [0]) < 0.5, f"the window kept answering ({len(ticks)} ticks in {spent:.1f} s,"
      f" longest pause {max(gaps or [0]):.2f} s)")
check(len(dlg.cards) == 121, f"121 data files listed, documents and sidecars passed over ({len(dlg.cards)})")
check(all(c.item["kind"] in ("points", "lines") for c in dlg.cards), "all recognised")
fl, truncated = detect.list_folder(folder, max_files=10)
check(len(fl) == 10 and truncated, "a folder is capped")
dlg.clear()
check(not dlg.cards and not dlg.add_button.isEnabled(), "clear the list")

# ---- paste and drop -----------------------------------------------------------------------------------------------
QApplication.clipboard().setText(f'"{gpx}"\n{dxf}\nnot a path')
check(dlg.paste() == 2, "paste: two paths from the clipboard text")
md = QMimeData()
md.setUrls([QUrl.fromLocalFile(photo), QUrl("https://tile.example.com/{z}/{x}/{y}.png")])
check(sources_from_mime(md) == [os.path.normpath(photo), "https://tile.example.com/{z}/{x}/{y}.png"],
      "drop: files and web addresses")
dlg.add_paths(sources_from_mime(md) + [qgz])
check(wait_idle(dlg), "read")
check({c.item["kind"] for c in dlg.cards} >= {"gps", "cad", "image", "web", "project"}, "GPS, CAD, image, web, project")
proj = next(c for c in dlg.cards if c.item["kind"] == "project")
check(proj.action is not None and not proj.check.isVisible(), "a project card has an Open button, no tick")
web = next(c for c in dlg.cards if c.item["kind"] == "web")
web.check.setChecked(False)
before = len(project.mapLayers())
added = dlg.add_checked(keep_open=True)
check(len(added) == len(project.mapLayers()) - before and added, "GPS / CAD / image added")
check(all(lyr.isValid() for lyr in added), "all valid")

# ---- web maps, services and the classic window --------------------------------------------------------------------
check(list(dlg.basemap_buttons) == ["osm", "topo"], "the plugin's basemaps are offered")
dlg.keep_open.setChecked(True)
dlg.basemap_buttons["osm"].click()
check(plugin.basemaps_added == ["osm"], "a basemap button adds that basemap")
dlg.service_buttons["postgres"].click()
check(plugin.iface.pages == ["postgres"], "PostgreSQL opens the classic window on its page")
dlg.service_buttons["WFS"].click()
check(plugin.iface.pages[-1] == "WFS", "WFS page")
dlg.open_classic()
check(plugin.iface.classic_hits == [1], "the classic Data Source Manager is opened by its action")
dlg.nav.setCurrentRow(1)
check(dlg.stack.currentIndex() == 1 and dlg.footer_add.isHidden(), "Web maps tab (no Add button there)")
dlg.url_edit.setText("https://h/geoserver/ows?service=WFS")
dlg.add_url()
check(wait_idle(dlg) and dlg.stack.currentIndex() == 0, "a pasted address goes to the Files list")
svc_card = next(c for c in dlg.cards if c.item["kind"] == "service")
svc_card.action.click()
check(plugin.iface.pages[-1] == "WFS", "its button opens the WFS page")

# ---- the data browser (places, folders, containers that open onto their layers) ---------------------------------
from biruni.add_data import browser as browser_mod  # noqa: E402
from biruni.vault.catalog import user_folders  # noqa: E402
from qgis.core import QgsMimeDataUtils  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402


def wait_browser(br, limit=60.0):
    end = time.monotonic() + limit
    while br.busy() and time.monotonic() < end:
        pump(30)
    pump(30)
    return not br.busy()


def top_row(br, name):
    for i in range(br.tree.topLevelItemCount()):
        if br.tree.topLevelItem(i).text(0) == name:
            return br.tree.topLevelItem(i)
    return None


gdb = os.path.join(work, "survey.gdb")
write(memory("Point", "EPSG:32638", ["Point(500000 2700000)", "Point(500010 2700010)"]), gdb, "OpenFileGDB",
      "boreholes")
write(memory("Polygon", "EPSG:32638", ["Polygon((500000 2700000, 500100 2700000, 500100 2700100, 500000 2700000))"]),
      gdb, "OpenFileGDB", "plots", append=True)
raw_tables = sorted(n for n in os.listdir(gdb) if n.endswith(".gdbtable"))
check(os.path.isdir(gdb) and raw_tables, f"a File Geodatabase made with the engine ({len(raw_tables)} raw tables)")
with open(dem + ".aux.xml", "w") as f:
    f.write("<PAMDataset/>")
with open(os.path.join(work, "dem.ovr"), "wb") as f:
    f.write(b"x")
with open(os.path.join(work, "report.pdf"), "wb") as f:
    f.write(b"%PDF")
dlg.clear()
br = dlg.browser
check(br is not None and dlg.split.count() == 2, "the Files tab has the browser beside the Ready to add list")
t0 = time.monotonic()
br.navigate(work)
check(time.monotonic() - t0 < 0.3, "opening a folder returns at once (read in the background)")
check(wait_browser(br), "the folder is read")
names = br.visible_names()
gdb_row = top_row(br, "survey.gdb")
check(gdb_row is not None and br.data(gdb_row)["type"] == "container" and gdb_row.text(1) == "Geodatabase",
      "the .gdb is one container item")
check(gdb_row is not None and gdb_row.childIndicatorPolicy() == gdb_row.ChildIndicatorPolicy.ShowIndicator,
      "with an expand arrow")
check(top_row(br, "city.gpkg") is not None and br.data(top_row(br, "city.gpkg"))["type"] == "container",
      "a GeoPackage is a container")
check(top_row(br, "delivery.zip") is not None and br.data(top_row(br, "delivery.zip"))["type"] == "container",
      "a ZIP is a container")
hidden = [n for n in names if n.lower().endswith((".shx", ".dbf", ".prj", ".cpg", ".aux.xml", ".ovr", ".pdf",
                                                    ".gdbtable", ".gdbtablx", ".gdbindexes", ".spx"))]
check(not hidden, f"sidecar files and documents are hidden ({hidden})")
check("roads.shp" in names and "dem.tif" in names and "survey_folder" in names, "data files and folders are shown")
check("readme.txt" not in names, "a text file the engine cannot read is hidden once recognised")
check(top_row(br, "roads.shp").text(1) == "Lines" and "2 features" in top_row(br, "roads.shp").text(2),
      f"a file's kind and count are filled in ({top_row(br, 'roads.shp').text(1)}, {top_row(br, 'roads.shp').text(2)})")
gdb_row.setExpanded(True)
check(wait_browser(br), "the geodatabase is opened in the background")
kids = {gdb_row.child(i).text(0): gdb_row.child(i) for i in range(gdb_row.childCount())}
check(set(kids) == {"boreholes", "plots"}, f"expanding it lists its 2 feature classes ({sorted(kids)})")
check(kids.get("boreholes") is not None and kids["boreholes"].text(1) == "Points"
      and "2 features" in kids["boreholes"].text(2), f"points, 2 features ({kids['boreholes'].text(2)})")
check(kids.get("plots") is not None and kids["plots"].text(1) == "Areas" and "Polygon" in kids["plots"].text(2),
      f"areas with their geometry type ({kids['plots'].text(2)})")
before = set(project.mapLayers())
br.activate(kids["boreholes"])
new = [project.mapLayer(i) for i in set(project.mapLayers()) - before]
check(len(new) == 1 and new[0].name() == "boreholes" and new[0].isValid() and new[0].featureCount() == 2
      and "survey.gdb" in new[0].source(), "double-click adds the layer to the map")
kids["plots"].setCheckState(0, Qt.CheckState.Checked)
check([c.item["name"] for c in dlg.cards] == ["plots"], "ticking a layer puts it in the Ready to add list")
kids["plots"].setCheckState(0, Qt.CheckState.Unchecked)
check(not dlg.cards, "unticking takes it out")
kids["plots"].setCheckState(0, Qt.CheckState.Checked)
dlg.remove_card(dlg.cards[0])
check(kids["plots"].checkState(0) == Qt.CheckState.Unchecked, "removing the card unticks the row")

# a raw .gdbtable from the Windows file dialog (or the .gdb folder itself) means the geodatabase
dlg.add_paths([os.path.join(gdb, raw_tables[-1])])
check(wait_idle(dlg), "read")
check(sorted(c.item["name"] for c in dlg.cards) == ["boreholes", "plots"] and all(c.item["path"] == gdb
                                                                                  for c in dlg.cards),
      f"a raw .gdbtable gives one card per feature class ({[c.item['name'] for c in dlg.cards]})")
check(dlg.add_paths([gdb, os.path.join(gdb, raw_tables[0])]) == 0, "the .gdb and its other files are the same")
check(detect.gdb_root(os.path.join(gdb, "a00000001.gdbtable")) == gdb and detect.gdb_root(shp) == "",
      "any path containing .gdb\\ maps to the geodatabase")
check(gdb_row.checkState(0) == Qt.CheckState.Checked, "the geodatabase row shows it is in the list")
dlg.clear()
br.navigate(os.path.join(gdb, raw_tables[0]))
check(wait_browser(br) and os.path.normcase(br.current) == os.path.normcase(work), "browsing into a .gdb shows its folder")
gdb_row = top_row(br, "survey.gdb")
check(gdb_row is not None and gdb_row.isExpanded() and br.tree.currentItem() is gdb_row, "with the .gdb opened")

# drag a layer from the browser onto the list (the same data goes onto the map)
wait_browser(br)
plots = next(gdb_row.child(i) for i in range(gdb_row.childCount()) if gdb_row.child(i).text(0) == "plots")
md = br.tree.mimeData([plots, top_row(br, "dem.tif")])
uris = QgsMimeDataUtils.decodeUriList(md) if QgsMimeDataUtils.isUriList(md) else []
check(len(uris) == 2 and {u.providerKey for u in uris} == {"ogr", "gdal"}, "dragging makes engine layer URIs")
check(dlg.handle_mime(md) == 2 and sorted(c.item["name"] for c in dlg.cards) == ["dem", "plots"],
      "dropping them on the list makes their cards")
dlg.clear()
md = br.tree.mimeData([top_row(br, "city.gpkg")])
check(md.hasUrls() and dlg.handle_mime(md) == 1 and wait_idle(dlg) and len(dlg.cards) == 2,
      "dropping a container gives its layers")
dlg.clear()

# Add with rows selected in the browser
roads_row = top_row(br, "roads.shp")
roads_row.setSelected(True)
gdb_row.setSelected(True)
check(dlg.add_button.isEnabled() and dlg.add_button.text() == "Add selected", "Add selected")
before = {lyr.name() for lyr in project.mapLayers().values()}
count = len(project.mapLayers())
dlg.add_checked(keep_open=True)
check(wait_idle(dlg), "read")
pump(100)
check(len(project.mapLayers()) == count + 3, f"the selected file and geodatabase are added ({len(project.mapLayers()) - count})")
check(not dlg.cards and not br.selected_rows(), "and the list is empty again")

# filter, back / up, breadcrumbs
br.filter.setText("road")
check(br.visible_names() and all("road" in n.lower() for n in br.visible_names()), "the filter box")
br.filter.setText("")
br.navigate(folder)
check(wait_browser(br) and "edge.shp" in br.visible_names() and "photo.jpg" not in br.visible_names()
      and "notes.pdf" not in br.visible_names(), "another folder")
check(br.crumb_buttons and br.crumb_buttons[-1].text() == "survey_folder", "the breadcrumb ends with the folder")
br.back()
check(os.path.normcase(br.current) == os.path.normcase(work), "Back")
br.navigate(folder)
wait_browser(br)
br.up()
check(os.path.normcase(br.current) == os.path.normcase(work), "Up")
wait_browser(br)
fol = top_row(br, "survey_folder")
br.activate(fol)
check(os.path.normcase(br.current) == os.path.normcase(folder), "double-click opens a folder")
wait_browser(br)

# places
project.setFileName(os.path.join(work, "here.qgz"))
br.refresh_places()
places = [os.path.normcase(p) for p in br.place_paths()]
check(os.path.normcase(work) in places, "the project folder is a place")
check(os.path.normcase(os.path.expanduser("~")) in places, "Home is a place")
check(any(len(p) <= 3 and p.endswith(("\\", "/")) for p in places), "the drives are places")
check(browser_mod.RECENT_PLACE in br.place_paths(), "Recent is a place")
br.add_place(folder)
check(os.path.normcase(folder) in [os.path.normcase(p) for p in user_folders()], "Add folder saves it for the Vault too")
check(os.path.normcase(folder) in [os.path.normcase(p) for p in br.place_paths()], "and shows it in the places")
br.navigate(browser_mod.RECENT_PLACE)
check(wait_browser(br) and br.visible_names(), f"Recent data ({len(br.visible_names())} items)")
project.setFileName("")
br.navigate(work)
wait_browser(br)
# ---- the ribbon command and the empty Layers panel card (the same CATALOG entry) ---------------------------------
from biruni.commands import Command, CommandRunner  # noqa: E402

dlg.keep_open.setChecked(False)
dlg.close()
pump(50)
cmd = Command("add_data", "Add\nData", "database-plus", run=lambda p: add_data.open_dialog(p))
runner = CommandRunner(plugin)
runner.execute(cmd)
second = add_data._OPEN.get(id(plugin))
check(second is not None and second is not dlg and second.isVisible(), "the command opens a new window")
check(second.minimumSizeHint().width() <= 1000, f"the window fits a small screen ({second.minimumSizeHint().width()} px)")
check(abs(second.frameGeometry().center().x() - plugin.iface.mw.frameGeometry().center().x()) < 40,
      "centred over the main window")
second.add_paths([geojson])
wait_idle(second)
second.keep_open.setChecked(False)
second.add_checked()
pump(50)
try:
    closed = not second.isVisible()
except RuntimeError:  # deleted on close
    closed = True
check(closed, "Add closes the window unless 'keep open' is ticked")
check(QgsSettings().value("biruni/add_data/keep_open", True, type=bool) is False, "keep open is remembered")

# ---- Arabic (right to left) ---------------------------------------------------------------------------------------
from biruni import i18n  # noqa: E402

i18n.init("ar")
third = add_data.open_dialog(plugin)
check(QApplication.layoutDirection().name == "RightToLeft", "Arabic: right to left")
check(third.add_button.text() != "Add" and third.windowTitle() != "Add data", f"Arabic texts ({third.windowTitle()})")
third.add_paths([csv_ar])
wait_idle(third)
check(third.cards and third.cards[0].chip.text() != "Points", f"Arabic kind name ({third.cards[0].chip.text()})")
third.close()
i18n.init("en_US")

project.clear()
pump(100)
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
