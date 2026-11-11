"""Overture download, formats, Google Earth tours and KML import, website publishing, the CRS doctor and the
GNSS quality check.

Run:  engine\\bin\\python-qgis.bat tests\\test_trends.py
(the live Overture download runs only when stac.overturemaps.org answers)
"""
import json
import math
import os
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsFeature,
                       QgsField, QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                       QgsRendererCategory, QgsSymbol, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor

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

gdal.UseExceptions()
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_trends_")
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def mem(geom, crs, rows, fields=()):
    """Memory layer from [(wkt, [attrs])]."""
    spec = "&".join(f"field={n}:{t}" for n, t in fields)
    lyr = QgsVectorLayer(f"{geom}?crs={crs}" + (f"&{spec}" if spec else ""), "mem", "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


# ======================================================================== formats
print("--- formats")
for drv in ("Parquet", "FlatGeobuf", "PMTiles", "COG"):
    d = gdal.GetDriverByName(drv)
    check(d is not None and d.GetMetadataItem("DCAP_CREATE") == "YES", f"GDAL can write {drv}")
pts = mem("Point", "EPSG:4326", [("POINT(46.68 24.71)", ["a"]), ("POINT(46.69 24.72)", ["ب"])], [("name", "string")])
for fmt, ext in (("FlatGeobuf", "fgb"), ("GeoJSONSeq", "geojsons")):
    path = os.path.join(TMP, f"round.{ext}")
    processing.run("native:savefeatures", {"INPUT": pts, "OUTPUT": path}, context=ctx, feedback=fb)
    back = QgsVectorLayer(path, "b", "ogr")
    names = sorted(str(f["name"]) for f in back.getFeatures()) if back.isValid() else []
    check(back.isValid() and back.featureCount() == 2 and names == ["a", "ب"], f"{fmt} round trip with Arabic")
# GeoParquet: reading works (see the live Overture test); writing fails in this GDAL/Arrow build
try:
    pq = ogr.GetDriverByName("Parquet").CreateDataSource(os.path.join(TMP, "w.parquet"))
    pl = pq.CreateLayer("w", None, ogr.wkbPoint)
    pf = ogr.Feature(pl.GetLayerDefn())
    pf.SetGeometry(ogr.CreateGeometryFromWkt("POINT(1 2)"))
    pl.CreateFeature(pf)
    del pf, pl
    pq.Close()
    print("     GeoParquet write: works")
except RuntimeError as e:
    print(f"     GeoParquet write: broken in this engine ({str(e)[:70]}…)")
ras = os.path.join(TMP, "src.tif")
ds = gdal.GetDriverByName("GTiff").Create(ras, 64, 64, 1, gdal.GDT_Float32)
ds.SetGeoTransform([46.6, 0.001, 0, 24.8, 0, -0.001])
srs = osr.SpatialReference()
srs.ImportFromEPSG(4326)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).Fill(5)
ds = None
cog = os.path.join(TMP, "cog.tif")
gdal.Translate(cog, ras, format="COG")
info = gdal.Info(cog, format="json")
check(info.get("metadata", {}).get("IMAGE_STRUCTURE", {}).get("LAYOUT") == "COG", "COG written and recognised")

# ======================================================================== Overture
print("--- Overture")
from biruni.algorithms import overture as ov  # noqa: E402

check(ov.intersecting_files([[0, 0, 10, 10], [20, 20, 30, 30], [-5, -5, 1, 1]], (0.5, 0.5, 2, 2)) == [0, 2],
      "files whose boxes touch the area are picked")
check(ov._names('{"ar": "الرياض", "en": "Riyadh"}') == {"ar": "الرياض", "en": "Riyadh"} and ov._names(None) == {}
      and ov._names("not json") == {}, "names.common JSON parsed safely")
check(ov._first_text('["+966 11 000"]') == "+966 11 000" and ov._first_text(["a", "b"]) == "a"
      and ov._first_text('[{"freeform": "طريق الملك فهد", "locality": "الرياض", "country": "SA"}]')
      == "طريق الملك فهد, الرياض, SA", "lists and addresses become readable text")
