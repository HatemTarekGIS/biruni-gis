"""Standalone tests for the point cloud / drone tools (PDAL).

Run:  engine\\bin\\python-qgis.bat tests\\test_point_cloud.py
"""
import csv
import math
import os
import sys
import tempfile

import numpy as np
from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointCloudLayer, QgsPointXY,
                       QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from osgeo import gdal  # noqa: E402

from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import point_cloud as pc  # noqa: E402

gdal.UseExceptions()
provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_pc_test_")
X0, Y0 = 500000.0, 2700000.0


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def plane(x, y):  # x, y relative to the origin
    return 100 + 0.05 * x + 0.02 * y


def make_las(path, with_srs=True, seed=1):
    """A 200 m × 200 m sloping ground, a 20 m × 20 m flat-roofed building 8 m high and a few noise points."""
    rng = np.random.default_rng(seed)
    n = 120000
    x = rng.uniform(0, 200, n)
    y = rng.uniform(0, 200, n)
    z = plane(x, y) + rng.normal(0, 0.03, n)
    box = (np.abs(x - 100) < 10) & (np.abs(y - 100) < 10)
    z[box] = plane(100, 100) + 8 + rng.normal(0, 0.03, box.sum())
    noise = rng.choice(n, 20, replace=False)
    z[noise] += 60
    txt = path + ".csv"
    np.savetxt(txt, np.c_[X0 + x, Y0 + y, z], delimiter=",", header="X,Y,Z", comments="", fmt="%.3f")
    reader = {"type": "readers.text", "filename": txt}
    writer = {"type": "writers.las", "filename": path, "compression": "laszip"}
    if with_srs:
        reader["override_srs"] = "EPSG:32637"
        writer["a_srs"] = "EPSG:32637"
    pc.run_pipeline([reader, writer])
    return box.sum() / n


def value_at(path, x, y):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    col = int((X0 + x - gt[0]) / gt[1])
    row = int((Y0 + y - gt[3]) / gt[5])
    return float(ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)[0, 0])


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()

# ---- helpers -------------------------------------------------------------------------------------
check(pc.nice_step(0.07) == 0.1 and pc.nice_step(0.3) == 0.5 and pc.nice_step(3) == 5, "nice steps")
check(pc.auto_resolution(40000, {"minx": 0, "maxx": 200, "miny": 0, "maxy": 200}) == 2, "auto cell size from density")
g = pc.grid({"minx": 10.3, "maxx": 20.1, "miny": 5.0, "maxy": 7.2}, 0.5)
check(g == {"origin_x": 10.0, "origin_y": 5.0, "width": 21, "height": 5}, f"grid snapped to the cell size ({g})")

las = os.path.join(TMP, "scene.laz")
roof_share = make_las(las)
info = pc.summary(las)
check(info["count"] == 120000 and info["wkt"] and not info["geographic"], "summary: count and coordinate system")

# ---- ground surface --------------------------------------------------------------------------------
layer = QgsPointCloudLayer(las, "scene", "pdal")
check(layer.isValid(), "the LAZ opens as a point cloud layer")
QgsProject.instance().addMapLayer(layer)
res = processing.run("biruni:lasground", {
    "INPUT": layer, "MODE": 0, "NOISE": True, "RESOLUTION": 1.0, "INTERVAL": 2.0,
    "DTM": os.path.join(TMP, "dtm.tif"), "DSM": os.path.join(TMP, "dsm.tif"), "NDSM": os.path.join(TMP, "ndsm.tif"),
    "HILLSHADE": os.path.join(TMP, "hs.tif"), "CONTOURS": os.path.join(TMP, "contours.gpkg"),
    "CLASSIFIED": os.path.join(TMP, "classified.laz")}, context=ctx, feedback=fb)
dtm = res["DTM"]
check(abs(value_at(dtm, 50.5, 150.5) - plane(50.5, 150.5)) < 0.15, "DTM follows the sloping ground")
check(abs(value_at(dtm, 100.5, 100.5) - plane(100.5, 100.5)) < 0.3,
      f"DTM under the building is the ground ({value_at(dtm, 100.5, 100.5):.2f} vs {plane(100.5, 100.5):.2f})")
check(abs(value_at(res["DSM"], 100.5, 100.5) - (plane(100, 100) + 8)) < 0.3, "DSM keeps the roof")
check(abs(value_at(res["NDSM"], 100.5, 100.5) - 8) < 0.4 and abs(value_at(res["NDSM"], 30.5, 30.5)) < 0.2,
      "nDSM: building 8 m, open ground 0 m")
