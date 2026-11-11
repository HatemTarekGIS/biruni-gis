"""Tests for the engineering-office tools: bill of quantities and setback check.

Run:  engine\\bin\\python-qgis.bat tests\\test_office_quantities.py
"""
import csv
import datetime as dt
import os
import sys
import tempfile

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry,
                       QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.quantities import match_rate, norm, read_rates, to_date  # noqa: E402
from biruni.algorithms.setback_check import classify_sides, parcel_sides, read_rules, _Streets  # noqa: E402

provider = BiruniProvider()  # keep a Python reference, or its virtual methods die with the wrapper
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_office_")
X0, Y0 = 500000.0, 2400000.0   # UTM 37N, on the central meridian


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, rel=0.005):
    return a is not None and abs(a - b) <= rel * max(abs(b), 1)


def layer(uri, name, rows):
    """rows: [(wkt, [attrs])]"""
    lyr = QgsVectorLayer(uri, name, "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        if wkt:
            f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def line(x1, y1, x2, y2):
    return f"LINESTRING({X0 + x1} {Y0 + y1},{X0 + x2} {Y0 + y2})"


def rect(x1, y1, x2, y2):
    a, b, c, d = X0 + x1, Y0 + y1, X0 + x2, Y0 + y2
    return f"POLYGON(({a} {b},{c} {b},{c} {d},{a} {d},{a} {b}))"


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
ctx.setEllipsoid("EPSG:7030")
fb = QgsProcessingFeedback()

# ---- helpers ---------------------------------------------------------------------------
check(norm("الطبقة") == norm("الطبقه") == norm(" الطبقة "), "norm: taa marbuta / spaces")
check(norm(3.0) == "3" and norm("٣") == "3" and norm("A ") == "a", "norm: numbers, Arabic digits, case")
check(to_date("2026-01-10") == dt.date(2026, 1, 10) and to_date("10/01/2026") == dt.date(2026, 1, 10)
      and to_date(QDate(2026, 1, 10)) == dt.date(2026, 1, 10) and to_date("") is None, "to_date formats")

# ---- bill of quantities ---------------------------------------------------------------------
kerbs = layer("LineString?crs=EPSG:32637&field=type:string&field=zone:string&field=done:date", "kerbs", [
    (line(0, 0, 100, 0), ["A", "Z1", QDate(2026, 1, 10)]),
    (line(0, 10, 50, 10), ["A", "Z2", QDate(2026, 2, 5)]),
    (line(0, 20, 0, 50), ["B", "Z1", QDate(2026, 1, 20)]),
])
asphalt = layer("Polygon?crs=EPSG:32637&field=zone:string", "asphalt", [(rect(0, 100, 50, 120), ["Z1"])])
poles = layer("Point?crs=EPSG:32637&field=type:string&field=zone:string", "poles", [
    (f"POINT({X0 + i} {Y0})", ["P", "Z2"]) for i in range(3)])

rates_csv = write_csv(os.path.join(TMP, "rates.csv"), [
    ["الطبقة", "البند", "الوحدة", "السعر", "الوصف"],
    ["kerbs", "A", "م.ط", "50", "بردورة A"],
    ["", "B", "", "٤٠", "بردورة B"],
    ["asphalt", "", "م2", "30", "أسفلت"],
])
rates = read_rates(rates_csv)
check(len(rates) == 3 and rates[1]["rate"] == 40, f"rates read with Arabic headers and digits ({rates})")
check(match_rate(rates, "kerbs", "A")["rate"] == 50 and match_rate(rates, "other", "B")["rate"] == 40
      and match_rate(rates, "asphalt", "")["rate"] == 30 and match_rate(rates, "poles", "P") is None,
      "rate matching: layer+item, item alone, layer alone, none")

xlsx = os.path.join(TMP, "boq.xlsx")
res = processing.run("biruni:quantities", {
    "LAYERS": [kerbs, asphalt, poles], "ITEM_FIELD": "type", "GROUP_FIELDS": "zone", "DATE_FIELD": "",
    "RATES": rates_csv, "VAT": 15, "CURRENCY": "SAR", "XLSX": xlsx, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
expected = 150 * 50 + 30 * 40 + 1000 * 30
check(res["FEATURE_COUNT"] == 7 and res["ITEM_COUNT"] == 4, f"BOQ: 7 features, 4 items ({res['ITEM_COUNT']})")
check(near(res["TOTAL_AMOUNT"], expected), f"BOQ total {res['TOTAL_AMOUNT']} ≈ {expected}")
check(near(res["GRAND_TOTAL"], expected * 1.15) and near(res["VAT_AMOUNT"], expected * 0.15), "BOQ VAT 15 %")
check(res["UNPRICED_COUNT"] == 1, "poles have no rate")
table = out(res["OUTPUT"])
trows = {(f["layer"], f["item"]): f for f in table.getFeatures()}
check(table.featureCount() == 4 and near(trows[("kerbs", "A")]["quantity"], 150)
      and trows[("kerbs", "A")]["unit"] == "م.ط" and near(trows[("asphalt", "")]["quantity"], 1000)
      and trows[("poles", "P")]["quantity"] == 3 and trows[("poles", "P")]["rate"] is None,
      "quantities table: lengths, areas, counts, units from the rates")

import openpyxl  # noqa: E402

wb = openpyxl.load_workbook(xlsx)
check(wb.sheetnames[:2] == ["Bill of quantities", "Rates"] and "By zone" in wb.sheetnames and "Details" in
      wb.sheetnames, f"workbook sheets {wb.sheetnames}")
ws = wb["Bill of quantities"]
check(ws["H6"].value == "=F6*G6" and str(ws["G6"].value).startswith("='Rates'!D"), "amount and rate are formulas")
labels = {ws.cell(row=r, column=2).value: r for r in range(6, ws.max_row + 1)}
t = labels.get("Total")
check(t == 10 and ws.cell(row=t, column=8).value == "=SUM(H6:H9)", "total row sums the amounts")
check(ws.cell(row=t + 1, column=7).value == 0.15 and ws.cell(row=t + 1, column=8).value == f"=H{t}*G{t + 1}"
      and ws.cell(row=t + 2, column=8).value == f"=H{t}+H{t + 1}", "VAT and grand total rows")
rws = wb["Rates"]
check(rws["D2"].value == 50 and rws["A2"].value == "kerbs" and rws.max_row == 5, "rates sheet holds the prices")
gws = wb["By zone"]
check(gws["A4"].value == "Z1" and "SUMPRODUCT" in str(gws.cell(row=4, column=6).value), "zone pivot with amounts")
z1 = {gws.cell(row=1, column=c).value: gws.cell(row=4, column=c).value for c in range(2, 6)}
check(near(z1["kerbs - A"], 100) and near(z1["kerbs - B"], 30) and near(z1["asphalt"], 1000), f"zone Z1 row {z1}")
check(wb["Details"].max_row == 8, "one detail row per feature")
wb.close()

# period: January only (features without a date are left out)
res = processing.run("biruni:quantities", {
    "LAYERS": [kerbs], "ITEM_FIELD": "type", "DATE_FIELD": "done", "DATE_FROM": QDate(2026, 1, 1),
    "DATE_TO": QDate(2026, 1, 31), "RATES": rates_csv, "VAT": 0, "XLSX": os.path.join(TMP, "jan.xlsx")},
    context=ctx, feedback=fb)
check(res["FEATURE_COUNT"] == 2 and near(res["TOTAL_AMOUNT"], 100 * 50 + 30 * 40) and res["VAT_AMOUNT"] == 0,
      f"period filter: January kerbs only ({res['FEATURE_COUNT']}, {res['TOTAL_AMOUNT']})")
wb = openpyxl.load_workbook(os.path.join(TMP, "jan.xlsx"))
ws = wb.worksheets[0]
check(ws["A2"].value.startswith("Period: 2026-01-01") and not any(
    ws.cell(row=r, column=2).value == "VAT" for r in range(1, ws.max_row + 1)), "period noted, no VAT rows at 0 %")
wb.close()

# no rates file: the Rates sheet becomes a template with the items found
res = processing.run("biruni:quantities", {
    "LAYERS": [kerbs, poles], "ITEM_FIELD": "type", "XLSX": os.path.join(TMP, "template.xlsx")},
    context=ctx, feedback=fb)
wb = openpyxl.load_workbook(os.path.join(TMP, "template.xlsx"))
rws = wb["Rates"]
items = [(rws.cell(row=r, column=1).value, rws.cell(row=r, column=2).value, rws.cell(row=r, column=4).value)
         for r in range(2, rws.max_row + 1)]
check(items == [("kerbs", "A", None), ("kerbs", "B", None), ("poles", "P", None)] and res["TOTAL_AMOUNT"] == 0,
      f"rates template lists the items ({items})")
wb.close()
# ... and that template, filled in, is a valid rates sheet (Excel)
wb = openpyxl.load_workbook(os.path.join(TMP, "template.xlsx"))
wb["Rates"]["D2"] = 10
wb.move_sheet("Rates", offset=-1)
wb.save(os.path.join(TMP, "filled.xlsx"))
filled = read_rates(os.path.join(TMP, "filled.xlsx"))
check(len(filled) == 1 and filled[0]["layer"] == "kerbs" and filled[0]["rate"] == 10, "filled template reads back")

# quantity field instead of measuring + selected features only
kerbs.selectByIds([f.id() for f in kerbs.getFeatures() if f["type"] == "A"])
cnt = layer("NoGeometry?field=item:string&field=qty:double", "counts",
            [(None, ["X", 2.5]), (None, ["X", 4]), (None, ["X", 100])])
cnt.selectByIds([f.id() for f in cnt.getFeatures() if f["qty"] < 100])
res = processing.run("biruni:quantities", {
    "LAYERS": [kerbs, cnt], "ITEM_FIELD": "item", "QUANTITY_FIELD": "qty", "SELECTED": True,
    "XLSX": os.path.join(TMP, "qty.xlsx"), "OUTPUT": "memory:"}, context=ctx, feedback=fb)
rows = {(f["layer"], f["item"]): f["quantity"] for f in out(res["OUTPUT"]).getFeatures()}
check(rows.get(("counts", "X")) == 6.5 and near(rows.get(("kerbs", "")), 150) and len(rows) == 2,
      f"quantity field summed; a layer without it is measured (selected kerbs only) ({rows})")
kerbs.removeSelection()

# ---- setbacks ----------------------------------------------------------------------------
square = QgsGeometry.fromWkt(f"POLYGON(({X0} {Y0},{X0 + 10} {Y0},{X0 + 20} {Y0},{X0 + 20} {Y0 + 30},"
                             f"{X0} {Y0 + 30},{X0} {Y0}))")
sides = parcel_sides(square)
check(len(sides) == 4 and len(sides[0]) == 3, "parcel sides: a straight vertex is not a corner")
st = _Streets({1: QgsGeometry.fromWkt(line(-50, -8, 150, -8))})
kinds = classify_sides(sides, st, 10)
check(sorted(kinds) == ["front", "rear", "side", "side"] and kinds[0] == "front", f"sides classified {kinds}")
corner = classify_sides(sides, _Streets({1: QgsGeometry.fromWkt(line(-50, -8, 150, -8)),
                                          2: QgsGeometry.fromWkt(line(28, -50, 28, 100))}), 10)
check(corner.count("front") == 2 and "rear" not in corner, f"corner parcel: two fronts, no rear ({corner})")

parcels = layer("Polygon?crs=EPSG:32637&field=pno:string&field=use:string", "parcels", [
    (rect(0, 0, 20, 30), ["1", "سكني"]), (rect(30, 0, 50, 30), ["2", "تجاري"]),
    (rect(60, 0, 80, 30), ["3", "صناعي"]), (rect(90, 0, 110, 30), ["4", "سكني"])])
buildings = layer("Polygon?crs=EPSG:32637&field=floors:integer", "buildings", [
    (rect(2, 3, 18, 27), [3]),      # P1: front 3 < 4, coverage 64 % > 60, 3 floors > 2
    (rect(35, 5, 60, 25), [1]),     # P2: crosses the east line by 10 m
    (rect(62, 5, 78, 25), [1]),     # P3: compliant with the default rules
    (rect(200, 0, 210, 10), [1])])  # on no parcel
streets = layer("LineString?crs=EPSG:32637", "streets", [(line(-50, -8, 150, -8), [])])
rules_csv = write_csv(os.path.join(TMP, "rules.csv"), [
    ["الاستخدام", "الارتداد الأمامي", "جانبي", "خلفي", "نسبة البناء", "عدد الأدوار"],
    ["سكني", "4", "2", "2", "60", "2"],
    ["تجاري", "0", "0", "0", "0.8", "0"]])
rules = read_rules(rules_csv)
check(rules[norm("سكني")]["front"] == 4 and rules[norm("تجاري")]["coverage"] == 80, f"rules table read ({rules})")


def setbacks(**kw):
    params = {"PARCELS": parcels, "BUILDINGS": buildings, "STREETS": streets, "FLOORS_FIELD": "floors",
              "USE_FIELD": "use", "RULES": rules_csv, "VIOLATIONS": "memory:", "SUMMARY": "memory:"}
    params.update(kw)
    r = processing.run("biruni:setbackcheck", params, context=ctx, feedback=fb)
    return r, list(out(r["VIOLATIONS"]).getFeatures()), {f["pno"]: f for f in out(r["SUMMARY"]).getFeatures()}


res, viol, summ = setbacks()
by = {}
for v in viol:
    by.setdefault(v["rule"], []).append(v)
check(res["VIOLATION_COUNT"] == 5 and res["NONCOMPLIANT_COUNT"] == 2 and res["NO_PARCEL_COUNT"] == 1,
      f"5 violations on 2 parcels + 1 building on no parcel ({sorted((v['rule'], v['parcel_id']) for v in viol)})")
fv = by.get("Setback", [None])[0]
check(fv is not None and fv["side_type"] == "Front" and near(fv["required"], 4) and near(fv["actual"], 3)
      and near(fv["shortfall"], 1) and near(fv["area_m2"], 16), "front setback: 3 m of 4, 16 m² inside the band")
cv = by.get("Building coverage", [None])[0]
check(cv is not None and near(cv["actual"], 64) and near(cv["required"], 60), "coverage 64 % > 60 %")
fl = by.get("Floors", [None])[0]
check(fl is not None and fl["actual"] == 3 and fl["required"] == 2, "3 floors > 2")
ov = by.get("Outside the parcel", [None])[0]
check(ov is not None and near(ov["area_m2"], 200) and near(ov["shortfall"], 10), "10 m over the parcel line, 200 m²")
check("No parcel" in by and by["No parcel"][0]["parcel_id"] is None, "building on no parcel")
check(summ["1"]["status"] == "Non-compliant" and summ["2"]["status"] == "Non-compliant"
      and summ["3"]["status"] == "Compliant" and summ["4"]["status"] == "No buildings"
      and summ["3"]["rule_set"] == "Default" and summ["1"]["rule_set"] == "سكني", "parcel statuses and rule sets")
check(near(summ["1"]["coverage_pct"], 64) and near(summ["1"]["far"], 1.92) and summ["1"]["violations"] == 3,
      "parcel 1: coverage, FAR, violations")
check(viol[0].geometry().type() == 2 and out(res["SUMMARY"]).crs().authid() == "EPSG:32637", "polygon outputs")

# FAR limit from the parameters, no rules table, no streets: every side is a side (2 m) -> no setback breach
res, viol, summ = setbacks(STREETS=None, RULES=None, USE_FIELD=None, MAX_FAR=1.5, COVERAGE=60, MAX_FLOORS=0)
rules_hit = sorted((v["rule"], v["parcel_id"]) for v in viol)
p1 = summ["1"]["parcel_id"]
p2 = summ["2"]["parcel_id"]
setback_hits = [v for v in viol if v["rule"] == "Setback"]
check([r for r in rules_hit if r[0] != "Setback"] == [("Building coverage", p1), ("Floor area ratio", p1), ("No parcel", None),
                                                    ("Outside the parcel", p2)]
      and len(setback_hits) == 1 and setback_hits[0]["parcel_id"] == p2 and setback_hits[0]["side_type"] == "Lateral"
      and near(setback_hits[0]["actual"], 0.0) and near(setback_hits[0]["area_m2"], 40),
      f"no streets: every side 2 m (only the encroaching building breaches it); FAR 1.92 > 1.5 ({rules_hit})")

# street polygons that touch the parcels mark the fronts too
road = layer("Polygon?crs=EPSG:32637", "road", [(rect(-50, -12, 150, 0), [])])
res, viol, summ = setbacks(STREETS=road, FRONT_DISTANCE=1)
check(any(v["rule"] == "Setback" and v["side_type"] == "Front" for v in viol), "street polygon front")

# geographic layers are measured in their UTM zone and come back in degrees
wgs = QgsCoordinateReferenceSystem("EPSG:4326")


def to_wgs(lyr, uri):
    xf = QgsCoordinateTransform(lyr.crs(), wgs, QgsProject.instance())
    rows = []
    for f in lyr.getFeatures():
        g = QgsGeometry(f.geometry())
        g.transform(xf)
        rows.append((g.asWkt(12), f.attributes()))
    return layer(uri, lyr.name() + "_wgs", rows)


p_wgs = to_wgs(parcels, "Polygon?crs=EPSG:4326&field=pno:string&field=use:string")
b_wgs = to_wgs(buildings, "Polygon?crs=EPSG:4326&field=floors:integer")
res, viol, summ = setbacks(PARCELS=p_wgs, BUILDINGS=b_wgs)
fv = [v for v in viol if v["rule"] == "Setback"]
check(res["VIOLATION_COUNT"] == 5 and fv and near(fv[0]["actual"], 3, 0.01)
      and out(res["VIOLATIONS"]).crs().authid() == "EPSG:4326", "geographic input: same result, output in degrees")

print()
print(f"{len(failures)} failure(s)" if failures else "ALL PASS")
sys.exit(1 if failures else 0)
