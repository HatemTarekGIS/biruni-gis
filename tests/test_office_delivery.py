"""Standalone tests for the delivery spec, delivery check and layer comparison tools.

Run:  engine\\bin\\python-qgis.bat tests\\test_office_delivery.py
"""
import os
import sys
import tempfile
import time
from collections import Counter

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                       QgsVectorFileWriter, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.delivery_check import norm, parse_allowed, spec_kind, value_allowed, yes  # noqa: E402

provider = BiruniProvider()  # keep a Python reference, or its virtual methods die with the wrapper
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_office_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def memory(geom_type, crs, fields, rows, name="test"):
    """rows: [(wkt or None, [attributes])]"""
    lyr = QgsVectorLayer(f"{geom_type}?crs={crs}&{fields}", name, "memory")
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


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


def features(lyr):
    return list(lyr.getFeatures())


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()

for alg in ("biruni:deliveryspectemplate", "biruni:deliverycheck", "biruni:comparelayers"):
    check(QgsApplication.processingRegistry().algorithmById(alg) is not None, f"{alg} is registered")

# ---- helpers --------------------------------------------------------------------------------------------
check(norm("الطبقة") == norm("طبقه") and norm("Allowed values (A, B)") == norm("allowed_values"),
      "header normalisation (Arabic article, ta marbuta, hints in parentheses)")
check(all(yes(v) for v in ("نعم", "Yes", "y", "1", "✓", "TRUE")) and not any(yes(v) for v in ("لا", "No", "", None)),
      "yes / no words")
check(spec_kind("عدد صحيح") == "integer" and spec_kind("Date & time") == "datetime" and spec_kind("نص") == "text"
      and spec_kind("رقم") == "number" and spec_kind("Decimal") == "decimal", "type words")
r = parse_allowed("1-1000")
check(r == ("range", 1.0, 1000.0) and value_allowed(r, 1000) is None and value_allowed(r, 1001), "range 1-1000")
r = parse_allowed(">= 0")
check(value_allowed(r, 0) is None and value_allowed(r, -1) == "Value out of range", "range >= 0")
r = parse_allowed("Residential، Commercial; Mixed")
check(r == ("list", ["Residential", "Commercial", "Mixed"]) and value_allowed(r, "mixed") is None
      and value_allowed(r, "Farm") == "Value not allowed", "list with Arabic and Latin separators")
check(value_allowed(parse_allowed("1, 2, 3"), 2.0) is None, "numeric list compares numbers")

# ---- delivery data ------------------------------------------------------------------------------------------
BOWTIE = "POLYGON((0 0,100 100,100 0,0 100,0 0))"
parcels_mem = memory(
    "Polygon", "EPSG:32637",
    "field=parcel_no:integer&field=owner:string&field=use:string&field=area:double&field=code:string"
    "&field=survey_date:string",
    [(sq(0, 0), [1, "Ali", "Residential", 500.0, "A1", "2024-01-01"]),
     (sq(200, 0), [2, "", "Commercial", 600.0, "B2", "2024-01-02"]),
     (sq(400, 0), [3, "   ", "Farm", 700.0, "C3", "2024-01-03"]),
     (sq(600, 0), [2, "Omar", "Residential", 0.0, "D4", "2024-01-04"]),
     (sq(800, 0), [5, "Sara", "residential", 2000.0, "ABCDEFGHIJK", "2024-01-05"]),
     (BOWTIE, [6, "Huda", "Commercial", 300.0, "F6", "2024-01-06"]),
     (None, [7, "Nour", "Commercial", 300.0, "G7", "2024-01-07"])])
gpkg = os.path.join(TMP, "delivery.gpkg")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName, opts.layerName = "GPKG", "parcels"
err = QgsVectorFileWriter.writeAsVectorFormatV3(parcels_mem, gpkg, ctx.transformContext(), opts)[0]
check(err == QgsVectorFileWriter.WriterError.NoError, "delivery GeoPackage written")
parcels = QgsVectorLayer(f"{gpkg}|layername=parcels", "My parcels (copy)", "ogr")
QgsProject.instance().addMapLayer(parcels)
check(parcels.featureCount() == 7, "delivery parcels loaded")
roads = memory("LineString", "EPSG:4326", "field=name:string",
               [("LINESTRING(46.7 24.7,46.71 24.7)", ["King Fahd"]), ("LINESTRING(46.7 24.71,46.71 24.71)", ["Olaya"])],
               name="roads")

# ---- tool 1: template ------------------------------------------------------------------------------------
from openpyxl import Workbook, load_workbook  # noqa: E402

