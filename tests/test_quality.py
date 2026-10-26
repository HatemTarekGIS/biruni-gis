"""Standalone tests for topology rules, the project health scan and the road
network tools.

Run:  engine\\bin\\python-qgis.bat tests\\test_quality.py
"""
import os
import random
import shutil
import sys
import tempfile
import time

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature,
                       QgsFeedback, QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.quality import health  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
UTM = "EPSG:32636"
X0, Y0 = 500000, 3300000  # inside UTM 36N's area of use


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, rel=0.005):
    return a is not None and abs(a - b) <= rel * max(abs(b), 1)


def layer(geom_type, crs, wkts, name="test", fields="field=name:string", attrs=None):
    uri = f"{geom_type}?crs={crs}&{fields}" if crs else f"{geom_type}?{fields}"
    lyr = QgsVectorLayer(uri, name, "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        if w:
            f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes(attrs[i] if attrs else [f"f{i}"] + [None] * (lyr.fields().count() - 1))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def run(alg, params):
    res = processing.run(alg, params, context=ctx, feedback=fb)
    return res


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def topo(lyr, rules, second=None, tol=0.0, min_area=0.0):
    return run("biruni:topologycheck", {
        "INPUT": lyr, "RULES": rules, "SECOND": second, "TOLERANCE": tol, "MIN_AREA": min_area,
        "ERROR_POINTS": "TEMPORARY_OUTPUT", "ERROR_LINES": "TEMPORARY_OUTPUT", "ERROR_AREAS": "TEMPORARY_OUTPUT"})


# ===== topology rules =========================================================
ring = [sq(x, y) for x, y in ((0, 0), (100, 0), (200, 0), (0, 100), (200, 100), (0, 200), (100, 200), (200, 200))]
polys = layer("Polygon", UTM, ring + [sq(50, 50), "POLYGON((1000 0,1100 100,1100 0,1000 100,1000 0))"], "polys")
r = topo(polys, [0, 1, 2])
check(r["NO_OVERLAP"] == 3, f"overlaps: 3 pairs (got {r['NO_OVERLAP']})")
areas = sorted(f["size"] for f in out(r["ERROR_AREAS"]).getFeatures() if f["rule"] == "no_overlap")
check(all(near(a, 2500) for a in areas), f"each overlap is 2500 m² ({areas})")
gaps = [f["size"] for f in out(r["ERROR_AREAS"]).getFeatures() if f["rule"] == "no_gaps"]
check(r["NO_GAPS"] == 1 and near(gaps[0], 7500), f"one gap of 7500 m² ({gaps})")
check(r["VALID"] == 1, f"bow-tie polygon is invalid (got {r['VALID']})")
pt_err = [f for f in out(r["ERROR_POINTS"]).getFeatures()]
check(len(pt_err) == 1 and pt_err[0]["rule"] == "valid" and pt_err[0]["feature_a"] is not None,
      "invalid geometry error is a point with the feature id")
check(r["ERROR_COUNT"] == 5, f"total 5 errors (got {r['ERROR_COUNT']})")

# rule on the wrong geometry type is skipped, not an error
r = topo(polys, [3])
check(r["ERROR_COUNT"] == 0, "line rule on polygons is skipped")

lines = layer("LineString", UTM, ["LINESTRING(0 0,100 0)", "LINESTRING(50 0,50 50)", "LINESTRING(60 0,150 0)",
                                  "LINESTRING(200 0,300 100,300 0,200 100)", "LINESTRING(400 0,500 0,500 100,400 0)"],
              "lines")
r = topo(lines, [3, 4, 5])
check(r["NO_DANGLES"] == 5, f"5 dangling ends (got {r['NO_DANGLES']})")
dangles = sorted((round(f.geometry().asPoint().x()), round(f.geometry().asPoint().y()))
                 for f in out(r["ERROR_POINTS"]).getFeatures() if f["rule"] == "no_dangles")
check(dangles == [(0, 0), (50, 50), (150, 0), (200, 0), (200, 100)], f"dangles at the right ends {dangles}")
ov = [f for f in out(r["ERROR_LINES"]).getFeatures()]
check(r["LINES_NO_OVERLAP"] == 1 and near(ov[0]["size"], 40), "one line overlap of 40 m")
si = [f.geometry().asPoint() for f in out(r["ERROR_POINTS"]).getFeatures() if f["rule"] == "lines_no_self_intersect"]
check(len(si) == 1 and near(si[0].x(), 250) and near(si[0].y(), 50),
      f"one self-intersection at (250, 50) {[(p.x(), p.y()) for p in si]}")

pts = layer("Point", UTM, ["POINT(50 50)", "POINT(175 175)", "POINT(5000 5000)", "POINT(0 0)", "POINT(125 125)"],
            "pts")
r = topo(pts, [6], polys)
check(r["POINTS_IN_POLYGONS"] == 2, f"2 points outside the polygons (got {r['POINTS_IN_POLYGONS']})")

pts2 = layer("Point", UTM, ["POINT(10 0)", "POINT(10 5)"], "pts2")
r = topo(pts2, [8], lines, tol=1)
check(r["POINTS_ON_LINES"] == 1, f"1 point off the lines at 1 m tolerance (got {r['POINTS_ON_LINES']})")
r = topo(pts2, [8], lines, tol=10)
check(r["POINTS_ON_LINES"] == 0, "no point off the lines at 10 m tolerance")

a = layer("Polygon", UTM, [sq(0, 0)], "a")
cover = layer("Polygon", UTM, ["POLYGON((0 0,60 0,60 100,0 100,0 0))"], "cover")
r = topo(a, [7], cover)
unc = [f["size"] for f in out(r["ERROR_AREAS"]).getFeatures()]
check(r["COVERED_BY"] == 1 and near(unc[0], 4000), f"uncovered part of 4000 m² ({unc})")

try:
    topo(pts, [6])
    check(False, "two-layer rule without a second layer raises")
except Exception:
    check(True, "two-layer rule without a second layer raises")

# ===== project health =========================================================
prj = QgsProject.instance()
prj.removeAllMapLayers()
tmp = tempfile.mkdtemp(prefix="biruni_health_")
os.makedirs(os.path.join(tmp, "proj"))
prj.setFileName(os.path.join(tmp, "proj", "demo.qgz"))
prj.setCrs(QgsCoordinateReferenceSystem(UTM))

geo = layer("Point", "EPSG:4326", ["POINT(31 30)", "POINT(31 30)", "POINT(30 120)", None], "geo",
            "field=name:string&field=note:string", [["a", None], ["b", ""], ["c", None], ["d", None]])
o = lambda x, y: (X0 + x, Y0 + y)  # noqa: E731


def osq(x, y, s=100):
    return sq(X0 + x, Y0 + y, s)


plist = [osq(0, 0), osq(100, 0), osq(50, 50),
         f"POLYGON(({X0+1000} {Y0},{X0+1100} {Y0+100},{X0+1100} {Y0},{X0+1000} {Y0+100},{X0+1000} {Y0}))",
         f"POLYGON(({X0+2000} {Y0},{X0+2100} {Y0},{X0+2100} {Y0},{X0+2100} {Y0+100},{X0+2000} {Y0+100},"
         f"{X0+2000} {Y0}))",
         f"POLYGON(({X0+3000} {Y0},{X0+3100} {Y0},{X0+3100} {Y0+0.05},{X0+3000} {Y0+0.05},{X0+3000} {Y0}))"]
area = layer("MultiPolygon", UTM, plist, "areas")
degrees = layer("Point", UTM, ["POINT(31 30)"], "degrees")
nocrs = layer("Point", None, ["POINT(1 1)"], "nocrs")
nocrs.setCrs(QgsCoordinateReferenceSystem())  # memory layers default to WGS 84
broken = QgsVectorLayer(os.path.join(tmp, "missing", "gone.shp"), "broken", "ogr")

other = os.path.join(tmp, "other")
os.makedirs(other)
shp = os.path.join(other, "outside.shp")
fields = QgsFields()
fields.append(QgsField("name", QMetaType.Type.QString))
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "ESRI Shapefile"
w = QgsVectorFileWriter.create(shp, fields, Qgis.WkbType.Point, QgsCoordinateReferenceSystem(UTM),
                               QgsCoordinateTransformContext(), opts)
f = QgsFeature(fields)
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0, Y0)))
f.setAttributes(["a"])
w.addFeature(f)
del w
if os.path.exists(os.path.join(other, "outside.cpg")):
    os.remove(os.path.join(other, "outside.cpg"))
