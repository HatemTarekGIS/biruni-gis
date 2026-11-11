r"""Thesis starter pack (thesis/starter.py, algorithms/thesis_starter.py, thesis/starter_dialog.py).

Run:  engine\bin\python-qgis.bat tests\test_thesis_starter.py

Offline: every network source is replaced by a fake that writes small synthetic files (a tilted-plane DEM,
land cover blocks, Sentinel-2 bands, OSM layers, geoBoundaries GeoJSON). With internet, one small live run
(about 2 x 2 km in Riyadh) is made at the end; without it that part prints SKIP. Set BIRUNI_NO_LIVE=1 to skip it.
"""
import datetime as dt
import json
import math
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,  # noqa: E402
                       QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorFileWriter, QgsVectorLayer)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import numpy as np  # noqa: E402
import processing  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.open_data import WORLDCOVER_CLASSES, utm_for  # noqa: E402
from biruni.thesis import starter  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


class Fb(QgsProcessingFeedback):
    def __init__(self, cancel_after=None):
        super().__init__()
        self.warnings = []
        self.infos = []

    def pushInfo(self, info):  # noqa: N802
        self.infos.append(info)

    def pushWarning(self, info):  # noqa: N802
        self.warnings.append(info)


# ---------------------------------------------------------------------------------------------------
# synthetic data
# ---------------------------------------------------------------------------------------------------
def write_raster(path, arr, gt, epsg, nodata=None, dtype=gdal.GDT_Float32):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, dtype)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(arr)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    ds = None
    return path


def utm_box(rect, res):
    crs = utm_for(rect)
    b = QgsCoordinateTransform(WGS84, crs, QgsProject.instance()).transformBoundingBox(rect)
    x0, y1 = math.floor(b.xMinimum() / res) * res, math.ceil(b.yMaximum() / res) * res
    nx = int(math.ceil((b.xMaximum() - x0) / res))
    ny = int(math.ceil((y1 - b.yMinimum()) / res))
    return crs, x0, y1, nx, ny


def write_vector(path, wkb, fields, rows, crs=WGS84):
    lyr = QgsVectorLayer(wkb, "t", "memory")
    lyr.setCrs(crs)
    lyr.dataProvider().addAttributes([QgsField(n, QMetaType.Type.QString) for n in fields])
    lyr.updateFields()
    feats = []
    for wkt, vals in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(vals)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = "GeoJSON" if path.endswith(".geojson") else "GPKG"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path, QgsProject.instance().transformContext(), o)[0]
    assert err == QgsVectorFileWriter.WriterError.NoError, err
    return path


def box(x0, y0, x1, y1):
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


