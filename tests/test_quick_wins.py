"""Export photos, georeference a PDF/image, layer facts, show only inside an area.

Run:  engine\\bin\\python-qgis.bat tests\\test_quick_wins.py
"""
import csv
import os
import shutil
import sys
import tempfile

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsVectorFileWriter, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
from osgeo import gdal, ogr, osr  # noqa: E402
from PIL import Image  # noqa: E402

from biruni import layer_info, spatial_filter  # noqa: E402
from biruni.algorithms import export_photos as ep  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


TMP = tempfile.mkdtemp(prefix="biruni_qw_")
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def jpeg(path, size=(640, 480), color=(200, 30, 30)):
    Image.new("RGB", size, color).save(path, "JPEG", quality=95)
    with open(path, "rb") as f:
        return f.read()


# ==== names ==========================================================================================================
check(ep.sanitise('a/b:c?d*"e<f>|g') == "a_b_c_d__e_f__g", "forbidden characters replaced")
check(ep.sanitise("CON") == "_CON" and ep.sanitise("lpt1.txt") == "_lpt1.txt", "reserved Windows names")
check(ep.sanitise("photo. . ") == "photo" and ep.sanitise("   ") == "_", "trailing dots/spaces, empty")
check(ep.sanitise("الروضة عمود ١٥") == "الروضة عمود ١٥", "Arabic kept")
check(ep.render_template("{Zone}_{pole_no}_{n}", {"zone": "Z1", "pole_no": 15.0, "n": 2}) == "Z1_15_2",
      "template: case-insensitive fields, 15.0 -> 15")
check(ep.render_template("{missing}x", {}) == "x", "unknown field -> empty")
u = ep.UniqueNames()
d = os.path.join(TMP, "names")
os.makedirs(d)
open(os.path.join(d, "a.jpg"), "w").close()
check([u.take(d, "a", ".jpg"), u.take(d, "a", ".jpg"), u.take(d, "A", ".jpg")] == ["a_2.jpg", "a_3.jpg", "A_4.jpg"],
      "unique names (on disk, this run, case-insensitive)")
check(ep.guess_ext(b"\xff\xd8\xff\xe0abc") == ".jpg" and ep.guess_ext(b"%PDF-1.4") == ".pdf"
      and ep.guess_ext(b"", "x.JPEG") == ".jpg" and ep.guess_ext(b"zz", "", "image/png") == ".png", "file types")

# ==== a GeoPackage with attachments, a path field and a blob field ==================================================
data_dir = os.path.join(TMP, "data")
os.makedirs(os.path.join(data_dir, "DCIM"))
gpkg = os.path.join(data_dir, "poles.gpkg")
ds = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
lyr = ds.CreateLayer("poles", srs, ogr.wkbPoint)
for name, typ in (("GlobalID", ogr.OFTString), ("zone", ogr.OFTString), ("pole_no", ogr.OFTInteger),
                  ("photo", ogr.OFTString), ("img", ogr.OFTBinary)):
    lyr.CreateField(ogr.FieldDefn(name, typ))
blob_bytes = jpeg(os.path.join(TMP, "blob.jpg"), (300, 200), (10, 120, 10))
rows = [("{AAA-1}", "الروضة", 15, "DCIM/p1.jpg", blob_bytes, (500000, 2700000)),
        ("{AAA-2}", "الروضة", 15, "DCIM/p2.jpg;DCIM/nothere.jpg", None, (500100, 2700000)),  # same name -> _2
        ("{AAA-3}", "Z/2", 7, "", None, (500200, 2700050))]
for p in ("p1", "p2"):
    jpeg(os.path.join(data_dir, "DCIM", p + ".jpg"))
for gid, zone, no, photo, img, (x, y) in rows:
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("GlobalID", gid)
    f.SetField("zone", zone)
    f.SetField("pole_no", no)
    if photo:
        f.SetField("photo", photo)
    if img:
        f.SetFieldBinaryFromHexString("img", img.hex())
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(x, y)
    f.SetGeometry(g)
    lyr.CreateFeature(f)