outside = QgsVectorLayer(shp, "outside", "ogr")
prj.addMapLayer(outside)

issues = health.scan_project(prj, QgsFeedback(), layers=[geo, area, degrees, nocrs, broken, outside])
by = {}
for it in issues:
    by.setdefault((it.layer_name, it.kind), it)
    print("   ", it.severity, it.layer_name, it.kind, it.count, "|", it.text())


def has(layer_name, kind, count=None, severity=None):
    it = by.get((layer_name, kind))
    ok = it is not None and (count is None or it.count == count) and (severity is None or it.severity == severity)
    check(ok, f"{layer_name}: {kind}" + (f" x{count}" if count is not None else "") +
          ("" if it is None else f" (got {it.count} {it.severity})"))
    return it


has("geo", "temporary", severity="warning")
has("geo", "crs_mismatch", severity="info")
has("geo", "empty_geometry", 1, "error")
has("geo", "duplicate_geometry", 1, "warning")
has("geo", "out_of_range", 1, "error")
has("geo", "swapped", severity="error")
it = has("geo", "empty_fields", 1, "info")
check(it is not None and it.values.get("fields") == "note", "the empty field is 'note'")
check(("geo", "invalid") not in by, "points are never 'invalid'")
inv = has("areas", "invalid", 1, "error")
dv = has("areas", "duplicate_vertices", 1, "info")
has("areas", "sliver", 1, "warning")
ovl = has("areas", "overlap", 3, "warning")
check(("areas", "crs_mismatch") not in by and ("areas", "outside_crs_area") not in by,
      "projected layer in the right place has no CRS issue")
