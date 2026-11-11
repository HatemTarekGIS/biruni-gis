"""Tests for the deed tools (parcel from deed text, deed against survey), the
Arabic name join and the Arabic text helpers.

Run:  engine\\bin\\python-qgis.bat tests\\test_office_deed_join.py
"""
import math
import os
import random
import sys
import tempfile
import time

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.stdout.reconfigure(encoding="utf-8")

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import arabic_text as at  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.deed_parcel import _quad, build_quad, max_area, parse_deed, polygon_area  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_deed_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


# ---- Arabic text helpers -------------------------------------------------------------------
check(at.normalise("الرَّوْضَة") == "الروضه", "normalise: tashkeel and ta marbuta")
check(at.normalise("أإآٱ") == "اااا", "normalise: alef forms")
check(at.normalise("مدينة ٦ أكتوبر") == "مدينه 6 اكتوبر", "normalise: Arabic-Indic digits")
check(at.normalise("عبد الله") == "عبدالله", "normalise: عبد ال joined")
check(at.normalise("حي الروضة", strip_prefixes=True) == "روضه", "normalise: prefixes stripped")
check(at.parse_number("٢٠٫٥") == 20.5 and at.parse_number("1,250.75") == 1250.75 and at.parse_number("20,5") == 20.5,
      "parse_number: Arabic decimal mark, thousands and decimal comma")
for text, want in [("20.50م", 20.5), ("٢٠٫٥ متر", 20.5), ("20 م و 50 سم", 20.5),
                   ("عشرون متراً وخمسون سنتيمتراً", 20.5), ("ثلاثون مترا ونصف", 30.5), ("خمسة عشر متراً", 15),
                   ("ثلاثمائة واثنا عشر مترا", 312), ("اربعة امتار وربع", 4.25), ("30m", 30)]:
    check(near(at.parse_length(text), want), f"parse_length {text!r} = {want}")
check(at.words_to_number("ستمائة وخمسة عشر") == 615 and at.words_to_number("ألف ومائتان وخمسون") == 1250,
      "number words to thousands")
for a, b in [("الروضه", "حي الروضة"), ("عبدالله", "عبد الله"), ("أبها", "ابها"), ("Riyadh", "RIYADH "),
             ("مؤسسة", "موسسه")]:
    check(at.similarity(a, b) >= 0.98, f"similar: {a} ~ {b}")
check(at.similarity("الملز", "المروج") < 0.5, "different names score low")
check(0.5 < at.similarity("العزيزية", "العزيزيه الجنوبية") < 0.85, "partial name scores in between")

