"""Map Studio: the map doctor (pre-flight checks + fixes) and the themes.

Run:  engine\\bin\\python-qgis.bat tests\\test_map_doctor_themes.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from osgeo import gdal  # noqa: E402
from qgis.core import (Qgis, QgsApplication, QgsCategorizedSymbolRenderer, QgsFeature, QgsFillSymbol,  # noqa: E402
                       QgsGeometry, QgsLayout, QgsLayoutExporter, QgsLayoutItemLabel, QgsLayoutItemLegend,
                       QgsLayoutItemMap, QgsLayoutItemMapGrid, QgsLayoutItemScaleBar, QgsLayoutPoint,
                       QgsLayoutSize, QgsMapLayerStyle, QgsPalLayerSettings, QgsProject, QgsRasterLayer,
                       QgsRectangle, QgsRendererCategory, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor, QFont  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
cfg = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = cfg

app = QgsApplication([], True)
app.initQgis()
from biruni.map_studio import doctor, themes  # noqa: E402

OUT = os.environ.get("BIRUNI_TEST_PNG", tempfile.mkdtemp())
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- colour science ---------------------------------------------------------------------------------------------
check(abs(themes.contrast_ratio("#000000", "#FFFFFF") - 21.0) < 0.01, "contrast black/white = 21")
check(themes.delta_e("#FF0000", "#FF0000") < 1e-9 and themes.delta_e("#000000", "#FFFFFF") > 99, "ΔE sanity")
check(not themes.confusable_pairs(themes.OKABE_ITO[:7]), "Okabe-Ito stays apart for all three deficiencies")
rg = themes.confusable_pairs(["#D62728", "#2CA02C"])
check(rg and rg[0][2] in ("protan", "deutan"), f"red/green flagged for protan/deutan ({rg})")
check(len(set(themes.safe_qualitative(9))) == 9 and len(themes.safe_qualitative(14)) == 14, "safe palettes sized")
sim = themes.simulate("#FF0000", "deutan")
check(all(0 <= v <= 1 for v in sim), "simulation stays in gamut")

# ---- data -------------------------------------------------------------------------------------------------------
P = QgsProject.instance()
P.setTitle("Test map")


def mem(kind, name, wkts, fields="field=name:string&field=kind:string"):
    lyr = QgsVectorLayer(f"{kind}?crs=EPSG:32638&{fields}", name, "memory")
    feats = []
    for i, (w, a) in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes(a)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    P.addMapLayer(lyr)
    return lyr


x0, y0 = 670000, 2730000
sq = lambda x, y, s=400: f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"  # noqa: E731
parcels = mem("Polygon", "parcels_2024.shp", [(sq(x0, y0), ["قطعة 1", "a"]), (sq(x0 + 400, y0), ["قطعة 2", "b"]),
                                               (sq(x0, y0 + 400), ["قطعة 3", "c"])])
cats = [QgsRendererCategory(v, QgsFillSymbol.createSimple({"color": c}), v)
        for v, c in (("a", "#D62728"), ("b", "#2CA02C"), ("c", "#1F77B4"))]
parcels.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
roads = mem("LineString", "layer_1", [(f"LINESTRING({x0} {y0},{x0+800} {y0+800})", ["شارع الملك فهد", "r"])])
s = QgsPalLayerSettings()
s.fieldName = "name"
fmt = QgsTextFormat()
fmt.setFont(QFont("Arial"))
fmt.setSize(4)
fmt.setColor(QColor("#DDDDDD"))
s.setFormat(fmt)
s.placement = Qgis.LabelPlacement.Curved
roads.setLabeling(QgsVectorLayerSimpleLabeling(s))
roads.setLabelsEnabled(True)

tif = os.path.join(tempfile.mkdtemp(), "coarse.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, 8, 8, 1)
ds.SetGeoTransform((x0, 100, 0, y0 + 800, 0, -100))
srs = gdal.osr.SpatialReference()
srs.ImportFromEPSG(32638)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).Fill(120)
ds = None
raster = QgsRasterLayer(tif, "coarse image")
P.addMapLayer(raster)
osm = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19", "OSM", "wms")
P.addMapLayer(osm)


def build_bad_layout():
    lay = QgsLayout(P)
    lay.initializeDefaults()  # A4 landscape
    mp = QgsLayoutItemMap(lay)
    mp.setRect(0, 0, 200, 150)
    lay.addLayoutItem(mp)
    mp.attemptMove(QgsLayoutPoint(10, 20))
    mp.attemptResize(QgsLayoutSize(200, 160))
    mp.setCrs(parcels.crs())
    mp.setLayers([roads, parcels, raster, osm])
    mp.setExtent(QgsRectangle(x0 - 100, y0 - 100, x0 + 900, y0 + 900))
    mp.setMapRotation(15)
    grid = QgsLayoutItemMapGrid("g", mp)
    grid.setEnabled(True)
    grid.setIntervalX(200)
    grid.setIntervalY(200)
    grid.setAnnotationEnabled(True)
    grid.setFrameStyle(QgsLayoutItemMapGrid.FrameStyle.NoFrame)
    for side in (QgsLayoutItemMapGrid.BorderSide.Left, QgsLayoutItemMapGrid.BorderSide.Top):
        grid.setAnnotationPosition(QgsLayoutItemMapGrid.AnnotationPosition.InsideMapFrame, side)
    mp.grids().addGrid(grid)

    def label(text, x, y, w, h, pt, font="Arial"):
        lab = QgsLayoutItemLabel(lay)
        lab.setText(text)
        f = QgsTextFormat()
        f.setFont(QFont(font))
        f.setSize(pt)
        lab.setTextFormat(f)
        lay.addLayoutItem(lab)
        lab.attemptMove(QgsLayoutPoint(x, y))
        lab.attemptResize(QgsLayoutSize(w, h))
        return lab

    label("Lorem ipsum", 215, 20, 70, 8, 5)
    label("Scale 1:2500", 215, 30, 60, 6, 8)
    label("Overlapping note", 220, 32, 60, 6, 8)
    label("رقم 12 / ١٣", 215, 45, 60, 8, 9, "Consolas")
    label("Off the page", 280, 180, 60, 10, 8)
    sb = QgsLayoutItemScaleBar(lay)
    sb.setStyle("Single Box")
    sb.setUnits(Qgis.DistanceUnit.Meters)
    sb.setUnitsPerSegment(1300)
    sb.setNumberOfSegments(2)
    lay.addLayoutItem(sb)
    sb.attemptMove(QgsLayoutPoint(215, 60))
    leg = QgsLayoutItemLegend(lay)
    leg.setLinkedMap(mp)
    lay.addLayoutItem(leg)
    leg.attemptMove(QgsLayoutPoint(215, 80))
    leg.attemptResize(QgsLayoutSize(30, 15))
    leg.setResizeToContents(False)
    lay.setCustomProperty("rasterize", True)
    return lay


lay = build_bad_layout()
t = time.perf_counter()
found = doctor.check(lay, "survey")
elapsed = time.perf_counter() - t
ids = {f.id for f in found}
print("     found:", sorted(ids), f"{elapsed * 1000:.0f} ms")
check(elapsed < 1.0, f"check() under a second ({elapsed * 1000:.0f} ms)")
for want in ("text-small", "placeholder", "scale-text", "overlap", "off-page", "scalebar-unlinked",
             "legend-raw-names", "credits", "north-missing", "grid-labels", "colour-blind", "label-contrast",
             "raster-blurry", "script-labels", "label-font", "mixed-digits", "date-missing", "crs-missing",
             "pdf-raster", "map-labels-small"):
    check(want in ids, f"finds {want}")
check(found[0].severity == "error", "most serious first")
check(all(f.title for f in found), "every finding has a title")

png_before = os.path.join(OUT, "doctor_before.png")
QgsLayoutExporter(lay).exportToImage(png_before, QgsLayoutExporter.ImageExportSettings())

done = doctor.fix_all(lay, found, "survey")
print("     fixes:", len(done))
left_findings = doctor.check(lay, "survey")
after = {f.id for f in left_findings}
for lf in left_findings:
    print("     -", lf.id, lf.detail)
print("     left:", sorted(after))
for gone in ("text-small", "placeholder", "scale-text", "off-page", "scalebar-unlinked", "legend-raw-names",
             "credits", "north-missing", "grid-labels", "colour-blind", "label-contrast", "script-labels",
             "label-font", "mixed-digits", "date-missing", "crs-missing", "pdf-raster", "map-labels-small"):
    check(gone not in after, f"fixed {gone}")
check("overlap" not in after, "overlaps resolved")
check("raster-blurry" in after, "blurry image stays a warning (not fixable)")
check(any(i.id() == "biruni-credits" and "OpenStreetMap" in i.text() for i in lay.items()
          if isinstance(i, QgsLayoutItemLabel)), "credit text added")
legend = [i for i in lay.items() if isinstance(i, QgsLayoutItemLegend)][0]
labels = [n.customProperty("legend/title-label") or n.name() for n in legend.model().rootGroup().findLayers()]
check("Parcels 2024" in labels and not any(l.endswith(".shp") for l in labels), f"legend names cleaned {labels}")
check(not P.layerTreeRoot().findLayer(parcels.id()).customProperty("legend/title-label"),
      "project layer tree untouched by legend fixes")
png_after = os.path.join(OUT, "doctor_after.png")
QgsLayoutExporter(lay).exportToImage(png_after, QgsLayoutExporter.ImageExportSettings())
check(os.path.exists(png_before) and os.path.exists(png_after), f"before/after rendered to {OUT}")

# a clean layout raises no errors
good = QgsLayout(P)
good.initializeDefaults()
gm = QgsLayoutItemMap(good)
good.addLayoutItem(gm)
gm.attemptMove(QgsLayoutPoint(10, 25))
gm.attemptResize(QgsLayoutSize(200, 170))
gm.setLayers([parcels])
gm.setExtent(QgsRectangle(x0 - 100, y0 - 100, x0 + 900, y0 + 900))
gl = QgsLayoutItemLabel(good)
gl.setText("Parcels of the plan")
f = QgsTextFormat()
f.setSize(18)
gl.setTextFormat(f)
good.addLayoutItem(gl)
gl.attemptMove(QgsLayoutPoint(10, 8))
gl.attemptResize(QgsLayoutSize(150, 12))
doctor.fix_all(good, doctor.check(good, "print"), "print")
check(not [x for x in doctor.check(good, "print") if x.severity == "error"], "a fixed simple layout has no errors")

# ---- themes ----------------------------------------------------------------------------------------------------
check(themes.detect_role(roads) is None and themes.detect_role(parcels) == "parcel", "roles from names")
water = mem("Polygon", "Lakes", [(sq(x0, y0), ["a", "b"])])
streets = mem("LineString", "شوارع", [(f"LINESTRING({x0} {y0},{x0+10} {y0+10})", ["a", "b"])])
check(themes.detect_role(water) == "water" and themes.detect_role(streets) == "road", "water / Arabic road names")


def style_xml(lyr):
    st = QgsMapLayerStyle()
    st.readFromLayer(lyr)
    return st.xmlData()


layers = [parcels, roads, water, streets]
before = {l.id(): style_xml(l) for l in layers}
bg = P.backgroundColor().name()
for theme in themes.THEMES:
    done = themes.apply_theme(layers, theme)
    check(len(done) == 4 and all(themes.current_theme(l) == theme for l in layers), f"theme {theme} applied")
r = parcels.renderer()
check(isinstance(r, QgsCategorizedSymbolRenderer) and len(r.categories()) == 3, "classes kept by themes")
check(P.backgroundColor().name().lower() == themes.theme_background("contrast").lower(), "background follows theme")
check(themes.undo_theme(layers) == 4, "undo restores 4 layers")
check(all(style_xml(l) == before[l.id()] for l in layers), "undo restores the exact styles")
check(P.backgroundColor().name() == bg, "undo restores the background")
themes.apply_theme([parcels], "atlas")
cols = [c.symbol().color() for c in parcels.renderer().categories()]
check(not themes.confusable_pairs(cols), "atlas colours safe for colour-blind readers")
themes.undo_theme([parcels])

t = time.perf_counter()
big = mem("Polygon", "parcels big", [(sq(x0 + i * 10, y0), ["x", str(i % 20)]) for i in range(2000)])
big.setRenderer(QgsCategorizedSymbolRenderer("kind", [QgsRendererCategory(str(i), QgsFillSymbol.createSimple({}), str(i))
                                                      for i in range(20)]))
themes.apply_theme([big], "technical")
check(time.perf_counter() - t < 2.0, "theme on 2000 features / 20 classes is quick")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
P.clear()
app.exitQgis()
sys.exit(1 if failures else 0)
