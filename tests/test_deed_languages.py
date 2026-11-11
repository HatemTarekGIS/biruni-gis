"""Units, bearings and boundary descriptions in the languages of land records.

Run:  engine\\bin\\python-qgis.bat tests\\test_deed_languages.py
"""
import math
import os
import sys
import tempfile

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QGIS_CUSTOM_CONFIG_PATH", tempfile.mkdtemp())

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import bearings as brg  # noqa: E402
from biruni import deed_languages as dl  # noqa: E402
from biruni import units as un  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=1e-9):
    return abs(a - b) <= tol


def ring_error(a, b):
    return max(math.hypot(p[0] - q[0], p[1] - q[1]) for p, q in zip(a, b))


# ---- units -----------------------------------------------------------------------------------
check(near(un.convert(1, "kanal", "marla"), 20), "1 kanal = 20 marla")
check(near(un.convert(1, "marla", "sqft"), 272.25), "marla = 272.25 sq ft")
check(near(un.convert(1, "marla_225", "sqft"), 225) and near(un.convert(1, "kanal_250", "sqft"), 5000),
      "marla/kanal variants 225 and 250 sq ft")
check(near(un.convert(1, "feddan", "m2"), 4200.833333333, 1e-6) and near(un.convert(24, "qirat", "feddan"), 1)
      and near(un.convert(24, "sahm", "qirat"), 1), "feddan = 24 qirat = 576 sahm = 4200.83 m²")
check(near(un.convert(1, "pyeong", "m2"), 400 / 121) and near(un.convert(1, "tsubo", "pyeong"), 1), "pyeong = tsubo")
check(near(un.convert(1, "rai", "ngan"), 4) and near(un.convert(1, "ngan", "sqwa"), 100), "rai = 4 ngan = 400 sq wa")
check(near(un.convert(1, "mu", "m2"), 2000 / 3), "mu = 666.67 m²")
check(near(un.convert(1, "acre", "m2"), 4046.8564224, 1e-7), "acre exact")
check(near(un.convert(1, "bigha", "katha"), 20) and near(un.convert(100, "decimal", "acre"), 1),
      "bigha = 20 katha; 100 decimal = 1 acre")
check(near(un.convert(1, "alqueire_sp", "ha"), 2.42) and near(un.convert(1, "alqueire_mg", "ha"), 4.84),
      "alqueire paulista / mineiro")
check(near(un.convert(1, "chain", "m"), 20.1168) and near(un.convert(1, "chain", "link"), 100)
      and near(un.convert(4, "rod", "chain"), 1), "chain, link, rod")
check(near(un.convert(1, "ft_us", "m"), 1200 / 3937) and near(un.convert(1, "ft_gold_coast", "m"), 0.3047997101815088),
      "US survey foot, Gold Coast foot")
check(un.area_text(2 * un.get("kanal").size + 5 * un.get("marla").size, ["kanal", "marla"]) == "2 kanal 5 marla",
      "area_text: 2 kanal 5 marla")
check(un.area_text(1600 + 800 + 122, ["rai", "ngan", "sqwa"]) == "1 rai 2 ngan 30.5 square wa",
      f"area_text: {un.area_text(1600 + 800 + 122, ['rai', 'ngan', 'sqwa'])}")
check(un.area_text(un.get("feddan").size + 3 * un.get("qirat").size, ["feddan", "qirat", "sahm"], "ar")
      == "1 فدان 3 قيراط 0 سهم" or "فدان" in un.area_text(un.get("feddan").size, ["feddan"], "ar"),
      "area_text in Arabic")
check(un.for_country("PK") == ["kanal", "marla"] and un.for_country("eg")[0] == "feddan"
      and un.for_country("XX") == ["m2"], "for_country")
try:
    un.convert(1, "m", "m2")
    check(False, "length ↔ area refused")
except ValueError:
    check(True, "length ↔ area refused")

