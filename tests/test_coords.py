r"""Tests for the coordinate converter.

Run:  engine\bin\python-qgis.bat tests\test_coords.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from biruni import coords  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(p, lon, lat, tol=1e-5):
    return p is not None and abs(p[0] - lon) <= tol and abs(p[1] - lat) <= tol


check(near(coords.parse_any("30.0444, 31.2357"), 31.2357, 30.0444), "decimal lat, lon")
check(near(coords.parse_any("30°02'39.84\"N 31°14'08.52\"E"), 31.2357, 30.0444, 1e-6), "DMS pair with N/E")
check(near(coords.parse_any("31°14'08.52\"E, 30°02'39.84\"N"), 31.2357, 30.0444, 1e-6), "DMS pair lon first")
check(near(coords.parse_any("https://maps.google.com/?q=24.7136,46.6753"), 46.6753, 24.7136), "map link")
zone, north, e, n = coords.to_utm(31.2357, 30.0444)
check(zone == 36 and north, "UTM zone 36N for Cairo")
check(near(coords.parse_any(f"36N {e:.3f} {n:.3f}"), 31.2357, 30.0444, 1e-6), "UTM text round trip")
m = coords.to_mgrs(31.2357, 30.0444)
check(m.startswith("36R") and len(m) == 15, f"MGRS format {m}")
check(near(coords.from_mgrs(m), 31.2357, 30.0444, 2e-5), "MGRS round trip (1 m square)")
# a well-known reference: Statue of Liberty 40.689247 N, 74.044502 W -> 18T WL 80694 06664 (approx.)
m2 = coords.to_mgrs(-74.044502, 40.689247)
check(m2[:5] == "18TWL", f"MGRS 100 km square letters {m2}")
mS = coords.to_mgrs(151.2153, -33.8568)   # Sydney Opera House -> 56H LH
check(mS[:5] == "56HLH", f"southern hemisphere MGRS {mS}")
check(near(coords.from_mgrs(mS), 151.2153, -33.8568, 2e-5), "southern MGRS round trip")
utm36 = QgsCoordinateReferenceSystem("EPSG:32636")
check(near(coords.parse_any(f"{e:.3f} {n:.3f}", utm36), 31.2357, 30.0444, 1e-6), "plain X Y in a chosen CRS")
check(coords.parse_any("hello") is None, "garbage rejected")
check(coords.dms(30.0444, "N", "S") == "30°02'39.84\"N", "DMS formatting")
check(coords.dms(-0.99999999, "N", "S") == "1°00'00.00\"S", "DMS rounding carries to degrees")
rows = dict(coords.formats(31.2357, 30.0444, [utm36]))
check("UTM" in rows and "MGRS" in rows and any("32636" in k for k in rows), "all formats listed")
lyr = coords.points_layer("pts")
coords.add_point(lyr, "x", 31.2357, 30.0444)
check(lyr.featureCount() == 1 and next(lyr.getFeatures())["mgrs"] == m, "point layer with formats")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