att = ds.CreateLayer("poles__ATTACH", None, ogr.wkbNone)
for name, typ in (("REL_GLOBALID", ogr.OFTString), ("CONTENT_TYPE", ogr.OFTString), ("ATT_NAME", ogr.OFTString),
                  ("DATA", ogr.OFTBinary)):
    att.CreateField(ogr.FieldDefn(name, typ))
att_bytes = jpeg(os.path.join(TMP, "att.jpg"), (800, 600), (0, 0, 200))
for gid, name in (("{AAA-1}", "front.jpg"), ("{AAA-1}", "back.jpg"), ("{AAA-3}", "doc.pdf")):
    f = ogr.Feature(att.GetLayerDefn())
    f.SetField("REL_GLOBALID", gid)
    f.SetField("ATT_NAME", name)
    f.SetField("CONTENT_TYPE", "application/pdf" if name.endswith("pdf") else "image/jpeg")
    f.SetFieldBinaryFromHexString("DATA", (b"%PDF-1.4 test" if name.endswith("pdf") else att_bytes).hex())
    att.CreateFeature(f)
ds = None

poles = QgsVectorLayer(f"{gpkg}|layername=poles", "poles", "ogr")
check(poles.isValid() and poles.featureCount() == 3, "test layer")
QgsProject.instance().addMapLayer(poles)
check(ep.attachment_table(gpkg, "poles") == "poles__ATTACH", "attachments table found")


def export(**kw):
    out = os.path.join(TMP, "out_" + str(len(os.listdir(TMP))))
    params = {"INPUT": poles, "SOURCE": 0, "TEMPLATE": "{zone}_{pole_no}_{n}", "OUTPUT": out,
              "INDEX": os.path.join(out + "_index.xlsx")}
    params.update(kw)
    res = processing.run("biruni:exportphotos", params, context=ctx, feedback=fb)
    files = []
    for root, _d, fs in os.walk(out):
        files += [os.path.relpath(os.path.join(root, f), out) for f in fs]
    return res, sorted(files), out


# attachments only
res, files, out = export(SOURCE=1)
check(files == sorted(["الروضة_15_1.jpg", "الروضة_15_2.jpg", "Z_2_7_1.pdf"]),
      f"attachments: named from fields, Arabic, '/' replaced, pdf kept ({files})")
check(res["FILE_COUNT"] == 3, "attachments: 3 files")
with open(os.path.join(out, "Z_2_7_1.pdf"), "rb") as f:
    check(f.read().startswith(b"%PDF"), "pdf bytes copied unchanged")

# automatic: attachments + blob field; path field chosen explicitly
res, files, out = export(SOURCE=0, FIELD="photo")
check(res["MISSING_COUNT"] == 1, "missing file counted")
check(len(files) == 3 + 2 + 1, f"automatic: attachments + path field + blob field ({files})")
res, files, out = export(SOURCE=0, TEMPLATE="{fid}-{name}")
check(len(files) == 4 and "1-front.jpg" in files and "1-.jpg" in files,
      f"automatic finds attachments and the blob field; {{name}} = original name ({files})")
res, files, out = export(SOURCE=2, FIELD="photo", TEMPLATE="{zone}_{pole_no}")
check(files == sorted(["الروضة_15.jpg", "الروضة_15_2.jpg"]), f"path field: DCIM relative, collision -> _2 ({files})")
res, files, out = export(SOURCE=3, FIELD="img", TEMPLATE="{pole_no}")
check(files == ["15.jpg"], f"blob field, type from the bytes ({files})")

# subfolders, selection, resize + GPS
poles.selectByIds([1])
res, files, out = export(SOURCE=1, SUBFOLDER="zone", SELECTED=True, MAX_SIZE=200, QUALITY=70, GPS=True,
                         INDEX=os.path.join(TMP, "index_sel.csv"))
check(files == sorted([os.path.join("الروضة", "الروضة_15_1.jpg"), os.path.join("الروضة", "الروضة_15_2.jpg")]),
      f"subfolder per zone, selected only ({files})")