tpl = os.path.join(TMP, "spec_template.xlsx")
res = processing.run("biruni:deliveryspectemplate", {"LAYERS": [parcels, roads], "OUTPUT": tpl}, context=ctx,
                     feedback=fb)
check(os.path.exists(tpl) and res["LAYER_COUNT"] == 2 and res["FIELD_COUNT"] == 7,
      f"template written ({res['LAYER_COUNT']} layers, {res['FIELD_COUNT']} fields; the gpkg fid is skipped)")
wb = load_workbook(tpl)
check(wb.sheetnames == ["Layers", "Fields", "Readme"], f"template sheets {wb.sheetnames}")
lrows = list(wb["Layers"].iter_rows(values_only=True))
check(lrows[1][:5] == ("My parcels (copy)", "Polygon", "Yes", None, "EPSG:32637")
      and lrows[2][1] == "Line" and lrows[2][4] == "EPSG:4326", f"template layer rows {lrows[1:]}")
frows = {(r[0], r[1]): r for r in wb["Fields"].iter_rows(min_row=2, values_only=True)}
check(frows[("My parcels (copy)", "owner")][2] == "No" and frows[("My parcels (copy)", "parcel_no")][2] == "Yes",
      "required is prefilled from the data (owner has empty values)")
check(frows[("My parcels (copy)", "parcel_no")][3] == "Integer" and frows[("My parcels (copy)", "area")][3] == "Decimal"
      and frows[("My parcels (copy)", "owner")][3] == "Text", "types are prefilled")
use_note = frows[("My parcels (copy)", "use")][8] or ""
check("Commercial" in use_note and "Farm" in use_note, f"current values as a hint: {use_note}")
check(wb["Readme"]["A1"].value == "How to fill this delivery spec"
      and any("كراسة" in str(c.value) for c in wb["Readme"]["A"]), "bilingual readme sheet")
check(len(wb["Fields"].data_validations.dataValidation) >= 2, "yes/no and type drop-downs")

# the template checks its own layers: only the geometry problems are left
res = processing.run("biruni:deliverycheck", {"SPEC": tpl, "LAYERS": [parcels, roads], "OUTPUT": "memory:",
                                              "REPORT": os.path.join(TMP, "self_report.xlsx")},
                     context=ctx, feedback=fb)
# (a GeoPackage hands a missing geometry back as an empty one)
rules = Counter(f["rule"].replace("Empty", "No") for f in features(out(res["OUTPUT"])))
check(res["PROBLEM_COUNT"] == 2 and rules == Counter({"Invalid geometry": 1, "No geometry": 1}),
      f"layers pass their own template except the bad geometries: {dict(rules)}")

# the same round trip with the Arabic interface: Arabic sheets, headers, Yes/No and type words
import json  # noqa: E402

from biruni import i18n  # noqa: E402

with open(os.path.join(HERE, "..", "app", "plugins", "biruni", "i18n", "ar.json"), encoding="utf-8") as fh:
    i18n._strings = json.load(fh)
i18n._lang = "ar"
tpl_ar = os.path.join(TMP, "spec_template_ar.xlsx")
processing.run("biruni:deliveryspectemplate", {"LAYERS": [parcels, roads], "OUTPUT": tpl_ar}, context=ctx,
               feedback=fb)
res = processing.run("biruni:deliverycheck", {"SPEC": tpl_ar, "LAYERS": [parcels, roads], "OUTPUT": "memory:",
                                              "REPORT": ""}, context=ctx, feedback=fb)
check(res["PROBLEM_COUNT"] == 2 and load_workbook(tpl_ar).worksheets[0].sheet_view.rightToLeft,
      f"Arabic template (right to left) reads back the same ({res['PROBLEM_COUNT']} problems)")
i18n._strings, i18n._lang = {}, "en"

# ---- tool 2: a hand-made spec with Arabic headers -----------------------------------------------------------
spec = os.path.join(TMP, "spec_ar.xlsx")
wb = Workbook()
ws = wb.active
ws.title = "الحقول"
ws.append(["كراسة شروط التسليم - مشروع تجريبي"])  # a title row above the headers
ws.append([])
ws.append(["الطبقة", "الحقل", "مطلوب", "النوع", "القيم المسموحة", "فريد", "أقصى طول", "الصفر يعتبر فارغ",
           "ملاحظات"])
