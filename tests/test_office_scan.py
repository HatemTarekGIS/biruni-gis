"""Tests for biruni:scantoparcels (parcels from a scanned plan).

Run:  engine\\bin\\python-qgis.bat tests\\test_office_scan.py
A synthetic plan is drawn with known parcels, numbers and control points.
"""
import math
import os
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms import _scan_ocr as ocr  # noqa: E402
from biruni.algorithms.scan_to_parcels import (PlanTransform, block_min, otsu, parse_control_points,  # noqa: E402
                                               pick_number)

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


TMP = tempfile.mkdtemp(prefix="biruni_scan_test_")
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()

# ---- control points ---------------------------------------------------------------------------------------
pts = parse_control_points("# my points\n١٠٠ ٢٠٠ 600000.5 2700000\n300,400,600010,2699990.25\n\nbad line\n")
check(pts == [(100, 200, 600000.5, 2700000), (300, 400, 600010, 2699990.25)], f"typed points, Arabic digits: {pts}")
qp = ("#CRS: EPSG:32638\nmapX,mapY,sourceX,sourceY,enable,dX,dY,residual\n"
      "600000,2700000,10,-20,1,0,0,0\n600100,2700000,510,-20,1,0,0,0\n600100,2699900,510,-520,0,0,0,0\n")
check(parse_control_points(qp) == [(10, 20, 600000, 2700000), (510, 20, 600100, 2700000)],
      "QGIS .points text: disabled point skipped, rows flipped downwards")
pfile = os.path.join(TMP, "plan.points")
with open(pfile, "w", encoding="utf-8") as f:
    f.write(qp)
check(len(parse_control_points(pfile)) == 2, "QGIS .points file path")

S, TH, X0, Y0 = 0.2, math.radians(12), 600000.0, 2700000.0


def truth(px, py):
    u, v = px, -py
    return (X0 + S * (math.cos(TH) * u - math.sin(TH) * v), Y0 + S * (math.sin(TH) * u + math.cos(TH) * v))


corners = [(80, 80), (2320, 80), (2320, 1720), (80, 1720), (1200, 900)]
gcp = [(px, py) + truth(px, py) for px, py in corners]
t2 = PlanTransform.fit(gcp[:2])
check(t2.kind == "similarity" and t2.rms < 1e-6, f"2 points: similarity, exact (rms {t2.rms:.2e})")
x, y = t2.apply(1500, 1300)
check(math.hypot(x - truth(1500, 1300)[0], y - truth(1500, 1300)[1]) < 1e-6, "similarity reproduces a third point")
t3 = PlanTransform.fit(gcp[:3])
check(t3.kind == "affine" and t3.rms < 1e-6, "3 points: affine")
t5 = PlanTransform.fit(gcp)
check(t5.kind == "projective" and t5.rms < 1e-5, f"5 points: projective, exact (rms {t5.rms:.2e})")
check(abs(t5.pixel_size(1200, 900) - S) < 1e-6, "pixel size on the ground")
noisy = [(px, py, X + (0.5 if i == 0 else 0), Y) for i, (px, py, X, Y) in enumerate(gcp)]
ta = PlanTransform.fit(noisy, "affine")
check(max(ta.residuals) == ta.residuals[0] and ta.rms > 0.1, "a wrong point shows the largest error")
try:
    PlanTransform.fit(gcp[:2], "affine")
    check(False, "affine with 2 points refused")
except ValueError:
    check(True, "affine with 2 points refused")

# ---- small helpers ----------------------------------------------------------------------------------------
g = np.full((10, 10), 250, np.uint8)
g[4, :] = 20
check(block_min(g, 3)[1].max() == 20 and block_min(g, 3).shape == (4, 4), "block_min keeps a 1-pixel line")
t = otsu(np.concatenate([np.full(900, 240), np.full(90, 25), np.full(10, 60)]).astype(np.uint8))
check(60 <= t < 240, f"Otsu threshold between ink and paper ({t})")
words = [{"text": "٤", "lang": "ar-SA", "tile": 0, "line": 1, "x": 100, "y": 10, "w": 12, "h": 20},
         {"text": "٨", "lang": "ar-SA", "tile": 0, "line": 1, "x": 115, "y": 10, "w": 12, "h": 20},
         {"text": "25.00", "lang": "en-US", "tile": 0, "line": 2, "x": 100, "y": 80, "w": 50, "h": 12},
         {"text": "abc", "lang": "en-US", "tile": 0, "line": 3, "x": 0, "y": 0, "w": 5, "h": 5}]
nums = ocr.deduplicate(ocr.numbers(words))
check(sorted(n["norm"] for n in nums) == ["25.00", "48"], f"split Arabic digits joined, words dropped: "
                                                          f"{[n['norm'] for n in nums]}")