img = Image.open(os.path.join(out, files[0]))
check(max(img.size) == 200, f"made smaller: {img.size}")
gps = img.getexif().get_ifd(0x8825)
lat = gps[2][0] + gps[2][1] / 60 + gps[2][2] / 3600 if gps else None
check(gps and gps[1] == "N" and gps[3] == "E" and abs(lat - 24.408) < 0.01, f"EXIF GPS written ({lat})")
with open(os.path.join(TMP, "index_sel.csv"), encoding="utf-8-sig") as f:
    rows_csv = list(csv.reader(f))
check(rows_csv[0][:3] == ["file", "fid", "original_name"] and len(rows_csv) == 3 and "zone" in rows_csv[0],
      "CSV index")
poles.removeSelection()
from openpyxl import load_workbook  # noqa: E402

res, files, out = export(SOURCE=1)
wb = load_workbook(res["INDEX"])
check(wb.active.max_row == 4, "Excel index: header + 3 files")
try:
    export(SOURCE=1, SELECTED=True)
    check(False, "selected only with nothing selected must fail")
except Exception as e:  # noqa: BLE001
    check("selected" in str(e).lower(), "nothing selected -> clear message")

# ==== georeference a PDF / image ======================================================================================
W, H = 400, 300
png = os.path.join(TMP, "plan.png")
Image.new("RGB", (W, H), (255, 255, 255)).save(png)
gcps = "\n".join(f"{px} {py} {500000 + px * 0.5} {2700000 - py * 0.5}" for px, py in ((0, 0), (400, 0), (0, 300),
                                                                                      (400, 300)))
dest = os.path.join(TMP, "plan_geo.tif")
res = processing.run("biruni:georefpdf", {"INPUT": png, "GCP_TEXT": gcps, "CRS": "EPSG:32637", "OUTPUT": dest},
                     context=ctx, feedback=fb)
check(res["GCP_RMS"] < 1e-6 and res["TRANSFORMATION"] == "affine", f"image: RMS {res['GCP_RMS']}")
g = gdal.Open(dest)
gt = g.GetGeoTransform()
check(abs(gt[0] - 500000) < 0.6 and abs(gt[3] - 2700000) < 0.6 and abs(gt[1] - 0.5) < 0.01,
      f"image: corner and pixel size {gt[:4]}")
check(abs(gt[0] + gt[1] * g.RasterXSize - 500200) < 0.6 and abs(gt[3] + gt[5] * g.RasterYSize - 2699850) < 0.6,
      "image: opposite corner")
check("32637" in g.GetProjection() or "UTM zone 37N" in g.GetProjection(), "image: CRS written")
g = None

pdf = os.path.join(TMP, "two_pages.pdf")
Image.new("RGB", (595, 842), "white").save(pdf, "PDF", resolution=72, save_all=True,
                                            append_images=[Image.new("RGB", (842, 595), "white")])
from biruni.algorithms import georef_pdf as gp  # noqa: E402

check(gp.pdf_page_count(pdf) == 2, "PDF: two pages")
dpi = 144
pts2 = f"0 0 1000 2000\n{842 * 2} 0 {1000 + 842} 2000\n0 {595 * 2} 1000 {2000 - 595}"
dest2 = os.path.join(TMP, "page2.tif")
res = processing.run("biruni:georefpdf", {"INPUT": pdf, "PAGE": 2, "DPI": dpi, "GCP_TEXT": pts2, "CRS": "EPSG:32637",
                                          "OUTPUT": dest2}, context=ctx, feedback=fb)
g = gdal.Open(dest2)
gt = g.GetGeoTransform()
check(res["GCP_RMS"] < 1e-6 and abs(gt[1] - 0.5) < 0.01 and abs(gt[0] - 1000) < 1,
      f"PDF page 2 (landscape) at 144 dpi: pixel 0.5 m ({gt[:2]})")
g = None
try:
    processing.run("biruni:georefpdf", {"INPUT": pdf, "PAGE": 3, "GCP_TEXT": pts2, "CRS": "EPSG:32637",
                                        "OUTPUT": os.path.join(TMP, "p3.tif")}, context=ctx, feedback=fb)
    check(False, "page 3 must fail")
except Exception as e:  # noqa: BLE001
    check("2" in str(e), f"page out of range -> message ({e})")
