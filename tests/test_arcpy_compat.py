r"""The arcpy compatibility layer, off screen: sample data made with GDAL (a shapefile of parcels with an Arabic
name, a GeoPackage of roads, a shapefile of wells, a DEM GeoTIFF) and realistic arcpy scripts run end to end with
the shim as `arcpy` - inventory with Walk/Describe/ListFields, a Pro-style processing chain into a file
geodatabase with a feature dataset (cursors with SHAPE@ tokens, field calculator in Python and Arcade, layers and
selections, Buffer/Clip/Intersect/Union/Erase/Dissolve/SpatialJoin/Statistics/Project/Merge/Append, Excel round
trip), geometry objects, a Spatial Analyst script, an ArcGIS 10 Python 2 script and an unsupported call. Outputs
are checked with GDAL directly. Also the coverage registry, analyse_script and the Processing algorithm.

Run:  engine\bin\python-qgis.bat tests\test_arcpy_compat.py
"""
import json
import math
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
app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")

import numpy as np  # noqa: E402
from osgeo import gdal, ogr, osr  # noqa: E402

gdal.DontUseExceptions()
ogr.DontUseExceptions()

from biruni import arcpy_compat  # noqa: E402
from biruni.arcpy_compat import analyse, registry  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ROOT = tempfile.mkdtemp(prefix="arcpy_compat_test_")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
SCRIPTS = os.path.join(ROOT, "scripts")
for d in (DATA, OUT, SCRIPTS):
    os.makedirs(d)

UTM = osr.SpatialReference()
UTM.ImportFromEPSG(32638)
X0, Y0 = 500000.0, 2700000.0


# ---------------------------------------------------------------- sample data (plain GDAL, not the shim)

