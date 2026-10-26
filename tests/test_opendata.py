r"""Online tests for the open data downloaders (need internet).

Run:  engine\bin\python-qgis.bat tests\test_opendata.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject,  # noqa: E402
                       QgsRasterLayer, QgsVectorLayer)

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import open_data  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp()
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())


class Fb(QgsProcessingFeedback):
    def pushInfo(self, info):  # noqa: N802
        print("   ", info)

    def pushWarning(self, info):  # noqa: N802
        print("   warning:", info)


fb = Fb()
TAHRIR = "31.2320,31.2400,30.0420,30.0480 [EPSG:4326]"   # xmin,xmax,ymin,ymax


def run(alg, params):
    try:
        return processing.run(alg, params, context=ctx, feedback=fb)
    except Exception as e:  # noqa: BLE001
        print("   error:", e)
        return None


# pure helpers
r = open_data.QgsRectangle(31.2, 30.0, 31.3, 30.1)
check(abs(open_data.area_km2(r) - 106.6) < 2, "area of a 0.1° box near Cairo ≈ 106 km²")
check(open_data.utm_for(r).authid() == "EPSG:32636", "UTM zone of the area")
check(open_data.dem_tiles(r)[0].endswith("Copernicus_DSM_COG_10_N30_00_E031_00_DEM.tif"), "DEM tile name")
check(open_data.worldcover_tiles(r)[0].endswith("_N30E030_Map.tif"), "WorldCover tile name")
check(open_data._tags('"name:ar"=>"ميدان التحرير","amenity"=>"cafe"') == {"name:ar": "ميدان التحرير",
                                                                          "amenity": "cafe"}, "OSM tag parsing")

res = run("biruni:downloadosm", {"EXTENT": TAHRIR, "DATA": 1, "OUTPUT": os.path.join(tmp, "roads.gpkg")})
lyr = QgsVectorLayer(res["OUTPUT"], "roads") if res else None
check(lyr is not None and lyr.featureCount() > 20 and lyr.fields().indexOf("highway") >= 0,
      f"OSM roads downloaded ({lyr.featureCount() if lyr else 0})")
if lyr:
    named_ar = sum(1 for f in lyr.getFeatures() if f["name_ar"])
    check(named_ar > 0, f"Arabic street names present ({named_ar})")

res = run("biruni:downloadosm", {"EXTENT": TAHRIR, "DATA": 0, "OUTPUT": os.path.join(tmp, "buildings.gpkg")})
lyr = QgsVectorLayer(res["OUTPUT"], "b") if res else None
check(lyr is not None and lyr.featureCount() > 20, f"OSM buildings downloaded ({lyr.featureCount() if lyr else 0})")

res = run("biruni:downloadosm", {"EXTENT": TAHRIR, "DATA": 2, "OUTPUT": os.path.join(tmp, "places.gpkg")})
lyr = QgsVectorLayer(res["OUTPUT"], "p") if res else None
check(lyr is not None and lyr.featureCount() > 5 and any(f["category"] for f in lyr.getFeatures()),
      f"OSM places with categories ({lyr.featureCount() if lyr else 0})")

big = "30.0,32.0,29.0,31.0 [EPSG:4326]"
check(run("biruni:downloadosm", {"EXTENT": big, "DATA": 0, "OUTPUT": "TEMPORARY_OUTPUT"}) is None,
      "too large an area is refused")

res = run("biruni:downloaddem", {"EXTENT": TAHRIR, "HILLSHADE": True, "OUTPUT": os.path.join(tmp, "dem.tif"),
                                 "HILLSHADE_OUTPUT": os.path.join(tmp, "hs.tif")})
dem = QgsRasterLayer(res["OUTPUT"]) if res else None
if dem is not None and dem.isValid():
    st = dem.dataProvider().bandStatistics(1)
    check(dem.crs().authid() == "EPSG:32636" and 0 < st.mean < 100, f"DEM in UTM, Cairo elevation ≈ {st.mean:.0f} m")
    check(os.path.exists(res.get("HILLSHADE_OUTPUT", "")), "hillshade made")
else:
    check(False, "DEM downloaded")

res = run("biruni:downloadlandcover", {"EXTENT": TAHRIR, "OUTPUT": os.path.join(tmp, "lc.tif")})
check(res is not None and os.path.exists(res["OUTPUT"]) and os.path.exists(os.path.join(tmp, "lc.qml")),
      "land cover with its legend style")
if res:
    areas = res.get("AREAS_KM2", {})
    check(areas and max(areas, key=areas.get) == "Built-up", f"downtown Cairo is mostly built-up {areas}")

res = run("biruni:downloadsentinel2", {"EXTENT": TAHRIR, "PRODUCT": 0, "DAYS": 120, "CLOUD": 20, "RESOLUTION": 10,
                                       "OUTPUT": os.path.join(tmp, "s2.tif")})
img = QgsRasterLayer(res["OUTPUT"]) if res else None
check(img is not None and img.isValid() and img.bandCount() == 3 and img.width() > 50,
      f"Sentinel-2 true colour ({img.width() if img else 0}x{img.height() if img else 0})")
res = run("biruni:downloadsentinel2", {"EXTENT": TAHRIR, "PRODUCT": 2, "DAYS": 120, "CLOUD": 20, "RESOLUTION": 10,
                                       "OUTPUT": os.path.join(tmp, "ndvi.tif")})
nd = QgsRasterLayer(res["OUTPUT"]) if res else None
if nd is not None and nd.isValid():
    st = nd.dataProvider().bandStatistics(1)
    check(-1 <= st.minimumValue and st.maximumValue <= 1 and os.path.exists(os.path.join(tmp, "ndvi.qml")),
          f"NDVI in range [{st.minimumValue:.2f}, {st.maximumValue:.2f}] with style")
else:
    check(False, "NDVI")

res = run("biruni:downloadboundaries", {"COUNTRY": "", "EXTENT": TAHRIR, "LEVEL": 1,
                                        "OUTPUT": os.path.join(tmp, "adm1.gpkg")})
lyr = QgsVectorLayer(res["OUTPUT"], "adm") if res else None
check(res is not None and res.get("COUNTRY") == "EGY" and lyr.featureCount() >= 25,
      f"Egypt governorates found from the area ({lyr.featureCount() if lyr else 0})")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