check(("areas", "empty_fields") not in by, "no empty fields on 'areas'")
has("degrees", "outside_crs_area", severity="warning")
has("nocrs", "no_crs", severity="error")
has("broken", "broken", severity="error")
check(sum(1 for i in issues if i.layer_name == "broken") == 1, "a broken layer reports only that")
has("outside", "outside_folder", severity="warning")
has("outside", "no_cpg", severity="warning")
check(("outside", "temporary") not in by, "shapefile is not temporary")
s = health.summary(issues)
check(s == {"error": 6, "warning": 10, "info": 3}, f"summary: 6 errors, 10 warnings, 3 notes {s}")
check(issues[0].layer_name == "geo" and issues[0].severity == "error", "issues are sorted per layer, errors first")

# fixes: undoable, in an edit session
n = inv.fix()
check(n == 1 and area.isEditable(), f"fix invalid -> 1 feature, layer left in edit mode (got {n})")
n = dv.fix()
check(n == 1, f"fix repeated vertices -> 1 feature (got {n})")
check(area.undoStack().count() == 2, f"two undo steps (got {area.undoStack().count()})")
again = {i.kind for i in health.scan_project(prj, layers=[area])}
check("invalid" not in again and "duplicate_vertices" not in again, f"re-scan is clean of fixed issues {again}")
area.undoStack().undo()
area.undoStack().undo()
again = {i.kind for i in health.scan_project(prj, layers=[area])}
check("invalid" in again and "duplicate_vertices" in again, "undo brings the problems back")
area.rollBack()
check(ovl.fix is None, "overlaps have no automatic fix")