class FakeSources:
    """Offline stand-in for OnlineSources. ``fail`` = set of source names that raise like a network error."""

    def __init__(self, fail=(), center=(46.70, 24.70)):
        self.fail = set(fail)
        self.calls = []
        self.cx, self.cy = center

    def _maybe_fail(self, what):
        self.calls.append(what)
        if what in self.fail or "all" in self.fail:
            raise starter.QgsProcessingException(f"Download failed: {what} host unreachable (fake)")

    def geocode(self, text, feedback=None, lang="en"):
        self._maybe_fail("geocode")
        cx, cy = self.cx, self.cy
        return [{"name": "A point somewhere", "wkt": f"POINT({cx} {cy})", "bbox": (cx, cy, cx, cy), "kind": "x/y"},
                {"name": f"{text}, Fake Region, Fakeland", "wkt": box(cx - 0.01, cy - 0.01, cx + 0.01, cy + 0.01),
                 "bbox": (cx - 0.01, cy - 0.01, cx + 0.01, cy + 0.01), "kind": "boundary/administrative"}]

    def boundaries(self, iso3, level, folder, feedback=None):
        self._maybe_fail(f"gb{level}")
        cx, cy = self.cx, self.cy
        path = os.path.join(folder, f"gb{level}.geojson")
        if level == 0:
            rows = [(box(cx - 5, cy - 5, cx + 5, cy + 5), ["Fakeland", iso3])]
        elif level == 1:
            rows = [(box(cx - 5, cy - 5, cx + 0.5, cy + 5), ["Central Region", iso3]),
                    (box(cx + 0.5, cy - 5, cx + 5, cy + 5), ["Eastern Region", iso3])]
        else:
            rows = [(box(cx - 0.5, cy - 0.5, cx, cy + 0.5), ["West District", iso3]),
                    (box(cx, cy - 0.5, cx + 0.5, cy + 0.5), ["East District", iso3]),
                    (box(cx + 1, cy + 1, cx + 2, cy + 2), ["Far District", iso3])]
        write_vector(path, "Polygon", ["shapeName", "shapeGroup"], rows)
        return path, {"boundaryName": "Fakeland", "boundaryISO": iso3, "boundaryType": f"ADM{level}",
                      "boundaryYearRepresented": "2019", "boundarySource": "Fake Survey Authority",
                      "boundaryLicense": "CC BY 4.0"}

    def osm(self, key, rect, out, feedback=None):
        self._maybe_fail(f"osm_{key}")
        x0, y0, x1, y1 = rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()
        cx, cy = rect.center().x(), rect.center().y()
        w = x1 - x0
        base = ["osm_id", "name", "name_ar", "name_en"]
        if key == "roads":
            rows = [(f"LINESTRING({x0 - w} {cy},{x1 + w} {cy})", ["1", "King Road", "طريق الملك", None, "primary"]),
                    (f"LINESTRING({cx} {y0},{cx} {y1})", ["2", None, None, None, "residential"]),
                    (f"LINESTRING({x1 + w} {y0},{x1 + 2 * w} {y1})", ["3", "Outside", None, None, "primary"])]
            return write_vector(out, "LineString", base + ["highway"], rows)
        if key == "buildings":
            d = w / 20
            rows = [(box(cx + k * 2 * d, cy + d, cx + k * 2 * d + d, cy + 2 * d), [str(k), None, None, None, "yes"])
                    for k in range(-3, 4)]
            rows.append((box(x1 - d / 2, cy, x1 + d / 2, cy + d), ["99", "edge", None, None, "yes"]))
            rows.append((box(x1 + w, cy, x1 + w + d, cy + d), ["98", "outside", None, None, "yes"]))
            return write_vector(out, "Polygon", base + ["building"], rows)
        if key == "places":
            rows = [(f"POINT({cx} {cy})", ["1", "Cafe", None, None, "amenity", "cafe"]),
                    (f"POINT({x1 + w} {cy})", ["2", "Far cafe", None, None, "amenity", "cafe"])]
            return write_vector(out, "Point", base + ["category", "type"], rows)
        if key == "landuse":
            rows = [(box(x0, y0, cx, cy), ["1", None, None, None, "residential", None])]
            return write_vector(out, "Polygon", base + ["landuse", "leisure"], rows)
        if key == "waterways":
            rows = [(f"LINESTRING({x0} {y0},{x1} {y1})", ["5", "Wadi", None, None, "stream"])]
            return write_vector(out, "LineString", base + ["waterway"], rows)
        rows = [(box(cx, y0, x1, cy), ["7", "Pond", None, None, "pond", "water"])]
        return write_vector(out, "Polygon", base + ["water", "natural"], rows)

    def dem(self, rect, out, feedback=None):
        self._maybe_fail("dem")
        crs, x0, y1, nx, ny = utm_box(rect, 30)
        xs = x0 + 30 * (np.arange(nx) + 0.5)
        z = np.tile(600 + 0.01 * (xs - x0), (ny, 1)).astype("float32")  # rises 1 m per 100 m eastwards
        return write_raster(out, z, (x0, 30, 0, y1, 0, -30), int(crs.authid().split(":")[1]), -32767)

    def landcover(self, rect, out, feedback=None):
        self._maybe_fail("landcover")
        crs, x0, y1, nx, ny = utm_box(rect, 10)
        a = np.full((ny, nx), 60, dtype="uint8")
        a[:, : nx // 4] = 50  # west quarter built-up
        return write_raster(out, a, (x0, 10, 0, y1, 0, -10), int(crs.authid().split(":")[1]), 0, gdal.GDT_Byte)

    def sentinel2(self, rect, start, end, cloud, folder, feedback=None):
        self._maybe_fail("sentinel2")
        crs, x0, y1, nx, ny = utm_box(rect, 10)
        rng = np.random.default_rng(1)
        paths = {}
        for b, base in (("red", 900), ("green", 1000), ("blue", 800), ("nir", 2700)):
            a = (base + rng.integers(0, 200, (ny, nx))).astype("uint16")
            paths[b] = write_raster(os.path.join(folder, f"{b}.tif"), a, (x0, 10, 0, y1, 0, -10),
                                    int(crs.authid().split(":")[1]), 0, gdal.GDT_UInt16)
        return paths, [{"id": "S2B_38RPN_20260915_0_L2A", "date": "2026-09-15", "cloud": 0.4}], 10.0

    def population(self, iso3, rect, folder, feedback=None):
        self._maybe_fail("population")
        res = 1 / 1200.0
        x0 = math.floor(rect.xMinimum() / res) * res - res
        y1 = math.ceil(rect.yMaximum() / res) * res + res
        nx = int((rect.xMaximum() - x0) / res) + 2
        ny = int((y1 - rect.yMinimum()) / res) + 2
        a = np.full((ny, nx), 10.0, dtype="float32")
        out = write_raster(os.path.join(folder, "pop.tif"), a, (x0, res, 0, y1, 0, -res), 4326, -99999)
        return out, {"title": "Fake population 2020", "doi": "10.0/fake", "citation": "Fake WorldPop citation 2020.",
                     "license": "CC BY 4.0", "date": "2020-09-16"}


work = tempfile.mkdtemp(prefix="starter_test_")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------------------------------
# 1. pure helpers
# ---------------------------------------------------------------------------------------------------
riyadh = QgsGeometry.fromWkt(box(46.69, 24.69, 46.71, 24.71))
crs, why = starter.choose_crs(riyadh)
check(crs.authid() == "EPSG:9358", f"Riyadh gets the Saudi national frame KSA-GRF17 / UTM 38N ({crs.authid()}: {why})")
cairo = QgsGeometry.fromWkt(box(31.23, 30.04, 31.25, 30.06))
crs_c, _ = starter.choose_crs(cairo)
check(crs_c.authid() == "EPSG:32636", f"Cairo gets WGS 84 / UTM 36N ({crs_c.authid()})")
crs_s, _ = starter.choose_crs(QgsGeometry.fromWkt(box(-58.5, -34.7, -58.3, -34.5)))
check(crs_s.authid() == "EPSG:32721", f"Buenos Aires (south) gets UTM 21S ({crs_s.authid()})")
check(not crs.isGeographic() and not crs_c.isGeographic(), "chosen systems are projected (metres)")

# land cover area maths on a known raster: 100 x 100 pixels of 10 m = 1 km²
lc = np.zeros((100, 100), dtype="uint8")
lc[:, :50] = 40       # 0.5 km² cropland
lc[:, 50:80] = 50     # 0.3 km² built-up
lc[:, 80:] = 80       # 0.2 km² water
lc[:10, 80:] = 0      # 200 pixels no data -> 0.02 km² less water
p = write_raster(os.path.join(work, "lc.tif"), lc, (500000, 10, 0, 2700000, 0, -10), 32638, 0, gdal.GDT_Byte)
rows = starter.class_areas(p, WORLDCOVER_CLASSES)
got = {r[0]: (round(r[2], 4), round(r[3], 3)) for r in rows}
# 0.98 km² classified: 0.5 / 0.98 = 51.02 %
check(got == {40: (0.5, 51.02), 50: (0.3, 30.612), 80: (0.18, 18.367)}, f"land cover km² and % per class {got}")
check(abs(sum(r[3] for r in rows) - 100) < 1e-9 and rows[0][1] == "Cropland", "percentages sum to 100, largest first")

check(starter.contour_interval(600, 640) == 2.5 or starter.contour_interval(600, 640) == 2,
      f"automatic contour interval for 40 m relief ({starter.contour_interval(600, 640)})")
check(starter.contour_interval(0, 2000) in (100, 200, 250), f"for 2000 m relief ({starter.contour_interval(0, 2000)})")

cand = starter.area_from_candidate({"name": "Tiny, X", "wkt": "POINT(46.7 24.7)", "bbox": (46.7, 24.7, 46.7, 24.7)})
check(cand.geometry.type() == starter.Qgis.GeometryType.Polygon and cand.rect.height() > 0.017,
      "a point place becomes a 2 km box")
a = starter.area_from_place("Al Olaya", FakeSources())
check(a.name == "Al Olaya" and abs(a.rect.width() - 0.02) < 1e-9, "place search prefers the polygon result")

# ---------------------------------------------------------------------------------------------------
# 2. full run, offline, Riyadh, with two failing sources (OSM water, population)
# ---------------------------------------------------------------------------------------------------
area = starter.StudyArea(QgsGeometry.fromWkt(
    "POLYGON((46.69 24.69,46.71 24.69,46.71 24.705,46.70 24.71,46.69 24.71,46.69 24.69))"), "Olaya test", "layer")
fake = FakeSources(fail={"osm_water", "population", "gb2"})
fb = Fb()
t0 = time.time()
st = starter.run(area, work, "Olaya test", {"s2_start": dt.date(2026, 9, 1), "s2_end": dt.date(2026, 9, 30)},
                 fake, fb)
print(f"    offline run: {time.time() - t0:.1f} s")
res = {r["key"]: r for r in st["results"]}
check(os.path.exists(st["qgz"]) and os.path.exists(st["gpkg"]), "project folder with .qgz and .gpkg made")
check(all(os.path.isdir(os.path.join(st["root"], s)) for s in ("data", "outputs", "exports")), "project sub-folders")
check(res["osm_water"]["ok"] is True and "unreachable" in res["osm_water"]["reason"],
      f"OSM water polygons failed, waterway lines kept, reason recorded: {res['osm_water']['reason']}")
check(res["population"]["ok"] is False, "population failure recorded")
check(res["boundaries"]["ok"] is True and "level 2" in res["boundaries"]["reason"],
      f"boundaries kept despite level 2 failing ({res['boundaries']['reason']})")
check(all(res[k]["ok"] for k in ("osm_roads", "osm_buildings", "osm_landuse", "osm_places", "terrain", "landcover",
                                 "sentinel2")), "the other sources succeeded")
check(any("unreachable" in w for w in fb.warnings), "failures were pushed as warnings")
check(st["crs"] == "EPSG:9358", "project CRS = KSA-GRF17 / UTM 38N")

prj = QgsProject()
check(prj.read(st["qgz"]), "the project file opens")
check(prj.crs().authid() == "EPSG:9358", "project CRS stored")
layers = {l.name(): l for l in prj.mapLayers().values()}
check(all(l.isValid() for l in layers.values()), f"all {len(layers)} layers valid")
groups = [g.name() for g in prj.layerTreeRoot().children()]
check(groups[:3] == ["Study area", "Administrative boundaries", "OpenStreetMap"] and "Terrain" in groups and
      "Land cover" in groups and "Satellite imagery (Sentinel-2)" in groups and "Population" not in groups,
      f"layer groups {groups}")

area_proj = QgsGeometry(area.geometry)
area_proj.transform(QgsCoordinateTransform(WGS84, prj.crs(), QgsProject.instance()))
grown = QgsGeometry(area_proj).buffer(0.5, 4)
for nm in ("Roads and streets", "Buildings", "Places and services (POIs)", "Land use and parks",
           "Waterways (lines)", "Second-level divisions (in the study area)"):
    lyr = layers.get(nm)
    if nm == "Second-level divisions (in the study area)":
        check(lyr is None, "failed level 2 boundaries are simply absent")
        continue
    inside = lyr is not None and lyr.featureCount() > 0 and all(grown.contains(f.geometry()) for f in lyr.getFeatures())
    check(inside and lyr.crs().authid() == "EPSG:9358", f"{nm}: clipped to the study area, in the project CRS "
          f"({lyr.featureCount() if lyr else 0} features)")
roads = layers["Roads and streets"]
check(roads.featureCount() == 2, "the road outside the area was dropped, the crossing one cut")
bld = layers["Buildings"]
check(bld.featureCount() == 8, f"buildings: 7 inside + 1 cut at the edge, the outside one dropped ({bld.featureCount()})")
edge = [f for f in bld.getFeatures() if f["name"] == "edge"]
check(edge and edge[0].geometry().area() > 0 and area_proj.contains(edge[0].geometry().buffer(-0.01, 2)),
      "the edge building is cut to the boundary")

s = st["stats"]
w_m = area_proj.boundingBox().width()
check(abs(s["area_km2"] - area_proj.area() / 1e6) < 0.01, f"area {s['area_km2']} km² (ellipsoidal ≈ projected)")
check(abs(s["roads_km_by_type"]["primary"] - w_m / 1000) < 0.05, f"primary road length ≈ area width "
      f"({s['roads_km_by_type']['primary']} vs {w_m / 1000:.3f} km)")
check(s["buildings"] == 8, "building count in the summary")

dem = layers["Elevation (m)"]
check(dem.crs().authid() == "EPSG:9358", "DEM warped into the project CRS (national frame)")
check(595 < s["elev_min"] < s["elev_mean"] < s["elev_max"] < 640, f"elevation min/mean/max {s['elev_min']}/"
      f"{s['elev_mean']}/{s['elev_max']}")
slope, _px, _gt = starter.raster_values(layers["Slope (degrees)"].source())
check(abs(float(np.ma.median(slope)) - math.degrees(math.atan(0.01))) < 0.05,
      f"slope of the synthetic plane ≈ 0.573° ({float(np.ma.median(slope)):.3f})")
asp, _px, _gt = starter.raster_values(layers["Aspect (degrees)"].source())
check(abs(float(np.ma.median(asp)) - 270) < 3, f"aspect faces west ({float(np.ma.median(asp)):.1f}°)")
hs, _px, _gt = starter.raster_values(layers["Hillshade"].source())
check(hs.count() > 0, "hillshade made")
dem_vals, _px, _gt = starter.raster_values(dem.source())
ds = gdal.Open(dem.source())
gt = ds.GetGeoTransform()
# cut corner of the polygon (north-east): a pixel there must be masked
xf = QgsCoordinateTransform(WGS84, prj.crs(), QgsProject.instance())
ne = xf.transform(QgsPointXY(46.7095, 24.7095))
col, row = int((ne.x() - gt[0]) / gt[1]), int((ne.y() - gt[3]) / gt[5])
check(0 <= row < dem_vals.shape[0] and 0 <= col < dem_vals.shape[1] and dem_vals.mask[row, col],
      "the cut north-east corner of the polygon is masked (clip follows the polygon, not its box)")
ds = None
cont = [l for n, l in layers.items() if n.startswith("Contours")]
check(cont and cont[0].featureCount() > 3 and "elev" in cont[0].fields().names(),
      f"contours made ({cont[0].featureCount() if cont else 0}, interval {s.get('contour_interval')} m)")

tab = layers.get("Land cover area per class")
check(tab is not None and tab.featureCount() == 2, "land cover area table in the project")
lc_rows = {f["code"]: (f["area_km2"], f["percent"]) for f in tab.getFeatures()}
check(abs(sum(v[1] for v in lc_rows.values()) - 100) < 0.05 and abs(sum(v[0] for v in lc_rows.values()) -
                                                                     s["area_km2"]) < 0.1,
      f"land cover table: % sum 100, km² sum ≈ area ({lc_rows})")
check(os.path.exists(os.path.join(st["root"], "outputs", "landcover_area.csv")), "land cover CSV written")
lcl = layers["Land cover (ESA WorldCover)"]
check(lcl.renderer().type() == "paletted", "land cover keeps the WorldCover legend")
check(layers["NDVI (vegetation index)"].renderer().type() == "singlebandpseudocolor", "NDVI styled")
nd = s.get("ndvi_mean")
check(nd is not None and 0.4 < nd < 0.6, f"NDVI mean of the synthetic bands ({nd})")
rgb = gdal.Open(layers["Sentinel-2 true colour"].source())
check(rgb.RasterCount == 3 and rgb.GetRasterBand(1).DataType == gdal.GDT_Byte, "true colour = 8-bit RGB")
rgb = None

# location layout
lm = prj.layoutManager().layoutByName("Location of the study area")
check(lm is not None, "location layout created")
if lm is not None:
    from qgis.core import QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutItemScaleBar

    maps = [i for i in lm.items() if isinstance(i, QgsLayoutItemMap)]
    check(len(maps) == 3, f"three map frames ({len(maps)})")
    prop = json.loads(lm.customProperty("biruni/thesis-figure") or "{}")
    check(prop == {"chapter": 1, "order": 1, "caption": "موقع منطقة الدراسة",
                   "caption_en": "Location of the study area"}, f"thesis figure property {prop}")
    by = {m.id(): m for m in maps}
    c, r_, m_ = by.get("Country"), by.get("Region"), by.get("Study area")
    reg_lyr = next(l for l in prj.mapLayers().values() if l.name() == "Region of the study area")
    check(c and r_ and m_ and c.extent().contains(reg_lyr.extent()) and r_.extent().contains(m_.extent()),
          "frames are nested: the country map shows the whole region, the region map the whole study map")
    check(c.overviews().size() == 1 and r_.overviews().size() == 1, "extent indicators on the two locator maps")
    check(any(isinstance(i, QgsLayoutItemPicture) for i in lm.items()), "north arrow")
    check(sum(isinstance(i, QgsLayoutItemScaleBar) for i in lm.items()) >= 1, "scale bar")
    texts = " ".join(i.text() for i in lm.items() if hasattr(i, "text") and callable(i.text))
    check("Fakeland" in texts and "Central Region" in texts and "Olaya test" in texts,
          f"frame labels name the country, region and study area")

# summary and citations
summ = read(st["summary"])
check("Olaya test" in summ and "km²" in summ and "primary" in summ and "Cropland" not in summ and
      "Bare / sparse vegetation" in summ, "summary has area, roads by type and land cover")
check("✖" in summ and "population" in summ.lower(), "summary lists the failed sources")
src = read(os.path.join(st["root"], "DATA_SOURCES.md"))
for needle in ("geoBoundaries", "10.1371/journal.pone.0231866", "OpenStreetMap contributors", "ODbL",
               "10.5270/ESA-c5d3d65", "30 m", "10.5281/zenodo.7254221", "CC BY 4.0", "S2B_38RPN_20260915_0_L2A",
               "2026-09-15", "Not downloaded", "KSA-GRF17"):
    check(needle in src, f"data sources file mentions {needle}")
check("WorldPop" not in src.split("## Not downloaded")[0], "a failed source gets no citation")
bib = read(os.path.join(st["root"], "references.bib"))
check(bib.count("@") == 5, f"references.bib has 5 entries ({bib.count('@')})")

# ---------------------------------------------------------------------------------------------------
# 3. everything offline (Cairo): still a valid project with the study area and a location map
# ---------------------------------------------------------------------------------------------------
cairo_area = starter.StudyArea(cairo, "Downtown", "extent")
st2 = starter.run(cairo_area, work, "Downtown", None, FakeSources(fail={"all"}, center=(31.24, 30.05)), Fb())
res2 = {r["key"]: r for r in st2["results"]}
check(all(r["ok"] is False for k, r in res2.items() if k != "boundaries"), "all network sources failed")
check(res2["boundaries"]["ok"] and "Natural Earth" in res2["boundaries"]["reason"],
      "country outline falls back to the bundled Natural Earth data")
p2 = QgsProject()
check(p2.read(st2["qgz"]) and p2.crs().authid() == "EPSG:32636", "partial project opens, UTM 36N")
names2 = [l.name() for l in p2.mapLayers().values()]
check("Study area" in names2 and "Country" in names2, f"partial project has the study area and country {names2}")
lay2 = p2.layoutManager().layoutByName("Location of the study area")
check(lay2 is not None and len([i for i in lay2.items() if i.type() == 65639]) == 3,
      "location layout still has three frames")
src2 = read(os.path.join(st2["root"], "DATA_SOURCES.md"))
check("Natural Earth" in src2 and "unreachable" in src2, "data sources file lists Natural Earth and the failures")

# same CRS (UTM): masking keeps the source grid exactly - no resampling
dem_src = FakeSources(center=(31.24, 30.05)).dem(cairo.boundingBox(), os.path.join(work, "c_dem.tif"))
cut_gpkg = os.path.join(st2["root"], "Downtown.gpkg")
out = starter.clip_raster(dem_src, os.path.join(work, "c_cut.tif"), cut_gpkg, "study_area",
                          QgsCoordinateReferenceSystem("EPSG:32636"))
a_src, b_out = gdal.Open(dem_src), gdal.Open(out)
check(a_src.GetGeoTransform() == b_out.GetGeoTransform() and a_src.RasterXSize == b_out.RasterXSize,
      "same CRS: the clip keeps the source grid")
va, vb = a_src.ReadAsArray(), b_out.ReadAsArray()
m = vb != b_out.GetRasterBand(1).GetNoDataValue()
check(m.any() and np.allclose(va[m], vb[m]), "same CRS: values unchanged inside the area")
a_src = b_out = None

# ---------------------------------------------------------------------------------------------------
# 4. the Processing tool, with a polygon layer as the study area (fake sources injected)
# ---------------------------------------------------------------------------------------------------
poly = os.path.join(work, "my_area.gpkg")
write_vector(poly, "Polygon", ["id"], [(box(46.695, 24.695, 46.705, 24.705), ["1"])])
saved = starter.OnlineSources
starter.OnlineSources = lambda context=None: FakeSources(fail={"sentinel2"})
try:
    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())
    out = processing.run("biruni:thesisstarter", {"AREA": poly, "NAME": "Alg run", "FOLDER": work,
                                                  "OSM": [0, 1], "POPULATION": True}, context=ctx, feedback=Fb())