# a local stand-in with Overture's flattened column names (GDAL shows GeoParquet structs as "names.primary" …)
gpq = os.path.join(TMP, "overture_like.gpkg")
drv = ogr.GetDriverByName("GPKG")
ds = drv.CreateDataSource(gpq)
lyr = ds.CreateLayer("building", srs, ogr.wkbUnknown)
for name, typ in (("id", ogr.OFTString), ("names.primary", ogr.OFTString), ("names.common", ogr.OFTString),
                  ("subtype", ogr.OFTString), ("class", ogr.OFTString), ("height", ogr.OFTReal),
                  ("num_floors", ogr.OFTInteger)):
    lyr.CreateField(ogr.FieldDefn(name, typ))


def add(wkt, fid, primary, common, height, floors):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
    f.SetField("id", fid)
    if primary:
        f.SetField("names.primary", primary)
    if common:
        f.SetField("names.common", json.dumps(common, ensure_ascii=False))
    f.SetField("subtype", "residential")
    f.SetField("class", "house")
    f.SetField("height", height)
    f.SetField("num_floors", floors)
    lyr.CreateFeature(f)


add("POLYGON((46.680 24.710,46.681 24.710,46.681 24.711,46.680 24.711,46.680 24.710))", "in1", "برج", {"en": "Tower"},
    40.0, 12)
add("POLYGON((46.682 24.712,46.683 24.712,46.683 24.713,46.682 24.713,46.682 24.712))", "in2", "Mall",
    {"ar": "مجمع", "en": "Mall"}, 15.0, 3)
add("POLYGON((47.5 25.5,47.51 25.5,47.51 25.51,47.5 25.51,47.5 25.5))", "out", "far", None, 9.0, 2)
add("POINT(46.681 24.711)", "pt", "a point among buildings", None, 0, 0)
ds = None
bld = ov.DATASETS[0]
got = list(ov.read_features([gpq], (46.67, 24.70, 46.69, 24.72), bld, 1000))
check(sorted(v[0] for _g, v in got) == ["in1", "in2"], f"area filter and geometry type ({[v[0] for _g, v in got]})")
row = {v[0]: v for _g, v in got}
check(row["in1"][2] == "برج" and row["in1"][3] == "Tower" and row["in2"][2] == "مجمع",
      "Arabic name from names.common, or the primary name when it is Arabic")
check(row["in1"][4:7] == ["residential", "house", 40.0] and row["in1"][7] == 12, "building fields mapped")
check(len(list(ov.read_features([gpq], (46.67, 24.70, 46.69, 24.72), bld, 1))) == 1, "feature limit respected")
check([f.name() for f in ov.output_fields(bld)][:4] == ["overture_id", "name", "name_ar", "name_en"], "output schema")
check(ov.TITLES == [d[1] for d in ov.DATASETS] and len(ov.DATASETS) == 7, "seven Overture datasets")

live = False
try:
    live = urllib.request.urlopen("https://stac.overturemaps.org/catalog.json", timeout=8).status == 200
except Exception:  # noqa: BLE001
    live = False
if live:
    res = processing.run("biruni:downloadoverture", {"EXTENT": "46.676,46.686,24.706,24.716 [EPSG:4326]", "DATA": 1,
                                                     "MAX_FEATURES": 5000, "OUTPUT": "memory:"}, context=ctx,
                         feedback=fb)
    out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
    arabic = sum(1 for f in out.getFeatures() if f["name_ar"])
    print(f"     live: release {res['RELEASE']}, {res['COUNT']} places, {arabic} with Arabic names")
    check(res["COUNT"] > 50 and arabic > 10, "live Overture places download in Riyadh")
else:
    print("     live Overture test skipped: no network")

# ======================================================================== GNSS quality
print("--- GNSS quality")
from biruni.algorithms import gnss_quality as gq  # noqa: E402

emlid = gq.detect_fields(["Name", "Easting", "Northing", "Elevation", "Easting RMS", "Northing RMS",
                          "Elevation RMS", "Solution status", "Satellites", "PDOP", "Antenna height"])
check(emlid.get("e_acc") == "Easting RMS" and emlid.get("n_acc") == "Northing RMS"
      and emlid.get("v_acc") == "Elevation RMS" and emlid.get("fix") == "Solution status"
      and emlid.get("sats") == "Satellites" and emlid.get("pdop") == "PDOP" and emlid.get("height") == "Elevation"
      and emlid.get("antenna") == "Antenna height", f"Emlid columns ({emlid})")
