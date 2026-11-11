"""The water tools on WhiteboxTools (the free open core shipped in engine\\tools\\whitebox): the watershed above
outlet points, where rain water ponds, the flood-prone land along wadis. On synthetic DEMs whose answers are known.

Run:  engine\\bin\\python-qgis.bat tests\\test_whitebox.py
"""
import math
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import whitebox  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


if not whitebox.available():
    print("SKIP WhiteboxTools is not in engine\\tools\\whitebox (scripts\\add_tools.py)")
    sys.exit(0)
check("v2.4" in whitebox.version(), "WhiteboxTools open core: " + whitebox.version())
provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)

tmp = tempfile.mkdtemp(prefix="biruni_wbt_test_")
X0, Y0, CELL = 500000.0, 2700000.0, 10.0


def write_dem(name, z):
    p = os.path.join(tmp, name)
    h, w = z.shape
    ds = gdal.GetDriverByName("GTiff").Create(p, w, h, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, CELL, 0, Y0 + h * CELL, 0, -CELL))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(32637)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(z.astype("float32"))
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds = None
    return p


def xy(col, row, h):
    return X0 + (col + 0.5) * CELL, Y0 + (h - row - 0.5) * CELL


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()

# ---- two valleys side by side, sloping south (rows grow southwards) -----------------------------------------
n = 200
r, c = np.mgrid[0:n, 0:n]
valleys = 100 - 40 * (r / n) + 15 * np.abs(np.sin(np.pi * 3 * c / n)) + 0.5 * np.sin(c / 7.0) * np.cos(r / 9.0)
dem = write_dem("valleys.tif", valleys)

# outlets at the foot of two valley floors (columns 66.7 and 133.3: |sin| = 0), two cells off them
pts = QgsVectorLayer("Point?crs=EPSG:32637&field=name:string", "outlets", "memory")
feats = []
for name, col in (("west", 69), ("east", 131)):
    f = QgsFeature(pts.fields())
    f.setAttributes([name])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*xy(col, 190, n))))
    feats.append(f)