# ---- bearings --------------------------------------------------------------------------------
cases = [("N 45°30' E", None, 45.5), ("S 45°30' E", None, 134.5), ("S45-30W", None, 225.5),
         ("N. 45 deg. 30' W.", None, 314.5), ("45°30'15'' SO", "pt", 225 + 30 / 60 + 15 / 3600),
         ("45°30'15\" NE", "pt", 45 + 30 / 60 + 15 / 3600), ("N 45°30' O", "es", 314.5), ("S 10° L", "pt", 170.0),
         ("N 45° O", "de", 45.0), ("K 45° D", "tr", 45.0), ("G 30° B", "tr", 210.0), ("U 10° T", "id", 10.0),
         ("50 gr", None, 45.0), ("50,0000 g", "tr", 45.0), ("100.0000 gon", None, 90.0),
         ("۴۵ درجه ۳۰ دقیقه", "fa", 45.5), ("٤٥° ٣٠'", "ar", 45.5), ("045°30'00\"", None, 45.5), ("123.25", None, 123.25),
         ("90 درجے 15 منٹ 36 سیکنڈ", "ur", 90.26), ("N 90°00' E", None, 90.0), ("S 0°00' W", None, 180.0)]
bad = [(t, l, brg.parse(t, l)) for t, l, a in cases if not near(brg.parse(t, l), a, 1e-6)]
check(not bad, f"{len(cases)} bearings read in every notation ({bad})")
fmt_bad = []
for az in (0.5, 45.5042, 89.99, 134.5, 180.0, 225.25, 270.0, 314.75, 359.9):
    for notation in brg.NOTATIONS:
        for lang in ("en", "es", "pt", "fr", "tr", "id", "de"):
            text = brg.format(az, notation, lang, sec_decimals=2)
            back = brg.parse(text, lang)
            tol = 0.5 / 3600 if notation in ("quadrant_ph",) else (1e-5 if notation == "azimuth_decimal" else 1e-4)
            if abs(((back - az) + 180) % 360 - 180) > tol:
                fmt_bad.append((az, notation, lang, text, back))
check(not fmt_bad, f"format → parse in 6 notations × 7 languages ({fmt_bad[:3]})")
check(brg.format(314.5, "quadrant", "es") == "N 45°30'00\" O" and brg.format(314.5, "quadrant", "tr").startswith("K ")
      and brg.format(45.5, "quadrant_ph") == "N. 45 deg. 30' E.", "quadrant letters per language, PH style")
check(brg.format(45.5, "wcb") == "045°30'00\"" and brg.format(45, "gon") == "50.0000 gr", "WCB and grads")
check(brg.for_country("BR") == "azimuth" and brg.for_country("PH") == "quadrant_ph" and brg.for_country("NG") == "wcb"
      and brg.for_country("FR") == "gon", "bearings.for_country")

# ---- round trips in every language -----------------------------------------------------------
ring = [(654300.0, 2456130.0), (654320.5, 2456131.2), (654323.1, 2456101.7), (654305.6, 2456098.4),
        (654299.2, 2456112.9), (654297.7, 2456121.35)]
nbs = [dl.Neighbour("parcel", number="12"), dl.Neighbour("street", name="King Fahd", width=15),
       dl.Neighbour("default", default=1), None, dl.Neighbour("parcel", number="7"), dl.Neighbour("text", text="X")]
tie = {"name": "BLLM No. 1, Cad. 712", "x": 654250.0, "y": 2456050.0}
for lang in dl.TRAVERSE_LANGUAGES:
    t = dl.from_ring(ring, neighbours=nbs, name_format=dl.name_format_for(lang), tie=tie if lang == "en-PH" else None)
    text = dl.write(t, lang, title="Test", crs_note="WGS 84 / UTM 37N", area_units=["dunam"])
    try:
        p = dl.parse(text, tie_xy=(tie["x"], tie["y"]) if lang == "en-PH" else None)
        err = ring_error(t.ring(), p.ring())
        ok = p.language == lang and len(p.vertices) == len(ring) and err < 0.01 and abs(p.area - t.area) < 0.2
        check(ok, f"{lang}: write → read gives the parcel back ({err * 100:.2f} cm, names "
                  f"{[v.name for v in p.vertices][:3]}…)")
    except ValueError as e:
        check(False, f"{lang}: {e}")