trimble = gq.detect_fields(["Point ID", "H Prec", "V Prec", "Fix Type", "Sats"])
check(trimble.get("h_acc") == "H Prec" and trimble.get("v_acc") == "V Prec" and trimble.get("fix") == "Fix Type"
      and trimble.get("sats") == "Sats", f"Trimble columns ({trimble})")
qfield = gq.detect_fields(["fid", "horizontal_accuracy", "vertical_accuracy", "fix_quality"])
check(qfield.get("h_acc") == "horizontal_accuracy" and qfield.get("v_acc") == "vertical_accuracy"
      and qfield.get("fix") == "fix_quality", f"QField columns ({qfield})")
arab = gq.detect_fields(["رقم النقطة", "الدقة الأفقية", "الدقة الرأسية", "نوع الحل", "عدد الأقمار"])
check(arab.get("h_acc") == "الدقة الأفقية" and arab.get("v_acc") == "الدقة الرأسية" and arab.get("fix") == "نوع الحل"
      and arab.get("sats") == "عدد الأقمار", f"Arabic columns ({arab})")
check([gq.fix_class(v) for v in ("FIX", "RTK Fixed", "float", "DGPS", "Single", 4, 5, 1, "4", "ثابت", "عائم", None,
                                 "?")]
      == ["fixed", "fixed", "float", "dgps", "single", "fixed", "float", "single", "fixed", "fixed", "float",
          "unknown", "unknown"], "solution types read")
check(gq.to_metres("1.2", "h_acc_cm") == 0.012 and gq.to_metres("٠٫٠١٥") == 0.015 and gq.to_metres(12, "acc (mm)")
      == 0.012 and gq.to_metres(None) is None, "accuracy units and Arabic digits")
ok, why = gq.check_point({"h_acc": 0.08, "v_acc": 0.05, "fix": "float", "pdop": 5.0, "sats": 4}, 0.05, 0.10, "fixed",
                         4.0, 6)
check(not ok and len(why) == 4, f"failing point gets every reason ({why})")

geoid = os.path.join(TMP, "geoid.tif")
ds = gdal.GetDriverByName("GTiff").Create(geoid, 10, 10, 1, gdal.GDT_Float32)
ds.SetGeoTransform([46.0, 0.2, 0, 25.5, 0, -0.2])
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).Fill(-12.5)
ds = None
g_rows = [("POINT(46.68 24.71)", ["P1", 0.012, 0.010, 0.020, "FIX", 18, 1.2, 620.0]),
          ("POINT(46.69 24.72)", ["P2", 0.040, 0.050, 0.060, "FLOAT", 12, 1.8, 621.0]),
          ("POINT(46.70 24.73)", ["P3", 0.010, 0.011, 0.150, "FIX", 9, 3.1, 622.0]),
          ("POINT(46.71 24.74)", ["P4", 0.300, 0.300, 0.500, "SINGLE", 6, 6.0, 623.0])]
glyr = mem("Point", "EPSG:4326", g_rows, [("Name", "string"), ("Easting RMS", "double"), ("Northing RMS", "double"),
                                         ("Elevation RMS", "double"), ("Solution status", "string"),
                                         ("Satellites", "integer"), ("PDOP", "double"), ("Ellipsoidal height",
                                                                                         "double")])
report = os.path.join(TMP, "gnss.xlsx")
res = processing.run("biruni:gnssquality", {"INPUT": glyr, "MAX_H": 0.05, "MAX_V": 0.10, "MIN_FIX": 3,
                                            "GEOID": geoid, "ANTENNA": 0, "OUTPUT": "memory:", "REPORT": report},
                     context=ctx, feedback=fb)
gout = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
verdict = {f["Name"]: (f["gnss_ok"], f["gnss_reasons"], f["h_acc_m"], f["ortho_h"]) for f in gout.getFeatures()}
check(res["TOTAL"] == 4 and res["PASSED"] == 1 and res["FAILED"] == 3, f"verdicts ({res['PASSED']}/{res['TOTAL']})")
check(verdict["P1"][0] and abs(verdict["P1"][2] - math.hypot(0.012, 0.010)) < 1e-9, "E/N RMS combined horizontally")
check(not verdict["P2"][0] and "solution" in verdict["P2"][1], "RTK float fails an RTK-fixed requirement")
check(not verdict["P3"][0] and "vertical" in verdict["P3"][1], "vertical accuracy checked")
check(abs(verdict["P1"][3] - (620.0 + 12.5)) < 1e-6, f"orthometric height H = h - N ({verdict['P1'][3]})")
from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(report)
check(len(wb.sheetnames) == 2 and wb.worksheets[1].max_row == 5, "Excel report: summary and one row per point")