pts.dataProvider().addFeatures(feats)
t0 = time.time()
res = processing.run("biruni:wbtwatershed", {"INPUT": dem, "POINTS": pts, "SNAP": 40, "OUTPUT": "TEMPORARY_OUTPUT",
                                             "SNAPPED": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
took = time.time() - t0
ws = res["OUTPUT"]
got = {f["outlet"]: f for f in ws.getFeatures()}
print(f"     watersheds of 2 outlets on a 200 x 200 DEM in {took:.1f} s: " +
      ", ".join(f"{k}: {f['area_km2']} km²" for k, f in sorted(got.items())))
check(len(got) == 2, "a watershed for each outlet point")
w1 = got.get(1)
inside = QgsGeometry.fromPointXY(QgsPointXY(*xy(67, 40, n)))      # high up the valley floor of column 66.7
check(w1 is not None and w1.geometry().contains(inside), "the first outlet drains its valley, up to its top")
check(w1 is not None and not w1.geometry().contains(QgsGeometry.fromPointXY(QgsPointXY(*xy(133, 40, n)))),
      "…and not the next valley")
area_all = n * n * CELL * CELL / 1e6
check(all(0 < f["area_km2"] < area_all for f in got.values()), "areas in km² inside the DEM's size")
moved = [f.geometry().asPoint() for f in res["SNAPPED"].getFeatures()]
check(len(moved) == 2 and abs(moved[0].x() - xy(66.7, 190, n)[0]) <= 1.6 * CELL,
      f"the point clicked two cells off the valley floor was moved onto the stream ({moved[0].x() - X0:.0f} m)")

# ---- ponding: a sloping street with two bowls, one 2 m deep (kept), one 0.1 m (left out) ----------------------
m = 120
r, c = np.mgrid[0:m, 0:m]
z = 50 - 0.05 * r - 0.02 * c
d1 = np.hypot(r - 40, c - 40) * CELL
z = z - np.where(d1 < 60, 2.0 * (1 - d1 / 60), 0)               # a cone 2 m deep, 60 m wide each side
d2 = np.hypot(r - 90, c - 90) * CELL
z = z - np.where(d2 < 40, 0.1 * (1 - d2 / 40), 0)
pond = write_dem("street.tif", z)
res = processing.run("biruni:wbtponding", {"INPUT": pond, "MIN_DEPTH": 0.3, "MIN_AREA": 100,
                                           "OUTPUT": "TEMPORARY_OUTPUT", "DEPTH": "TEMPORARY_OUTPUT"},
                     context=ctx, feedback=fb)
spots = list(res["OUTPUT"].getFeatures())
print("     " + "; ".join(f"spot {f['spot']}: {f['max_depth_m']} m deep, {f['area_m2']} m², {f['volume_m3']} m³"
                          for f in spots))
check(len(spots) == 1, "the 2 m bowl is a ponding spot; the 10 cm dip is left out")
if spots:
    s = spots[0]
    check(1.6 <= s["max_depth_m"] <= 2.05, f"its depth is the bowl's ({s['max_depth_m']} m)")
    check(s.geometry().contains(QgsGeometry.fromPointXY(QgsPointXY(*xy(40, 40, m)))), "it is where the bowl is")
    check(0 < s["volume_m3"] < math.pi * 60 ** 2 * 2.0 / 3 * 1.2, f"its volume is at most the cone's ({s['volume_m3']} m³)")
check(os.path.exists(res["DEPTH"]), "the water depth raster is written when asked")
flat = write_dem("flat.tif", 10 - 0.05 * r)
res = processing.run("biruni:wbtponding", {"INPUT": flat, "MIN_DEPTH": 0.3, "MIN_AREA": 100,
                                           "OUTPUT": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
check(res["OUTPUT"].featureCount() == 0, "a slope with no low spot: none")

# ---- the flood-prone land along the wadis -------------------------------------------------------------------
res = processing.run("biruni:wbtfloodzone", {"INPUT": dem, "MIN_AREA": 0.05, "HEIGHT": 2.0,
                                             "ZONE": "TEMPORARY_OUTPUT", "STREAMS": "TEMPORARY_OUTPUT",
                                             "HAND": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
zone, streams = res["ZONE"], res["STREAMS"]
zarea = sum(f["area_ha"] for f in zone.getFeatures())
orders = sorted({f["strahler"] for f in streams.getFeatures()})
print(f"     flood-prone land: {zone.featureCount()} pieces, {zarea:.1f} ha; streams: {streams.featureCount()} "
      f"lines, Strahler orders {orders}")
check(streams.featureCount() > 0 and orders and orders[0] >= 1, "the streams with their Strahler order")
check(zone.featureCount() > 0 and 0 < zarea < area_all * 100 * 0.6, "the flood-prone land is a part of the DEM")
floor = QgsGeometry.fromPointXY(QgsPointXY(*xy(67, 120, n)))
ridge = QgsGeometry.fromPointXY(QgsPointXY(*xy(100, 120, n)))       # |sin| = 1: 15 m above the floor
in_zone = lambda g: any(f.geometry().contains(g) for f in zone.getFeatures())   # noqa: E731
check(in_zone(floor) and not in_zone(ridge), "the valley floor floods, the ridge between the valleys does not")
hand = gdal.Open(res["HAND"]).ReadAsArray()
check(np.nanmax(hand[hand > -1000]) > 5, "the height-above-stream raster is written when asked")

# ---- cancelling stops it -----------------------------------------------------------------------------------------
class Stop(QgsProcessingFeedback):
    def isCanceled(self):  # noqa: N802
        return True


try:
    processing.run("biruni:wbtponding", {"INPUT": pond, "OUTPUT": "TEMPORARY_OUTPUT"}, context=ctx, feedback=Stop())
    check(False, "a cancelled run went on")
except Exception as e:  # noqa: BLE001
    check("ancel" in str(e) or "cancel" in str(e).lower(), f"cancelled: stopped ({str(e)[:40]})")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