def make_data():
    drv = ogr.GetDriverByName("ESRI Shapefile")
    ds = drv.CreateDataSource(os.path.join(DATA, "parcels.shp"))
    lyr = ds.CreateLayer("parcels", UTM, ogr.wkbPolygon, ["ENCODING=UTF-8"])
    f = ogr.FieldDefn("NAME", ogr.OFTString)
    f.SetWidth(40)
    lyr.CreateField(f)
    lyr.CreateField(ogr.FieldDefn("POP", ogr.OFTInteger))
    z = ogr.FieldDefn("ZONE", ogr.OFTString)
    z.SetWidth(5)
    lyr.CreateField(z)
    k = 0
    for row in range(2):
        for col in range(4):
            x, y = X0 + col * 100, Y0 + row * 100
            feat = ogr.Feature(lyr.GetLayerDefn())
            feat.SetField("NAME", "حي النور" if k == 0 else f"P{k}")
            feat.SetField("POP", (k + 1) * 10)
            feat.SetField("ZONE", "A" if col < 2 else "B")
            feat.SetGeometry(ogr.CreateGeometryFromWkt(
                f"POLYGON(({x} {y},{x} {y + 100},{x + 100} {y + 100},{x + 100} {y},{x} {y}))"))
            lyr.CreateFeature(feat)
            k += 1
    ds = None

    ds = ogr.GetDriverByName("GPKG").CreateDataSource(os.path.join(DATA, "network.gpkg"))
    lyr = ds.CreateLayer("roads", UTM, ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("RNAME", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("WIDTH", ogr.OFTReal))
    for name, wkt, width in (("King Road", f"LINESTRING({X0 - 50} {Y0 + 50},{X0 + 450} {Y0 + 50})", 20.0),
                             ("Side St", f"LINESTRING({X0 + 150} {Y0 - 50},{X0 + 150} {Y0 + 250})", 8.0)):
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetField("RNAME", name)
        feat.SetField("WIDTH", width)
        feat.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(feat)
    ds = None

    ds = drv.CreateDataSource(os.path.join(DATA, "wells.shp"))
    lyr = ds.CreateLayer("wells", UTM, ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("DEPTH", ogr.OFTReal))
    for i, (x, y) in enumerate([(X0 + 20, Y0 + 20), (X0 + 30, Y0 + 70), (X0 + 120, Y0 + 20), (X0 + 350, Y0 + 150),
                                (X0 + 900, Y0 + 900)]):
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetField("DEPTH", 10.0 * (i + 1))
        feat.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({x} {y})"))
        lyr.CreateFeature(feat)
    ds = None

    # DEM: 50 x 50 cells of 10 m; z rises 1 m per cell eastwards (slope atan(0.1) = 5.71 degrees)
    dem = gdal.GetDriverByName("GTiff").Create(os.path.join(DATA, "dem.tif"), 50, 50, 1, gdal.GDT_Float32)
    dem.SetGeoTransform((X0, 10, 0, Y0 + 500, 0, -10))
    dem.SetProjection(UTM.ExportToWkt())
    arr = np.tile(np.arange(50, dtype=np.float32) + 100.0, (50, 1))
    arr[0, 0] = -9999
    dem.GetRasterBand(1).WriteArray(arr)
    dem.GetRasterBand(1).SetNoDataValue(-9999)
    dem = None


make_data()


def ogr_count(path, layer=None):
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    if ds is None:
        return -1
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    return lyr.GetFeatureCount() if lyr is not None else -1


def ogr_rows(path, layer, fields):
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    out = []
    for f in lyr:
        out.append({k: f.GetField(k) for k in fields} | {"_g": f.GetGeometryRef().Clone()
                                                          if f.GetGeometryRef() else None})
    return out


def write_script(name, text):
    path = os.path.join(SCRIPTS, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def run(path, args=None, workspace=None):
    msgs = []
    res = arcpy_compat.run_script(path, args, lambda s, t: msgs.append((s, t)), workspace=workspace)
    if not res["ok"]:
        print("   script error:", res["error"])
        for s, t in msgs[-15:]:
            print("   ", s, t)
    return res, msgs


# ---------------------------------------------------------------- 1. inventory script (Walk, Describe, ListFields)

INVENTORY = r'''
import os
import arcpy

folder = arcpy.GetParameterAsText(0)
arcpy.env.workspace = folder
report = []
for dirpath, dirnames, filenames in arcpy.da.Walk(folder, datatype="FeatureClass"):
    for name in filenames:
        fc = os.path.join(dirpath, name)
        desc = arcpy.Describe(fc)
        count = int(arcpy.management.GetCount(fc)[0])
        fields = [f.name for f in arcpy.ListFields(fc) if f.type not in ("OID", "Geometry")]
        report.append("%s|%s|%s|%d|%s" % (desc.baseName, desc.shapeType, desc.spatialReference.factoryCode,
                                           count, ",".join(fields)))
        arcpy.AddMessage("%s: %d features" % (name, count))
shps = arcpy.ListFeatureClasses()
polys = arcpy.ListFeatureClasses(feature_type="Polygon")
rasters = arcpy.ListRasters()
arcpy.SetParameterAsText(1, ";".join(sorted(report)))
arcpy.SetParameterAsText(2, "%s/%s/%s" % (sorted(shps), polys, rasters))
if not arcpy.Exists(os.path.join(folder, "parcels.shp")) or arcpy.Exists(os.path.join(folder, "nothing.shp")):
    arcpy.AddError("Exists is wrong")
'''

res, msgs = run(write_script("inventory.py", INVENTORY), [DATA])
check(res["ok"], "inventory script runs")
inv = res["outputs"].get(1, "")
check("parcels|Polygon|32638|8|NAME,POP,ZONE" in inv, "Walk + Describe + GetCount + ListFields on a shapefile")
check("roads|Polyline|32638|2|RNAME,WIDTH" in inv, "Walk enters the GeoPackage and describes its layer")
check("wells|Point|32638|5|DEPTH" in inv, "points described")
check(res["outputs"].get(2) == "['parcels.shp', 'wells.shp']/['parcels.shp']/['dem.tif']",
      "ListFeatureClasses / feature_type / ListRasters")
check(any(t == "parcels.shp: 8 features" for s, t in msgs if s == 0), "AddMessage reaches the message callback")
check(not any(s == 2 for s, t in msgs), "no errors reported (Exists right)")
check("arcpy" not in sys.modules, "shim removed from sys.modules after the run")

# ---------------------------------------------------------------- 2. Pro-style processing chain into a gdb

PROCESS = r'''
import os
import arcpy
from arcpy import env

parcels = arcpy.GetParameterAsText(0)
roads = arcpy.GetParameterAsText(1)
wells = arcpy.GetParameterAsText(2)
out_folder = arcpy.GetParameterAsText(3)

env.overwriteOutput = True
sr = arcpy.Describe(parcels).spatialReference
gdb = arcpy.management.CreateFileGDB(out_folder, "city")[0]
arcpy.management.CreateFeatureDataset(gdb, "cadastre", sr)
env.workspace = gdb

arcpy.conversion.FeatureClassToFeatureClass(parcels, os.path.join(gdb, "cadastre"), "parcels")
fc = os.path.join(gdb, "cadastre", "parcels")
arcpy.management.AddField(fc, "AREA_M2", "DOUBLE")
arcpy.management.AddField(fc, "CLASS", "TEXT", field_length=20)
with arcpy.da.UpdateCursor(fc, ["SHAPE@AREA", "AREA_M2", "POP", "SHAPE@XY"]) as cursor:
    for area, _a, pop, xy in cursor:
        cursor.updateRow([area, round(area, 2), pop, xy])

code = """
def classify(pop):
    if pop >= 50:
        return "dense"
    return "sparse"
"""
arcpy.management.CalculateField(fc, "CLASS", "classify(!POP!)", "PYTHON3", code)
arcpy.management.CalculateField(fc, "LABEL", "Concatenate([$feature.ZONE, Text($feature.POP)], '-')", "ARCADE",
                                field_type="TEXT")
arcpy.management.CalculateField(fc, "HA", "!shape.area@hectares!", "PYTHON3", field_type="DOUBLE")

lyr = arcpy.management.MakeFeatureLayer(fc, "parcels_lyr", "POP > 0")[0]
arcpy.management.SelectLayerByLocation(lyr, "INTERSECT", roads)
on_road = int(arcpy.management.GetCount(lyr)[0])
arcpy.management.SelectLayerByAttribute(lyr, "SUBSET_SELECTION", "ZONE = 'A'")
on_road_a = int(arcpy.management.GetCount(lyr)[0])
arcpy.management.CopyFeatures(lyr, "parcels_on_road_a")
arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")

arcpy.analysis.Buffer(roads, "road_buf", "10 Meters", dissolve_option="NONE")
arcpy.analysis.Clip(fc, "road_buf", "parcels_clip")
arcpy.analysis.Intersect([fc, "road_buf"], "parcels_x_roads")
arcpy.analysis.Union([fc, "road_buf"], "parcels_union")
arcpy.analysis.Erase(fc, "road_buf", os.path.join(out_folder, "parcels_erase.shp"))
arcpy.management.Dissolve(fc, "zones", "ZONE", [["POP", "SUM"], ["POP", "COUNT"]])
arcpy.analysis.SpatialJoin(fc, wells, "parcels_wells", "JOIN_ONE_TO_ONE", "KEEP_ALL", match_option="INTERSECT")
arcpy.analysis.Statistics(fc, "pop_stats", [["POP", "SUM"], ["POP", "MEAN"], ["POP", "MAX"]], "ZONE")
arcpy.management.Project(fc, "parcels_wgs84", arcpy.SpatialReference(4326))
arcpy.management.Merge([fc, os.path.join(out_folder, "parcels_erase.shp")], "merged")
arcpy.management.CreateFeatureclass(gdb, "appended", "POLYGON", template=fc, spatial_reference=sr)
arcpy.management.Append(fc, "appended", "NO_TEST")
arcpy.management.Append(fc, "appended", "NO_TEST", expression="ZONE = 'B'")
arcpy.conversion.TableToExcel(fc, os.path.join(out_folder, "parcels.xlsx"))
arcpy.conversion.ExcelToTable(os.path.join(out_folder, "parcels.xlsx"), "from_excel")
arcpy.conversion.ExportFeatures(fc, os.path.join(out_folder, "export.gpkg", "big"), "POP >= 40")
arcpy.management.DeleteField("merged", ["CLASS", "LABEL"])

# nested cursors on the same table, ORDER BY, OID@ and a where clause
names = []
with arcpy.da.SearchCursor(fc, ["OID@", "NAME"], "ZONE = 'B'", sql_clause=(None, "ORDER BY POP DESC")) as outer:
    for oid, name in outer:
        with arcpy.da.SearchCursor(fc, ["POP"], "OBJECTID = %d" % oid) as inner:
            names.append("%s:%d" % (name, next(inner)[0]))

arcpy.management.Delete("parcels_union")
try:
    arcpy.management.CreateRelationshipClass(fc, fc, "rel", "SIMPLE")
    unsupported = "no error"
except arcpy.ExecuteError as e:
    unsupported = str(e)

arcpy.SetParameterAsText(4, "%d|%d|%s|%s" % (on_road, on_road_a, ",".join(names), unsupported))
'''

res, msgs = run(write_script("process.py", PROCESS),
                [os.path.join(DATA, "parcels.shp"), os.path.join(DATA, "network.gpkg", "roads"),
                 os.path.join(DATA, "wells.shp"), OUT])
check(res["ok"], "processing chain script runs")
GDB = os.path.join(OUT, "city.gdb")
summary = res["outputs"].get(4, "").split("|")
check(len(summary) == 4 and summary[0] == "5", f"SelectLayerByLocation INTERSECT selects 5 parcels ({summary[:1]})")
check(len(summary) == 4 and summary[1] == "3", "SUBSET_SELECTION keeps the 3 zone-A parcels")
check(len(summary) == 4 and summary[2] == "P7:80,P6:70,P3:40,P2:30",
      f"ORDER BY DESC + nested cursors on the same gdb table ({summary[2:3]})")
check(len(summary) == 4 and "CreateRelationshipClass" in summary[3] and "not available in Biruni yet" in summary[3],
      "unsupported tool raises ExecuteError naming it")

# the gdb: feature dataset membership and values
gds = gdal.OpenEx(GDB, gdal.OF_VECTOR)
root = gds.GetRootGroup() if gds is not None else None
check(root is not None and "cadastre" in (root.GetGroupNames() or []) and
      "parcels" in (root.OpenGroup("cadastre").GetVectorLayerNames() or []),
      "feature class written inside the feature dataset of the file geodatabase")
gds = None
rows = ogr_rows(GDB, "parcels", ["NAME", "POP", "AREA_M2", "CLASS", "LABEL", "HA"])
check(len(rows) == 8, "8 parcels copied into the gdb")
by_pop = {r["POP"]: r for r in rows}
check(all(abs(r["AREA_M2"] - 10000) < 1e-6 for r in rows), "UpdateCursor wrote SHAPE@AREA")
check(by_pop[10]["NAME"] == "حي النور", "Arabic text survives shp -> gdb")
check(by_pop[60]["CLASS"] == "dense" and by_pop[20]["CLASS"] == "sparse", "CalculateField PYTHON3 with code block")
check(by_pop[30]["LABEL"] == "B-30", "CalculateField ARCADE-lite (Concatenate/Text/$feature)")
check(abs(by_pop[10]["HA"] - 1.0) < 1e-9, "CalculateField !shape.area@hectares!")
check(ogr_count(GDB, "parcels_on_road_a") == 3, "CopyFeatures copies only the selection of a layer")
check(ogr_count(GDB, "road_buf") == 2, "Buffer made one polygon per road")
buf_rows = ogr_rows(GDB, "road_buf", ["BUFF_DIST", "RNAME"])
check(all(abs(r["BUFF_DIST"] - 10) < 1e-9 for r in buf_rows), "Buffer BUFF_DIST field")
check(ogr_count(GDB, "parcels_clip") == 5, "Clip keeps the 5 parcels under the road buffers")
check(ogr_count(GDB, "parcels_x_roads") == 6, "Intersect gives 6 parcel/road pieces")


def field_names(path, layer):
    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    defn = ds.GetLayerByName(layer).GetLayerDefn()
    return [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]


xnames = field_names(GDB, "parcels_x_roads")
check("FID_parcels" in xnames and "FID_road_buf" in xnames and "RNAME" in xnames, "Intersect FID_ and join fields")
check(not os.path.exists(os.path.join(GDB, "parcels_union")) and ogr_count(GDB, "parcels_union") == -1,
      "Delete removed the union output")
erase = ogr_rows(os.path.join(OUT, "parcels_erase.shp"), None, ["POP"])
check(len(erase) == 8 and sum(r["_g"].GetArea() for r in erase) < 80000 - 1000, "Erase into a shapefile")
zones = {r["ZONE"]: r for r in ogr_rows(GDB, "zones", ["ZONE", "SUM_POP", "COUNT_POP"])}
check(set(zones) == {"A", "B"} and zones["A"]["SUM_POP"] == 10 + 20 + 50 + 60 and zones["B"]["COUNT_POP"] == 4,
      "Dissolve by ZONE with SUM / COUNT statistics")
check(abs(zones["A"]["_g"].GetArea() - 40000) < 1e-6, "Dissolve unions the geometry")
sj = {r["POP"]: r for r in ogr_rows(GDB, "parcels_wells", ["POP", "Join_Count", "DEPTH", "TARGET_FID"])}
check(len(sj) == 8 and sj[10]["Join_Count"] == 2 and sj[20]["Join_Count"] == 1 and sj[80]["Join_Count"] == 1
      and sj[70]["Join_Count"] == 0, "SpatialJoin one-to-one counts and keeps all")
check(sj[10]["DEPTH"] == 10.0 and sj[80]["DEPTH"] == 40.0 and sj[70]["DEPTH"] is None,
      "SpatialJoin copies the first match's attributes")
st = {r["ZONE"]: r for r in ogr_rows(GDB, "pop_stats", ["ZONE", "FREQUENCY", "SUM_POP", "MEAN_POP", "MAX_POP"])}
check(st["B"]["FREQUENCY"] == 4 and st["B"]["SUM_POP"] == 30 + 40 + 70 + 80 and st["B"]["MEAN_POP"] == 55 and
      st["B"]["MAX_POP"] == 80, "Statistics with a case field (table in the gdb)")
pds = gdal.OpenEx(GDB, gdal.OF_VECTOR)
plyr = pds.GetLayerByName("parcels_wgs84")
env4326 = plyr.GetExtent()
check(plyr.GetSpatialRef().GetAuthorityCode(None) == "4326" and 44.9 < env4326[0] < 45.1 and
      24.3 < env4326[2] < 24.5, "Project to WGS 84")
pds = None
check(ogr_count(GDB, "merged") == 16, "Merge of a gdb feature class and a shapefile")
mnames = field_names(GDB, "merged")
check("CLASS" not in mnames and "LABEL" not in mnames and "POP" in mnames, "DeleteField with a list")
check(ogr_count(GDB, "appended") == 12, "CreateFeatureclass from template + Append with expression")
check(os.path.isfile(os.path.join(OUT, "parcels.xlsx")) and ogr_count(GDB, "from_excel") == 8,
      "TableToExcel -> ExcelToTable round trip")
xl = {r["POP"]: r for r in ogr_rows(GDB, "from_excel", ["NAME", "POP"])}
check(xl.get(10, {}).get("NAME") == "حي النور", "Arabic text survives the Excel round trip")
check(ogr_count(os.path.join(OUT, "export.gpkg"), "big") == 5, "ExportFeatures with where clause into a GeoPackage")
check(GDB in res["created"] or any(p.startswith(GDB) for p in res["created"]), "created outputs are reported")

# ---------------------------------------------------------------- 3. geometry objects + gpkg cursors

GEOMETRY = r'''
import arcpy
out_gpkg = arcpy.GetParameterAsText(0)
arcpy.env.overwriteOutput = True
sr = arcpy.SpatialReference(32638)
pt = arcpy.Point(500000, 2700000)
poly = arcpy.Polygon(arcpy.Array([arcpy.Point(500000, 2700000), arcpy.Point(500000, 2700100),
                                  arcpy.Point(500100, 2700100), arcpy.Point(500100, 2700000)]), sr)
hole = arcpy.Array([arcpy.Point(500025, 2700025), arcpy.Point(500075, 2700025), arcpy.Point(500075, 2700075),
                    arcpy.Point(500025, 2700075)])
outer = arcpy.Array([arcpy.Point(500000, 2700000), arcpy.Point(500000, 2700100), arcpy.Point(500100, 2700100),
                     arcpy.Point(500100, 2700000)])
donut = arcpy.Polygon(arcpy.Array([outer, hole]), sr)
line = arcpy.Polyline(arcpy.Array([arcpy.Point(499950, 2700050), arcpy.Point(500150, 2700050)]), sr)
p = arcpy.PointGeometry(arcpy.Point(500050, 2700050), sr)
res = {}
res["area"] = poly.area
res["donut"] = donut.area
res["length"] = line.length
res["perimeter"] = poly.length
res["centroid"] = (poly.centroid.X, poly.centroid.Y)
res["within"] = p.within(poly)
res["donut_within"] = p.within(donut)
res["buffer"] = round(p.buffer(10).area, 0)
res["inter"] = line.intersect(poly, 2).length
res["contains"] = poly.contains(p)
res["disjoint"] = poly.disjoint(arcpy.PointGeometry(arcpy.Point(0, 0), sr))
wgs = p.projectAs(arcpy.SpatialReference(4326))
res["lonlat"] = (round(wgs.firstPoint.X, 3), round(wgs.firstPoint.Y, 3))
res["geodesic_area"] = round(poly.getArea("GEODESIC", "SQUAREMETERS"), 0)
res["ha"] = poly.getArea("PLANAR", "HECTARES")
res["parts"] = [len(part) for part in poly]
res["extent"] = (poly.extent.XMin, poly.extent.YMax, poly.extent.width)
res["along"] = line.positionAlongLine(0.5, True).firstPoint.X
res["wkt"] = arcpy.FromWKT(line.WKT, sr).length
res["sr"] = (sr.name, sr.type, sr.linearUnitName, arcpy.SpatialReference("WGS 1984 UTM Zone 38N").factoryCode)

arcpy.management.CreateFeatureclass(out_gpkg, "shapes", "POLYGON", spatial_reference=sr)
arcpy.management.AddField(out_gpkg + "/shapes", "KIND", "TEXT")
with arcpy.da.InsertCursor(out_gpkg + "/shapes", ["SHAPE@", "KIND"]) as cur:
    cur.insertRow([poly, "square"])
    cur.insertRow([donut, "donut"])
    cur.insertRow([p.buffer(5), "circle"])
arcpy.management.CreateFeatureclass(out_gpkg, "pts", "POINT", spatial_reference=sr)
with arcpy.da.InsertCursor(out_gpkg + "/pts", ["SHAPE@XY"]) as cur:
    for i in range(3):
        cur.insertRow([(500000 + i, 2700000 + i)])
read = []
with arcpy.da.SearchCursor(out_gpkg + "/shapes", ["KIND", "SHAPE@AREA", "SHAPE@"], "KIND <> 'circle'",
                           spatial_reference=arcpy.SpatialReference(4326)) as cur:
    for kind, area, shape in cur:
        read.append((kind, shape.spatialReference.factoryCode, round(shape.centroid.X, 3)))
res["read"] = read
res["xy"] = [r for r in arcpy.da.SearchCursor(out_gpkg + "/pts", ["SHAPE@X", "SHAPE@Y"])]
with arcpy.da.UpdateCursor(out_gpkg + "/shapes", ["KIND"]) as cur:
    for row in cur:
        if row[0] == "circle":
            cur.deleteRow()
res["after_delete"] = int(arcpy.GetCount_management(out_gpkg + "/shapes").getOutput(0))
arcpy.SetParameterAsText(1, repr(res))
'''

res, msgs = run(write_script("geometry.py", GEOMETRY), [os.path.join(OUT, "shapes.gpkg")])
check(res["ok"], "geometry script runs")
g = eval(res["outputs"].get(1, "{}")) if res["ok"] else {}  # noqa: S307 - our own repr
check(g.get("area") == 10000.0 and g.get("donut") == 10000.0 - 2500.0, "Polygon area, holes from an Array of Arrays")
check(g.get("length") == 200.0 and g.get("perimeter") == 400.0, "Polyline length and polygon perimeter")
check(g.get("centroid") == (500050.0, 2700050.0), "centroid")
check(g.get("within") is True and g.get("donut_within") is False and g.get("contains") is True and
      g.get("disjoint") is True, "within / contains / disjoint (point in a hole is outside)")
check(abs(g.get("buffer", 0) - round(math.pi * 100)) <= 1, "buffer")
check(g.get("inter") == 100.0, "intersect with dimension 2 (line part inside)")
check(g.get("lonlat") == (45.0, 24.411) or (g.get("lonlat") and abs(g["lonlat"][0] - 45.0) < 0.002),
      f"projectAs to WGS 84 ({g.get('lonlat')})")
check(abs(g.get("geodesic_area", 0) - 10000) < 50 and g.get("ha") == 1.0, "getArea geodesic and in hectares")
check(g.get("parts") == [5] and g.get("extent") == (500000.0, 2700100.0, 100.0), "iteration over parts, extent")
check(g.get("along") == 500050.0 and g.get("wkt") == 200.0, "positionAlongLine and FromWKT")
check(g.get("sr") == ("WGS_84_UTM_zone_38N", "Projected", "Meter", 32638), "SpatialReference properties and Esri names")
check([r[0] for r in g.get("read", [])] == ["square", "donut"] and all(r[1] == 4326 for r in g.get("read", [])),
      "SearchCursor with where_clause and spatial_reference")
check(g.get("xy") == [(500000.0, 2700000.0), (500001.0, 2700001.0), (500002.0, 2700002.0)],
      "InsertCursor SHAPE@XY into a GeoPackage, SHAPE@X/Y back")
check(g.get("after_delete") == 2 and ogr_count(os.path.join(OUT, "shapes.gpkg"), "shapes") == 2,
      "UpdateCursor.deleteRow in a GeoPackage")

# ---------------------------------------------------------------- 4. Spatial Analyst script

RASTER = r'''
import os
import arcpy
from arcpy import env
from arcpy.sa import *

env.workspace = arcpy.GetParameterAsText(0)
out = arcpy.GetParameterAsText(1)
env.overwriteOutput = True
if arcpy.CheckExtension("Spatial") == "Available":
    arcpy.CheckOutExtension("Spatial")
dem = Raster("dem.tif")
slope = Slope(dem, "DEGREE")
slope.save(os.path.join(out, "slope.tif"))
Aspect("dem.tif").save(os.path.join(out, "aspect.tif"))
Hillshade(dem, 315, 45).save(os.path.join(out, "hill.tif"))
high = Con(dem > 130, 1, 0)
high.save(os.path.join(out, "high.tif"))
calc = (dem - 100) * 2 + 1
calc.save(os.path.join(out, "calc.tif"))
nulls = SetNull(dem < 120, dem)
nulls.save(os.path.join(out, "setnull.tif"))
rc = Reclassify(dem, "VALUE", RemapRange([[100, 120, 1], [120, 140, 2], [140, 150, 3]]))
rc.save(os.path.join(out, "reclass.tif"))
arcpy.management.Clip("dem.tif", "500000 2700000 500200 2700300", os.path.join(out, "dem_clip.tif"))
arcpy.ddd.Slope("dem.tif", os.path.join(out, "slope3d.tif"))
arcpy.SetParameterAsText(2, "%s|%s|%s|%s" % (dem.width, dem.meanCellWidth, round(dem.maximum, 1),
                                                arcpy.Describe("dem.tif").bandCount))
arcpy.CheckInExtension("Spatial")
'''


def band(path):
    ds = gdal.Open(path)
    if ds is None:
        return None, None
    b = ds.GetRasterBand(1)
    return b.ReadAsArray().astype(float), b.GetNoDataValue()


RDIR = os.path.join(OUT, "rasters")
os.makedirs(RDIR)
res, msgs = run(write_script("raster.py", RASTER), [DATA, RDIR])
check(res["ok"], "Spatial Analyst script runs")
check(res["outputs"].get(2) == "50|10.0|149.0|1", "Raster properties (width, cell size, maximum, Describe)")
a, nd = band(os.path.join(RDIR, "slope.tif"))
check(a is not None and abs(a[25, 25] - math.degrees(math.atan(0.1))) < 0.01, "Slope in degrees")
a, nd = band(os.path.join(RDIR, "aspect.tif"))
check(a is not None and abs(a[25, 25] - 270.0) < 0.01, "Aspect faces west when z rises eastwards")
a, nd = band(os.path.join(RDIR, "hill.tif"))
check(a is not None and 0 < a[25, 25] <= 255, "Hillshade")
a, nd = band(os.path.join(RDIR, "high.tif"))
check(a is not None and a[10, 40] == 1 and a[10, 10] == 0 and a[0, 0] == nd, "Con on a comparison, NoData kept")
a, nd = band(os.path.join(RDIR, "calc.tif"))
check(a is not None and a[5, 7] == (107 - 100) * 2 + 1, "map algebra on Raster objects")
a, nd = band(os.path.join(RDIR, "setnull.tif"))
check(a is not None and a[3, 5] == nd and a[3, 30] == 130, "SetNull")
a, nd = band(os.path.join(RDIR, "reclass.tif"))
check(a is not None and a[3, 5] == 1 and a[3, 25] == 2 and a[3, 45] == 3, "Reclassify with RemapRange")
cds = gdal.Open(os.path.join(RDIR, "dem_clip.tif"))
check(cds is not None and (cds.RasterXSize, cds.RasterYSize) == (20, 30), "raster Clip by rectangle")
cds = None
check(os.path.isfile(os.path.join(RDIR, "slope3d.tif")), "arcpy.ddd.Slope tool form")

# ---------------------------------------------------------------- 5. ArcGIS 10 (Python 2) script

PY2 = r'''
import arcpy, os
arcpy.env.workspace = arcpy.GetParameterAsText(0)
fc = "parcels.shp"
print "Counting", fc
total = 0
rows = arcpy.SearchCursor(fc, "ZONE = 'A'")
for row in rows:
    total += row.getValue("POP")
del rows
try:
    arcpy.Densify_edit(fc)
except arcpy.ExecuteError, e:
    print "skipped:", e.message if hasattr(e, "message") else "x"
if total <> 0:
    arcpy.SetParameterAsText(1, unicode(total))
'''

res, msgs = run(write_script("legacy_py2.py", PY2), [DATA])
check(res["ok"] and res["python2"], "Python 2 script runs after the print/except fix-up")
check(res["outputs"].get(1) == str(10 + 20 + 50 + 60), "old-style arcpy.SearchCursor with Row.getValue")
check(any(t == "Counting parcels.shp" for s, t in msgs), "print statements routed to the messages")
check(any("skipped:" in t for s, t in msgs), "except E, e: works and the missing tool raised ExecuteError")

# ---------------------------------------------------------------- 6. direct use, errors, overwrite

arcpy = arcpy_compat.install()
check(sys.modules.get("arcpy") is arcpy and sys.modules.get("arcpy.da") is arcpy.da and
      sys.modules.get("arcpy.sa") is arcpy.sa, "install() registers arcpy and its submodules")
import arcpy.management as amg  # noqa: E402
check(amg.CopyFeatures is arcpy.management.CopyFeatures and arcpy.CopyFeatures_management is amg.CopyFeatures,
      "arcpy.management.X and arcpy.X_management are the same tool")
arcpy.env.workspace = OUT
arcpy.env.overwriteOutput = False
try:
    arcpy.management.CopyFeatures(os.path.join(DATA, "parcels.shp"), "copy1")
    first_ok = os.path.isfile(os.path.join(OUT, "copy1.shp"))
    arcpy.management.CopyFeatures(os.path.join(DATA, "parcels.shp"), "copy1")
    second = "no error"
except arcpy.ExecuteError as e:
    second = str(e)
check(first_ok and "000725" in second, "existing output refused unless overwriteOutput")
arcpy.env.overwriteOutput = True
r = arcpy.management.CopyFeatures(os.path.join(DATA, "parcels.shp"), "copy1")
check(str(r) == os.path.join(OUT, "copy1.shp") and "Succeeded" in r.getMessages(), "Result object")
try:
    arcpy.na.MakeRouteAnalysisLayer("x")
    na_err = ""
except arcpy.ExecuteError as e:
    na_err = str(e)
check("arcpy.na.MakeRouteAnalysisLayer" in na_err and "not available in Biruni yet" in na_err,
      "whole missing toolbox raises ExecuteError naming the function")
try:
    arcpy.management.GetCount(os.path.join(DATA, "missing.shp"))
    miss = ""
except arcpy.ExecuteError as e:
    miss = str(e)
check("000732" in miss, "missing input gives ERROR 000732")
mem = arcpy.management.CopyFeatures(os.path.join(DATA, "wells.shp"), r"memory\wells")[0]
check(arcpy.Exists(mem) and arcpy.management.GetCount(mem)[0] == "5", "memory workspace")
arr = arcpy.da.FeatureClassToNumPyArray(os.path.join(DATA, "parcels.shp"), ["POP", "ZONE"])
check(len(arr) == 8 and int(arr["POP"].sum()) == 360, "da.FeatureClassToNumPyArray")
env_ok = True
with arcpy.EnvManager(workspace=DATA, overwriteOutput=False):
    env_ok = arcpy.env.workspace == DATA and not arcpy.env.overwriteOutput
check(env_ok and arcpy.env.workspace == OUT and arcpy.env.overwriteOutput, "EnvManager restores settings")
arcpy.env.outputCoordinateSystem = 4326
arcpy.management.CopyFeatures(os.path.join(DATA, "wells.shp"), "wells_ocs")
check(arcpy.Describe("wells_ocs.shp").spatialReference.factoryCode == 4326, "env.outputCoordinateSystem applied")
arcpy.env.outputCoordinateSystem = None
parcels_shp = os.path.join(DATA, "parcels.shp")
arcpy.management.CheckGeometry(parcels_shp, "check_tbl")
check(arcpy.management.GetCount("check_tbl.dbf")[0] == "0", "CheckGeometry finds no problems in valid data")
arcpy.management.PolygonToLine(parcels_shp, "outlines", "IGNORE_NEIGHBORS")
check(arcpy.Describe("outlines.shp").shapeType == "Polyline" and arcpy.GetCount_management("outlines")[0] == "8",
      "PolygonToLine")
arcpy.management.FeatureToPolygon([os.path.join(DATA, "network.gpkg", "roads"), parcels_shp], "faces")
check(int(arcpy.GetCount_management("faces")[0]) > 8, "FeatureToPolygon splits parcels along the roads")
arcpy.management.MinimumBoundingGeometry(os.path.join(DATA, "wells.shp"), "hull", "CONVEX_HULL", "ALL")
arcpy.management.MinimumBoundingGeometry(parcels_shp, "boxes", "RECTANGLE_BY_AREA", "LIST", "ZONE")
check(arcpy.GetCount_management("hull")[0] == "1" and arcpy.GetCount_management("boxes")[0] == "2",
      "MinimumBoundingGeometry convex hull / rectangles by group")
dem_arr = arcpy.RasterToNumPyArray(os.path.join(DATA, "dem.tif"), nodata_to_value=0)
back = arcpy.NumPyArrayToRaster(dem_arr * 2, arcpy.Point(X0, Y0), 10, 10, 0)
check(dem_arr.shape == (50, 50) and dem_arr[0, 0] == 0 and back.maximum == 298.0 and
      back.extent.YMax == Y0 + 500, "RasterToNumPyArray / NumPyArrayToRaster")
check(arcpy.GetSigninToken() is None, "GetSigninToken: not signed in")
aprx = arcpy.mp.ArcGISProject("CURRENT")
m = aprx.listMaps()[0]
added = m.addDataFromPath(parcels_shp)
m.addDataFromPath(os.path.join(DATA, "dem.tif"))
names_mp = [lyr.name for lyr in m.listLayers()]
added.definitionQuery = "ZONE = 'B'"
check("parcels" in names_mp and "dem" in names_mp and added.isFeatureLayer and
      arcpy.management.GetCount(added)[0] == "4", "arcpy.mp CURRENT project: layers, definition query used by tools")
added.setSelectionSet([3, 4])
check(arcpy.management.GetCount(m.listLayers("parcels")[0])[0] == "2", "map layer selection honoured by tools")
try:
    arcpy.mp.ArcGISProject(os.path.join(DATA, "x.aprx"))
    aprx_err = ""
except arcpy.ExecuteError as e:
    aprx_err = str(e)
check("not available in Biruni yet" in aprx_err, ".aprx projects refused with a clear error")
from qgis.core import QgsProject as _QP  # noqa: E402
_QP.instance().removeAllMapLayers()
arcpy_compat.uninstall()
check("arcpy" not in sys.modules, "uninstall() restores sys.modules")

# ---------------------------------------------------------------- 7. registry and analyse_script

arcpy = arcpy_compat.install()
bad = []
for key in registry.COVERAGE:
    if key.startswith("env."):
        try:
            getattr(arcpy.env, key[4:])
        except AttributeError:
            bad.append(key)
        continue
    obj = arcpy
    try:
        for part in key.split("."):
            obj = getattr(obj, part)
    except AttributeError:
        bad.append(key)
        continue
    if getattr(obj, "_arcpy_missing", None):
        bad.append(key)
arcpy_compat.uninstall()
check(not bad, f"every registry entry exists in the shim {bad[:5]}")
lst = registry.as_list()
check(len(lst) > 150 and all(set(x) == {"name", "status", "note"} for x in lst), "registry.as_list is machine-readable")
json.dumps(lst)

rep = analyse.analyse_script(PROCESS)
check(set(rep) >= {"calls", "supported", "partial", "missing", "score", "missing_names"}, "analyse_script keys")
check(rep["missing_names"] == ["management.CreateRelationshipClass"] and rep["score"] >= 85 and
      set(rep["partial_names"]) == {"management.Append", "management.Merge", "management.Project",
                                    "analysis.SpatialJoin", "analysis.Union"},
      f"analyse_script finds the one missing tool (score {rep['score']})")
check(all(set(c) >= {"name", "line", "status"} for c in rep["calls"]), "calls carry name, line and status")
rep2 = analyse.analyse_script(RASTER)
check(rep2["missing"] == 0 and "sa.Slope" in [c["name"] for c in rep2["calls"]] and
      "ddd.Slope" in [c["name"] for c in rep2["calls"]], "star import from arcpy.sa and ddd tools recognised")
rep3 = analyse.analyse_script(PY2)
check(rep3["python2"] and "edit.Densify" in rep3["missing_names"] and "SearchCursor" in rep3["partial_names"],
      "Python 2 script analysed; classic Tool_toolbox names normalised")
rep4 = analyse.analyse_script("import arcpy as ap\nfrom arcpy import env as e\ne.workspace = 'x'\n"
                              "ap.mapping.MapDocument('CURRENT')\nap.Buffer_analysis('a', 'b', '1 Meters')\n")
names4 = {c["name"]: c["status"] for c in rep4["calls"]}
check(names4.get("env.workspace") == "supported" and names4.get("mapping.MapDocument") == "missing" and
      names4.get("analysis.Buffer") == "supported" and rep4["score"] == 67, "aliases and score")
check(analyse.analyse_script("def broken(:\n")["score"] == 0, "unparseable text gives score 0 without raising")

# ---------------------------------------------------------------- 8. the Processing algorithm

import processing  # noqa: E402,F401
from processing.core.Processing import Processing  # noqa: E402
from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
alg = QgsApplication.processingRegistry().algorithmById("biruni:runarcpyscript")
check(alg is not None, "Run arcpy script algorithm registered")


class Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.info, self.warn, self.err = [], [], []

    def pushInfo(self, info):  # noqa: N802
        self.info.append(info)

    def pushWarning(self, warning):  # noqa: N802
        self.warn.append(warning)

    def reportError(self, error, fatalError=False):  # noqa: N802,N803
        self.err.append(error)


ALG = r'''
import arcpy
arcpy.AddMessage("hello " + arcpy.GetParameterAsText(0))
arcpy.AddWarning("careful")
arcpy.AddError("bad thing")
fc = arcpy.GetParameterAsText(1)
arcpy.env.overwriteOutput = True
arcpy.management.CopyFeatures(fc, arcpy.GetParameterAsText(2))
arcpy.SetParameterAsText(3, arcpy.management.GetCount(arcpy.GetParameterAsText(2))[0])
'''
apath = write_script("for_alg.py", ALG)
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = Feedback()
out_shp = os.path.join(OUT, "alg copy.shp")
params = {"SCRIPT": apath, "ARGS": f'world "{os.path.join(DATA, "parcels.shp")}" "{out_shp}"', "ADD_OUTPUTS": True}
results, ok = alg.run(params, ctx, fb)
check(ok and json.loads(results["OUTPUTS"]).get("3") == "8", "algorithm runs the script with quoted arguments")
check("hello world" in fb.info and "careful" in fb.warn and "bad thing" in fb.err,
      "AddMessage / AddWarning / AddError routed to the feedback")
check(os.path.isfile(out_shp) and out_shp in results["CREATED"], "created dataset reported")
check(len(ctx.layersToLoadOnCompletion()) == 1, "created dataset queued to be added to the map")
fb2 = Feedback()
bad_script = write_script("fails.py", "import arcpy\narcpy.cartography.SimplifyPolygon('a', 'b')\n")
results2, ok2 = alg.run({"SCRIPT": bad_script}, QgsProcessingContext(), fb2)
check(not ok2 and any("not available in Biruni yet" in e for e in fb2.err), "algorithm reports unsupported calls")
from biruni.algorithms.run_arcpy_script import split_arguments  # noqa: E402
check(split_arguments('a "C:\\x y\\z.shp" 3') == ["a", "C:\\x y\\z.shp", "3"] and
      split_arguments("C:\\a b\\c.shp\nsecond") == ["C:\\a b\\c.shp", "second"], "argument splitting keeps backslashes")
QgsApplication.processingRegistry().removeProvider(provider)

# ---------------------------------------------------------------- done

shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(SETTINGS_DIR, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