# speed on big layers
random.seed(4)
big = QgsVectorLayer(f"Point?crs={UTM}&field=v:integer", "big", "memory")
feats = []
for i in range(100000):
    f = QgsFeature(big.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + random.random() * 50000, Y0 + random.random() * 50000)))
    f.setAttributes([i])
    feats.append(f)
big.dataProvider().addFeatures(feats)
t = time.time()
res = health.scan_project(prj, QgsFeedback(), layers=[big])
dt = time.time() - t
check(dt < 30, f"100,000 points scanned in {dt:.1f} s")
grid = QgsVectorLayer(f"Polygon?crs={UTM}&field=v:integer", "grid", "memory")
feats = []
for i in range(150):
    for j in range(150):
        f = QgsFeature(grid.fields())
        f.setGeometry(QgsGeometry.fromWkt(osq(i * 10, j * 10, 10)))
        f.setAttributes([i * 150 + j])
        feats.append(f)
grid.dataProvider().addFeatures(feats)
t = time.time()
res = health.scan_project(prj, QgsFeedback(), layers=[grid])
dt = time.time() - t
check(dt < 60 and not any(i.kind in ("overlap", "sliver", "invalid") for i in res),
      f"22,500 touching squares: no false overlaps, scanned in {dt:.1f} s")
fbc = QgsFeedback()
fbc.cancel()
check(health.scan_project(prj, fbc, layers=[big, grid]) == [], "a cancelled scan returns nothing")

# ===== road network ===========================================================
roads = QgsVectorLayer(f"LineString?crs={UTM}&field=oneway:string&field=kmh:double", "roads", "memory")
feats = []


def road(x1, y1, x2, y2, oneway=None, kmh=36.0):
    f = QgsFeature(roads.fields())
    f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(X0 + x1, Y0 + y1), QgsPointXY(X0 + x2, Y0 + y2)]))
    f.setAttributes([oneway, kmh])
    feats.append(f)


for y in range(0, 400, 100):
    for x in range(0, 300, 100):
        road(x, y, x + 100, y, "yes" if (x, y) == (0, 0) else None, 3.6 if (x, y) == (100, 0) else 36.0)
for x in range(0, 400, 100):
    for y in range(0, 300, 100):
        road(x, y, x, y + 100)
road(5000, 5000, 5100, 5000)  # isolated
roads.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(roads)


def pts_layer(name, coords):
    lyr = QgsVectorLayer(f"Point?crs={UTM}&field=code:string", name, "memory")
    fs = []
    for code, (x, y) in coords:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + x, Y0 + y)))
        f.setAttributes([code])
        fs.append(f)
    lyr.dataProvider().addFeatures(fs)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


origins = pts_layer("origins", [("O1", (100, 0)), ("O2", (0, 0))])
dests = pts_layer("dests", [("D1", (0, 0)), ("D2", (300, 300)), ("D3", (5050, 5000))])
K = 1 / 0.9996  # UTM scale factor near the central meridian: ellipsoidal metres per grid metre


def od(cost, geometry=0, d=dests, extra=None):
    p = {"NETWORK": roads, "COST": cost, "ORIGINS": origins, "ORIGIN_ID": "code", "DESTINATIONS": d,
         "DESTINATION_ID": "code", "GEOMETRY": geometry, "DIRECTION_FIELD": "oneway", "SPEED_FIELD": "kmh",
         "DEFAULT_SPEED": 50, "TOLERANCE": 0, "OUTPUT": "TEMPORARY_OUTPUT"}
    p.update(extra or {})
    r = run("biruni:odmatrix", p)
    return r, {(f["origin_id"], f["destination_id"]): f for f in out(r["OUTPUT"]).getFeatures()}


r, m = od(0)
check(r["PAIR_COUNT"] == 6 and r["UNREACHABLE_COUNT"] == 2, f"6 pairs, 2 unreachable ({r})")
check(near(m[("O1", "D1")]["distance_m"], 300 * K), f"one-way street forces a 300 m detour "
      f"({m[('O1', 'D1')]['distance_m']})")