finally:
    starter.OnlineSources = saved
check(os.path.exists(out["PROJECT"]) and out["PROJECT"].endswith("Alg run.qgz"), f"tool made {out['PROJECT']}")
check("FAILED" in out["REPORT"] and "Sentinel-2" in out["REPORT"] and "OK: OpenStreetMap: Roads" in out["REPORT"],
      "tool report lists what worked and what failed")
p3 = QgsProject()
p3.read(out["PROJECT"])
n3 = [l.name() for l in p3.mapLayers().values()]
check("Roads and streets" in n3 and "Buildings" in n3 and "Land use and parks" not in n3, "only the chosen OSM data")
pop = next((l for l in p3.mapLayers().values() if l.name().startswith("Population 2020")), None)
check(pop is not None and pop.crs().authid() == "EPSG:4326" and
      pop.renderer().type() == "singlebandpseudocolor", "population kept on its own grid (WGS 84) and styled")
sm = json.loads(read(os.path.join(out["PROJECT_FOLDER"], "outputs", "summary.json")))
# 10 people per cell everywhere: total = 10 x number of cells inside the polygon
pv, _px, _gt = starter.raster_values(pop.source()) if pop else (np.ma.array([]), 0, 0)
check(pop is not None and sm["stats"].get("population") == round(float(pv.sum())) == 10 * pv.count() and
      pv.count() > 100, f"population total = sum of the cells inside the area ({sm['stats'].get('population')})")
