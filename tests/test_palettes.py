"""Map Studio colours: semantic colours, land-use colour standards, brand palette from a logo, colour proofs, the
colour check window and biruni:terrainstyle.

Run:  engine\\bin\\python-qgis.bat tests\\test_palettes.py   (BIRUNI_TEST_PNG=<folder> keeps the preview pictures)
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsHillshadeRenderer, QgsProject, QgsRasterLayer, QgsRectangle, QgsRendererCategory,
                       QgsFillSymbol, QgsSingleBandPseudoColorRenderer, QgsVectorLayer)
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage, QPainter  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
cfg = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = cfg

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import terrain_style  # noqa: E402
from biruni.map_studio import palettes, themes  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
OUT = os.environ.get("BIRUNI_TEST_PNG", tempfile.mkdtemp())
os.makedirs(OUT, exist_ok=True)
failures = []
t0 = time.time()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- semantic classes -------------------------------------------------------------------------------------------
cases = {"سكني": "residential", "Residential": "residential", "R-Residential Villas": "residential",
         "تجاري": "commercial", "سكني تجاري": "mixed", "Mixed Use": "mixed", "صناعي": "industrial",
         "مبنى حكومي": "government", "مدرسة": "education", "جامعة": "education", "مستشفى": "health",
         "مسجد": "religious", "جامع": "religious", "حديقة": "green", "Park": "green", "Parking": "transport",
         "مواقف سيارات": "transport", "زراعي": "agriculture", "مياه": "water", "طرق": "transport",
         "أرض فضاء": "vacant", "مرافق": "utilities", "XYZ-17": None, "": None, None: None}
bad = {k: palettes.classify(k) for k, v in cases.items() if palettes.classify(k) != v}
check(not bad, f"semantic classes in Arabic and English ({len(cases)} values) {bad or ''}")


def poly_layer(values, field="landuse"):
    lyr = QgsVectorLayer(f"Polygon?crs=EPSG:32638&field={field}:string", "landuse", "memory")
    feats = []
    for i, v in enumerate(values):
        f = QgsFeature(lyr.fields())
        x = 500000 + (i % 4) * 120
        y = 2700000 + (i // 4) * 120
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x, y, x + 100, y + 100)))
        f.setAttribute(0, v)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


values = ["سكني", "تجاري", "سكني تجاري", "مسجد", "مدرسة", "مستوصف", "حديقة", "مواقف", "مرافق", "طرق",
          "Residential", "ZONE-X"]
lyr = poly_layer(values)
res = palettes.apply_semantic(lyr, "landuse")
r = lyr.renderer()
cols = {c.value(): c.symbol().color().name().upper() for c in r.categories()}
check(isinstance(r, QgsCategorizedSymbolRenderer) and res["unmatched"] == ["ZONE-X"] and res["matched"] == 11,
      f"apply_semantic: 11 recognised, ZONE-X left over ({res})")
check(cols["سكني"] == palettes.SEMANTIC_COLOURS["residential"].upper() == cols["Residential"]
      and cols["حديقة"] == palettes.SEMANTIC_COLOURS["green"].upper(), "same meaning → same colour (Arabic = English)")

# ---- standards ----------------------------------------------------------------------------------------------------
lbcs = {c[0]: c[3] for c in palettes.STANDARDS["lbcs_function"]["classes"]}
check(lbcs == {"1000": "#FFFF00", "2000": "#FF0000", "3000": "#A020F0", "4000": "#BEBEBE", "5000": "#90EE90",
               "6000": "#0000FF", "7000": "#008B8B", "8000": "#551A8B", "9000": "#228B22"},
      "LBCS Function colours as published by APA")
check({s: d["status"] for s, d in palettes.STANDARDS.items()}
      == {"lbcs_function": "verified", "lbcs_activity": "verified", "sa_landuse": "approximate",
          "eg_landuse": "approximate"}, "verified vs approximate flags")
check(palettes.STANDARD_NAMES == [d["label"] for d in palettes.STANDARDS.values()], "STANDARD_NAMES in step")
check(palettes.standards_for("sa")[0] == "sa_landuse" and palettes.standards_for("EG")[0] == "eg_landuse",
      "the country's standard comes first")
m, un = palettes.match_standard(["1100", "2", "Residential", "فيلا", "Parking", "شي غريب"], "lbcs_function")
check(m["1100"][0] == "1000" and m["Residential"][0] == "1000" and m["فيلا"][0] == "1000"
      and m["Parking"][0] == "4000" and un == ["2", "شي غريب"], f"LBCS matching by code, word and meaning ({m}, {un})")
lyr2 = poly_layer(values)
res = palettes.apply_standard(lyr2, "landuse", "sa_landuse")
cols = {c.value(): c.symbol().color().name().upper() for c in lyr2.renderer().categories()}
check(res["status"] == "approximate" and cols["مسجد"] == "#3C9A5F" and cols["سكني تجاري"] == "#F5B971"
      and "ZONE-X" in res["unmatched"], f"apply_standard Saudi convention + unmatched report ({res['unmatched']})")
res = palettes.apply_standard(poly_layer(values), "landuse", "eg_landuse")
check(res["matched"] >= 9, f"Egyptian convention matches most values ({res['matched']}/{len(values)})")

# ---- themes use meanings; brand theme ----------------------------------------------------------------------------
lyr3 = poly_layer(values)
cats = [QgsRendererCategory(v, QgsFillSymbol.createSimple({"color": "#000000"}), v) for v in values]
lyr3.setRenderer(QgsCategorizedSymbolRenderer("landuse", cats))
themes.apply_theme([lyr3], "atlas", background=False)
cols = {c.value(): c.symbol().color().name().upper() for c in lyr3.renderer().categories()}
check(cols["مسجد"] == palettes.SEMANTIC_COLOURS["religious"].upper(), "a theme keeps the meaning colours")
themes.undo_theme([lyr3])

# ---- brand palette from a logo ----------------------------------------------------------------------------------
logo = QImage(200, 120, QImage.Format.Format_ARGB32)
logo.fill(Qt.GlobalColor.transparent)
p = QPainter(logo)
p.fillRect(0, 0, 200, 40, QColor("#FFFFFF"))
p.fillRect(0, 40, 120, 80, QColor("#0B6E4F"))     # green, largest
p.fillRect(120, 40, 60, 80, QColor("#C8102E"))    # red
p.fillRect(180, 40, 20, 80, QColor("#F2A900"))    # gold
p.end()
logo_path = os.path.join(OUT, "logo.png")
logo.save(logo_path)
pal = palettes.palette_from_image(logo_path, 3)
check(len(pal) == 3 and all(palettes._de(a, b) < 3 for a, b in zip(pal, ["#0B6E4F", "#C8102E", "#F2A900"])),
      f"palette_from_image finds the 3 logo colours, biggest first, white and transparent ignored ({pal})")
acc = palettes.make_accessible(["#C8102E", "#0B6E4F", "#2E7D32"])
check(all(themes.delta_e(a, b) >= 10 for i, a in enumerate(acc) for b in acc[i + 1:])
      and not themes.confusable_pairs(acc, threshold=5), f"make_accessible pulls close greens apart ({acc})")
palettes.save_brand_palette(acc)
check(palettes.brand_palette() == [QColor(c).name() for c in acc], "brand palette saved in settings")
lyr4 = poly_layer(["A", "B", "C"])
lyr4.setRenderer(QgsCategorizedSymbolRenderer("landuse", [QgsRendererCategory(
    v, QgsFillSymbol.createSimple({}), v) for v in "ABC"]))
themes.apply_theme([lyr4], "brand", background=False)
got = [c.symbol().color().name() for c in lyr4.renderer().categories()]
check(got == [QColor(c).name() for c in acc] and "brand" in themes.THEMES, f"'brand' theme uses the logo colours")

# ---- proofs -----------------------------------------------------------------------------------------------------
img = QImage(4, 1, QImage.Format.Format_RGBA8888)
for x, c in enumerate(["#FF0000", "#00FF00", "#0000FF", "#FFFFFF"]):
    img.setPixelColor(x, 0, QColor(c))
for key in ("deutan", "protan", "tritan"):
    im = palettes.proof(img, key)
    ok = all(palettes._de(im.pixelColor(x, 0).name(), themes.simulate(c, key)) < 1.5
             for x, c in enumerate(["#FF0000", "#00FF00", "#0000FF"]))
    check(ok, f"{key} image proof = themes.simulate per pixel")
g = palettes.proof(img, "gray")
check(all(len({g.pixelColor(x, 0).red(), g.pixelColor(x, 0).green(), g.pixelColor(x, 0).blue()}) == 1
          for x in range(4)), "grayscale proof is gray")
c = palettes.proof(img, "cmyk")
check(c.pixelColor(3, 0).red() < 255 and themes.relative_luminance(c.pixelColor(1, 0).name())
      < themes.relative_luminance("#00FF00"), "CMYK proof dulls screen green and paper white")

# ---- terrain ----------------------------------------------------------------------------------------------------
check(terrain_style.nice_breaks(-37, 2468, 10)[:3] == [-250.0, 0.0, 250.0], "nice breaks every 250 m with 0 on a break")
yy, xx = np.mgrid[0:200, 0:240]
dem = (1800 * np.exp(-((xx - 150) ** 2 + (yy - 80) ** 2) / 2500.0) + 600 * np.exp(-((xx - 60) ** 2 + (yy - 140) ** 2)
                                                                                  / 900.0) - 120 + xx * 1.2)
dem_path = os.path.join(OUT, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(dem_path, 240, 200, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000, 30, 0, 2700000, 0, -30))
srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(dem.astype(np.float32))
ds = None
pics = []
for preset in range(len(terrain_style.TERRAIN_PRESETS)):
    proj = QgsProject.instance()
    proj.clear()
    rl = QgsRasterLayer(dem_path, f"dem{preset}")
    proj.addMapLayer(rl)
    out = processing.run("biruni:terrainstyle", {"INPUT": rl, "PRESET": preset, "CLASSES": 10, "SMOOTH": preset == 1,
                                                 "HILLSHADE": True, "OPACITY": 0.55})
    shade = proj.mapLayer(out["HILLSHADE_LAYER"])
    ok = isinstance(rl.renderer(), QgsSingleBandPseudoColorRenderer) and isinstance(shade.renderer(),
                                                                                    QgsHillshadeRenderer)
    ok = ok and shade.renderer().multiDirectional() and proj.layerTreeRoot().layerOrder()[0] == shade
    check(ok, f"terrainstyle preset {preset}: tint + multidirectional hillshade above the DEM")
    pics.append(palettes.render_map([shade, rl], rl.extent(), rl.crs(), 360, 300))
    blue = pics[-1].pixelColor(5, 5)  # the west edge is below sea level
    check(blue.blue() > blue.red(), f"preset {preset}: below sea level is blue")
sheet = QImage(360 * len(pics), 300, QImage.Format.Format_ARGB32)
p = QPainter(sheet)
for i, im in enumerate(pics):
    p.drawImage(360 * i, 0, im)
p.end()
sheet.save(os.path.join(OUT, "terrain_presets.png"))

# ---- colour check window ---------------------------------------------------------------------------------------
QgsProject.instance().clear()
QgsProject.instance().addMapLayer(lyr2)
from biruni.map_studio.color_dialog import ColorPreviewDialog  # noqa: E402

dlg = ColorPreviewDialog(None)
check(len(dlg.images) == 6 and all(not im.isNull() for im in dlg.images), "colour window renders 6 proofs offscreen")
grab = dlg.map_image()
board = QImage(grab.width() * 3, grab.height() * 2, QImage.Format.Format_ARGB32)
p = QPainter(board)
for i, im in enumerate(dlg.images):
    p.drawImage(grab.width() * (i % 3), grab.height() * (i // 3), im)
p.end()
board.save(os.path.join(OUT, "proofs_sa_landuse.png"))
pm = dlg.grab()
check(not pm.isNull(), "window grab")
dlg.close()

print(f"pictures in {OUT}; {time.time() - t0:.1f} s")
QgsProject.instance().clear()
app.exitQgis()
sys.exit(1 if failures else 0)