# ---- deed text parser: Saudi and Egyptian wordings -----------------------------------------
DEEDS = [
    # (text, {dir: length}, area, {dir: kind}, street widths, parcel)
    ("شمالاً: شارع عرض 15م بطول 20.50م\nجنوباً: قطعة رقم 12 بطول 20.50م\nشرقاً: قطعة رقم 14 بطول 30م\n"
     "غرباً: ممر عرض 6م بطول 30م\nومساحتها 615 م²",
     {"N": 20.5, "S": 20.5, "E": 30, "W": 30}, 615, {"N": "street", "S": "parcel", "W": "street"}, {"N": 15, "W": 6},
     ""),
    ("قطعة الأرض رقم 237 من المخطط رقم 1/245 يحدها شمالاً شارع عرض ٢٠م بطول ٢٥٫٠٠م وجنوباً القطعة رقم ٢٣٨ "
     "بطول ٢٥٫٠٠م وشرقاً القطعة رقم ٢٣٩ بطول ٣٠٫٠٠م وغرباً شارع عرض ١٥م بطول ٣٠٫٠٠م ومساحتها ٧٥٠م٢ "
     "سبعمائة وخمسون متراً مربعاً",
     {"N": 25, "S": 25, "E": 30, "W": 30}, 750, {"N": "street", "S": "parcel", "E": "parcel"}, {"N": 20, "W": 15},
     "237"),
    ("الحد البحري: شارع عرض 8 متر بطول 12.40 متر\nالحد القبلي: ملك ورثة محمد علي بطول 12.40 متر\n"
     "الحد الشرقي: ملك أحمد حسن بطول 15 متر\nالحد الغربي: شارع عرض 6 متر بطول 15 متر\nوالمساحة 186 متر مربع",
     {"N": 12.4, "S": 12.4, "E": 15, "W": 15}, 186, {"N": "street", "S": "property", "E": "property"}, {"N": 8},
     ""),
    ("الحد الشمالي: شارع بطول عشرون متراً وخمسون سنتيمتراً، الحد الجنوبي: قطعة رقم 5 بطول عشرون متراً وخمسون "
     "سنتيمتراً، الحد الشرقي: قطعة رقم 7 وطوله ثلاثون متراً، الحد الغربي: قطعة رقم 3 وطوله ثلاثون متراً، "
     "ومساحتها ستمائة وخمسة عشر متراً مربعاً",
     {"N": 20.5, "S": 20.5, "E": 30, "W": 30}, 615, {"N": "street", "W": "parcel"}, {}, ""),
    ("شمالا: شارع عرض 30م بطول 25م ثم شطفة بطول 3م\nجنوبا: القطعة رقم 40 بطول 28م\nشرقا: شارع عرض 20م بطول 32م\n"
     "غربا: القطعة رقم 42 بطول 34.5م\nالمساحة: 880 م2",
     {"N": 28, "S": 28, "E": 32, "W": 34.5}, 880, {"E": "street"}, {"N": 30, "E": 20}, ""),
    ("يحدها من الشمال: سكة خصوصي عرض 4 م بطول 10.00 م / ومن الجنوب: ملك الغير بطول 10.00 م / "
     "ومن الشرق: منزل الحاج سيد بطول 9.50 م / ومن الغرب: شارع عرض 10 م بطول 9.50 م ومسطحها 95 م2",
     {"N": 10, "S": 10, "E": 9.5, "W": 9.5}, 95, {"N": "street", "S": "property", "E": "property"}, {"N": 4, "W": 10},
     ""),
    ("شمالاً بطول 20م ويحده شارع عرض 12م، جنوباً بطول 22م ويحده القطعة رقم 9، شرقاً بطول 30م ويحده القطعة رقم 10، "
     "غرباً بطول 31م ويحده ممر، بمساحة إجمالية قدرها 640.25 م2",
     {"N": 20, "S": 22, "E": 30, "W": 31}, 640.25, {"N": "street", "S": "parcel", "W": "street"}, {"N": 12}, ""),
    ("الحد البحري: أرض زراعية بطول 50 م\nالحد القبلي: مصرف بطول 50 م\nالحد الشرقي: طريق بطول 84 م\n"
     "الحد الغربي: ملك الغير بطول 84 م\nالمساحة 1 ف 0 ط 0 س",
     {"N": 50, "S": 50, "E": 84, "W": 84}, 4200.83, {"E": "street", "W": "property"}, {}, ""),
    ("من الشمال: الشارع التجاري عرض 36م وطوله 40.00م. من الجنوب: ارتداد بطول 40.00م. من الشرق: قطعة رقم 18 "
     "وطوله 50.00م. من الغرب: قطعة رقم 16 وطوله 50.00م. اجمالي المساحة 2000م2",
     {"N": 40, "S": 40, "E": 50, "W": 50}, 2000, {"N": "street", "E": "parcel"}, {"N": 36}, ""),
]
for k, (text, lengths, area, kinds, widths, parcel) in enumerate(DEEDS, 1):
    d = parse_deed(text)
    got = {s: d.sides[s].length for s in d.sides}
    check(d.complete() and all(near(got.get(s), v) for s, v in lengths.items()),
          f"deed {k}: four sides {got}")
    check(near(d.area, area, 0.05), f"deed {k}: area {d.area}")
    check(all(d.sides[s].kind == v for s, v in kinds.items()),
          f"deed {k}: neighbour types {[(s, d.sides[s].kind) for s in d.sides]}")
    check(all(near(d.sides[s].width, v) for s, v in widths.items()), f"deed {k}: street widths")
    if parcel:
        check(d.parcel == parcel, f"deed {k}: parcel number {d.parcel}")
