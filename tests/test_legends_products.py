"""Map Studio: smart legends, written captions and ready map products.

Run:  engine\\bin\\python-qgis.bat tests\\test_legends_products.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsGeometry,  # noqa: E402
                       QgsGraduatedSymbolRenderer, QgsLayoutExporter, QgsLayoutItemLegend, QgsLayoutItemMap,
                       QgsPointXY, QgsProject, QgsRasterLayer, QgsRendererRange, QgsSymbol, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()

from biruni.map_studio import auto_layout, captions, legends, products  # noqa: E402

failures = []
TMP = tempfile.mkdtemp(prefix="biruni_lp_")
SHOTS = os.environ.get("BIRUNI_STUDIO_SHOTS", "")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def shot(layout, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        QgsLayoutExporter(layout).renderPageToImage(0, dpi=110).save(os.path.join(SHOTS, name + ".png"))


project = QgsProject.instance()
crs = QgsCoordinateReferenceSystem("EPSG:32637")
project.setCrs(crs)

# ---- districts: 4x4 grid, values higher in the north-east ---------------------------------------------------
dist = QgsVectorLayer("Polygon?crs=EPSG:32637&field=name:string&field=pop:double&field=use:string", "Districts",
                      "memory")
feats = []
uses = ["سكني", "تجاري", "حدائق", "سكني"]
for i in range(4):
    for j in range(4):
        f = QgsFeature(dist.fields())
        x0, y0 = 500000 + i * 1000, 2700000 + j * 1000
        f.setGeometry(QgsGeometry.fromWkt(
            f"POLYGON(({x0} {y0},{x0 + 1000} {y0},{x0 + 1000} {y0 + 1000},{x0} {y0 + 1000},{x0} {y0}))"))
        f.setAttributes([f"حي {i}{j}", float(100 + 300 * i * j), uses[(i + j) % 4]])
        feats.append(f)
dist.dataProvider().addFeatures(feats)
project.addMapLayer(dist)

# ---- captions ----------------------------------------------------------------------------------------------
cap_ar = captions.caption(dist, "pop", "name", "ar")
cap_en = captions.caption(dist, "pop", "name", "en")
check("حي 33" in cap_ar and "حي 00" in cap_ar, f"caption names the max and min place: {cap_ar[:80]}")
check("شمال شرق" in cap_ar and "north-east" in cap_en, "caption finds the north-east concentration")
check("standard deviation" in cap_en, "caption counts the outstanding features")
cat = captions.caption(dist, "use", "name", "en")
check("سكني" in cat and "%" in cat, f"categorical caption: {cat}")

# ---- legends -----------------------------------------------------------------------------------------------
ranges = []
for lo, hi, col in ((0, 500, "#fee5d9"), (500, 1500, "#fcae91"), (1500, 3000, "#cb181d")):
    s = QgsSymbol.defaultSymbol(dist.geometryType())
    s.setColor(QColor(col))
    ranges.append(QgsRendererRange(lo, hi, s, f"{lo}-{hi}"))
dist.setRenderer(QgsGraduatedSymbolRenderer("pop", ranges))
layout = auto_layout.build(project, "report", layers=[dist], title="Population", lang="en", add_to_manager=False)
mp = next(i for i in layout.items() if isinstance(i, QgsLayoutItemMap))
ph = layout.pageCollection().page(0).pageSize().height()
pic = legends.add_histogram_legend(layout, dist, 15, ph - 70)
check(pic is not None and os.path.exists(pic.picturePath()), "histogram legend picture written")
svg_of = lambda item: open(item.picturePath()[:-4] + ".svg", encoding="utf-8").read()  # noqa: E731
svg = svg_of(pic)
check(svg.count("<rect") >= 10 and "#cb181d" in svg, "histogram bars use the class colours")
check(legends.nice_values(12345) == [10000, 5000, 1000], "nested circles get round values")
circ = legends.add_nested_circles(layout, 12345, 85, ph - 70)
check(circ is not None and "circle" in svg_of(circ), "nested circles legend")
biv = legends.add_bivariate_legend(layout, "Income", "Density", 145, ph - 70)
check(svg_of(biv).count("<rect") == 9, "bivariate legend is 3×3")
leg = legends.add_frame_legend(layout, mp, 20, 20)
check(isinstance(leg, QgsLayoutItemLegend) and leg.legendFilterByMapEnabled(), "frame-filtered legend")
cap_item = captions.add_caption(layout, cap_en, 15, ph - 26, 180, lang="en")
check(cap_item.text() == cap_en, "caption added as a label")
shot(layout, "legends")

# ---- land use ----------------------------------------------------------------------------------------------
check(products.land_use_colour("سكني") == "#F6DD6E" and products.land_use_colour("Park") == "#79C36A",
      "land-use colours from Arabic/English names")
lu = products.land_use_map(dist, "use", project, purpose="report", lang="ar")
check(dist.renderer().type() == "categorizedSymbol" and lu.pageCollection().pageCount() == 1, "land-use map")
shot(lu, "land_use")

# ---- topographic -------------------------------------------------------------------------------------------
dem = os.path.join(TMP, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(dem, 200, 200, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000, 20, 0, 2704000, 0, -20))
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
ds.SetProjection(srs.ExportToWkt())
yy, xx = np.mgrid[0:200, 0:200]
z = 600 + 120 * np.exp(-((xx - 70) ** 2 + (yy - 80) ** 2) / 900) + 60 * np.exp(-((xx - 150) ** 2 + (yy - 140) ** 2) / 600)
ds.GetRasterBand(1).WriteArray(z.astype(np.float32))
ds = None
demlyr = QgsRasterLayer(dem, "DEM")
project.addMapLayer(demlyr)
topo, parts = products.topographic_map(demlyr, project, purpose="report", lang="en")
check(parts["contours"].featureCount() > 5 and parts["interval"] in (2, 5, 10), f"contours every {parts['interval']}")
check(parts["spots"].featureCount() >= 20, "spot heights placed")
top_spot = max(f["elev"] for f in parts["spots"].getFeatures())
check(abs(top_spot - float(z.max())) < 1, f"highest spot height = DEM maximum ({top_spot})")
shot(topo, "topographic")

# ---- subdivision -------------------------------------------------------------------------------------------
parcels = QgsVectorLayer("Polygon?crs=EPSG:32637&field=parcel_no:string", "Parcels", "memory")
pf = []
for k in range(6):
    f = QgsFeature(parcels.fields())
    x0 = 500000 + k * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x0} 2700000,{x0 + 20} 2700000,{x0 + 20} 2700030,{x0} 2700030,"
                                      f"{x0} 2700000))"))
    f.setAttributes([str(101 + k)])
    pf.append(f)
parcels.dataProvider().addFeatures(pf)
streets = QgsVectorLayer("LineString?crs=EPSG:32637&field=width:double", "Streets", "memory")
sf = QgsFeature(streets.fields())
sf.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(500000, 2699990), QgsPointXY(500120, 2699990)]))
sf.setAttributes([15.0])
streets.dataProvider().addFeatures([sf])
for lyr in (parcels, streets):
    project.addMapLayer(lyr)
sub = products.subdivision_plan(parcels, project, streets=streets, width_field="width", purpose="report", lang="ar")
sides = project.mapLayersByName("Side lengths") or [l for l in project.mapLayers().values() if l.name() == "أطوال الأضلاع"]
check(sides and sides[0].featureCount() == 6 * 3 + 1, "shared parcel sides written once")
check(any(type(i).__name__ == "QgsLayoutFrame" for i in sub.items()), "areas table on the plan")
shot(sub, "subdivision")

# ---- swipe -------------------------------------------------------------------------------------------------
html_path = products.swipe_page(demlyr, parts["hillshade"], os.path.join(TMP, "swipe.html"), "Before / after",
                                width_px=400, labels=("2020", "2026"))
page = open(html_path, encoding="utf-8").read()
check(page.count("data:image/jpeg;base64,") == 2 and "type=\"range\"" in page and "2026" in page,
      "swipe page embeds both images and a slider")

# ---- statistical atlas ---------------------------------------------------------------------------------------
pdf = products.statistical_atlas(dist, "name", ["pop"], os.path.join(TMP, "atlas.pdf"), project, lang="en")
info = gdal.Info(pdf) if os.path.exists(pdf) else ""
check(os.path.exists(pdf) and os.path.getsize(pdf) > 10000, "atlas PDF written")
charts = os.listdir(os.path.splitext(pdf)[0] + "_charts")
check(len(charts) == 16, "one chart per district")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