check("Fake WorldPop citation 2020." in read(out["SOURCES"]), "WorldPop citation from the dataset record")
check(p3.layoutManager().layoutByName("Location of the study area") is not None, "tool wrote the location layout")
again = processing.run("biruni:thesisstarter", {"EXTENT": "46.695,46.705,24.695,24.705 [EPSG:4326]",
                                                "NAME": "Alg run", "FOLDER": work, "OSM": [], "TERRAIN": False,
                                                "LANDCOVER": False, "SENTINEL": False, "POPULATION": False,
                                                "BOUNDARIES": False, "LOCATION_MAP": False}, context=ctx,
                       feedback=Fb())
check(again["PROJECT_FOLDER"].endswith("Alg run (2)"), "an existing project folder is never overwritten")

# cancelling: the feedback is cancelled after the first source -> the rest is marked cancelled
fbc = Fb()


class CancelAfterBoundaries(FakeSources):
    def boundaries(self, *a, **k):
        r = super().boundaries(*a, **k)
        fbc.cancel()
        return r


st4 = starter.run(starter.StudyArea(riyadh, "Cancel test"), work, "Cancel test", None, CancelAfterBoundaries(), fbc)
check(st4["cancelled"] and all(r["reason"] == "cancelled" for r in st4["results"][1:]) and
      os.path.exists(st4["qgz"]), "cancel stops the remaining sources; the project is still written")