d = parse_deed(DEEDS[0][0])
check(d.sides["S"].number == "12" and d.sides["N"].neighbour == "شارع عرض 15م", "neighbour text and parcel number")
check(parse_deed(DEEDS[4][0]).sides["N"].broken and parse_deed(DEEDS[4][0]).sides["N"].lengths == [25, 3],
      "chamfer: north side has two segments")
check(not parse_deed("شمالاً: شارع بطول 20م\nجنوباً: قطعة بطول 20م").complete(), "two sides only: incomplete")


# ---- geometry -------------------------------------------------------------------------------
def sides_of(pts):
    return [math.dist(pts[i], pts[(i + 1) % 4]) for i in range(4)]


pts, method, _ = build_quad(20.5, 30, 20.5, 30, 615)
check(method == "rectangle" and near(polygon_area(pts), 615, 1e-6), "equal opposite sides + matching area: rectangle")
for a, b, c, dd, area in [(22, 30, 20, 31, 630), (25, 32, 28, 34.5, 870), (20.5, 30, 20.5, 30, 560),
                          (12, 18, 15, 16, 220)]:
    pts, method, warn = build_quad(a, b, c, dd, area)
    check(method == "area" and near(polygon_area(pts), area, 0.01), f"quad {a},{b},{c},{dd}: area {area} solved "
                                                                     f"({polygon_area(pts):.4f})")
    check(all(near(x, y, 1e-6) for x, y in zip(sides_of(pts), (a, b, c, dd))), f"quad {a},{b},{c},{dd}: side lengths kept")
pts, method, warn = build_quad(20, 30, 22, 31, 900)
check(method == "cyclic" and warn and near(polygon_area(pts), max_area(20, 30, 22, 31), 0.05),
      "impossible area: largest shape and a warning")
pts, method, warn = build_quad(20, 30, 22, 31, None)
check(method == "cyclic" and warn, "no area: cyclic with a warning")
try:
    build_quad(1, 1, 1, 10, 5)
    check(False, "impossible lengths rejected")
except ValueError:
    check(True, "impossible lengths rejected")
check(_quad(10, 10, 10, 10, math.pi / 2) is not None, "square closes at 90°")

# ---- Parcel from deed text (Processing) ---------------------------------------------------
res = processing.run("biruni:deedtoparcel", {
    "TEXT": DEEDS[6][0].replace("640.25", "630"), "ANCHOR": "500000,2700000 [EPSG:32637]", "ANCHOR_AT": 0,
    "CRS": "EPSG:32637", "ROTATION": 15, "OUTPUT": "memory:", "SIDES": "memory:"}, context=ctx, feedback=fb)
poly, lines = out(res["OUTPUT"]), out(res["SIDES"])
pf = next(poly.getFeatures())
check(near(pf.geometry().area(), 630, 0.01) and near(pf["deed_area"], 630), "parcel area equals the deed area")
c = pf.geometry().vertices()
pts = [(v.x(), v.y()) for v in c][:4]
cx, cy = sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4
check(near(cx, 500000, 1e-6) and near(cy, 2700000, 1e-6), "anchor point is the corner average")
lens = {f["direction"]: f["length"] for f in lines.getFeatures()}
check(lines.featureCount() == 4 and all(near(f.geometry().length(), f["length"], 1e-6) for f in lines.getFeatures()),
      f"four side lines with their lengths {lens}")
south = [f for f in lines.getFeatures() if f["length"] == 22][0]
p0, p1 = south.geometry().asPolyline()
az = math.degrees(math.atan2(p1.x() - p0.x(), p1.y() - p0.y())) % 360
check(near(az, 105, 1e-6), f"turned 15° clockwise (south side azimuth {az:.3f})")
check(res["METHOD"] == "area", "method recorded")
res = processing.run("biruni:deedtoparcel", {"TEXT": DEEDS[6][0], "CRS": "EPSG:32637", "OUTPUT": "memory:",
                                              "SIDES": "memory:"}, context=ctx, feedback=fb)