ws.append(["PARCELS", "parcel_no", "✓", "عدد صحيح", None, "نعم", None, None, None])
ws.append(["PARCELS", "owner", "نعم", "نص", None, None, None, None, None])
ws.append(["PARCELS", "use", "لا", "نص", "Residential، Commercial", None, None, None, None])
ws.append(["PARCELS", "area", "Y", "رقم", "1-1000", None, None, "نعم", None])
ws.append(["PARCELS", "code", None, None, None, None, 5, None, None])
ws.append(["PARCELS", "district", "نعم", "نص", None, None, None, None, None])
ws.append(["PARCELS", "survey_date", None, "تاريخ", None, None, None, None, None])
ws2 = wb.create_sheet("الطبقات")
ws2.append(["الطبقة", "الشكل الهندسي", "مطلوب", "أقل عدد عناصر", "نظام الإحداثيات"])
ws2.append(["parcels", "مضلع", "نعم", 3, "EPSG:32637"])
ws2.append(["Roads", "مضلع", "نعم", 5, "32637"])
ws2.append(["wells", "نقطة", "نعم", None, None])
ws2.append(["optional layer", "نقطة", "لا", None, None])
wb.save(spec)

report = os.path.join(TMP, "check_report.xlsx")
res = processing.run("biruni:deliverycheck", {"SPEC": spec, "LAYERS": [parcels, roads], "OUTPUT": "memory:",
                                              "REPORT": report}, context=ctx, feedback=fb)
probs = features(out(res["OUTPUT"]))
rules = Counter(f["rule"].replace("Empty", "No") for f in probs)
expected = Counter({"Required value missing": 3, "Value not allowed": 1, "Value out of range": 1,
                    "Duplicate value": 2, "Text too long": 1, "Invalid geometry": 1, "No geometry": 1,
                    "Missing field": 1, "Wrong field type": 1, "Wrong geometry type": 1,
                    "Wrong coordinate system": 1, "Too few features": 1, "Missing layer": 1})
check(rules == expected, f"every rule found once as expected: {dict(rules)}")
check(res["PROBLEM_COUNT"] == 16 and res["LAYER_PROBLEMS"] == 6, f"totals {res['PROBLEM_COUNT']} / "
                                                                  f"{res['LAYER_PROBLEMS']}")
check(res["READINESS"] == 33.3, f"readiness = clean features / all features ({res['READINESS']}%)")


def problem(rule, fid=None, field=None):
    return [f for f in probs if f["rule"] == rule and (fid is None or f["fid"] == fid)
            and (field is None or f["field"] == field)]


owner_missing = sorted(f["fid"] for f in problem("Required value missing", field="owner"))
check(owner_missing == [2, 3], f"empty and blank owner are missing values ({owner_missing})")
check(len(problem("Required value missing", fid=4, field="area")) == 1, "zero counts as empty when the spec says so")
check(sorted(f["fid"] for f in problem("Duplicate value")) == [2, 4], "duplicate parcel numbers on both features")
check(problem("Value not allowed")[0]["value"] == "Farm", "Farm is not an allowed use; 'residential' is")
check(problem("Value out of range")[0]["value"] == "2000.0", "area 2000 is out of 1-1000")
check(problem("Text too long")[0]["fid"] == 5, "code longer than 5 characters")
check(problem("Missing layer")[0]["layer"] == "wells", "the required wells layer is missing; the optional one is not")
check(problem("Wrong field type")[0]["field"] == "survey_date", "text field where a date is required")
with_point = [f for f in probs if f.hasGeometry()]
check(len(with_point) == 9, f"feature problems have points, layer problems do not ({len(with_point)})")
pt = problem("Text too long")[0].geometry().asPoint()
check(800 < pt.x() < 900 and 0 < pt.y() < 100, "the point is on the feature")
check(out(res["OUTPUT"]).crs().authid() == "EPSG:32637", "problems in the first layer's CRS")

wb = load_workbook(report)
summary = [[c for c in row] for row in wb.worksheets[0].iter_rows(values_only=True)]
check(summary[1][1] == "33.3%" and summary[2][1] == 16, f"report summary {summary[1][:2]} {summary[2][:2]}")
lay = {row[0]: row for row in summary if row and row[0] in ("PARCELS", "Roads", "wells", "optional layer")}
check(lay["PARCELS"][4] == "14.3%" and lay["Roads"][4] == "100.0%" and lay["wells"][1] == "Missing",
      f"readiness per layer {[(k, v[1], v[4]) for k, v in lay.items()]}")
check(wb.worksheets[1].max_row == 17, f"detail sheet has every problem ({wb.worksheets[1].max_row - 1})")

# ---- tool 3: compare two versions ---------------------------------------------------------------------------
FIELDS = "field=id:integer&field=name:string&field=h:double"


def moved_sq(x, y, dx=0.0, dy=0.0):
    return sq(x + dx, y + dy)