ground_pct = res["GROUND_PERCENT"]
check(100 * (1 - roof_share) - 2.5 < ground_pct < 100 * (1 - roof_share) + 0.5,
      f"ground share {ground_pct:.1f}% (roofs are {100 * roof_share:.1f}%)")
check(res["POINT_COUNT"] == 120000 and 2.5 < res["DENSITY"] < 3.5, "point count and density reported")
d1, d2 = gdal.Open(dtm), gdal.Open(res["DSM"])
check(d1.GetGeoTransform() == d2.GetGeoTransform() and d1.RasterXSize == d2.RasterXSize, "DTM and DSM on one grid")
check("32637" in (d1.GetSpatialRef().GetAuthorityCode(None) or ""), "DTM carries the coordinate system")
check(QgsRasterLayer(res["HILLSHADE"], "hs").isValid(), "hillshade written")
cont = QgsVectorLayer(res["CONTOURS"], "c", "ogr")
elevs = [f["elev"] for f in cont.getFeatures()]
check(cont.featureCount() > 5 and all(abs(e / 2 - round(e / 2)) < 1e-6 for e in elevs),
      f"contours every 2 m ({cont.featureCount()} lines)")
counts = pc.class_counts(res["CLASSIFIED"])
check(counts.get(2, 0) > 100000 and counts.get(1, 0) > 1000, f"classified copy has ground and other classes {counts}")
check(counts.get(7, 0) >= 15, f"noise points found ({counts.get(7, 0)} of 20)")

# automatic cell size and contour interval, no optional outputs
res2 = processing.run("biruni:lasground", {"INPUT": las, "DTM": os.path.join(TMP, "dtm2.tif"),
                                           "HILLSHADE": None, "CONTOURS": None}, context=ctx, feedback=fb)
check(res2["RESOLUTION"] == 2 and os.path.exists(res2["DTM"]), f"automatic cell size {res2['RESOLUTION']} m")

# keep the ground class of an already classified file
res3 = processing.run("biruni:lasground", {"INPUT": res["CLASSIFIED"], "MODE": 1, "NOISE": False, "RESOLUTION": 1,
                                           "DTM": os.path.join(TMP, "dtm3.tif"), "HILLSHADE": None,
                                           "CONTOURS": None}, context=ctx, feedback=fb)
check(abs(res3["GROUND_PERCENT"] - ground_pct) < 0.5, "ground class of the file kept")

# a file with no coordinate system, given one in the tool
bare = os.path.join(TMP, "nosrs.laz")
make_las(bare, with_srs=False, seed=2)
check(not pc.summary(bare)["wkt"], "test file has no coordinate system")
res4 = processing.run("biruni:lasground", {"INPUT": bare, "CRS": "EPSG:32637", "RESOLUTION": 2,
                                           "DTM": os.path.join(TMP, "dtm4.tif"), "HILLSHADE": None,
                                           "CONTOURS": None}, context=ctx, feedback=fb)
check("32637" in (gdal.Open(res4["DTM"]).GetSpatialRef().GetAuthorityCode(None) or ""),
      "missing coordinate system assigned")

# ---- profile ---------------------------------------------------------------------------------------
line = QgsVectorLayer("LineString?crs=EPSG:32637&field=name:string", "lines", "memory")
f = QgsFeature(line.fields())
f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(X0 + 20, Y0 + 30), QgsPointXY(X0 + 180, Y0 + 150)]))
f.setAttributes(["section"])
line.dataProvider().addFeatures([f])
prof_csv = os.path.join(TMP, "profile.csv")
pr = processing.run("biruni:lasprofile", {"INPUT": dtm, "LINES": line, "STEP": 5, "OUTPUT": "memory:",
                                          "CSV": prof_csv}, context=ctx, feedback=fb)
pts = pr["OUTPUT"] if not isinstance(pr["OUTPUT"], str) else ctx.getMapLayer(pr["OUTPUT"])
feats = list(pts.getFeatures())
check(len(feats) == 41 and abs(pr["LENGTH"] - 200) < 1e-6, f"profile: a point every 5 m over 200 m ({len(feats)})")
errs = [abs(ft["elevation"] - plane(ft.geometry().asPoint().x() - X0, ft.geometry().asPoint().y() - Y0))
        for ft in feats if ft["elevation"] is not None]