# ======================================================================== CRS doctor
print("--- CRS doctor")
from biruni import crs_doctor as cd  # noqa: E402

prj = QgsProject.instance()
prj.clear()
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
utm_pts = [(f"POINT({674000 + 50 * k} {2733000 + 40 * k})", []) for k in range(10)]
good = mem("Point", "EPSG:32638", utm_pts)
good.setName("good")
m_as_deg = mem("Point", "EPSG:4326", utm_pts)
m_as_deg.setName("metres_as_degrees")
d_as_m = mem("Point", "EPSG:32638", [(f"POINT({46.7 + k / 1000} {24.7 + k / 1000})", []) for k in range(10)])
d_as_m.setName("degrees_as_metres")
swapped = mem("Point", "EPSG:4326", [(f"POINT({-6.2 + k / 1000} {106.8 + k / 1000})", []) for k in range(5)])
swapped.setName("swapped")
nulls = mem("Point", "EPSG:4326", [("POINT(0 0)", []), ("POINT(0.0001 0)", []), ("POINT(46.7 24.71)", [])])
nulls.setName("null_island")
brazil = mem("Point", "EPSG:4326", [("POINT(-47.9 -15.8)", []), ("POINT(-47.8 -15.7)", [])])
brazil.setName("far")
ain = mem("Point", "EPSG:20438", [(f"POINT({674100 + 30 * k} {2733150 + 25 * k})", []) for k in range(5)])
ain.setName("ain_el_abd")
for lyr in (good, m_as_deg, d_as_m, swapped, nulls, brazil, ain):
    prj.addMapLayer(lyr)