check("lote nº 12" in dl.write(dl.from_ring(ring, neighbours=nbs, name_format="V-{:02d}"), "pt-BR")
      and "calle King Fahd de 15 m de ancho" in dl.write(dl.from_ring(ring, neighbours=nbs), "es")
      and "۱۵" in dl.write(dl.from_ring(ring, neighbours=nbs), "fa"), "neighbours written in each language")
th = dl.write(dl.from_ring(ring), "en", area_units=["rai", "ngan", "sqwa"])
check("ngan" in th and "square wa" in th, "area also in local units")
head, rows = dl.vertex_table(dl.from_ring(ring, neighbours=nbs), "pt-BR")
check(head[3] == "Azimute" and len(rows) == 6 and rows[0][5] == "lote nº 12", "vertex table (pt-BR)")
check(dl.for_country("BR") == "pt-BR" and dl.for_country("PH") == "en-PH" and dl.for_country("SA") == "ar-SA"
      and dl.defaults_for_country("PK")["area_units"] == ["kanal", "marla"], "deed_languages.for_country")

# ---- real-looking samples ----------------------------------------------------------------------
memorial = (
    "MEMORIAL DESCRITIVO\nImóvel: Fazenda Boa Vista\n"
    "Inicia-se a descrição deste perímetro no vértice ABCD-M-0001, de coordenadas N 7.456.000,000 m e "
    "E 654.000,000 m; deste, segue confrontando com Estrada Municipal, com os seguintes azimute e distância: "
    "90°00'00'' e 100,00 m até o vértice ABCD-M-0002, de coordenadas N 7.456.000,000 m e E 654.100,000 m; "
    "deste, segue confrontando com Sítio São João, com os seguintes azimute e distância: 180°00'00'' e 50,00 m "
    "até o vértice ABCD-P-0003, de coordenadas N 7.455.950,000 m e E 654.100,000 m; deste, segue confrontando "
    "com Rio Claro, com azimute de 270°00'00'' e distância de 100,00 m, até o vértice ABCD-P-0004, de "
    "coordenadas N 7.455.950,000 m e E 654.000,000 m; deste, segue com azimute de 0°00'00'' e distância de "
    "50,00 m, até o vértice ABCD-M-0001, ponto inicial da descrição deste perímetro.")
p = dl.parse(memorial)
check(p.language == "pt-BR" and near(p.area, 5000, 0.01) and p.vertices[0].name == "ABCD-M-0001"
      and near(p.vertices[0].y, 7456000, 1e-6) and math.isinf(p.precision), "Brazilian memorial (SIGEF wording)")

ph = ("TECHNICAL DESCRIPTION\nLot 3, Psd-04-012345. Beginning at a point marked \"1\" on plan, being N. 45 deg. "
      "00' E., 141.42 m. from BLLM No. 1, Cad. 712; thence S. 12 deg. 15' E., 50.00 m. to point 2; thence S. 77 deg. "
      "45' W., 30.00 m. to point 3; thence N. 12 deg. 15' W., 50.00 m. to point 4; thence N. 77 deg. 45' E., "
      "30.00 m. to the point of beginning; containing an area of ONE THOUSAND FIVE HUNDRED (1,500) SQUARE METERS.")
p = dl.parse(ph, tie_xy=(1000.0, 1000.0))
check(p.language == "en-PH" and near(p.area, 1500, 0.01) and abs(p.vertices[0].x - 1100) < 0.01
      and abs(p.vertices[0].y - 1100) < 0.01 and p.tie["name"].startswith("BLLM No. 1"),
      "Philippine technical description with its tie line")
p0 = dl.parse(ph)
check(any("tie" in w.lower() for w in p0.warnings), "tie point missing: a warning")