no, conf = pick_number(nums, 113, 20, 200)
check(no == "48" and conf == 0.9, f"parcel number = the whole number, not the length ({no}, {conf})")


def ink_of(text, size, font="C:/Windows/Fonts/tahoma.ttf"):
    img = Image.new("L", (size * 8, size * 3), 0)
    ImageDraw.Draw(img).text((size, size), text, font=ImageFont.truetype(font, size), fill=255)
    return np.asarray(img) > 128


for text, want, integer in (("١٠٥", "105", True), ("30.00", "30.00", False), ("247", "247", True)):
    got = ocr.glyph_numbers(ink_of(text, 36))
    check(len(got) == 1 and got[0]["norm"] == want and got[0]["is_integer"] == integer,
          f"read by shape: {text} -> {[(g['norm'], g['is_integer']) for g in got]}")
check(ocr.glyph_numbers(ink_of("قطعة", 36)) == [], "Arabic letters are not read as a number")

# ---- a synthetic plan -------------------------------------------------------------------------------------
W, H = 2400, 1800
FONT = "C:/Windows/Fonts/arial.ttf"
big = ImageFont.truetype(FONT, 40)
tiny = ImageFont.truetype(FONT, 16)
AR = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
expected = []  # (pixel polygon, number, arabic digits?)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def draw_plan(path, shade=False):
    img = Image.new("L", (W, H), 245)
    d = ImageDraw.Draw(img)
    d.rectangle((76, 76, 2324, 1724), outline=25, width=8)          # thick frame around the plan
    n = 1
    for bx, arabic in ((200, False), (1300, True)):                  # two blocks of 3 × 2 parcels
        for row in range(2):
            for col in range(3):
                x0, y0 = bx + 300 * col, 200 + 300 * row
                expected.append((rect(x0, y0, x0 + 300, y0 + 300), str(n), arabic))
                n += 1
    for col in range(4):                                             # a block of 4 tall parcels + a split one
        x0 = 200 + 400 * col
        expected.append((rect(x0, 1000, x0 + 400, 1600), str(n), False))
        n += 1
    expected.append(([(1800, 1000), (2200, 1000), (2200, 1200), (1800, 1300)], str(n), False))
    expected.append(([(1800, 1300), (2200, 1200), (2200, 1600), (1800, 1600)], str(n + 1), False))
    for poly, _no, _ar in expected:
        d.line(poly + [poly[0]], fill=25, width=3)
    for poly, no, arabic in expected:
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        text = no.translate(AR) if arabic else no
        d.text((cx, cy), text, font=big, fill=25, anchor="mm")
        d.text((poly[0][0] + 110, poly[0][1] + 12), "30.00", font=tiny, fill=25)   # a length along the edge
    for x in range(120, 2280, 40):                                   # dashed street centre line
        d.line((x, 900, x + 20, 900), fill=60, width=2)
    rng = np.random.default_rng(1)
    for _ in range(300):                                             # specks of dust
        x, y = rng.integers(0, W), rng.integers(0, H)
        d.ellipse((x, y, x + 2, y + 2), fill=40)
    a = np.asarray(img).astype(np.float32)
    if shade:  # a dark corner, as on a scan of a folded, yellowed sheet
        yy, xx = np.mgrid[0:H, 0:W]
        a = a - 110 * np.clip(1 - np.hypot(xx - W, yy - H) / 1400, 0, 1)
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(path)


plan = os.path.join(TMP, "plan.png")
draw_plan(plan)
# read the plan's numbers without templates of its font (Arial), as with the fonts of a real scan
ocr._FONTS[:] = [f for f in ocr._FONTS if f != "arial"]
ocr.DigitTemplates._instance = None
gcp_text = "\n".join(f"{px} {py} {X:.4f} {Y:.4f}" for px, py, X, Y in gcp)


def run(params):
    base = {"INPUT": plan, "GCP_TEXT": gcp_text, "CRS": "EPSG:32638", "RASTER": os.path.join(TMP, "geo.tif"),
            "PARCELS": "TEMPORARY_OUTPUT", "LINES": "TEMPORARY_OUTPUT", "NUMBERS": "TEMPORARY_OUTPUT"}
    base.update(params)
    return processing.run("biruni:scantoparcels", base, context=ctx, feedback=fb)


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def map_poly(poly):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(*truth(px, py)) for px, py in poly + [poly[0]]]])


def compare(lyr, label):
    feats = list(lyr.getFeatures())
    check(len(feats) == len(expected), f"{label}: {len(feats)} parcels (expected {len(expected)})")
    worst, matched = 0.0, {}
    for poly, no, arabic in expected:
        g = map_poly(poly)
        c = g.centroid()
        f = min(feats, key=lambda f: f.geometry().centroid().distance(c))
        worst = max(worst, abs(f["area_m2"] - g.area()) / g.area())
        matched[no] = (f, arabic)
    check(worst < 0.02, f"{label}: every area within 2% (worst {100 * worst:.2f}%)")
    used = {f.id() for f, _a in matched.values()}
    for f in feats:
        if f.id() not in used:
            print(f"     extra: area {f['area_m2']} at {f.geometry().centroid().asWkt(1)} {f['review_note']}")
    return matched


