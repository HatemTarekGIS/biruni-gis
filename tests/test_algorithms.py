"""Standalone tests for the Biruni Processing algorithms.

Run:  engine\\bin\\python-qgis.bat tests\\test_algorithms.py
"""
import os
import sys
import tempfile

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()  # keep a Python reference, or its virtual methods die with the wrapper
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def layer(geom_type, crs, wkts, fields="field=name:string"):
    lyr = QgsVectorLayer(f"{geom_type}?crs={crs}&{fields}", "test", "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes([f"f{i}"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()

# ---- overlaps & gaps: ring of 8 squares around a hole + one overlapping square
ring = [sq(x, y) for x in (0, 100, 200) for y in (0, 100, 200) if not (x == 100 and y == 100)]
ring.append(sq(250, 250, 30))  # overlaps the top-right square by 30x30 (fully inside it)
polys = layer("Polygon", "EPSG:32637", ring)
r = processing.run("biruni:overlapsgaps", {"INPUT": polys, "MIN_AREA": 0, "MAX_GAP": 0,
                                           "OVERLAPS": "memory:", "GAPS": "memory:"}, context=ctx, feedback=fb)
check(r["OVERLAP_COUNT"] == 1, f"overlaps: 1 expected, got {r['OVERLAP_COUNT']}")
check(r["GAP_COUNT"] == 1, f"gaps: 1 expected, got {r['GAP_COUNT']}")
ov = out(r["OVERLAPS"])
gp = out(r["GAPS"])
check(abs(next(ov.getFeatures())["area"] - 900) < 1e-6, "overlap area = 900")
check(abs(next(gp.getFeatures())["area"] - 10000) < 1e-6, "gap area = 10000")
r = processing.run("biruni:overlapsgaps", {"INPUT": polys, "MIN_AREA": 0, "MAX_GAP": 5000,
                                           "OVERLAPS": "memory:", "GAPS": "memory:"}, context=ctx, feedback=fb)
check(r["GAP_COUNT"] == 0, "max gap area filters the 10000 m2 hole")

# ---- geometry attributes
r = processing.run("biruni:geometryattributes", {"INPUT": layer("Polygon", "EPSG:32637", [sq(500000, 2700000)]),
                                                 "METHOD": 1, "AREA_UNIT": 1, "LENGTH_UNIT": 0, "DECIMALS": 4,
                                                 "OUTPUT": "memory:"}, context=ctx, feedback=fb)
f = next(out(r["OUTPUT"]).getFeatures())
check(abs(f["area_ha"] - 1.0) < 1e-9, f"flat area 1 ha, got {f['area_ha']}")
check(abs(f["perim_m"] - 400) < 1e-9, f"perimeter 400 m, got {f['perim_m']}")
r = processing.run("biruni:geometryattributes", {"INPUT": layer("Polygon", "EPSG:32637", [sq(500000, 2700000)]),
                                                 "METHOD": 0, "AREA_UNIT": 0, "LENGTH_UNIT": 0, "DECIMALS": 2,
                                                 "OUTPUT": "memory:"}, context=ctx, feedback=fb)
f = next(out(r["OUTPUT"]).getFeatures())
# UTM scale factor 0.9996 on the central meridian: true area = 10000 / 0.9996^2 = 10008.0
check(10006 < f["area_m2"] < 10010, f"ellipsoidal area ~10008 m2, got {f['area_m2']}")
r = processing.run("biruni:geometryattributes", {"INPUT": layer("LineString", "EPSG:32637",
                                                                ["LINESTRING(0 0, 300 400)"]),
                                                 "METHOD": 1, "AREA_UNIT": 0, "LENGTH_UNIT": 1, "DECIMALS": 3,
                                                 "OUTPUT": "memory:"}, context=ctx, feedback=fb)
f = next(out(r["OUTPUT"]).getFeatures())
check(abs(f["length_km"] - 0.5) < 1e-9, f"line length 0.5 km, got {f['length_km']}")

# ---- numbering: 3x3 grid, slightly jittered rows
grid = [sq(x * 100, y * 100 + (5 if x == 1 else 0), 90) for y in range(3) for x in range(3)]
g = layer("Polygon", "EPSG:32637", grid)
r = processing.run("biruni:numberfeatures", {"INPUT": g, "FIELD": "num", "ORDER": 0, "TOLERANCE": 0, "START": 1,
                                             "STEP": 1, "PREFIX": "", "OUTPUT": "memory:"}, context=ctx, feedback=fb)
numbered = out(r["OUTPUT"])
order = [f["name"] for f in sorted(numbered.getFeatures(), key=lambda f: f["num"])]
check(order == ["f6", "f7", "f8", "f3", "f4", "f5", "f0", "f1", "f2"], f"rows L->R: {order}")
r = processing.run("biruni:numberfeatures", {"INPUT": g, "FIELD": "num", "ORDER": 1, "TOLERANCE": 0, "START": 10,
                                             "STEP": 10, "PREFIX": "P-", "OUTPUT": "memory:"}, context=ctx,
                   feedback=fb)
numbered = out(r["OUTPUT"])
first = {f["name"]: f["num"] for f in numbered.getFeatures()}
check(first["f8"] == "P-10" and first["f6"] == "P-30" and first["f0"] == "P-90", f"rows R->L with prefix: {first}")

# ---- export layers
tmp = tempfile.mkdtemp(prefix="biruni-export-")
for fmt, ext in ((0, "export.gpkg"), (1, "test.shp"), (2, "test.geojson"), (3, "test.kml"), (4, "test.csv"),
                 (5, "test.xlsx"), (6, "export.dxf")):
    folder = os.path.join(tmp, str(fmt))
    r = processing.run("biruni:exportlayers", {"LAYERS": [polys], "FORMAT": fmt, "SELECTED": False,
                                               "FOLDER": folder}, context=ctx, feedback=fb)
    check(os.path.exists(os.path.join(folder, ext)), f"export format {fmt} -> {ext}")
back = QgsVectorLayer(os.path.join(tmp, "0", "export.gpkg") + "|layername=test", "back", "ogr")
check(back.isValid() and back.featureCount() == len(ring), "GeoPackage export readable with all features")

app.exitQgis()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED"))
sys.exit(1 if failures else 0)
