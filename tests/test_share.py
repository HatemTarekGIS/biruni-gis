"""Standalone tests for sharing: web map, KMZ, QField field copy and return.

Run:  engine\\bin\\python-qgis.bat tests\\test_share.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsEditorWidgetSetup, QgsFeature, QgsGeometry,
                       QgsGraduatedSymbolRenderer, QgsLineSymbol, QgsPalLayerSettings, QgsProject, QgsRendererCategory,
                       QgsRendererRange, QgsSymbol, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # the dialogs are built without showing them

app = QgsApplication([], True)
app.initQgis()

from osgeo import ogr  # noqa: E402

from biruni import i18n  # noqa: E402
from biruni.project import copy_vector_into_gpkg, create_folder_structure  # noqa: E402
from biruni.share import qfield  # noqa: E402
from biruni.share.kmz import export_kmz  # noqa: E402
from biruni.share.webmap import export_web_map  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni-share-")
root, qgz, gpkg = create_folder_structure(tmp, "Demo")
prj = QgsProject.instance()
X0, Y0 = 330000, 3330000  # UTM 36N, near Cairo


def sq(i, s=50):
    x, y = X0 + i * 60, Y0
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


# parcels: 6 polygons in the project database, categorized, labelled, one hidden field
mem = QgsVectorLayer("Polygon?crs=EPSG:32636&field=name:string&field=use:string&field=area:double"
                     "&field=secret:string", "parcels", "memory")
feats = []
for i in range(6):
    f = QgsFeature(mem.fields())
    f.setGeometry(QgsGeometry.fromWkt(sq(i)))
    f.setAttributes([f"قطعة {i + 1}", "سكني" if i % 2 == 0 else "تجاري", 2500.0, "x"])
    feats.append(f)
mem.dataProvider().addFeatures(feats)
prj.addMapLayer(mem)
copy_vector_into_gpkg(mem, gpkg)
parcels = mem
check(parcels.providerType() == "ogr" and parcels.featureCount() == 6, "parcels stored in the project gpkg")
cats = []
for val, col in (("سكني", "#e31a1c"), ("تجاري", "#33a02c")):
    sym = QgsSymbol.defaultSymbol(parcels.geometryType())
    sym.setColor(QColor(col))
    cats.append(QgsRendererCategory(val, sym, val))
parcels.setRenderer(QgsCategorizedSymbolRenderer("use", cats))
parcels.setFieldAlias(parcels.fields().indexOf("name"), "الاسم")
parcels.setEditorWidgetSetup(parcels.fields().indexOf("secret"), QgsEditorWidgetSetup("Hidden", {}))
pal = QgsPalLayerSettings()
pal.fieldName = "name"
parcels.setLabeling(QgsVectorLayerSimpleLabeling(pal))
parcels.setLabelsEnabled(True)

# wells: temporary (memory) points with a graduated renderer
wells = QgsVectorLayer("Point?crs=EPSG:32636&field=code:string&field=depth:integer", "wells", "memory")
feats = []
for i in range(4):
    f = QgsFeature(wells.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POINT({X0 + 25 + i * 60} {Y0 + 25})"))
    f.setAttributes([f"W{i}", 10 + i * 20])
    feats.append(f)
wells.dataProvider().addFeatures(feats)
s1 = QgsSymbol.defaultSymbol(wells.geometryType())
s1.setColor(QColor("#1f78b4"))
s2 = QgsSymbol.defaultSymbol(wells.geometryType())
s2.setColor(QColor("#ff7f00"))
wells.setRenderer(QgsGraduatedSymbolRenderer("depth", [QgsRendererRange(0, 30, s1, "0 - 30"),
                                                        QgsRendererRange(30, 100, s2, "30 - 100")]))
prj.addMapLayer(wells)

# roads: dashed line, project gpkg
rmem = QgsVectorLayer("LineString?crs=EPSG:32636&field=road:string", "roads", "memory")
f = QgsFeature(rmem.fields())
f.setGeometry(QgsGeometry.fromWkt(f"LINESTRING({X0} {Y0 - 10},{X0 + 400} {Y0 - 10})"))
f.setAttributes(["شارع النيل"])
rmem.dataProvider().addFeatures([f])
prj.addMapLayer(rmem)
copy_vector_into_gpkg(rmem, gpkg)
roads = rmem
ls = QgsLineSymbol.createSimple({"color": "#555555", "width": "0.8", "line_style": "dash"})
from qgis.core import QgsSingleSymbolRenderer  # noqa: E402

roads.setRenderer(QgsSingleSymbolRenderer(ls))
check(prj.write(qgz), "demo project saved")

# ---- web map --------------------------------------------------------------
html_path = os.path.join(root, "exports", "web map.html")
export_web_map([parcels, wells, roads], html_path, title="خريطة تجريبية")
with open(html_path, encoding="utf-8") as fh:
    page = fh.read()
m = re.search(r'<script type="application/json" id="biruni-data">(.*?)</script>', page, re.S)
check(m is not None, "web map has the data block")
data = json.loads(m.group(1).replace("<\\/", "</"))
check([ly["name"] for ly in data["layers"]] == ["parcels", "wells", "roads"], "web map layers in order")
lp, lw, lr = data["layers"]
check(len(lp["geojson"]["features"]) == 6 and len(lw["geojson"]["features"]) == 4, "all features exported")
check(len(lp["styles"]) == 2 and {s["fillColor"] for s in lp["styles"]} == {"#e31a1c", "#33a02c"},
      "categorized colours become 2 styles")
check([it["label"] for it in lp["legend"]] == ["سكني", "تجاري"], "categorized legend labels (Arabic)")
check(len(lw["styles"]) == 2 and [it["label"] for it in lw["legend"]] == ["0 - 30", "30 - 100"],
      "graduated renderer -> 2 styles + legend")
check(lw["styles"][lw["geojson"]["features"][3]["properties"]["s"]]["fillColor"] == "#ff7f00",
      "deep well drawn with the second class colour")
check(lr["styles"][0]["dashArray"] is not None and lr["styles"][0]["weight"] > 2, "dashed road keeps dash + width")
check("الاسم" in lp["fields"] and "secret" not in lp["fields"] and "fid" not in lp["fields"],
      "popup fields use aliases, skip hidden field and fid")
check(lp["geojson"]["features"][0]["properties"]["l"] == "قطعة 1", "labels exported")
lon, lat = lp["geojson"]["features"][0]["geometry"]["coordinates"][0][0]
check(30.9 < lon < 31.5 and 29.9 < lat < 30.2, f"reprojected to lon/lat ({lon:.4f}, {lat:.4f})")
check("قطعة 1" in page and "خريطة تجريبية" in page, "Arabic text survives as UTF-8")
check(len(page) > 150000 and "L.map(" in page and "leaflet-container" in page, "Leaflet inlined (offline)")
check("bindPopup" in page and "L.control.layers" in page and "L.control.scale" in page, "popups, switcher, scale bar")
check('dir="ltr"' in page and data["rtl"] is False, "English UI -> left-to-right page")
i18n._lang = "ar"
export_web_map([parcels], os.path.join(root, "exports", "ar.html"), title="عربي")
with open(os.path.join(root, "exports", "ar.html"), encoding="utf-8") as fh:
    page_ar = fh.read()
check('dir="rtl"' in page_ar and '"rtl":true' in page_ar, "Arabic UI -> right-to-left page")
i18n._lang = "en"
node = shutil.which("node")
if node:
    js = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
    jsf = os.path.join(tmp, "page.js")
    open(jsf, "w", encoding="utf-8").write(js)
    import subprocess

    r = subprocess.run([node, "--check", jsf], capture_output=True, text=True)
    check(r.returncode == 0, "page script parses (node --check) " + r.stderr[:200])
else:
    try:
        from qgis.PyQt.QtQml import QJSEngine

        js = re.findall(r"<script>(.*?)</script>", page, re.S)[-1]
        res = QJSEngine().evaluate("try { new Function(" + json.dumps(js) + "); 'ok' } catch (e) { String(e) }")
        check(res.toString() == "ok", "page script parses (QJSEngine) " + res.toString())
    except ImportError:
        print("SKIP no JavaScript engine - script syntax not checked")

# ---- KMZ ---------------------------------------------------------------------
kmz_path, written = export_kmz([parcels, wells, roads], os.path.join(root, "exports", "map.kmz"))
check(written == {"parcels": 6, "wells": 4, "roads": 1}, f"KMZ placemarks {written}")
ds = ogr.Open(kmz_path)
counts = {ds.GetLayer(i).GetName(): ds.GetLayer(i).GetFeatureCount() for i in range(ds.GetLayerCount())}
ds = None
check(counts == {"parcels": 6, "wells": 4, "roads": 1}, f"KMZ readable by OGR {counts}")
with zipfile.ZipFile(kmz_path) as z:
    kml = "".join(z.read(n).decode("utf-8") for n in z.namelist() if n.endswith(".kml"))
    check("doc.kml" in z.namelist(), "KMZ has doc.kml (Google Earth entry point)")
check("<PolyStyle>" in kml and "<LineStyle>" in kml and "قطعة 1" in kml, "KMZ has styles and Arabic names")
check("1c1ae3" in kml.lower(), "KML colour of residential parcels (aabbggrr)")

# ---- QField: send -------------------------------------------------------------
out_dir = os.path.join(tmp, "field")
os.makedirs(out_dir)
folder = qfield.package_for_qfield(prj, out_dir)
with open(os.path.join(folder, qfield.MANIFEST), encoding="utf-8") as fh:
    man = json.load(fh)
check(os.path.exists(os.path.join(folder, "Demo.qgz")) and os.path.exists(os.path.join(folder, "Demo.gpkg")),
      "field folder has project + database")
check(len(man["layers"]) == 3 and man["layers"][wells.id()]["table"] == "wells", "manifest lists 3 layers")
check(len(man["layers"][parcels.id()]["features"]) == 6, "6 parcel fingerprints")
fp = QgsProject()
check(fp.read(os.path.join(folder, "Demo.qgz")), "field project opens")
fl = {lyr.name(): lyr for lyr in fp.mapLayers().values()}
check(all(lyr.isValid() for lyr in fl.values()), "all field layers valid")
check(all(os.path.normcase(folder) in os.path.normcase(lyr.source()) for lyr in fl.values()),
      "field layers point inside the field folder")
check(fl["wells"].providerType() == "ogr" and fl["wells"].featureCount() == 4, "temporary layer converted")
check(fl["parcels"].editorWidgetSetup(fl["parcels"].fields().indexOf(qfield.SRC_FIELD)).type() == "Hidden",
      "tracking column hidden in field forms")
check(isinstance(fl["parcels"].renderer(), QgsCategorizedSymbolRenderer), "field copy keeps the style")
fp.clear()
qgs_text = zipfile.ZipFile(os.path.join(folder, "Demo.qgz")).read("Demo.qgs").decode("utf-8")
check("./Demo.gpkg" in qgs_text and tmp.replace("\\", "/") not in qgs_text, "relative paths in the field project")

# ---- simulate the field work (as QField would edit the GeoPackage) -------------
fgpkg = os.path.join(folder, "Demo.gpkg")
fpar = QgsVectorLayer(f"{fgpkg}|layername=parcels", "p", "ogr")
by_src = {f[qfield.SRC_FIELD]: f for f in fpar.getFeatures()}
ids = sorted(f.id() for f in parcels.getFeatures())
A, B, C, D, E = ids[:5]
fpar.startEditing()
fpar.changeAttributeValue(by_src[A].id(), fpar.fields().indexOf("name"), "قطعة معدلة")
fpar.changeGeometry(by_src[B].id(), QgsGeometry.fromWkt(sq(1, 70)))
fpar.deleteFeature(by_src[C].id())
fpar.changeAttributeValue(by_src[D].id(), fpar.fields().indexOf("use"), "حديقة")
fpar.deleteFeature(by_src[E].id())
new = QgsFeature(fpar.fields())
new.setGeometry(QgsGeometry.fromWkt(sq(10)))
new.setAttribute("name", "قطعة جديدة")
new.setAttribute("use", "سكني")
fpar.addFeature(new)
dup = QgsFeature(by_src[A])  # QField "duplicate feature" copies the hidden column too
dup.setId(-1)
dup.setGeometry(QgsGeometry.fromWkt(sq(11)))
dup.setAttribute(fpar.fields().indexOf("fid"), None)
fpar.addFeature(dup)
check(fpar.commitChanges(), "field edits saved in the field copy")
del fpar
fw = QgsVectorLayer(f"{fgpkg}|layername=wells", "w", "ogr")
wsrc = {f[qfield.SRC_FIELD]: f for f in fw.getFeatures()}
wid = sorted(f.id() for f in wells.getFeatures())
fw.startEditing()
fw.changeAttributeValue(wsrc[wid[0]].id(), fw.fields().indexOf("depth"), 99)
fw.deleteFeature(wsrc[wid[3]].id())
check(fw.commitChanges(), "field edits on the converted temporary layer")
del fw

# meanwhile in the office: D and E change too (-> conflicts)
parcels.startEditing()
parcels.changeAttributeValue(D, parcels.fields().indexOf("name"), "تعديل المكتب")
parcels.changeAttributeValue(E, parcels.fields().indexOf("area"), 1.0)
check(parcels.commitChanges(), "office edits committed")

# ---- QField: return -----------------------------------------------------------
diff = qfield.compare_field_copy(prj, folder)
dp = next(d for d in diff.layers if d.name == "parcels")
dw = next(d for d in diff.layers if d.name == "wells")
dr = next(d for d in diff.layers if d.name == "roads")
check(len(dp.added) == 2, f"parcels added 2 (new + duplicate): {dp.added}")
check(sorted(m["src"] for m in dp.modified) == sorted([A, B]), f"parcels modified A,B: {dp.modified}")
ma = next(m for m in dp.modified if m["src"] == A)
mb = next(m for m in dp.modified if m["src"] == B)
check(ma["fields"] == ["name"] and not ma["geometry"], "A: only the name changed")
check(mb["fields"] == [] and mb["geometry"], "B: only the shape changed")
check(dp.deleted == [C], f"parcels deleted C: {dp.deleted}")
check(sorted((c["src"], c["kind"]) for c in dp.conflicts) == sorted([(D, "modified"), (E, "deleted")]),
      f"conflicts D (both changed) and E (field deleted, office changed): {dp.conflicts}")
check(len(dw.modified) == 1 and dw.modified[0]["fields"] == ["depth"] and dw.deleted == [wid[3]],
      "wells: 1 modified depth, 1 deleted")
check(dr.count() == 0, "roads unchanged")
check(diff.count() == 2 + 2 + 1 + 2 + 2, f"total changes {diff.count()}")

# apply without committing: undoable, then roll back
counts = qfield.apply_field_changes(prj, diff)
check(counts == {"added": 2, "modified": 3, "deleted": 2}, f"applied counts {counts}")
check(parcels.isEditable() and parcels.featureCount() == 6 + 2 - 1, "parcels in edit mode with changes")
check(parcels.getFeature(A)["name"] == "قطعة معدلة", "A renamed in the edit buffer")
check(parcels.getFeature(D)["use"] == "تجاري" and parcels.getFeature(D)["name"] == "تعديل المكتب",
      "conflict D not applied (office value kept)")
parcels.undoStack().undo()
check(parcels.featureCount() == 6 and parcels.getFeature(A)["name"] == "قطعة 1", "one Undo reverts the field changes")
parcels.rollBack()
wells.rollBack()
check(diff.already_applied(), "manifest remembers it was applied")

# apply again and commit
diff2 = qfield.compare_field_copy(prj, os.path.join(folder, "Demo.gpkg"))
check(diff2.count() == diff.count(), "compare from the GeoPackage path gives the same diff")
counts = qfield.apply_field_changes(prj, diff2, commit=True)
check(not parcels.isEditable() and not wells.isEditable(), "layers committed")
fresh = QgsVectorLayer(f"{gpkg}|layername=parcels", "fresh", "ogr")
names = sorted(f["name"] for f in fresh.getFeatures())
check(fresh.featureCount() == 7, f"project gpkg now has 7 parcels ({fresh.featureCount()})")
check("قطعة جديدة" in names and "قطعة معدلة" in names and "قطعة 3" not in names, f"persisted names {names}")
check(abs(fresh.getFeature(B).geometry().area() - 4900) < 1e-6, "B has the field shape (70 x 70)")
check(fresh.getFeature(E).isValid() and fresh.getFeature(D)["use"] == "تجاري", "conflicts left untouched")
check(qfield.SRC_FIELD not in fresh.fields().names(), "tracking column never reaches the project")
check(wells.featureCount() == 3 and wells.getFeature(wid[0])["depth"] == 99, "temporary layer updated")
del fresh

# zip package + offline basemap (needs the internet)
zpath = qfield.package_for_qfield(prj, out_dir, make_zip=True)
check(zpath.endswith(".zip") and zipfile.ZipFile(zpath).testzip() is None and
      any(n.endswith(qfield.MANIFEST) for n in zipfile.ZipFile(zpath).namelist()), "ZIP field package")
dz = qfield.compare_field_copy(prj, zpath)
check(dz.count() == 0, "fresh package has no changes")
try:
    qfield.package_for_qfield(prj, out_dir, basemap_zoom=(10, 12), folder_name="no basemap")
    check(False, "offline basemap without an online basemap must be refused")
except ValueError:
    check(not os.path.exists(os.path.join(out_dir, "no basemap")), "refused before writing anything")
from qgis.core import QgsRasterLayer  # noqa: E402

osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0", "OSM", "wms")
prj.addMapLayer(osm)
try:
    qfield.package_for_qfield(prj, out_dir, folder_name="unsaved")
    check(False, "a layer added after saving must ask to save first")
except ValueError:
    check(not os.path.exists(os.path.join(out_dir, "unsaved")), "unsaved project refused")
prj.write()
try:
    from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle, QgsReferencedRectangle

    world = QgsReferencedRectangle(QgsRectangle(-170, -80, 170, 80), QgsCoordinateReferenceSystem("EPSG:4326"))
    qfield.package_for_qfield(prj, out_dir, extent=world, basemap_zoom=(0, 10), folder_name="too big")
    check(False, "a huge tile count must be refused")
except ValueError as e:
    check("tiles" in str(e), f"tile cap: {e}")
try:
    bfolder = qfield.package_for_qfield(prj, out_dir, basemap_zoom=(14, 16), folder_name="with basemap")
    mb = os.path.join(bfolder, "data", "basemap.mbtiles")
    import sqlite3

    con = sqlite3.connect(mb)
    n_tiles = con.execute("select count(*) from tiles").fetchone()[0]
    zooms = [r[0] for r in con.execute("select distinct zoom_level from tiles order by 1")]
    meta = dict(con.execute("select name, value from metadata").fetchall())
    con.close()
    check(n_tiles > 0 and zooms == [14, 15, 16], f"offline basemap MBTiles: {n_tiles} tiles, zooms {zooms}")
    check(meta["format"] == "jpg" and meta["minzoom"] == "14" and meta["maxzoom"] == "16", "MBTiles metadata")
    b = [float(v) for v in meta["bounds"].split(",")]
    check(31.2 < b[0] < 31.3 and 30.0 < b[1] < 30.2, f"MBTiles bounds around the data in lon/lat {b}")
    bl = QgsRasterLayer(mb, "b", "gdal")
    check(bl.isValid() and bl.width() > 0, "MBTiles opens as a raster")
    fp = QgsProject()
    fp.read(os.path.join(bfolder, "Demo.qgz"))
    names = {lyr.name(): lyr for lyr in fp.mapLayers().values()}
    check("Offline basemap" in names and names["Offline basemap"].isValid(), "field project has the offline basemap")
    check(not fp.layerTreeRoot().findLayer(osm.id()).isVisible(), "online basemap switched off in the field copy")
    fp.clear()
except Exception as e:  # offline machine
    print(f"SKIP offline basemap: {e}")

# ---- dialogs (built offscreen; message boxes and the browser stubbed) ------------
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.share import qfield_dialog, webmap_dialog  # noqa: E402

opened, shown = [], []
for mod in (webmap_dialog, qfield_dialog):
    mod.QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2]) or QMessageBox.StandardButton.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[2]) or QMessageBox.StandardButton.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: shown.append(a[2]) or QMessageBox.StandardButton.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)


class Bar:
    def pushSuccess(self, *a):
        shown.append(a[-1])

    pushInfo = pushWarning = pushCritical = pushSuccess


class Iface:
    canvas = QgsMapCanvas()

    def mainWindow(self):
        return None

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return Bar()


class Activity:
    events = []

    def log(self, *a):
        self.events.append(a)


class Plugin:
    iface = Iface()
    activity = Activity()


plugin = Plugin()
dlg = webmap_dialog.WebMapDialog(plugin)
check(dlg.layers.count() == 3 and len(dlg.layers.selected_layers()) == 3, "web map dialog lists the vector layers")
dlg.out.edit.setText(os.path.join(root, "exports", "from dialog.html"))
dlg._run()
check(os.path.exists(os.path.join(root, "exports", "from dialog.html")) and opened[-1].endswith("from dialog.html"),
      "web map dialog writes and opens the page")
dlg = webmap_dialog.KmzDialog(plugin)
dlg.out.edit.setText(os.path.join(root, "exports", "dialog"))
dlg._run()
check(os.path.exists(os.path.join(root, "exports", "dialog.kmz")), "KMZ dialog writes the file (.kmz added)")
dlg = qfield_dialog.SendToFieldDialog(plugin)
check(dlg.basemap.isEnabled(), "offline basemap offered when the project has an online basemap")
dlg.basemap.setChecked(True)
check("tiles" in dlg.estimate.text(), f"tile estimate shown: {dlg.estimate.text()}")
dlg.basemap.setChecked(False)
dlg.out.edit.setText(os.path.join(tmp, "dialog field"))
dlg._run()
sent = [d for d in os.listdir(os.path.join(tmp, "dialog field"))] if os.path.exists(os.path.join(tmp, "dialog field")) else []
check(sent == ["Demo - field"], f"send dialog made the field copy {sent}")
dlg = qfield_dialog.BringBackDialog(plugin)
dlg.path.setText(folder)
dlg._compare()
check(dlg.table.rowCount() == 3 and dlg.apply_btn.isEnabled(), "bring-back dialog shows the diff")
check("already applied" in dlg.note.text(), "bring-back dialog warns that the changes were applied")
check([e[1] for e in Activity.events] == ["web_map", "kmz", "field_sent"], f"activity logged {Activity.events}")

prj.clear()
print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