check(near(m[("O1", "D1")]["straight_m"], 100 * K), "straight-line distance 100 m")
check(near(m[("O2", "D2")]["distance_m"], 600 * K), "corner to corner 600 m")
check(near(m[("O1", "D2")]["distance_m"], 500 * K), "O1 -> D2 500 m")
check(m[("O2", "D1")]["distance_m"] == 0 and m[("O2", "D1")]["reachable"] == 1, "same place = 0 m")
check(m[("O1", "D3")]["reachable"] == 0 and m[("O1", "D3")]["distance_m"] is None, "isolated road unreachable")
check(near(m[("O1", "D1")]["time_min"], 300 * K / 10 / 60), "time from the speed field (36 km/h)")

# fastest avoids the slow street
fast_dest = pts_layer("fd", [("E", (200, 0))])
r, m = od(0, d=fast_dest)
check(near(m[("O2", "E")]["distance_m"], 200 * K) and near(m[("O2", "E")]["time_min"], 110 * K / 60),
      f"shortest: 200 m through the slow street, 1.83 min ({m[('O2', 'E')]['distance_m']}, "
      f"{m[('O2', 'E')]['time_min']})")
r, m = od(1, geometry=1, d=fast_dest)
check(near(m[("O2", "E")]["distance_m"], 400 * K) and near(m[("O2", "E")]["time_min"], 40 * K / 60),
      f"fastest: 400 m around it in 0.67 min ({m[('O2', 'E')]['distance_m']}, {m[('O2', 'E')]['time_min']})")
check(near(m[("O2", "E")].geometry().length(), 400), "route geometry follows the roads (400 m)")
r, m = od(0, geometry=2, d=fast_dest)
check(not m[("O2", "E")].hasGeometry(), "table output has no geometry")

r, m = od(0, d=None)
check(r["PAIR_COUNT"] == 4, "no destinations = origins x origins")

incidents = pts_layer("inc", [("I1", (100, 0)), ("I2", (5050, 5000))])
facilities = pts_layer("fac", [("F1", (0, 0)), ("F2", (300, 0))])


def cf(count, travel):
    r = run("biruni:closestfacility", {
        "NETWORK": roads, "COST": 0, "INCIDENTS": incidents, "INCIDENT_ID": "code", "FACILITIES": facilities,
        "FACILITY_ID": "code", "COUNT": count, "TRAVEL": travel, "DIRECTION_FIELD": "oneway", "SPEED_FIELD": "kmh",
        "DEFAULT_SPEED": 50, "TOLERANCE": 0, "OUTPUT": "TEMPORARY_OUTPUT"})
    return r, sorted(out(r["OUTPUT"]).getFeatures(), key=lambda f: (f["incident_id"], f["rank"] or 0))


r, rows = cf(1, 0)
i1 = [f for f in rows if f["incident_id"] == "I1"]
check(len(i1) == 1 and i1[0]["facility_id"] == "F2" and near(i1[0]["distance_m"], 200 * K),
      "incident -> facility: F2 at 200 m beats F1 (one-way detour)")
i2 = [f for f in rows if f["incident_id"] == "I2"]
check(r["UNREACHABLE_COUNT"] == 1 and len(i2) == 1 and i2[0]["reachable"] == 0, "isolated incident flagged")
r, rows = cf(2, 0)
i1 = [f for f in rows if f["incident_id"] == "I1"]
check([f["facility_id"] for f in i1] == ["F2", "F1"] and near(i1[1]["distance_m"], 300 * K),
      "two facilities ranked F2 (200 m), F1 (300 m)")
r, rows = cf(1, 1)
i1 = [f for f in rows if f["incident_id"] == "I1"]
check(i1[0]["facility_id"] == "F1" and near(i1[0]["distance_m"], 100 * K),
      "facility -> incident: F1 drives 100 m along the one-way street")
check(i1[0].geometry().length() > 99, "route line drawn")

shutil.rmtree(tmp, ignore_errors=True)
print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
QgsProject.instance().clear()
app.exitQgis()
sys.exit(1 if failures else 0)
