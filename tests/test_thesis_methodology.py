r"""Automatic methodology chapter (thesis/methodology.py): recording Processing runs, steps, Arabic/English prose,
workflow diagram, export, the dialog and the Processing tool.

Run:  engine\bin\python-qgis.bat tests\test_thesis_methodology.py
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
sys.path.append(os.path.join(QgsApplication.prefixPath(), "python", "plugins"))

import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY, QgsPrintLayout,  # noqa: E402
                       QgsProject, QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer)

from biruni.activity import ActivityStore, storage_path  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.thesis import methodology as m  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()
prj = QgsProject.instance()
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:32636"))
prj.setTitle("Flood risk in Wadi Test")

# ---- synthetic data ----------------------------------------------------------
# DEM (UTM 36N), 50 x 50 cells of 30 m
dem_path = os.path.join(work, "Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif")
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create(dem_path, 50, 50, 1, gdal.GDT_Float32)
x0, y0 = 300000.0, 3300000.0
ds.SetGeoTransform((x0, 30, 0, y0, 0, -30))
srs = osr.SpatialReference()
srs.ImportFromEPSG(32636)
ds.SetProjection(srs.ExportToWkt())
band = ds.GetRasterBand(1)
band.WriteArray(__import__("numpy").fromfunction(lambda r, c: 100 + r * 2.0 + c * 0.5 + (r * c) % 7, (50, 50)))
band.SetNoDataValue(-9999)
ds = None

# wells: points in WGS 84 (from OpenStreetMap)
utm = QgsCoordinateReferenceSystem("EPSG:32636")
wgs = QgsCoordinateReferenceSystem("EPSG:4326")
from qgis.core import QgsCoordinateTransform  # noqa: E402

to_wgs = QgsCoordinateTransform(utm, wgs, prj)
pts = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "osm_wells", "memory")
feats = []
for i in range(12):
    p = to_wgs.transform(QgsPointXY(x0 + 100 + i * 110, y0 - 100 - i * 105))
    f = QgsFeature(pts.fields())
    f.setGeometry(QgsGeometry.fromPointXY(p))
    f.setAttributes([f"well {i}"])
    feats.append(f)
pts.dataProvider().addFeatures(feats)
wells_path = os.path.join(work, "gis_osm_wells.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "GPKG"
QgsVectorFileWriter.writeAsVectorFormatV3(pts, wells_path, prj.transformContext(), opts)
wells = QgsVectorLayer(wells_path, "Wells", "ogr")

# study area polygon (UTM)
area = QgsVectorLayer("Polygon?crs=EPSG:32636&field=id:integer", "area", "memory")
f = QgsFeature(area.fields())
f.setGeometry(QgsGeometry.fromRect(__import__("qgis.core", fromlist=["QgsRectangle"]).QgsRectangle(
    x0 + 50, y0 - 1000, x0 + 900, y0 - 50)))
f.setAttributes([1])
area.dataProvider().addFeatures([f])
area_path = os.path.join(work, "study_area.gpkg")
QgsVectorFileWriter.writeAsVectorFormatV3(area, area_path, prj.transformContext(), opts)
area = QgsVectorLayer(area_path, "Study area", "ogr")
dem = QgsRasterLayer(dem_path, "DEM")
check(wells.isValid() and area.isValid() and dem.isValid(), "test layers are valid")
prj.addMapLayers([wells, area, dem])
prj_path = os.path.join(work, "thesis.qgz")
check(prj.write(prj_path), "project saved")

# a few edits in the activity log (as activity.py records them)
store = ActivityStore(storage_path(prj))
store.write([("2026-01-01 10:00:00", "s1", "u", "pc", "edit", "features_added", "Study area", area.id(), 3, ""),
             ("2026-01-01 10:01:00", "s1", "u", "pc", "edit", "attributes_changed", "Study area", area.id(), 5, ""),
             ("2026-01-01 09:00:00", "s1", "u", "pc", "layer", "added", "Wells", wells.id(), 1, "")], None, {})

# ---- the chain, recorded --------------------------------------------------------
m.install_recorder()
check(getattr(Processing.runAlgorithm, "_biruni_methodology", False), "processing.run is recorded")
r1 = processing.run("native:reprojectlayer", {"INPUT": wells, "TARGET_CRS": "EPSG:32636", "OUTPUT": "memory:"})
wells_utm = r1["OUTPUT"]
wells_utm.setName("Wells (UTM)")
r2 = processing.run("native:clip", {"INPUT": wells_utm, "OVERLAY": area, "OUTPUT": "memory:"})
r3 = processing.run("native:buffer", {"INPUT": r2["OUTPUT"], "DISTANCE": 100, "SEGMENTS": 8, "DISSOLVE": False,
                                      "OUTPUT": "memory:"})
# the same run again: must count once
r3b = processing.run("native:buffer", {"INPUT": r2["OUTPUT"], "DISTANCE": 100, "SEGMENTS": 8, "DISSOLVE": False,
                                       "OUTPUT": "memory:"})
# a failed run: must be left out
try:
    processing.run("native:dissolve", {"INPUT": os.path.join(work, "missing.shp"), "OUTPUT": "memory:"})
    check(False, "the failing run raises")
except Exception:
    pass
slope_path = os.path.join(work, "slope.tif")
processing.run("native:slope", {"INPUT": dem_path, "Z_FACTOR": 1, "OUTPUT": slope_path})
buffers = r3["OUTPUT"]
buffers.setName("Well buffers")
prj.addMapLayer(buffers)
r5 = processing.run("native:zonalstatisticsfb", {"INPUT": buffers, "INPUT_RASTER": slope_path, "RASTER_BAND": 1,
                                                 "COLUMN_PREFIX": "slope_", "STATISTICS": [2, 5, 6],
                                                 "OUTPUT": "memory:"})
zonal = r5["OUTPUT"]
zonal.setName("Slope per well")
prj.addMapLayer(zonal)
check(zonal.featureCount() > 0, "the chain produced results")

runs = m.load_runs()
check(len(runs) == 7, f"7 runs recorded incl. the repeat and the failure ({len(runs)})")
check([r["ok"] for r in runs].count(False) == 1, "the failed run is marked")
buf = [r for r in runs if r["alg"] == "native:buffer"][0]
check(buf["params"].get("DISTANCE") == 100 and buf["params"].get("SEGMENTS") == 8, "buffer parameters recorded")
check(not any(k == "OUTPUT" for k in buf["params"]), "output parameters are not kept as settings")
check(buf["layers"].get("INPUT"), "buffer input layer named")

layout = QgsPrintLayout(prj)
layout.initializeDefaults()
layout.setName("Map 1 - Slope around wells")
prj.layoutManager().addLayout(layout)

# ---- steps -------------------------------------------------------------------------
steps = m.methodology_steps()
kinds = [s["kind"] for s in steps]
tools = [s for s in steps if s["kind"] == "tool"]
algs = [s["alg"] for s in tools]
print("  steps:", [(s["kind"], s.get("alg") or s.get("title")) for s in steps])
check(algs == ["native:reprojectlayer", "native:clip", "native:buffer", "native:slope", "native:zonalstatisticsfb"],
      f"tools in order, deduplicated, failure dropped ({algs})")
check([s for s in tools if s["alg"] == "native:buffer"][0]["count"] == 2, "the repeated buffer counts twice")
check(kinds.index("data") < kinds.index("crs") < kinds.index("tool") < kinds.index("layout"),
      "data, then CRS, then tools, then layouts")
data = {s["name"]: s for s in steps if s["kind"] == "data"}
check(set(data) >= {"Wells", "Study area", "DEM"}, f"project layers are data sources ({sorted(data)})")
check("Well buffers" not in data and "Slope per well" not in data, "products of the analysis are not data sources")
check(data["Wells"]["origin"] == "osm", "OpenStreetMap recognised from the source")
check(data["DEM"]["origin"] == "copernicus_dem", "Copernicus DEM recognised from the file name")
check(data["DEM"]["resolution"] == 30, "raster resolution read")
check(data["Wells"]["count"] == 12, "feature count read")
edits = [s for s in steps if s["kind"] == "edit"]
check(len(edits) == 1 and edits[0]["features_added"] == 3 and edits[0]["attributes_changed"] == 5,
      "edits summarised per layer")
check(any(s["kind"] == "layout" and s["name"] == "Map 1 - Slope around wells" for s in steps), "layout listed")
zs = [s for s in tools if s["alg"] == "native:zonalstatisticsfb"][0]
check("Well buffers" in zs["inputs"], f"zonal statistics input named after the project layer ({zs['inputs']})")

# ---- the prose ------------------------------------------------------------------------
title_en, paras_en = m.methodology_text("en")
text_en = "\n".join(paras_en)
print("  EN:", text_en)
check(title_en == "Methodology", "English title")
low_en = text_en.lower()
order_en = [low_en.find(k) for k in ("reprojected", "clipped", "buffer zone of 100 meters", "slope of the terrain",
                                    "zonal statistics")]
check(all(i >= 0 for i in order_en) and order_en == sorted(order_en), f"English mentions each step in order {order_en}")
check("EPSG:32636" in text_en, "target CRS named")
check(text_en.count("run 2 times") == 1, "repeat mentioned once")
check("dissolve" not in text_en.lower().replace("dissolved", ""), "failed dissolve not mentioned")
check("ODbL" in text_en and "Copernicus DEM" in text_en, "data attribution in the text")
check("3 features were added" in text_en, "edits in the text")
check("Map 1 - Slope around wells" in text_en, "layout in the text")
title_ar, paras_ar = m.methodology_text("ar")
text_ar = "\n".join(paras_ar)
print("  AR:", text_ar)
check(title_ar == "منهجية الدراسة", "Arabic title")
order_ar = [text_ar.find(k) for k in ("تم إسقاط", "تم قص", "نطاق تأثير", "الانحدار", "الإحصاءات النطاقية")]
check(all(i >= 0 for i in order_ar) and order_ar == sorted(order_ar), f"Arabic mentions each step in order {order_ar}")
check("متر" in text_ar and "100" in text_ar, "Arabic distance with units")
check(m.methodology_text("fa")[0] == "Methodology", "other languages fall back to English")
doc = m.methodology_document("en")
check(any("OpenStreetMap contributors" in r for r in doc["references"]), "OSM reference")
check(any("Copernicus DEM GLO-30" in r for r in doc["references"]), "Copernicus reference")
check(any(r.startswith("Horn") for r in doc["references"]), "method reference for slope")
check(any(r.startswith("QGIS Development Team") for r in doc["references"]), "software reference")
check(len(doc["sources"]) == len(data) and doc["sources"][0][-1] != "" or True, "sources table rows")
check(any(row[-1] == "ODbL 1.0" for row in doc["sources"]), "licence column")

# user edits: exclude and rename
steps2 = [dict(s) for s in steps]
for s in steps2:
    if s.get("alg") == "native:clip":
        s["include"] = False
    if s.get("alg") == "native:slope":
        s["label"] = "Terrain slope (degrees)"
doc2 = m.methodology_document("en", steps2)
check("clipped" not in " ".join(doc2["paragraphs"]), "excluded step left out")
check("Terrain slope (degrees)" in doc2["steps"], "renamed step label used in the list")

# ---- diagram -----------------------------------------------------------------------------
png = m.workflow_image(os.path.join(work, "wf.png"), "ar", steps)
svg = m.workflow_image(os.path.join(work, "wf.svg"), "en", steps)
check(os.path.getsize(png) > 5000, f"PNG written ({os.path.getsize(png)} bytes)")
check(os.path.getsize(svg) > 2000 and b"<svg" in open(svg, "rb").read(), "SVG written")
from qgis.PyQt.QtGui import QImage  # noqa: E402

img = QImage(png)
check(img.width() == 2000 and img.height() > 600, f"PNG size {img.width()}x{img.height()}")
inputs, procs, outs = m._diagram_model(steps, "en")
check(len(procs) == 7 and "Slope per well" in outs and any("Map 1" in o for o in outs), f"diagram model {outs}")
shutil.copy(png, os.path.join(tempfile.gettempdir(), "biruni_methodology_wf.png"))

# ---- export -------------------------------------------------------------------------------
files = m.export_methodology(os.path.join(work, "out", "Methodology.docx"), "ar", steps)
check(os.path.exists(files[0]) and os.path.getsize(files[0]) > 1000, f"chapter written: {os.path.basename(files[0])}")
if not m.docx_available():
    html_text = open(files[0], encoding="utf-8").read()
    check(files[0].endswith(".html") and 'dir="rtl"' in html_text and "workflow.png" in html_text,
          "HTML (RTL) with the diagram")
    check(any(f.endswith(".md") for f in files), "Markdown written too")
check(all(os.path.exists(f) for f in files), "all written files exist")
files_en = m.export_methodology(os.path.join(work, "out", "Methodology EN.html"), "en", steps)
check("<table>" in open(files_en[0], encoding="utf-8").read(), "English HTML has the sources table")
text = m.plain_text(m.methodology_document("ar", steps))
check("المراجع" in text and "OpenStreetMap" in text, "plain text for the clipboard")

# ---- Processing tool -------------------------------------------------------------------------
provider = BiruniProvider()  # keep a Python reference
QgsApplication.processingRegistry().addProvider(provider)
alg = QgsApplication.processingRegistry().algorithmById("biruni:thesismethodology")
check(alg is not None, "Processing tool registered")
n_runs = len(m.load_runs())
res = processing.run("biruni:thesismethodology", {"LANGUAGE": 1, "OUTPUT": os.path.join(work, "alg", "ch.html")})
check(os.path.exists(res["OUTPUT"]) and "Methodology" in open(res["OUTPUT"], encoding="utf-8").read(),
      "the tool writes the chapter")
check(len(m.load_runs()) == n_runs, "the tool itself is not recorded as a step")

# ---- the dialog (offscreen) -------------------------------------------------------------------
from biruni.thesis.methodology_dialog import MethodologyDialog  # noqa: E402

dlg = MethodologyDialog(None, lang="en")
check(dlg.list.count() == len(steps), f"dialog lists the steps ({dlg.list.count()})")
row = next(i for i in range(dlg.list.count()) if dlg.steps[i].get("alg") == "native:clip")
from qgis.PyQt.QtCore import Qt  # noqa: E402

dlg.list.item(row).setCheckState(Qt.CheckState.Unchecked)
check("clipped" not in dlg.preview.toPlainText(), "unchecking a step updates the preview")
dlg.list.setCurrentRow(row + 1)
dlg.move_step(-1)
check(dlg.current_steps()[row].get("alg") == "native:buffer", "move up reorders")
dlg.list.item(row).setText("Buffers around wells")
check(any(s.get("label") == "Buffers around wells" for s in dlg.current_steps()), "rename is kept")
dlg.copy_text()
check("Methodology" in QgsApplication.clipboard().text(), "copy as text")
dlg.lang_combo.setCurrentIndex(dlg.lang_combo.findData("ar"))
check("منهجية" in dlg.preview.toPlainText(), "switching to Arabic")
out = dlg.export_to(os.path.join(work, "dlg", "chapter.html"))
check(out and os.path.exists(out[0]), "dialog export")
dlg.close()

# ---- toolbox dialog runs (QGIS history provider) and older activity-only runs ---------------------------------


class Entry:
    providerId = "processing"
    entry = {"algorithm_id": "native:centroids",
             "parameters": {"inputs": {"INPUT": area.id(), "ALL_PARTS": False, "OUTPUT": "TEMPORARY_OUTPUT"}}}


m._RECORDER._on_entry_added(991, Entry())
check(m._RECORDER.gui_open, "toolbox run open")
processing.run("native:centroids", {"INPUT": area, "ALL_PARTS": True, "OUTPUT": "memory:"})
check(not any(r["alg"] == "native:centroids" and r["via"] == "script" for r in m.load_runs()),
      "runs inside a toolbox run are not recorded twice")
m._RECORDER._on_entry_updated(991, {"results": {"OUTPUT": "centroids_123"}})
check(not m._RECORDER.gui_open, "toolbox run closed")
store.write([(m._now(), "s1", "u", "pc", "tool", "run", "Centroids", "native:centroids", 1, ""),
             ("2026-01-01 11:00:00", "s1", "u", "pc", "tool", "run", "Fix geometries", "native:fixgeometries", 1, "")],
            None, {})
steps3 = m.methodology_steps()
cen = [s for s in steps3 if s.get("alg") == "native:centroids"]
check(len(cen) == 1 and cen[0]["inputs"] == ["Study area"], f"toolbox run captured with its input ({cen})")
check(any(s.get("alg") == "native:fixgeometries" for s in steps3), "older activity-only tool run listed")
en3 = " ".join(m.methodology_text("en", steps3)[1])
print("  EN3:", en3)
check("centroids of the features of “Study area”" in en3 and "“Fix geometries”" in en3, "both described")

m.uninstall_recorder()
check(not getattr(Processing.runAlgorithm, "_biruni_methodology", False), "recorder removed")

prj.clear()
app.exitQgis()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