res = run({})
lyr = out(res["PARCELS"])
check(res["TRANSFORMATION"] == "projective" and res["GCP_RMS"] < 1e-4, f"georeference rms {res['GCP_RMS']:.2e}")
matched = compare(lyr, "plan")
check(all(f["perimeter_m"] > 0 for f, _a in matched.values()), "perimeters measured")
trap = matched[expected[-2][1]][0]
check(abs(trap["area_m2"] - 100000 * S * S) / (100000 * S * S) < 0.02, f"split parcel area {trap['area_m2']}")

langs = ocr.available_languages()
print(f"     Windows OCR languages: {langs or 'none'}")
if "en-US" in langs or "ar-SA" in langs:
    latin = [(no, f) for no, (f, ar) in matched.items() if not ar]
    good = sum(1 for no, f in latin if f["parcel_no"] == no)
    check(good == len(latin), f"Latin parcel numbers read: {good}/{len(latin)} "
                              f"{[(no, f['parcel_no']) for no, f in latin if f['parcel_no'] != no]}")
    arab = [(no, f) for no, (f, ar) in matched.items() if ar]
    agood = sum(1 for no, f in arab if f["parcel_no"] == no)
    print(f"     Arabic-Indic parcel numbers read: {agood}/{len(arab)} "
          f"{[(no, f['parcel_no'], f['ocr_text']) for no, f in arab]}")
    check(all(f["needs_review"] for no, (f, _a) in matched.items() if f["parcel_no"] != no),
          "every parcel whose number was not read right is marked for review")
    check(not any("30" == f["parcel_no"] for f, _a in matched.values()), "edge lengths are not taken as numbers")
    nlyr = out(res["NUMBERS"])
    check(nlyr.featureCount() >= len(latin), f"numbers layer: {nlyr.featureCount()} numbers")
else:
    print("     (Windows OCR not available here: number checks skipped)")
    check(all(f["parcel_no"] in ("", None) for f, _a in matched.values()), "no OCR: parcel_no left empty")

lines = out(res["LINES"])
check(lines.featureCount() > 0 and abs(sum(f["length_m"] for f in lines.getFeatures()) -
                                       sum(f.geometry().length() for f in lines.getFeatures())) < 1,
      f"parcel lines: {lines.featureCount()} lines")

# the georeferenced plan: north-up GeoTIFF where the frame corners fall on their map coordinates
from osgeo import gdal  # noqa: E402

ds = gdal.Open(os.path.join(TMP, "geo.tif"))
check(ds is not None and "32638" in ds.GetSpatialRef().GetAuthorityCode(None), "GeoTIFF written in EPSG:32638")
gt = ds.GetGeoTransform()
check(gt[2] == 0 and gt[4] == 0, "north-up")
band = ds.GetRasterBand(1).ReadAsArray()
dark = 0
for px, py in corners[:4]:
    X, Y = truth(px, py)
    col, row = int((X - gt[0]) / gt[1]), int((Y - gt[3]) / gt[5])
    dark += int(band[row - 1:row + 2, col - 1:col + 2].min() < 100)
check(dark == 4, f"frame corners on their map coordinates ({dark}/4)")
ds = None

# 3 control points from a layer -> affine; squared corners
gl = QgsVectorLayer("Point?crs=EPSG:32638&field=pixel_x:double&field=pixel_y:double", "gcp", "memory")
feats = []
for px, py, X, Y in gcp[:3]:
    f = QgsFeature(gl.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X, Y)))
    f.setAttributes([px, py])
    feats.append(f)
gl.dataProvider().addFeatures(feats)
res = run({"GCP_TEXT": "", "GCP_LAYER": gl, "RASTER": None, "SQUARE": True, "OCR": 3, "NUMBERS": None,
           "LINES": None})
check(res["TRANSFORMATION"] == "affine", "control points layer: affine")
compare(out(res["PARCELS"]), "layer + squared")

# an unevenly lit scan
shaded = os.path.join(TMP, "shaded.png")
expected.clear()
draw_plan(shaded, shade=True)
res = run({"INPUT": shaded, "THRESHOLD": 1, "RASTER": None, "OCR": 3, "NUMBERS": None, "LINES": None})
compare(out(res["PARCELS"]), "uneven scan")

# too few points
try:
    run({"GCP_TEXT": gcp_text.splitlines()[0], "RASTER": None})
    check(False, "one control point refused")
except Exception as e:  # noqa: BLE001
    check("2" in str(e), f"one control point refused: {e}")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