cuadro = ("MEMORIA DESCRIPTIVA\nCUADRO DE CONSTRUCCIÓN\nEST   PV   RUMBO   DISTANCIA   V   Y   X\n"
          "1   2   S 90°00'00\" E   100.00   2   2,000,000.000   500,100.000\n"
          "2   3   S 0°00'00\" W   50.00   3   1,999,950.000   500,100.000\n"
          "3   4   N 90°00'00\" O   100.00   4   1,999,950.000   500,000.000\n"
          "4   1   N 0°00'00\" E   50.00   1   2,000,000.000   500,000.000\n")
p = dl.parse(cuadro)
check(p.language == "es" and near(p.area, 5000, 1e-6) and near(p.vertices[0].x, 500000, 1e-6),
      "Mexican cuadro de construcción (spaces, rumbos with O)")

fr = ("Du point A (E = 1000,00, N = 2000,00) au point B : gisement 100,0000 gr, distance 40,00 m ; limite avec la "
      "parcelle n° 12.\nDu point B (E = 1040,00, N = 2000,00) au point C : gisement 200,0000 gr, distance 40,00 m ; "
      "limite avec la voie communale.\nDu point C (E = 1040,00, N = 1960,00) au point D : gisement 300,0000 gr, "
      "distance 40,00 m ; limite avec ...\nDu point D (E = 1000,00, N = 1960,00) au point A : gisement 0,0000 gr, "
      "distance 40,00 m ; limite avec ...")
p = dl.parse(fr)
check(p.language == "fr" and near(p.area, 1600, 1e-6) and [v.name for v in p.vertices] == ["A", "B", "C", "D"],
      "French bornage with gisements in grads")

ng = ("SURVEY PLAN OF PLOT 12\nSCHEDULE OF BEACONS\nBeacon   Easting   Northing\n"
      "SC/OG 1   543210.00   712345.00\n".replace("SC/OG 1", "SCOG1") +
      "SCOG2   543250.00   712345.00\nSCOG3   543250.00   712295.00\nSCOG4   543210.00   712295.00\n\n"
      "BEARINGS AND DISTANCES\nFrom   To   Bearing   Distance\n"
      "SCOG1   SCOG2   090°00'00\"   40.00\nSCOG2   SCOG3   180°00'00\"   50.00\n"
      "SCOG3   SCOG4   270°00'00\"   40.00\nSCOG4   SCOG1   000°00'00\"   50.00\n")
p = dl.parse(ng)
check(p.language == "en-NG" and near(p.area, 2000, 1e-6) and p.vertices[0].name == "SCOG1",
      "Nigerian schedule of beacons (WCB, space separated)")

chains = ("Commencing at corner 1 (E 0.00, N 0.00); thence N 0°00' E a distance of 5 chains to corner 2; thence "
          "N 90°00' E a distance of 2 chains to corner 3; thence S 0°00' E a distance of 5 chains to corner 4; thence "
          "S 90°00' W a distance of 2 chains to corner 1, the point of commencement.")
p = dl.parse(chains)
check(near(p.area, un.get("acre").size, 1e-6), "metes and bounds in chains: 10 square chains = 1 acre")

# misclosure and the compass rule
t = dl.from_ring([(0, 0), (0, 100), (60, 100), (60, 0)])
t.courses[1].distance += 0.10
txt = dl.write(t, "en")
p = dl.parse(txt)
pr = dl.parse(txt, adjust=False)
check(abs(p.misclosure - 0.10) < 0.002 and p.adjusted and not pr.adjusted and abs(pr.vertices[2].x - 60.10) < 0.002,
      f"misclosure 0.10 m read, compass rule spreads it ({p.misclosure:.3f} m, 1:{p.precision:,.0f})")
check(p.precision_ok(3000) and not p.precision_ok(5000) and any("below 1:5,000" in w for w in p.warnings),
      "precision ratio 1:N and the 1:5,000 check")
check(dl.detect("شمالاً: شارع عرض 15م بطول 20م") is None, "Arabic side deeds are not taken for a traverse")

# ---- inside the tools ------------------------------------------------------------------------
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
lyr = QgsVectorLayer("Polygon?crs=EPSG:32637&field=parcel_no:string", "parcels", "memory")
feats = []
for no, x0 in (("1", 0), ("2", 30)):
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({500000 + x0} 2700000,{500030 + x0} 2700000,{500030 + x0} 2700040,"
                                      f"{500000 + x0} 2700040,{500000 + x0} 2700000))"))
    f.setAttributes([no])
    feats.append(f)