old_rows = [(sq(0, 0), [1, "A", 1.0]), (sq(200, 0), [2, "B", 2.0]), (sq(400, 0), [3, "C", 3.0]),
            (sq(600, 0), [4, "D", 4.0]), (sq(800, 0), [5, "E", 5.0]), (sq(1200, 0), [7, "G", 7.0]),
            (sq(1400, 0), [8, "H", 8.0])]
new_rows = [(sq(0, 0), [1, "A", 1.0]),
            (moved_sq(200, 0, 3), [2, "B", 2.0]),                                       # moved 3 m
            ("POLYGON((400 0,500 0,502 100,400 100,400 0))", [3, "C", 3.0]),           # reshaped
            (sq(600, 0), [4, "DD", 4.0]),                                               # attributes
            (sq(1000, 0), [6, "F", 6.0]),                                               # added (5 deleted)
            (moved_sq(1200, 0, 0.01), [7, "G", 7.0]),                                   # below tolerance
            (moved_sq(1400, 0, 0, 2), [8, "H", 8.5])]                                   # moved + attributes
old = memory("Polygon", "EPSG:32637", FIELDS, old_rows, "old")
new = memory("Polygon", "EPSG:32637", FIELDS, new_rows, "new")


def compare(o, n, **kw):
    params = {"OLD": o, "NEW": n, "KEY": "", "TOLERANCE": 0.05, "SEARCH": 5.0, "IGNORE": [], "UNCHANGED": True,
              "OUTPUT": "memory:", "REPORT": os.path.join(TMP, "compare.xlsx")}
    params.update(kw)
    r = processing.run("biruni:comparelayers", params, context=ctx, feedback=fb)
    return r, {f["new_fid"] if f["new_fid"] is not None else -f["old_fid"]: f for f in features(out(r["OUTPUT"]))}


for mode, key in (("key", "id"), ("geometry", "")):
    res, rows = compare(old, new, KEY=key)
    by_id = {}
    for f in rows.values():
        fid = f["new_fid"] if f["new_fid"] is not None else f["old_fid"]
        src = new if f["new_fid"] is not None else old
        by_id[src.getFeature(fid)["id"]] = f
    kinds = {i: by_id[i]["change_type"] for i in sorted(by_id)}
    check(kinds == {1: "unchanged", 2: "moved", 3: "reshaped", 4: "attributes", 5: "deleted", 6: "added",
                    7: "unchanged", 8: "moved"}, f"[{mode}] change types {kinds}")
    check(abs(by_id[2]["moved_m"] - 3.0) < 1e-6 and abs(by_id[8]["moved_m"] - 2.0) < 1e-6,
          f"[{mode}] moved distances {by_id[2]['moved_m']} / {by_id[8]['moved_m']}")
    check(by_id[8]["changed_fields"] == "h" and "8.0 → 8.5" in by_id[8]["details"], f"[{mode}] moved and edited")
    check(by_id[4]["changed_fields"] == "name" and "D → DD" in by_id[4]["details"], f"[{mode}] name D → DD")
    check(abs(by_id[3]["area_delta"] - 100.0) < 1e-6, f"[{mode}] reshape area change {by_id[3]['area_delta']} m²")
    check(res["CHANGED"] == 4 and res["ADDED"] == 1 and res["DELETED"] == 1 and res["UNCHANGED"] == 2
          and res["ATTRIBUTE_CHANGES"] == 2, f"[{mode}] totals: {res['SUMMARY']}")
    if key:
        check(by_id[2]["key"] == "2", "the key is in the output")

check(res["SUMMARY"].startswith("4 changed (2 moved more than 0.05 m, 1 reshaped, 2 with changed attributes), "
                                "1 added, 1 deleted, 2 unchanged"), f"summary sentence: {res['SUMMARY']}")
wb = load_workbook(res["REPORT"])
check(wb.sheetnames == ["Summary", "Top moves", "Changes"], f"compare report sheets {wb.sheetnames}")
check(wb["Summary"]["A2"].value == res["SUMMARY"] and wb["Changes"].max_row == 7,
      f"report sentence and change rows ({wb['Changes'].max_row - 1})")
check(wb["Top moves"]["E2"].value == 3.0, "top moves sorted by distance")

res, rows = compare(old, new, KEY="id", IGNORE=["name"], UNCHANGED=False)
check(res["UNCHANGED"] == 3 and res["ATTRIBUTES"] == 0 and len(rows) == 5,
      f"ignored fields are not compared; unchanged features left out of the layer ({len(rows)} rows)")

# a field only in one version, auto fields ignored
new2 = memory("Polygon", "EPSG:32637", FIELDS + "&field=Shape_Area:double&field=extra:string",
              [(w, a + [123.0, "x"]) for w, a in old_rows], "new2")