# ---------------------------------------------------------------------------------------------------
# 5. the wizard, off screen
# ---------------------------------------------------------------------------------------------------
try:
    from qgis.gui import QgsMapCanvas

    from biruni.thesis import starter_dialog

    class FakePlugin:
        class iface:  # noqa: N801
            _canvas = QgsMapCanvas()

            @staticmethod
            def mainWindow():  # noqa: N802
                return None

            @classmethod
            def mapCanvas(cls):  # noqa: N802
                return cls._canvas

    dlg = starter_dialog.StarterWizard(FakePlugin())
    dlg.sources = FakeSources()
    dlg.place.setText("Al Olaya")
    dlg.search_place()
    check(dlg.results.count() == 2, "wizard: place search lists the candidates")
    dlg.results.setCurrentRow(1)
    dlg.folder.setText(work)
    params = dlg.parameters()
    check(params.get("AREA") and os.path.exists(params["AREA"].split("|")[0]) and params["NAME"] == "Al Olaya",
          f"wizard: the chosen place becomes the AREA polygon ({params.get('NAME')})")
    check(params["OSM"] == [0, 1, 2, 3, 4] and params["TERRAIN"] and params["LOCATION_MAP"],
          "wizard: all data chosen by default")
    dlg.deleteLater()
except Exception as e:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    check(False, f"wizard builds off screen ({e})")