try:
    processing.run("biruni:georefpdf", {"INPUT": pdf, "MODE": 1, "OUTPUT": os.path.join(TMP, "x.tif")},
                   context=ctx, feedback=fb)
    check(False, "plain PDF in GeoPDF mode must fail")
except Exception as e:  # noqa: BLE001
    check("control points" in str(e), "plain PDF in GeoPDF mode -> asks for control points")
geopdf = os.path.join(TMP, "geo.pdf")
made = gdal.Translate(geopdf, dest, format="PDF")
made = None
if os.path.exists(geopdf) and gdal.Open(geopdf) is not None and gdal.Open(geopdf).GetProjection():
    res = processing.run("biruni:georefpdf", {"INPUT": geopdf, "MODE": 1, "DPI": 72,
                                              "OUTPUT": os.path.join(TMP, "from_geopdf.tif")},
                         context=ctx, feedback=fb)
    g = gdal.Open(res["OUTPUT"])
    gt = g.GetGeoTransform()
    check(abs(gt[0] - 500000) < 2 and abs(gt[3] - 2700000) < 2, f"GeoPDF: its own georeference used ({gt[:4]})")
    g = None
else:
    print("     (GDAL here cannot write a GeoPDF - GeoPDF mode checked only for the error case)")

# ==== layer facts =====================================================================================================
mem = QgsVectorLayer("Point?crs=EPSG:4326&field=a:integer", "mem", "memory")
feats = []
for i in range(5):
    f = QgsFeature(mem.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
    f.setAttributes([i])
    feats.append(f)
mem.dataProvider().addFeatures(feats)
facts = layer_info.layer_facts(mem)
check(facts["count"] == 5 and facts["size"] is None and "EPSG:4326" in facts["crs"], "facts: memory layer")
facts = layer_info.layer_facts(poles)
check(facts["count"] == 3 and facts["size"] > 1000 and facts["modified"] and facts["path"].endswith("poles.gpkg")
      and facts.get("table") == "poles", "facts: GeoPackage (count, size, date, path, table)")
shp = os.path.join(TMP, "shp", "pts.shp")
os.makedirs(os.path.dirname(shp))
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "ESRI Shapefile"
opts.fileEncoding = "UTF-8"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, shp, QgsProject.instance().transformContext(), opts)
shp_layer = QgsVectorLayer(shp, "pts", "ogr")
facts = layer_info.layer_facts(shp_layer)
single = os.path.getsize(shp)
check(facts["size"] > single and facts["encoding"].upper().replace("-", "") == "UTF8",
      f"facts: shapefile size counts its .dbf/.shx/.prj ({facts['size']} > {single}), encoding {facts['encoding']}")
text = layer_info.facts_text(layer_info.layer_facts(poles))
check("3" in text and "poles.gpkg" in text and "<table>" in text, "tooltip text")
check(layer_info.human_size(1536) == "1.5 KB" and layer_info.human_size(10) == "10 B", "sizes")
fake = dict(facts, provider="postgres", count=None)
check("database" in layer_info.facts_text(fake), "remote layers are not counted")
QgsProject.instance().addMapLayers([mem, shp_layer])
n = layer_info.set_feature_counts(True)
check(n == 3 and layer_info.feature_counts_shown(), "feature counts shown for every vector layer")
layer_info.set_feature_counts(False)
check(not layer_info.feature_counts_shown(), "and hidden again")
QgsProject.instance().removeMapLayers([mem.id(), shp_layer.id()])

