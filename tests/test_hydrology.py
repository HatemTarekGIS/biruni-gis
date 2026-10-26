"""Tests for the hydrology tool (streams and watersheds from a DEM, on GRASS).

Run:  engine\\bin\\python-qgis.bat tests\\test_hydrology.py
"""
import array
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.normpath(os.path.join(HERE, "..", "engine"))
# what the engine's qgis-bin.env gives the app: where GRASS lives
GRASS = os.path.join(ENGINE, "apps", "grass", "grass85")
os.environ.setdefault("GISBASE", GRASS)
os.environ["PATH"] = os.pathsep.join([os.path.join(GRASS, "lib"), os.path.join(GRASS, "bin"), os.environ["PATH"]])
os.environ.setdefault("GRASS_PYTHON", os.path.join(ENGINE, "bin", "python3.exe"))
os.environ.setdefault("GRASS_COMPATIBILITY_TEST", "0")
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from osgeo import gdal, osr  # noqa: E402
from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject  # noqa: E402

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.hydrology import strahler  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- Strahler order on a hand-made network (lines point downstream)
#   a   b       c
#    \ /        |
#     d   e     |
#      \ /      |
#       f ----- g (c joins f's end: g gets max(2,1) = 2)
net = {"a": ((0, 4), (1, 3)), "b": ((2, 4), (1, 3)), "d": ((1, 3), (2, 2)), "e": ((3, 3), (2, 2)),
       "f": ((2, 2), (3, 1)), "c": ((5, 4), (3, 1)), "g": ((3, 1), (3, 0))}
o = strahler(net)
check(o["a"] == o["b"] == o["e"] == o["c"] == 1, "strahler: sources are order 1")
check(o["d"] == 2, "strahler: two order-1 streams make order 2")
check(o["f"] == 2, "strahler: order 2 meeting order 1 stays 2")
check(o["g"] == 2, "strahler: order 2 meeting order 1 downstream stays 2")
o = strahler({"x": ((0, 1), (1, 0)), "y": ((2, 1), (1, 0)), "z": ((1, 0), (1, -1))})
check(o["z"] == 2, "strahler: confluence of two sources")

# ---- the tool on a synthetic DEM: two valleys joining, sloping south, one pit
tmp = tempfile.mkdtemp(prefix="biruni_hydro_")
dem = os.path.join(tmp, "dem.tif")
n = 200
ds = gdal.GetDriverByName("GTiff").Create(dem, n, n, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000, 10, 0, 2700000 + n * 10, 0, -10))
sr = osr.SpatialReference()
sr.ImportFromEPSG(32637)
ds.SetProjection(sr.ExportToWkt())
vals = array.array("f")
for r in range(n):
    for c in range(n):
        x, y = c / n, r / n
        v = 100 - 40 * y + 15 * abs(math.sin(math.pi * 2 * x)) + 3 * math.sin(7 * x + 3 * y)
        if (c - 60) ** 2 + (r - 50) ** 2 < 25:
            v -= 5
        vals.append(v)
ds.GetRasterBand(1).WriteRaster(0, 0, n, n, vals.tobytes())
ds.GetRasterBand(1).SetNoDataValue(-9999)
ds = None

ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
r = processing.run("biruni:streamsbasins", {"INPUT": dem, "MIN_AREA": 0.05, "STREAMS": "TEMPORARY_OUTPUT",
                                            "BASINS": "TEMPORARY_OUTPUT", "ACCUMULATION": "TEMPORARY_OUTPUT",
                                            "FILLED": os.path.join(tmp, "filled.tif")}, context=ctx, feedback=fb)
check(r["THRESHOLD_CELLS"] == 500, f"0.05 km² on 10 m cells = 500 cells (got {r['THRESHOLD_CELLS']})")
def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


streams = out(r["STREAMS"])
basins = out(r["BASINS"])
check(streams.featureCount() >= 3, f"streams found ({streams.featureCount()})")
orders = sorted({f["strahler"] for f in streams.getFeatures()})
check(orders == [1, 2], f"two valleys joining give orders 1 and 2 ({orders})")
check(all(f["length_m"] > 0 for f in streams.getFeatures()), "every stream has a length")
check(basins.featureCount() >= 3, f"watersheds found ({basins.featureCount()})")
total = sum(f["area_km2"] for f in basins.getFeatures())
# the valleys at x = 0 and x = 1 run along the edges: what drains off the sides has no basin
check(1.5 < total <= 4.0 + 1e-6, f"watersheds cover the middle valley of the 4 km² DEM ({total:.3f} km²)")
check(os.path.isfile(r["FILLED"]), "filled DEM written")
acc = gdal.Open(r["ACCUMULATION"])
mn, mx = acc.GetRasterBand(1).ComputeRasterMinMax(False)
check(mn >= 0 and mx > 500, f"flow accumulation is positive and reaches the outlet ({mn:.0f}..{mx:.0f})")

r = processing.run("biruni:streamsbasins", {"INPUT": dem, "MIN_AREA": 0, "STREAMS": "TEMPORARY_OUTPUT",
                                            "BASINS": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
check(r["THRESHOLD_CELLS"] == 200, f"automatic threshold = 0.5% of the cells ({r['THRESHOLD_CELLS']})")

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
