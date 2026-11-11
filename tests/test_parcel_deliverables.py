"""Parcel deliverables: the boundaries text of a parcel (reverse of «Parcel from deed text») and the
Ihkam / Balady bundle (Shapefile, DXF sketch, KML, PDF report, ZIP).

Run:  engine\\bin\\python-qgis.bat tests\\test_parcel_deliverables.py
"""
import os
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsProcessingContext, QgsProcessingFeatureSourceDefinition, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.deed_parcel import build_quad, parse_deed  # noqa: E402
from biruni.algorithms.parcel_bundle import preflight, target_crs  # noqa: E402
from biruni.algorithms.parcel_description import (area_words, corner_table, describe_geometry,  # noqa: E402
                                                  number_words)
from biruni.arabic_text import words_to_number  # noqa: E402
from biruni.survey import dxf as dxfreader  # noqa: E402
from biruni.survey.report_pdf import ReportPage, render_png  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
TMP = tempfile.mkdtemp(prefix="biruni_parcels_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


X0, Y0 = 500000.0, 2380000.0          # Jeddah area, UTM 37N


def rect(x, y, w, h):
    return f"POLYGON(({x} {y},{x + w} {y},{x + w} {y + h},{x} {y + h},{x} {y}))"


def poly(pts):
    pts = list(pts) + [pts[0]]
    return "POLYGON((" + ",".join(f"{X0 + x} {Y0 + y}" for x, y in pts) + "))"


def layer(kind, fields, rows, name="t"):
    lyr = QgsVectorLayer(f"{kind}?crs=EPSG:32637&{fields}", name, "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


# ---- number words ---------------------------------------------------------------------------
samples = list(range(1, 125)) + [199, 200, 201, 300, 615, 999, 1000, 1001, 1500, 2000, 2500, 3000, 10000, 11000,
                                 12345, 99999, 100000, 250000, 999999]
bad = [n for n in samples if words_to_number(number_words(n)) != n]
check(not bad, f"Arabic number words read back to the same number ({len(samples)} numbers; wrong: {bad[:5]})")
check(number_words(615) == "ستمائة وخمسة عشر", f"615 = {number_words(615)}")
check(number_words(2000) == "ألفان" and number_words(3000) == "ثلاثة آلاف" and number_words(11000) == "أحد عشر ألفاً",
      "thousands: ألفان / ثلاثة آلاف / أحد عشر ألفاً")
check(area_words(615) == "فقط ستمائة وخمسة عشر متراً مربعاً لا غير", f"area words: {area_words(615)}")
check(area_words(300) == "فقط ثلاثمائة متر مربع لا غير" and "أمتار مربعة" in area_words(1005),
      "the counted noun follows the number (متر / أمتار / متراً)")
check("فاصلة خمسة وعشرون متراً مربعاً" in area_words(615.25) and "فاصلة صفر خمسة" in area_words(10.05),
      f"hundredths after «فاصلة»: {area_words(615.25)}")

# ---- corner table ---------------------------------------------------------------------------
ct = corner_table([(0, 0), (20, 0), (20, 30), (0, 30)])
check([c[0] for c in ct] == [1, 2, 3, 4] and ct[0][1:] == (0, 30) and ct[1][1:] == (20, 30) and ct[2][1:] == (20, 0),
      f"corners from the north-west, clockwise: {ct}")

# ---- the description against neighbours ------------------------------------------------------
parcels = layer("Polygon", "field=parcel_no:string&field=owner:string&field=deed_no:string&field=city:string"
                "&field=district:string&field=deed_area:double", [
                    (rect(X0, Y0, 20.5, 30), ["11", "عبدالله بن محمد", "4310123456", "جدة", "الروضة", 615.0]),
                    (rect(X0 + 20.5, Y0, 20, 30), ["12", "سعد", "4310999999", "جدة", "الروضة", 590.0]),
                ], "parcels")
streets = layer("Polygon", "field=name:string&field=width:double", [
    (rect(X0 - 50, Y0 + 30, 150, 15), ["", 15.0]),
    (rect(X0 - 10, Y0 - 40, 10, 70), ["الأمير سلطان", 10.0]),
], "streets")

res = processing.run("biruni:parceldescription", {
    "INPUT": parcels, "NUMBER": "parcel_no", "STREETS": streets, "STREET_NAME": "name", "STREET_WIDTH": "width",
    "DEFAULT": 0, "STYLE": 0, "TOLERANCE": 0.5, "SEARCH": 20, "OUTPUT": "memory:",
    "TEXT": os.path.join(TMP, "desc.txt"), "XLSX": os.path.join(TMP, "desc.xlsx")}, context=ctx, feedback=fb)
lyr = out(res["OUTPUT"])
feats = {f["parcel_no"]: f for f in lyr.getFeatures()}
p11 = feats["11"]
print("     " + p11["desc_ar"].replace("\n", "\n     "))
check("شمالاً: شارع عرض 15م بطول 20.50م" == p11["north"], f"north: {p11['north']}")
check(p11["south"] == "جنوباً: أرض فضاء بطول 20.50م", f"south (nothing next to it): {p11['south']}")
check(p11["east"] == "شرقاً: قطعة رقم 12 بطول 30.00م", f"east (the neighbour parcel): {p11['east']}")
check(p11["west"] == "غرباً: شارع الأمير سلطان عرض 10م بطول 30.00م", f"west (street name and width): {p11['west']}")
check(near(p11["area_m2"], 615.0) and p11["area_words"] == area_words(615), "area and area in words")
check("ستمائة وخمسة عشر" in p11["desc_ar"] and "615.00 م²" in p11["desc_ar"], "area line in the text")
check("North: street 15 m wide, length 20.50 m" in p11["desc_en"] and "parcel no. 11" in feats["12"]["desc_en"],
      "English description and the neighbour seen from the other side")
check(p11["corners"].startswith("1: 500000.000, 2380030.000"), f"corners text: {p11['corners'][:40]}")
check(os.path.getsize(os.path.join(TMP, "desc.txt")) > 200 and os.path.getsize(os.path.join(TMP, "desc.xlsx")) > 3000,
      "text and Excel files written")
from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(os.path.join(TMP, "desc.xlsx"))
check(wb.worksheets[0].max_row == 3 and wb.worksheets[1].max_row == 9, "Excel: 2 parcels, 8 corners")

# round trip through the deed reader
deed = parse_deed(p11["desc_ar"])
lens = {d: deed.sides[d].length for d in deed.sides}
check(near(lens.get("N"), 20.5) and near(lens.get("S"), 20.5) and near(lens.get("E"), 30) and near(lens.get("W"), 30),
      f"deed reader gets the same lengths back: {lens}")
check(near(deed.area, 615.0), f"deed reader gets the same area back: {deed.area}")
check(deed.sides["N"].kind == "street" and deed.sides["N"].width == 15 and deed.sides["E"].number == "12",
      "deed reader sees the street width and the neighbour parcel number")
r = processing.run("biruni:deedtoparcel", {"TEXT": p11["desc_ar"], "CRS": "EPSG:32637", "ROTATION": 0,
                                           "OUTPUT": "memory:", "SIDES": "memory:"}, context=ctx, feedback=fb)
check(near(r["GEOMETRIC_AREA"], 615.0) and r["METHOD"] == "rectangle", f"parcel redrawn from its own text: "
                                                                     f"{r['GEOMETRIC_AREA']:.3f} ({r['METHOD']})")

# a four-sided parcel that is not a rectangle
quad = QgsGeometry.fromWkt(poly([(0, 0), (25, 0), (22, 28), (-1, 30)]))
qd = describe_geometry(quad)
qdeed = parse_deed(qd.text_ar())
s = {d: qdeed.sides[d].length for d in "NESW"}
corners, method, _ = build_quad(s["S"], s["E"], s["N"], s["W"], qdeed.area)
from biruni.algorithms.deed_parcel import polygon_area  # noqa: E402

check(near(qdeed.area, round(quad.area(), 2)) and near(polygon_area(corners), qdeed.area) and method == "area",
      f"irregular parcel: text -> sides and area -> same area ({polygon_area(corners):.3f} vs {quad.area():.3f})")
check(all(near(s[d], qd.side_length(d), 0.006) for d in "NESW"), "irregular parcel: same side lengths")

# broken sides and a corner cut
step = describe_geometry(QgsGeometry.fromWkt(poly([(0, 0), (20, 0), (20, 15), (25, 15), (25, 30), (0, 30)])))
check("بطول 15.00م ثم 15.00م (منكسر)" in step.side_ar("E"), f"broken side: {step.side_ar('E')}")
check(near(parse_deed(step.text_ar()).sides["E"].length, 30.0), "broken side read back as its total length")
cut = describe_geometry(QgsGeometry.fromWkt(poly([(0, 0), (20, 0), (20, 26), (16, 30), (0, 30)])))
check("وشطفة بطول 5.66م" in cut.side_ar("E") or "وشطفة بطول 5.66م" in cut.side_ar("N"),
      f"corner cut written as شطفة: {cut.side_ar('E')} | {cut.side_ar('N')}")
cd = parse_deed(cut.text_ar())
check(near(sum(cd.sides[d].length for d in "NESW"), 20 + 26 + 5.66 + 16 + 30, 0.02), "corner cut read back")

res_eg = processing.run("biruni:parceldescription", {
    "INPUT": parcels, "NUMBER": "parcel_no", "STYLE": 1, "DEFAULT": 1, "OUTPUT": "memory:"}, context=ctx,
    feedback=fb)
eg = {f["parcel_no"]: f for f in out(res_eg["OUTPUT"]).getFeatures()}["11"]
check(eg["north"].startswith("بحري: ملك الغير") and eg["south"].startswith("قبلي:") and
      eg["east"].startswith("شرقي: قطعة رقم 12"), f"Egyptian wording: {eg['north']} | {eg['east']}")
check(near(parse_deed(eg["desc_ar"]).sides["S"].length, 20.5), "Egyptian wording read back")

# centre lines instead of street polygons
lines = layer("LineString", "field=name:string&field=width:double", [
    (f"LINESTRING({X0 - 30} {Y0 + 37.5},{X0 + 100} {Y0 + 37.5})", ["التحلية", 15.0])], "centrelines")
res_l = processing.run("biruni:parceldescription", {
    "INPUT": parcels, "NUMBER": "parcel_no", "STREETS": lines, "STREET_NAME": "name", "STREET_WIDTH": "width",
    "SEARCH": 15, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
pl = {f["parcel_no"]: f for f in out(res_l["OUTPUT"]).getFeatures()}["11"]
check(pl["north"] == "شمالاً: شارع التحلية عرض 15م بطول 20.50م" and pl["west"].startswith("غرباً: أرض فضاء"),
      f"street centre line found beyond the north side: {pl['north']}")

# ---- pre-flight checks --------------------------------------------------------------------------
utm = QgsCoordinateReferenceSystem("EPSG:32637")
e, w = preflight(QgsGeometry.fromWkt(rect(X0, Y0, 20, 30)), utm, 580.0, 1.0)
check(not e and any("differs from the deed area" in x for x in w), f"deed area 580 against 600 surveyed: {w}")
e, w = preflight(QgsGeometry.fromWkt(rect(X0, Y0, 20, 30)), utm, 605.0, 1.0)
check(not e and not w, "area within 1% of the deed: no warning")
e, _ = preflight(QgsGeometry.fromWkt(f"POLYGON(({X0} {Y0},{X0 + 20} {Y0 + 20},{X0 + 20} {Y0},{X0} {Y0 + 20},"
                                     f"{X0} {Y0}))"), utm)
check(any("crosses itself" in x for x in e), f"self-intersection is blocking: {e}")
e, _ = preflight(QgsGeometry.fromWkt(f"MULTIPOLYGON((({X0} {Y0},{X0 + 5} {Y0},{X0 + 5} {Y0 + 5},{X0} {Y0}))"
                                     f",(({X0 + 9} {Y0},{X0 + 14} {Y0},{X0 + 14} {Y0 + 5},{X0 + 9} {Y0})))"), utm)
check(any("separate parts" in x for x in e), "two parts are blocking")
e, _ = preflight(QgsGeometry.fromWkt(rect(39.1, 21.5, 0.001, 0.001)), QgsCoordinateReferenceSystem("EPSG:4326"))
check(any("degrees" in x for x in e), "a system in degrees is blocking")
e, w = preflight(QgsGeometry.fromWkt(rect(X0, Y0, 20, 30)), utm, None, 1.0, {"Owner": "ع" * 200})
check(not e and any("shortened" in x for x in w), "too long for a Shapefile: warning")
check(target_crs(None, utm, 39.2, 21.5).authid() == "EPSG:9357", "default system in Jeddah: KSA-GRF17 / UTM 37N")
check(target_crs(None, utm, 31.2, 30.0).authid() == "EPSG:32637", "outside Saudi Arabia: the layer's own system")

# ---- the bundle -----------------------------------------------------------------------------------
parcels.selectByIds([f.id() for f in parcels.getFeatures() if f["parcel_no"] == "11"])
bundle_dir = os.path.join(TMP, "تسليم")
res_b = processing.run("biruni:parcelbundle", {
    "INPUT": QgsProcessingFeatureSourceDefinition(parcels.id(), selectedFeaturesOnly=True),
    "NUMBER": "parcel_no", "OWNER": "owner", "DEED": "deed_no", "CITY": "city", "DISTRICT": "district",
    "DEED_AREA": "deed_area", "STREETS": streets, "STREET_NAME": "name", "STREET_WIDTH": "width",
    "OFFICE": "مكتب البيروني للمساحة", "ENGINEER": "م. حاتم", "NOTES": "الأطوال بالمتر والإحداثيات بنظام KSA-GRF17.",
    "DATE": "2026-10-28", "OUTPUT": bundle_dir}, context=ctx, feedback=fb)
folder = os.path.join(bundle_dir, "11")
names = sorted(os.listdir(folder))
print("     " + ", ".join(names))
check(res_b["PARCEL_COUNT"] == 1 and res_b["CRS"] == "EPSG:9357", f"one parcel in {res_b['CRS']}")
for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".dxf", ".kml", ".pdf", ".txt"):
    check(f"11{ext}" in names, f"bundle has 11{ext}")
check("11_points.shp" in names, "bundle has the corner points shapefile")
check(open(os.path.join(folder, "11.cpg")).read().strip().upper().replace("-", "") == "UTF8", ".cpg says UTF-8")
check("GRF17" in open(os.path.join(folder, "11.prj")).read().replace("-", "").replace("_", "").upper().replace(
    "KSAGRF17", "GRF17"), ".prj is KSA-GRF17")
shp = QgsVectorLayer(os.path.join(folder, "11.shp"), "s", "ogr")
sf = next(shp.getFeatures())
check(sf["OWNER"] == "عبدالله بن محمد" and sf["PARCEL_NO"] == "11" and sf["CITY"] == "جدة",
      f"Arabic attributes survive the Shapefile: {sf['OWNER']}")
check(near(sf["AREA_M2"], 615.0, 0.05) and near(shp.getFeature(sf.id()).geometry().area(), 615.0, 0.05),
      f"area kept in the new system: {sf['AREA_M2']}")
check(sf["NORTH"].startswith("شمالاً: شارع عرض 15م"), "side texts in the Shapefile")
sg = sf.geometry()
check((len(sg.asMultiPolygon()) if sg.isMultipart() else 1) == 1 and sg.isGeosValid(), "one closed valid polygon")
pts = QgsVectorLayer(os.path.join(folder, "11_points.shp"), "p", "ogr")
check(pts.featureCount() == 4 and sorted(f["PT_NO"] for f in pts.getFeatures()) == [1, 2, 3, 4], "4 corner points")

raw = open(os.path.join(folder, "11.dxf"), "rb").read()
check(b"AC1009" in raw and b"ANSI_1256" in raw and raw.rstrip().endswith(b"EOF"), "DXF R12 with the Arabic code page")
txt = raw.decode("cp1256")
check("شارع عرض 15م" in txt and "قطعة رقم 12" in txt and "جدول الإحداثيات" in txt and b"?" not in raw,
      "DXF Arabic text intact in Windows-1256 (no character lost)")
drawing = dxfreader.parse_bytes(raw, "cp1256")
kinds = {}
for ent in drawing.entities:
    kinds[ent.dxf_type] = kinds.get(ent.dxf_type, 0) + 1
closed = [e for e in drawing.entities if e.dxf_type == "POLYLINE" and e.closed]
check(len(closed) == 1 and len(closed[0].verts) == 4 and kinds.get("TEXT", 0) > 20 and kinds.get("CIRCLE") == 4,
      f"DXF reads back with Biruni's own reader: {kinds}")
check(drawing.version == "AC1009" and drawing.encoding.lower() in ("cp1256", "windows-1256"),
      f"DXF header: {drawing.version} {drawing.encoding}")

kml = ET.parse(os.path.join(folder, "11.kml")).getroot()
ns = {"k": "http://www.opengis.net/kml/2.2"}
coords = kml.find(".//k:Polygon//k:coordinates", ns).text.split()
lon, lat = map(float, coords[0].split(",")[:2])
check(len(coords) == 5 and 38.9 < lon < 39.1 and 21.4 < lat < 21.6, f"KML polygon in degrees ({lon:.5f}, {lat:.5f})")
check(kml.find(".//k:Placemark/k:name", ns).text == "قطعة رقم 11" and
      len(kml.findall(".//k:Folder/k:Placemark", ns)) == 4, "KML name and corner points")

from pypdf import PdfReader  # noqa: E402

pdf = PdfReader(os.path.join(folder, "11.pdf"))
check(len(pdf.pages) == 1 and os.path.getsize(os.path.join(folder, "11.pdf")) > 15000, "one-page PDF report")
check("Biruni" in pdf.pages[0].extract_text(), "PDF has text (not a picture)")
zpath = res_b["ZIP"]
zn = zipfile.ZipFile(zpath).namelist()
check("11/11.pdf" in zn and "11/11.shp" in zn and "checks.txt" in zn, f"ZIP holds everything ({len(zn)} files)")
check("All parcels passed" in open(os.path.join(bundle_dir, "checks.txt"), encoding="utf-8-sig").read() or
      "differs" not in open(os.path.join(bundle_dir, "checks.txt"), encoding="utf-8-sig").read(), "checks.txt")

# bad parcels are skipped and listed
badl = layer("MultiPolygon", "field=parcel_no:string&field=deed_area:double", [
    (f"MULTIPOLYGON((({X0} {Y0},{X0 + 20} {Y0 + 20},{X0 + 20} {Y0},{X0} {Y0 + 20},{X0} {Y0})))", ["7", None]),
    (f"MULTIPOLYGON((({X0} {Y0},{X0 + 5} {Y0},{X0 + 5} {Y0 + 5},{X0} {Y0})),(({X0 + 9} {Y0},{X0 + 14} {Y0},"
     f"{X0 + 14} {Y0 + 5},{X0 + 9} {Y0})))", ["8", None]),
    (f"MULTIPOLYGON((({X0 + 50} {Y0},{X0 + 70} {Y0},{X0 + 70} {Y0 + 30},{X0 + 50} {Y0 + 30},{X0 + 50} {Y0})))",
     ["9", 500.0]),
], "bad")
bad_dir = os.path.join(TMP, "bad")
res_bad = processing.run("biruni:parcelbundle", {"INPUT": badl, "NUMBER": "parcel_no", "DEED_AREA": "deed_area",
                                                 "PDF": False, "OUTPUT": bad_dir}, context=ctx, feedback=fb)
checks = open(os.path.join(bad_dir, "checks.txt"), encoding="utf-8-sig").read()
check(res_bad["PARCEL_COUNT"] == 1 and res_bad["SKIPPED_COUNT"] == 2 and "قطعة رقم 7" in checks and
      "قطعة رقم 8" in checks and "differs from the deed area" in checks, f"2 parcels skipped, 1 warned:\n{checks}")
check(sorted(os.listdir(bad_dir)) == ["9", "bad.zip", "checks.txt"], f"only the good parcel gets a folder: "
                                                                     f"{sorted(os.listdir(bad_dir))}")
try:
    only_bad = layer("MultiPolygon", "field=parcel_no:string", [(badl.getFeature(1).geometry().asWkt(), ["7"])],
                     "only_bad")
    processing.run("biruni:parcelbundle", {"INPUT": only_bad, "OUTPUT": os.path.join(TMP, "none")}, context=ctx,
                   feedback=fb)
    check(False, "nothing to prepare must stop the tool")
except Exception as ex:  # noqa: BLE001
    check("No parcel could be prepared" in str(ex), "nothing to prepare stops with the reasons")

# a picture of the report page, to look at
png = render_png(os.path.join(TMP, "report.png"), ReportPage(
    office="مكتب البيروني", info=[("رقم القطعة", "11"), ("المالك", "عبدالله")], pts=[(0, 0), (20, 0), (20, 30),
                                                                                     (0, 30)],
    corners=[(1, 0, 30), (2, 20, 30), (3, 20, 0), (4, 0, 0)], side_labels={"N": "شمالاً: شارع عرض 15م"},
    boundaries=[("شمالاً", "20.00", "شارع عرض 15م")], area=600, area_words=area_words(600), date="2026-10-28"))
check(os.path.getsize(png) > 20000, "report renders to a picture")
import math  # noqa: E402

ring20 = [(30 * math.cos(2 * math.pi * i / 20), 30 * math.sin(2 * math.pi * i / 20)) for i in range(20)]
many = describe_geometry(QgsGeometry.fromWkt(poly(ring20)))
png20 = render_png(os.path.join(TMP, "report20.png"), ReportPage(
    pts=[(x, y) for _, x, y in reversed(many.corners)], corners=many.corners,
    boundaries=[(d, f"{many.side_length(d):.2f}", "أرض فضاء") for d in "NSEW"], area=many.area,
    area_words=area_words(many.area), notes="ملاحظة طويلة " * 12, date="2026-10-28"))
check(len(many.corners) == 20 and os.path.getsize(png20) > 20000, "a 20-corner parcel fits on one page")
print(f"     outputs: {TMP}")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