shp = os.path.join(TMP, "noprj.shp")
sds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(shp)
sl = sds.CreateLayer("noprj", None, ogr.wkbPoint)
f = ogr.Feature(sl.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(674010 2733010)"))
sl.CreateFeature(f)
sds = None
noprj = QgsVectorLayer(shp, "noprj", "ogr")
noprj.setCrs(QgsCoordinateReferenceSystem("EPSG:32638"))
prj.addMapLayer(noprj)

findings = cd.analyse(prj)
kinds = {}
for fd in findings:
    kinds.setdefault(fd.kind, []).append(fd.layer_name)
print("     " + "; ".join(f"{k}: {v}" for k, v in kinds.items()))
check(kinds.get("metres_as_degrees") == ["metres_as_degrees"], "metres stored as degrees found")
check(kinds.get("degrees_as_metres") == ["degrees_as_metres"], "degrees stored as metres found")
check(kinds.get("swapped") == ["swapped"], "swapped latitude/longitude found")
check(kinds.get("null_island") == ["null_island"], "Null Island points found")
check("far" in kinds.get("far_away", []) and "good" not in kinds.get("far_away", []), "the far-away layer found")
check(kinds.get("missing_prj") == ["noprj"], "shapefile without .prj found")
check("web_mercator" in kinds, "Web Mercator measuring warning")
check("good" not in sum(kinds.values(), []), "the correct layer has no finding")
wm = next(fd for fd in findings if fd.kind == "web_mercator")
check(1.08 < wm.data["length_factor"] < 1.12, f"Web Mercator scale at Riyadh ({wm.data['length_factor']:.3f})")
dat = [fd for fd in findings if fd.kind == "datum" and fd.layer_name == "ain_el_abd"]
from qgis.core import QgsDatumTransform  # noqa: E402

n_ops = len([o for o in QgsDatumTransform.operations(ain.crs(), prj.crs()) if o.isAvailable])
if n_ops >= 2:
    check(bool(dat) and dat[0].data["spread_m"] > 0.5,
          f"Ain el Abd: {n_ops} transformations, up to {dat[0].data['spread_m']:.1f} m apart" if dat else
          "Ain el Abd datum finding")
else:
    print(f"     datum check skipped: {n_ops} transformation(s) available")
check(findings[0].severity == "error" and findings[-1].severity in ("info", "warning"), "most serious first")
fix = next(fx for fx in next(fd for fd in findings if fd.kind == "metres_as_degrees").fixes
           if fx["action"] == "assign_crs" or fx["action"] == "guess")
check(fix is not None, "metres-as-degrees has a fix")
cd.apply_fix({"action": "assign_crs", "layer": m_as_deg.id(), "crs": "EPSG:32638"}, prj)
check(m_as_deg.crs().authid() == "EPSG:32638", "assign CRS fix applied")
msg = cd.apply_fix({"action": "select_null_island", "layer": nulls.id()}, prj)
check(nulls.selectedFeatureCount() == 2, f"Null Island fix selects them ({msg})")
before = len(prj.mapLayers())
cd.apply_fix({"action": "swap_xy", "layer": swapped.id()}, prj)
copy = [lyr for lyr in prj.mapLayers().values() if lyr.name().startswith("swapped (")]
pt = next(copy[0].getFeatures()).geometry().asPoint() if copy else None
check(len(prj.mapLayers()) == before + 1 and pt is not None and abs(pt.x() - 106.8) < 1e-9, "swap X/Y makes a copy")
cd.apply_fix({"action": "project_crs", "crs": "EPSG:32638"}, prj)
check(prj.crs().authid() == "EPSG:32638", "project CRS fix")
after = {fd.kind for fd in cd.analyse(prj, guess=False)}
check("metres_as_degrees" not in after and "web_mercator" not in after, "fixed problems are gone on a new check")
check("✖" in cd.summary_text(findings) and cd.summary_text([]), "plain-text summary")

# ======================================================================== Google Earth tour and KML import
print("--- Google Earth tour")
from biruni.share import kml_tour as kt  # noqa: E402
from biruni.share.kmz import export_kmz  # noqa: E402

check(kt.kml_color("#ff0000", 1.0) == "ff0000ff" and kt.kml_color("#0080ff", 0.5) == "80ff8000",
      "KML colours are aabbggrr")
parcels = mem("Polygon", "EPSG:32638", [
    ("POLYGON((674000 2733000,674100 2733000,674100 2733100,674000 2733100,674000 2733000))", ["سكني", "قطعة 1"]),
    ("POLYGON((674200 2733000,674300 2733000,674300 2733100,674200 2733100,674200 2733000))", ["تجاري", "قطعة 2"]),
    ("POLYGON((674400 2733000,674500 2733000,674500 2733100,674400 2733100,674400 2733000))", ["سكني", "قطعة 3"])],
    [("use", "string"), ("name", "string")])
parcels.setName("القطع")
cats = []
for val, color in (("سكني", "#ff0000"), ("تجاري", "#0000ff")):
    sym = QgsSymbol.defaultSymbol(parcels.geometryType())
    sym.setColor(QColor(color))
    cats.append(QgsRendererCategory(val, sym, val))
parcels.setRenderer(QgsCategorizedSymbolRenderer("use", cats))
road = mem("LineString", "EPSG:32638", [("LINESTRING(673900 2733050,674600 2733050)", [])])
road.setName("route")
kmz = os.path.join(TMP, "tour.kmz")
path, n_fly = kt.export_tour([parcels], kmz, "جولة", fly_seconds=2, wait_seconds=1, route=road, route_step=100,
                             route_speed=50)
with zipfile.ZipFile(path) as z:
    text = z.read("doc.kml").decode("utf-8")
root = ET.fromstring(text)
ns = {"k": "http://www.opengis.net/kml/2.2", "gx": "http://www.google.com/kml/ext/2.2"}
flys = root.findall(".//gx:FlyTo", ns)
updates = root.findall(".//gx:AnimatedUpdate", ns)
marks = root.findall(".//k:Folder/k:Placemark", ns)
check(len(updates) == 6 and len(flys) >= 3 + 7, f"tour: a stop per parcel and the route flown ({len(flys)} fly-tos)")
check(len(marks) == 4 and "قطعة 2" in text, "placemarks with Arabic names (3 parcels + route)")
styles = {s.get("id"): s for s in root.findall(".//k:Style", ns)}
poly_colours = sorted(s.find("k:PolyStyle/k:color", ns).text for s in styles.values()
                      if s.find("k:PolyStyle", ns) is not None)
check(len(poly_colours) == 2 and all(c.endswith("0000ff") or c.endswith("ff0000") for c in poly_colours),
      f"category colours kept ({poly_colours})")
heads = [float(h.text) for h in root.findall(".//gx:FlyTo/k:LookAt/k:heading", ns)][3:]
check(heads and all(abs(h - 90) < 1 for h in heads), "route camera looks along the road (east = 90°)")
samples = kt.route_samples(QgsGeometry.fromWkt("LINESTRING(46.70 24.70, 46.70 24.71)"), 200)
check(len(samples) >= 5 and all(abs(h) < 1 or abs(h - 360) < 1 for _x, _y, h in samples), "northward route heading")
ogr_back = ogr.Open(path)
check(ogr_back is not None and sum(ogr_back.GetLayer(i).GetFeatureCount() for i in range(ogr_back.GetLayerCount()))
      == 4, "the KMZ reads back with GDAL")
ogr_back = None

# KML import keeps colours (export_kmz writes an Arabic-named layer to a sub-file GDAL cannot read back, so this
# round trip uses an English layer name; the tour KMZ below keeps Arabic folder names)
def colours_of(lyr):
    from qgis.core import Qgis, QgsFeatureRequest

    out = set()
    for f in lyr.getFeatures(QgsFeatureRequest().setFlags(Qgis.FeatureRequestFlag.EmbeddedSymbols)):
        s = f.embeddedSymbol()
        if s is not None:
            out.add(s.color().name())
    return out


parcels.setName("parcels")
plain = os.path.join(TMP, "colours.kmz")
export_kmz([parcels], plain, prj)
parcels.setName("القطع")
imported = kt.import_kml(plain, prj, add=False)
check(len(imported) == 1 and imported[0].featureCount() == 3, "KMZ folder imported as a layer")
rtype = imported[0].renderer().type() if imported else ""
cols = colours_of(imported[0]) if imported else set()
check(rtype == "embeddedSymbol" and {"#ff0000", "#0000ff"} <= cols, f"original colours drawn ({rtype}, {cols})")
tour_layers = kt.import_kml(path, prj, add=False)
names = sorted(lyr.name() for lyr in tour_layers)
tour_cols = set().union(*(colours_of(lyr) for lyr in tour_layers)) if tour_layers else set()
check(any("القطع" in n for n in names) and {"#ff0000", "#0000ff"} <= tour_cols,
      f"tour KMZ re-imported with Arabic folder names and colours ({names}, {tour_cols})")

# ======================================================================== publishing a website
print("--- website")
from biruni.share import pages_publish as pp  # noqa: E402

site = os.path.join(TMP, "site_inline")
r1 = pp.publish_site([parcels], site, "موقع المشروع", mode="auto", project=prj)
check(r1["mode"] == "inline" and os.path.exists(os.path.join(site, "index.html"))
      and os.path.exists(os.path.join(site, ".nojekyll")) and os.path.exists(os.path.join(site, "README.md")),
      "small data: one-file site with README and .nojekyll")
readme = open(os.path.join(site, "README.md"), encoding="utf-8").read()
check("netlify.com/drop" in readme and "GitHub Pages" in readme and "1. " in readme, "README has the three steps")
site2 = os.path.join(TMP, "site_tiles")
r2 = pp.publish_site([parcels, road], site2, "Tiles", mode="tiles", project=prj,
                     portfolio={"title": "Portfolio", "author": "Test"})
tiles = [p for p, _mb in r2["files"] if p.endswith(".pmtiles")]
check(r2["mode"] == "tiles" and len(tiles) == 2, f"tiles site: one PMTiles per layer ({tiles})")
pm = ogr.Open(os.path.join(site2, tiles[0]))
n = sum(pm.GetLayer(i).GetFeatureCount() for i in range(pm.GetLayerCount())) if pm else 0
check(n >= 1, f"PMTiles readable ({n} features in its tiles)")
pm = None
page = open(os.path.join(site2, "index.html"), encoding="utf-8").read()
check("pmtiles://" in page and "lib/maplibre-gl.mjs" in page and "data/" in page and "Portfolio" in page
      and os.path.exists(os.path.join(site2, "lib", "maplibre-gl.mjs"))
      and os.path.exists(os.path.join(site2, "portfolio", "index.html")), "MapLibre page, portfolio linked")
check(pp.safe_file_name("القطع / 2", set()) == "القطع___2" and r2["warnings"] == [], "safe names, no size warnings")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