check(errs and max(errs) < 0.2, f"profile elevations match the ground (max error {max(errs):.3f} m)")
rows = list(csv.reader(open(prof_csv, encoding="utf-8-sig")))
check(rows[0] == ["line_id", "distance_m", "elevation_m"] and len(rows) == 42 and rows[-1][1] == "200.000",
      "profile CSV for Excel")


# ---- volumes ---------------------------------------------------------------------------------------
def make_raster(path, func, res=0.25, size=80):
    n = int(size / res)
    xs = (np.arange(n) + 0.5) * res
    ys = size - (np.arange(n) + 0.5) * res
    xx, yy = np.meshgrid(xs, ys)
    z = func(xx, yy).astype("float32")
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, res, 0, Y0 + size, 0, -res))
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32637)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(pc.NODATA)
    ds.GetRasterBand(1).WriteArray(z)
    ds = None
    return path


R, H = 15.0, 6.0
cone_v = math.pi * R * R * H / 3


def cone(xx, yy):
    r = np.hypot(xx - 40, yy - 40)
    return np.where(r < R, H * (1 - r / R), 0.0)


flat = make_raster(os.path.join(TMP, "flat_pile.tif"), lambda x, y: 50 + cone(x, y))
base_flat = make_raster(os.path.join(TMP, "flat_base.tif"), lambda x, y: 50 + 0 * x)
tilt = make_raster(os.path.join(TMP, "tilt_pile.tif"), lambda x, y: 50 + 0.1 * x + cone(x, y))
base_tilt = make_raster(os.path.join(TMP, "tilt_base.tif"), lambda x, y: 50 + 0.1 * x, res=0.5)
pit = make_raster(os.path.join(TMP, "pit.tif"), lambda x, y: 50 - cone(x, y))

polys = QgsVectorLayer("Polygon?crs=EPSG:32637&field=name:string", "areas", "memory")
pf = QgsFeature(polys.fields())
pf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + 40, Y0 + 40)).buffer(20, 64))
pf.setAttributes(["pile"])
polys.dataProvider().addFeatures([pf])


def volume(surface, base, level=0.0, base_surface=None):
    out = processing.run("biruni:lasvolume", {"INPUT": surface, "POLYGONS": polys, "BASE": base, "LEVEL": level,
                                              "BASE_SURFACE": base_surface, "OUTPUT": "memory:"},
                         context=ctx, feedback=fb)
    lyr = out["OUTPUT"] if not isinstance(out["OUTPUT"], str) else ctx.getMapLayer(out["OUTPUT"])
    return out, next(lyr.getFeatures())


for base, label in ((0, "lowest edge"), (1, "mean edge"), (2, "fixed level 50"), (3, "base surface")):
    out, ft = volume(flat, base, 50.0, base_flat if base == 3 else None)
    check(abs(out["ABOVE_M3"] - cone_v) / cone_v < 0.03 and out["BELOW_M3"] < 1,
          f"cone volume, base = {label}: {out['ABOVE_M3']:.1f} m³ (true {cone_v:.1f})")
check(abs(ft["area_m2"] - math.pi * 400) / (math.pi * 400) < 0.02 and ft["name"] == "pile"
      and abs(ft["surface_max"] - 56) < 0.2 and ft["covered_pct"] == 100,
      f"area {ft['area_m2']:.1f} m², original fields kept, surface max {ft['surface_max']}")
out, ft = volume(tilt, 3, base_surface=base_tilt)
check(abs(out["ABOVE_M3"] - cone_v) / cone_v < 0.03, f"cone on a slope against the ground before: {out['ABOVE_M3']:.1f}")
out, ft = volume(tilt, 0)
check(out["ABOVE_M3"] > cone_v * 1.5, "lowest edge on a slope counts the slope too (as expected)")
out, ft = volume(pit, 2, 50.0)
check(abs(out["BELOW_M3"] - cone_v) / cone_v < 0.03 and abs(out["NET_M3"] + out["BELOW_M3"]) < 1e-6,
      f"pit: volume below the level {out['BELOW_M3']:.1f} m³, net negative")

# ---- the other tools still load ------------------------------------------------------------------------
ids = {a.id() for a in provider.algorithms()}
check({"biruni:lasground", "biruni:lasprofile", "biruni:lasvolume"} <= ids, "tools registered")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