res, rows = compare(old, new2, KEY="id")
check(res["UNCHANGED"] == 7 and res["CHANGED"] == 0, "a new field and Shape_Area are not attribute changes")
wb = load_workbook(res["REPORT"])
cells = [c.value for row in wb["Summary"].iter_rows() for c in row]
check("extra" in cells and "Field added" in cells and "Shape_Area" not in cells, "added field listed in the report")

# the same data in another CRS is unchanged; distances are metres in geographic layers
xf = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:32637"), QgsCoordinateReferenceSystem("EPSG:4326"),
                            QgsProject.instance())
geo_rows = []
for w, a in old_rows:
    g = QgsGeometry.fromWkt(w)
    g.translate(500000, 2700000)
    geo_rows.append((g, a))
old_utm = memory("Polygon", "EPSG:32637", FIELDS, [(g.asWkt(), a) for g, a in geo_rows], "old_utm")
wgs = []
for g, a in geo_rows:
    g2 = QgsGeometry(g)
    g2.transform(xf)
    wgs.append((g2.asWkt(17), a))
new_wgs = memory("Polygon", "EPSG:4326", FIELDS, wgs, "new_wgs")
res, rows = compare(old_utm, new_wgs, KEY="")
check(res["UNCHANGED"] == 7, f"reprojected copy is unchanged ({res['SUMMARY']})")

p0 = QgsGeometry.fromPointXY(QgsPointXY(500000, 2700000))
p1 = QgsGeometry.fromPointXY(QgsPointXY(500001, 2700000))
for g in (p0, p1):
    g.transform(xf)
pa = memory("Point", "EPSG:4326", "field=id:integer", [(p0.asWkt(17), [1])], "pa")
pb = memory("Point", "EPSG:4326", "field=id:integer", [(p1.asWkt(17), [1])], "pb")
res, rows = compare(pa, pb, KEY="id")
f = list(rows.values())[0]
check(f["change_type"] == "moved" and abs(f["moved_m"] - 1.0) < 0.01,
      f"a point moved 1 m in a geographic layer: {f['moved_m']} m")

la = memory("LineString", "EPSG:32637", "field=id:integer", [("LINESTRING(0 0,100 0)", [1])], "la")
lb = memory("LineString", "EPSG:32637", "field=id:integer", [("LINESTRING(0 0,110 0)", [1])], "lb")
res, rows = compare(la, lb, KEY="id")
f = list(rows.values())[0]
check(f["change_type"] == "reshaped" and abs(f["length_delta"] - 10.0) < 1e-6, "line extended by 10 m")

# tables need a key
ta = memory("None", "", "field=id:integer&field=v:string", [(None, [1, "a"]), (None, [2, "b"])], "ta")
tb = memory("None", "", "field=id:integer&field=v:string", [(None, [1, "a"]), (None, [2, "c"])], "tb")
res, rows = compare(ta, tb, KEY="id")
check(res["ATTRIBUTES"] == 1 and res["UNCHANGED"] == 1, "tables compared by key")
try:
    compare(ta, tb, KEY="")
    check(False, "a table without a key is refused")
except Exception as e:
    check("key" in str(e), "a table without a key is refused")

# ---- 50,000 points, matched by geometry ---------------------------------------------------------------------
N = 50000
big_old = QgsVectorLayer("Point?crs=EPSG:32637&field=id:integer&field=v:string", "big_old", "memory")
big_new = QgsVectorLayer("Point?crs=EPSG:32637&field=id:integer&field=v:string", "big_new", "memory")
fo, fn = [], []
for i in range(N):
    x, y = (i % 250) * 20.0, (i // 250) * 20.0
    a = QgsFeature(big_old.fields())
    a.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    a.setAttributes([i, "x"])
    fo.append(a)
    b = QgsFeature(big_new.fields())
    b.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x + (1.0 if i % 100 == 0 else 0.0), y)))
    b.setAttributes([i, "y" if i % 250 == 1 else "x"])
    fn.append(b)
big_old.dataProvider().addFeatures(fo)
big_new.dataProvider().addFeatures(fn)
t0 = time.time()
res, rows = compare(big_old, big_new, KEY="", UNCHANGED=False, REPORT=os.path.join(TMP, "big.xlsx"))
secs = time.time() - t0
check(res["MOVED"] == 500 and res["ATTRIBUTES"] == 200 and res["ADDED"] == 0 and res["DELETED"] == 0,
      f"50,000 points: {res['SUMMARY']}")
check(secs < 120, f"50,000 points compared in {secs:.1f} s")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