lyr.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(lyr)
xlsx = os.path.join(tempfile.mkdtemp(), "desc.xlsx")
res = processing.run("biruni:parceldescription", {
    "INPUT": lyr, "NUMBER": "parcel_no", "LANGUAGE": 1 + dl.TRAVERSE_LANGUAGES.index("pt-BR"),
    "AREA_UNITS": "ha, nothing", "OUTPUT": "memory:", "XLSX": xlsx}, context=ctx, feedback=fb)
out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
first = next(f for f in out.getFeatures() if f["parcel_no"] == "1")
local = first["desc_local"]
back = dl.parse(local)
corners = {(round(v.x, 2), round(v.y, 2)) for v in back.vertices}
expect = {(500000.0, 2700000.0), (500030.0, 2700000.0), (500030.0, 2700040.0), (500000.0, 2700040.0)}
check(first["language"] == "pt-BR" and "MEMORIAL DESCRITIVO" in local and "lote nº 2" in local and corners == expect,
      "parceldescription in pt-BR: memorial with the neighbour, read back to the same corners")
check("V-01" in local and back.vertices[0].name == "V-01" and near(back.area, 1200, 0.01),
      "memorial starts at V-01 (north-west corner), area 1,200 m²")
from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(xlsx)
check(len(wb.sheetnames) == 4, f"Excel has the bearings-and-distances and description sheets ({wb.sheetnames})")
res2 = processing.run("biruni:parceldescription", {"INPUT": lyr, "NUMBER": "parcel_no", "OUTPUT": "memory:"},
                      context=ctx, feedback=fb)
out2 = ctx.getMapLayer(res2["OUTPUT"]) if isinstance(res2["OUTPUT"], str) else res2["OUTPUT"]
check(out2.fields().lookupField("desc_local") < 0, "default run unchanged (Arabic sides only)")

res3 = processing.run("biruni:deedtoparcel", {"TEXT": memorial, "CRS": "EPSG:31983", "OUTPUT": "memory:",
                                              "SIDES": "memory:"}, context=ctx, feedback=fb)
pl = ctx.getMapLayer(res3["OUTPUT"]) if isinstance(res3["OUTPUT"], str) else res3["OUTPUT"]
sl = ctx.getMapLayer(res3["SIDES"]) if isinstance(res3["SIDES"], str) else res3["SIDES"]
pf = next(pl.getFeatures())
check(res3["METHOD"] == "traverse" and near(pf.geometry().area(), 5000, 0.01) and sl.featureCount() == 4
      and pf.geometry().boundingBox().xMinimum() == 654000, "deedtoparcel reads a Brazilian memorial at its coordinates")
res4 = processing.run("biruni:deedtoparcel", {"TEXT": ph, "CRS": "EPSG:3123", "TIE_POINT": "1000,1000 [EPSG:3123]",
                                              "OUTPUT": "memory:", "SIDES": "memory:"}, context=ctx, feedback=fb)
pl4 = ctx.getMapLayer(res4["OUTPUT"]) if isinstance(res4["OUTPUT"], str) else res4["OUTPUT"]
check(near(next(pl4.getFeatures()).geometry().area(), 1500, 0.01) and math.isinf(res4["PRECISION"]),
      "deedtoparcel: Philippine description placed from the tie point")
res5 = processing.run("biruni:deedtoparcel", {"TEXT": "شمالاً: شارع عرض 15م بطول 20م\nجنوباً: قطعة 3 بطول 20م\n"
                                                      "شرقاً: قطعة 5 بطول 30م\nغرباً: قطعة 7 بطول 30م\nومساحتها 600م2",
                                              "CRS": "EPSG:32637", "OUTPUT": "memory:", "SIDES": "memory:"},
                      context=ctx, feedback=fb)
check(res5["METHOD"] == "rectangle", "Arabic side deeds still drawn as before")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