# ---------------------------------------------------------------------------------------------------
# 6. one small live run (2 x 2 km, Riyadh) when the internet answers
# ---------------------------------------------------------------------------------------------------
def online():
    if os.environ.get("BIRUNI_NO_LIVE"):
        return False
    import socket

    try:
        socket.create_connection(("nominatim.openstreetmap.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


if online():
    live_area = starter.StudyArea(QgsGeometry.fromWkt(box(46.680, 24.690, 46.700, 24.708)), "Riyadh live", "extent")
    fbl = Fb()
    t0 = time.time()
    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())
    stl = starter.run(live_area, work, "Riyadh live", None, starter.OnlineSources(ctx), fbl, ctx)
    print(f"    live run: {time.time() - t0:.0f} s, folder {stl['root']}")
    for r in stl["results"]:
        print(f"    {'OK    ' if r['ok'] else 'FAILED'} {r['key']:<14} {r['seconds']:>6} s  {r['reason'][:150]}")
    print("    stats:", json.dumps({k: v for k, v in stl["stats"].items() if k not in ("landcover",)},
                                   ensure_ascii=False)[:600])
    ok = [r for r in stl["results"] if r["ok"]]
    check(len(ok) >= 3, f"live run: {len(ok)}/{len(stl['results'])} sources worked")
    pl = QgsProject()
    check(pl.read(stl["qgz"]) and all(l.isValid() for l in pl.mapLayers().values()), "live project opens")
    try:
        cands = starter.OnlineSources().geocode("Al Olaya, Riyadh")
        check(bool(cands), f"live geocoder: {cands[0]['name'][:80] if cands else 'nothing'}")
    except Exception as e:  # noqa: BLE001
        check(False, f"live geocoder: {e}")
else:
    print("SKIP live run: no internet (or BIRUNI_NO_LIVE set)")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILURE(S):\n  " + "\n  ".join(failures))
app.exitQgis()
sys.exit(1 if failures else 0)