pf = next(out(res["OUTPUT"]).getFeatures())
check(res["METHOD"] == "cyclic" and "larger than any parcel" in pf["warnings"] and pf["area_diff"] < 0,
      f"deed area impossible for its sides is reported ({pf['area_diff']:.2f} m²)")
res = processing.run("biruni:deedtoparcel", {
    "TEXT": DEEDS[0][0], "ANCHOR": "46.7,24.7 [EPSG:4326]", "ANCHOR_AT": 1, "CRS": "EPSG:4326",
    "OUTPUT": "memory:", "SIDES": "memory:"}, context=ctx, feedback=fb)
poly = out(res["OUTPUT"])
check(poly.crs().authid() == "EPSG:32638" and near(next(poly.getFeatures()).geometry().area(), 615, 1e-6),
      "degrees CRS + point: drawn in the point's UTM zone")
try:
    processing.run("biruni:deedtoparcel", {"TEXT": "شمالا شارع", "CRS": "EPSG:32637", "OUTPUT": "memory:",
                                            "SIDES": "memory:"}, context=ctx, feedback=fb)
    check(False, "unreadable text raises an error")
except Exception:
    check(True, "unreadable text raises an error")


# ---- Compare deed with survey --------------------------------------------------------------
def poly_layer(wkts, texts=None):
    lyr = QgsVectorLayer("Polygon?crs=EPSG:32637&field=deed:string", "survey", "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes([texts[i] if texts else None])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


DEED = DEEDS[0][0]  # 20.50 x 30, 615 m²
X, Y = 500000, 2700000
exact = f"POLYGON(({X} {Y},{X+20.5} {Y},{X+20.5} {Y+30},{X} {Y+30},{X} {Y}))"
within = f"POLYGON(({X} {Y},{X+20.55} {Y},{X+20.55} {Y+30.06},{X} {Y+30.06},{X} {Y}))"
wrong = f"POLYGON(({X} {Y},{X+21.4} {Y},{X+21.4} {Y+30},{X} {Y+30},{X} {Y}))"
extra_vertex = f"POLYGON(({X} {Y},{X+10} {Y},{X+20.5} {Y},{X+20.5} {Y+30},{X} {Y+30},{X} {Y}))"
rotated = QgsGeometry.fromWkt(exact)
rotated.rotate(20, QgsPointXY(X, Y))
srv = poly_layer([exact, within, wrong, extra_vertex, rotated.asWkt()])
report = os.path.join(TMP, "deed_report.xlsx")
res = processing.run("biruni:deedvssurvey", {"INPUT": srv, "TEXT": DEED, "SIDES": "memory:", "OUTPUT": "memory:",
                                             "REPORT": report}, context=ctx, feedback=fb)
parcels = {f["parcel_id"]: f for f in out(res["OUTPUT"]).getFeatures()}
verdicts = [parcels[i]["verdict"] for i in sorted(parcels)]
check(verdicts == ["Matches", "Within tolerance", "Does not match", "Matches", "Matches"], f"verdicts {verdicts}")
check(near(parcels[sorted(parcels)[2]]["area_diff"], 21.4 * 30 - 615, 0.01), "area difference reported")
sides = list(out(res["SIDES"]).getFeatures())
check(len(sides) == 20, f"four sides per parcel ({len(sides)})")
bad = [f for f in sides if f["parcel_id"] == sorted(parcels)[2] and not f["within_tolerance"]]
check(sorted(f["direction"] for f in bad) == ["North", "South"], "the wrong sides are the north and south ones")
check(os.path.exists(report) and os.path.getsize(report) > 3000, "Excel report written")
from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(report)
check(wb.sheetnames == ["Parcels", "Sides"] and wb["Sides"].max_row == 21, "report sheets and rows")

# chamfered survey against the chamfer deed (north 25 + 3 cut, south 28, east 32, west 34.5)
cham_deed = DEEDS[4][0]
pts, _, _ = build_quad(28, 32, 28, 34.5, 880)
# put a 3 m-ish corner cut at the north-east corner of the solved shape is complex; use the drawn tool instead
res = processing.run("biruni:deedtoparcel", {"TEXT": cham_deed, "ANCHOR": f"{X},{Y} [EPSG:32637]",
                                              "CRS": "EPSG:32637", "OUTPUT": "memory:", "SIDES": "memory:"},
                     context=ctx, feedback=fb)
drawn = next(out(res["OUTPUT"]).getFeatures()).geometry()
res = processing.run("biruni:deedvssurvey", {"INPUT": poly_layer([drawn.asWkt()]), "TEXT": cham_deed,
                                             "SIDES": "memory:", "OUTPUT": "memory:"}, context=ctx, feedback=fb)
check(next(out(res["OUTPUT"]).getFeatures())["verdict"] == "Matches", "drawn deed parcel matches its own deed")
# a real corner cut: square 30 x 30 with a 3 x 3 cut at the north-east corner
cut = f"POLYGON(({X} {Y},{X+30} {Y},{X+30} {Y+27},{X+27} {Y+30},{X} {Y+30},{X} {Y}))"
cut_deed = ("شمالاً: شارع بطول 27م وشطفة بطول 4.24م\nشرقاً: شارع بطول 27م\nجنوباً: قطعة رقم 1 بطول 30م\n"
            "غرباً: قطعة رقم 2 بطول 30م\nالمساحة 895.5 م2")
res = processing.run("biruni:deedvssurvey", {"INPUT": poly_layer([cut]), "TEXT": cut_deed, "SIDES": "memory:",
                                             "OUTPUT": "memory:"}, context=ctx, feedback=fb)
pf = next(out(res["OUTPUT"]).getFeatures())
check(pf["verdict"] == "Matches", f"corner cut goes to the side the deed names ({pf['verdict']}, {pf['sides_ok']})")
# deed text per feature from a field
res = processing.run("biruni:deedvssurvey", {"INPUT": poly_layer([exact, wrong], [DEED, ""]), "TEXT": DEED,
                                             "TEXT_FIELD": "deed", "SIDES": "memory:", "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
check(res["MATCH"] == 1 and res["FAIL"] == 1, "deed text from a field (empty falls back to the pasted text)")
res = processing.run("biruni:deedvssurvey", {"INPUT": poly_layer([exact]), "TEXT": "نص بلا حدود",
                                             "SIDES": "memory:", "OUTPUT": "memory:"}, context=ctx, feedback=fb)
check(res["UNREAD"] == 1, "unreadable deed reported, not guessed")


# ---- Join by similar Arabic names -------------------------------------------------------------
def table(name, values, extra=None):
    flds = "field=name:string" + ("&field=pop:integer" if extra else "")
    lyr = QgsVectorLayer(f"None?{flds}", name, "memory")
    feats = []
    for i, v in enumerate(values):
        f = QgsFeature(lyr.fields())
        f.setAttributes([v, extra[i]] if extra else [v])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def points(values):
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=hay:string", "districts", "memory")
    feats = []
    for i, v in enumerate(values):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(46 + i * 0.01, 24)))
        f.setAttributes([v])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


target = points(["الروضة", "حي الروضه", "عبدالله", "مدينة ٦ أكتوبر", "Al Malaz", "النسيم الشرقي", "العزيزيه",
                 "الصفا", None, "المنصورة"])
tbl = table("tbl", ["الروضة", "عبد الله", "مدينه 6 اكتوبر", "AL MALAZ", "النسيم الشرقى", "العزيزية الجديدة",
                    "المروة", "الملز"], [10, 20, 30, 40, 50, 60, 70, 80])
res = processing.run("biruni:arabicjoin", {"INPUT": target, "FIELD": "hay", "JOIN": tbl, "JOIN_FIELD": "name",
                                           "THRESHOLD": 0.85, "OUTPUT": "memory:", "REVIEW": "memory:"},
                     context=ctx, feedback=fb)
j = {f["hay"]: f for f in out(res["OUTPUT"]).getFeatures()}
check(j["الروضة"]["match_type"] == "exact" and j["الروضة"]["pop"] == 10, "exact name")
check(j["حي الروضه"]["match_type"] == "normalised" and j["حي الروضه"]["pop"] == 10, "حي الروضه = الروضة")
check(j["عبدالله"]["pop"] == 20 and j["مدينة ٦ أكتوبر"]["pop"] == 30, "عبدالله / digits")
check(j["Al Malaz"]["match_type"] == "exact" and j["Al Malaz"]["pop"] == 40, "Latin names ignore case")
check(j["النسيم الشرقي"]["pop"] == 50, "ى / ي")
check(j["العزيزيه"]["match_type"] == "none" and j["الصفا"]["match_type"] == "none", "unrelated names stay unmatched")
check(out(res["OUTPUT"]).featureCount() == 10 and out(res["OUTPUT"]).crs().authid() == "EPSG:4326",
      "every feature kept with its geometry")
review = list(out(res["REVIEW"]).getFeatures())
check(any(r["value"] == "العزيزيه" and r["candidate_1"] == "العزيزية الجديدة" for r in review),
      "unmatched name listed with its best candidate")
res = processing.run("biruni:arabicjoin", {"INPUT": target, "FIELD": "hay", "JOIN": tbl, "JOIN_FIELD": "name",
                                           "THRESHOLD": 0.6, "OUTPUT": "memory:", "REVIEW": "memory:"},
                     context=ctx, feedback=fb)
j = {f["hay"]: f for f in out(res["OUTPUT"]).getFeatures()}
check(j["العزيزيه"]["match_type"] == "fuzzy" and j["العزيزيه"]["pop"] == 60, "lower threshold accepts the similar name")
check(any(r["value"] == "العزيزيه" and r["status"] == "doubtful" for r in out(res["REVIEW"]).getFeatures()),
      "low-score match flagged as doubtful")
res = processing.run("biruni:arabicjoin", {"INPUT": target, "FIELD": "hay", "JOIN": tbl, "JOIN_FIELD": "name",
                                           "ONE_TO_ONE": True, "FIELDS": ["pop"], "PREFIX": "t_",
                                           "OUTPUT": "memory:", "REVIEW": "memory:"}, context=ctx, feedback=fb)
lyr = out(res["OUTPUT"])
rows = {f["hay"]: f for f in lyr.getFeatures()}
check([f.name() for f in lyr.fields()] == ["hay", "t_pop", "matched_value", "match_score", "match_type"],
      "chosen fields copied with the prefix")
check(rows["الروضة"]["t_pop"] == 10 and rows["حي الروضه"]["match_type"] == "none",
      "one-to-one: the table row is used once (exact match wins)")

# speed: 10 000 x 10 000 names
random.seed(4)
letters = "ابتثجحخدذرزسشصضطظعغفقكلمنهوي"
names = ["".join(random.choice(letters) for _ in range(random.randint(4, 9))) + " " +
         "".join(random.choice(letters) for _ in range(random.randint(3, 7))) for _ in range(10000)]


def variant(i, n):
    if i % 4 == 0:
        return "حي " + n.replace("ه", "ة").replace("ا", "أ", 1)        # prefix, ta marbuta, hamza
    if i % 4 == 1:
        return n.replace(" ", "  ")[:2] + "َ" + n.replace(" ", "  ")[2:]  # tashkeel, double space
    if i % 4 == 2:
        k = len(n) // 3
        return n[:k] + n[k + 1:]                                        # one letter missing
    return n


variants = [variant(i, n) for i, n in enumerate(names)]
t0 = time.time()
res = processing.run("biruni:arabicjoin", {"INPUT": points(variants), "FIELD": "hay", "JOIN": table("big", names),
                                           "JOIN_FIELD": "name", "THRESHOLD": 0.8, "OUTPUT": "memory:", "REVIEW": "memory:"},
                     context=ctx, feedback=fb)
dt = time.time() - t0
matched = res["EXACT"] + res["NORMALISED"] + res["FUZZY"]
check(dt < 60, f"10 000 x 10 000 names in {dt:.1f} s")
check(matched >= 9800, f"10 000 x 10 000: {matched} matched (fuzzy {res['FUZZY']})")
check(res["FUZZY"] >= 2300, "one-letter slips found by similarity")

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