# ==== show only inside an area ========================================================================================
grid = QgsVectorLayer("Point?crs=EPSG:32637&field=val:integer", "g_mem", "memory")
feats = []
for i in range(10):
    for j in range(10):
        f = QgsFeature(grid.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({500000 + i * 10} {2700000 + j * 10})"))
        f.setAttributes([i * 10 + j])
        feats.append(f)
grid.dataProvider().addFeatures(feats)
paths = {}
for driver, ext in (("GPKG", "gpkg"), ("ESRI Shapefile", "shp")):
    p = os.path.join(TMP, "sf", f"grid.{ext}")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = driver
    QgsVectorFileWriter.writeAsVectorFormatV3(grid, p, QgsProject.instance().transformContext(), o)
    paths[ext] = p
g_gpkg = QgsVectorLayer(paths["gpkg"], "g_gpkg", "ogr")
g_shp = QgsVectorLayer(paths["shp"], "g_shp", "ogr")
g_mem = grid
g_gpkg.setSubsetString('"val" >= 10')        # an existing filter must survive
QgsProject.instance().addMapLayers([g_gpkg, g_shp, g_mem])
before = {lyr.id(): lyr.subsetString() for lyr in (g_gpkg, g_shp, g_mem)}
# area: x 500000..500045, y 2700000..2700045 -> i, j in 0..4 -> 25 points (WGS84 polygon to test reprojection)
area = QgsGeometry.fromWkt("POLYGON((499999 2699999, 500045 2699999, 500045 2700045, 499999 2700045, "
                           "499999 2699999))")
crs_utm = QgsCoordinateReferenceSystem("EPSG:32637")
res = spatial_filter.apply_to_layers([g_gpkg, g_shp, g_mem], area, crs_utm)
counts = {lyr.name(): lyr.featureCount() for lyr in (g_gpkg, g_shp, g_mem)}
check(counts == {"g_gpkg": 20, "g_shp": 25, "g_mem": 25},
      f"filtered: gpkg keeps its own filter (val>=10) too, shapefile, memory ({counts})")
check(res["filtered"] == {g_gpkg.id(): 20, g_shp.id(): 25, g_mem.id(): 25}, "result counts")
check(spatial_filter.is_active() and set(spatial_filter.filtered_layer_ids()) == set(before), "state in the project")
check(g_gpkg.subsetString().startswith('("val" >= 10) AND ('), f"combined filter: {g_gpkg.subsetString()[:40]}")
geom, crs = spatial_filter.filter_area()
check(geom is not None and abs(geom.area() - area.area()) < 1e-6 and crs.authid() == "EPSG:32637", "area remembered")
# in degrees, applied again: the previous one is cleared first
wgs = QgsGeometry(area)
from qgis.core import QgsCoordinateTransform  # noqa: E402

wgs.transform(QgsCoordinateTransform(crs_utm, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()))
spatial_filter.apply_to_layers([g_gpkg, g_shp, g_mem], wgs, QgsCoordinateReferenceSystem("EPSG:4326"))
check(g_shp.featureCount() == 25 and g_gpkg.subsetString().count("val") == 1,
      "area in degrees reprojected; applying again does not stack filters")
n = spatial_filter.clear_layers()
check(n == 3 and {lyr.id(): lyr.subsetString() for lyr in (g_gpkg, g_shp, g_mem)} == before,
      "clear puts back exactly the previous filters")
check(g_gpkg.featureCount() == 90 and g_shp.featureCount() == 100 and g_mem.featureCount() == 100, "counts back")
check(not spatial_filter.is_active(), "state cleared")
# nothing inside / a layer being edited
far = QgsGeometry.fromWkt("POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))")
g_mem.startEditing()
res = spatial_filter.apply_to_layers([g_gpkg, g_shp, g_mem], far, crs_utm)
check(g_gpkg.featureCount() == 0 and g_shp.featureCount() == 0, "nothing inside -> nothing shown")
check([s[0] for s in res["skipped"]] == ["g_mem"] and g_mem.subsetString() == "", "layer being edited is skipped")
g_mem.rollBack()
spatial_filter.clear_layers()
check(g_gpkg.subsetString() == '"val" >= 10' and g_shp.subsetString() == "", "cleared again")
# the state survives saving and reopening the project
spatial_filter.apply_to_layers([g_shp], area, crs_utm)
proj_path = os.path.join(TMP, "sf", "p.qgz")
QgsProject.instance().write(proj_path)
QgsProject.instance().clear()
QgsProject.instance().read(proj_path)
check(spatial_filter.is_active(), "filter remembered in the saved project")
reopened = QgsProject.instance().mapLayersByName("g_shp")[0]
check(reopened.featureCount() == 25, "reopened layer still filtered")
spatial_filter.clear_layers()
check(reopened.subsetString() == "" and reopened.featureCount() == 100, "and cleared after reopening")

QgsProject.instance().clear()
shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
