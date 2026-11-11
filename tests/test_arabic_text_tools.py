"""Arabic text: shaping and display order for curved labels, the label fixer, text repair
(code pages, back-to-front text, presentation forms, digits) and Hijri ↔ Gregorian dates.

Run:  engine\\bin\\python-qgis.bat tests\\test_arabic_text_tools.py
"""
import datetime
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()  # settings written here, not in the shared profile
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from qgis.core import (Qgis, QgsApplication, QgsExpression, QgsExpressionContext, QgsFeature, QgsField, QgsFields,
                       QgsGeometry, QgsPalLayerSettings, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                       QgsTextFormat, QgsVectorFileWriter, QgsVectorLayer, QgsVectorLayerSimpleLabeling,
                       QgsCoordinateTransformContext, QgsWkbTypes, QgsAnnotationLayer, QgsAnnotationLineTextItem,
                       QgsLineString, QgsPoint)
from qgis.PyQt.QtCore import QDate, QMetaType
from qgis.PyQt.QtGui import QFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import arabic_labels  # noqa: E402
from biruni import arabic_text as at  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.arabic_text_tools import detect_dbf_encoding, read_dbf_text, write_cpg  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
TMP = tempfile.mkdtemp()

# ---- shaping -------------------------------------------------------------------------------------
check([hex(ord(c)) for c in at.shape("شارع")] == ["0xfeb7", "0xfe8e", "0xfead", "0xfec9"],
      "شارع: initial shin, final alef, isolated reh, isolated ain")
check(at.shape("سلام") == "\ufeb3\ufefc\ufee1", "lam-alef becomes one ligature (final form after a joining letter)")
check(at.shape("لا") == "\ufefb", "lam-alef alone: isolated ligature")
check(at.shape("محمد") == "\ufee3\ufea4\ufee4\ufeaa", "medial forms")
check(at.shape("بَيت") == "\ufe91\u064e\ufef4\ufe96", "marks are skipped for joining and stay after their letter")
check(at.shape("Road 12") == "Road 12", "Latin text is left alone")
for s in ("شارع الملك فهد", "مخطط ١٢٣/أ", "لأن الإسلام", "طريق الأمير سلطان بن عبدالعزيز"):
    check(at.unshape(at.shape(s)) == s, f"unshape(shape(x)) == x: {s}")

# ---- display order -------------------------------------------------------------------------------
t = "شارع الملك فهد ١٢"
check(at.visual(t) == "١٢ دهف كلملا عراش", "Arabic words reversed, numbers kept left to right")
check(at.visual(at.visual(t)) == t, "display order is its own inverse")
check(at.visual("قطعة (12) A1 مخطط 345") == "345 ططخم A1 (12) ةعطق",
      "brackets around numbers mirrored with the Arabic, Latin runs kept")
check(at.visual("Road شارع 5") == "Road عراش 5", "left-to-right text with an Arabic word inside")
check(at.visual("رقم 1234.5") == "1234.5 مقر", "decimal number kept whole")
lt = at.label_text(t)
check(all("\ufe70" <= c <= "\ufeff" or c in " ١٢" for c in lt) and at.unshape(at.visual(lt)) == t,
      "label_text = shaped + display order, reversible")
check(at.label_text("Main Road") == "Main Road", "label_text leaves Latin text alone")

# ---- digits, tatweel -----------------------------------------------------------------------------
check(at.to_arabic_digits("12.5 م") == "١٢٫٥ م", "digits to Arabic-Indic with the Arabic decimal mark")
check(at.to_latin_digits("١٢٫٥ و ۴۵") == "12.5 و 45", "Arabic-Indic and Persian digits to Latin")
check(at.remove_tatweel("شـــارع") == "شارع", "tatweel removed")

# ---- repair ------------------------------------------------------------------------------------
check(at.is_reversed("كلملا عراش") and at.fix_reversed("كلملا عراش") == "شارع الملك", "back-to-front text fixed")
check(not at.is_reversed("شارع الملك") and not at.is_reversed("مدرسة البنات"), "normal text not taken as reversed")
check(at.is_reversed("ةسردم") and at.fix_reversed("ةسردم") == "مدرسة", "ة at the start of a word = reversed")
stored = at.visual(at.shape("حي النخيل"))
check(at.is_reversed(stored) and at.fix_reversed(stored) == "حي النخيل", "reversed presentation forms (old CAD)")
m1 = "شارع الملك".encode("cp1256").decode("latin-1")
m2 = "حي الروضة".encode("utf-8").decode("cp1252")
check(at.fix_mojibake(m1)[0] == "شارع الملك", f"cp1256 read as Latin-1 repaired ({m1})")
check(at.fix_mojibake(m2)[0] == "حي الروضة", f"UTF-8 read as cp1252 repaired ({m2})")
check(at.fix_mojibake("Main Street")[1] == "" and at.fix_mojibake("Café")[1] == "", "real Latin text untouched")
check(at.guess_encoding(["شارع الملك".encode("cp1256"), "مخطط 3".encode("cp1256")])[0] == "cp1256",
      "code page guessed: cp1256")
check(at.guess_encoding(["شارع".encode("utf-8")])[0] == "utf-8", "code page guessed: UTF-8")

# ---- letters of other keyboards, presentation forms, invisible marks ----------------------------------
check(at.normalise("محمد کریم") == at.normalise("محمد كريم"), "Persian keheh and yeh: the same name")
check(at.normalise("مکہ") == at.normalise("مكه"), "Urdu heh goal: the same name")
check(at.normalise("ﻻﺎﻟﺎ") == at.normalise("لاالا") and at.normalise("ﻻ") == "لا",
      "presentation forms (old PDFs, CAD texts) read as the letters they show")
check(at.normalise("عبد‌الله‏") == at.normalise("عبدالله"), "zero-width and direction marks ignored")
t, idx = at.light_normalise("ﻻب")
check(t == "لاب" and idx == [0, 0, 1], "one position per letter, even for a ligature: " + repr((t, idx)))
check(at.normalise("أحمد إبراهيم آل ٱلشيخ") == at.normalise("احمد ابراهيم ال الشيخ"), "the alef forms as before")

# ---- Hijri -------------------------------------------------------------------------------------------
known = [((1445, 9, 1), datetime.date(2024, 3, 11)), ((1446, 1, 1), datetime.date(2024, 7, 7)),
         ((1446, 3, 20), datetime.date(2024, 9, 23)), ((1444, 10, 1), datetime.date(2023, 4, 21)),
         # 1343-1401 AH from the official almanac (hijridate): the Windows table had 1351/5/23 for this day
         ((1351, 5, 22), datetime.date(1932, 9, 23)), ((1420, 9, 24), datetime.date(2000, 1, 1)),
         ((1318, 1, 1), datetime.date(1900, 4, 30)), ((1500, 12, 30), datetime.date(2077, 11, 16))]
for h, g in known:
    check(at.hijri_to_gregorian(*h) == (g, True) and at.gregorian_to_hijri(g) == (h, True),
          f"Umm al-Qura {h} = {g}")
check(at._hijridate() is not None, "hijridate is loaded from the bundled folder")
check(at.hijri_to_gregorian(1342, 12, 1) == (at.hijri_to_gregorian(1342, 12, 1)[0], True)
      and at.gregorian_to_hijri(datetime.date(1924, 7, 31))[1], "before 1343 AH: the table still answers, exactly")
check(at.gregorian_to_hijri(datetime.date(1924, 8, 1)) == ((1343, 1, 1), True), "1 August 1924 = 1 Muharram 1343")
ok = True
d = datetime.date(1900, 4, 30)
while d <= datetime.date(2077, 11, 16):
    h, exact = at.gregorian_to_hijri(d)
    if not exact or at.hijri_to_gregorian(*h)[0] != d:
        ok = False
        break
    d += datetime.timedelta(days=1)
check(ok, "every day 1900-2077 converts there and back")
check(at.hijri_to_gregorian(1300, 1, 1) == (datetime.date(1882, 11, 12), False),
      "1 Muharram 1300 = 12 Nov 1882 (arithmetical calendar, marked approximate)")
check(at.gregorian_to_hijri(at.hijri_to_gregorian(1600, 7, 3)[0]) == ((1600, 7, 3), False), "beyond 1500 AH")
try:
    at.hijri_to_gregorian(1445, 1, 30)
    check(False, "30 Muharram 1445 does not exist (29 days)")
except ValueError:
    check(True, "30 Muharram 1445 does not exist (29 days)")
check(at.hijri_to_gregorian(1445, 2, 30)[0] == datetime.date(2023, 9, 15), "30 Safar 1445 exists")
for s in ("١٤٤٥/٠٣/١٢ هـ", "12-3-1445", "1445/3/12H", "1445-03-12 هجري", "12/03/1445ه"):
    check(at.parse_hijri(s) == (1445, 3, 12), f"Hijri text read: {s}")
check(at.parse_hijri("2024-03-11") is None or at.parse_hijri("2024-03-11")[0] == 2024, "Gregorian not taken as Hijri")
check(at.format_hijri((1445, 9, 1), arabic_digits=True) == "١٤٤٥/٠٩/٠١ هـ", "Hijri written with Arabic digits")
check(at.parse_gregorian("11/03/2024") == datetime.date(2024, 3, 11), "Gregorian day-first text")

# ---- expression functions ------------------------------------------------------------------------


def ev(expr):
    e = QgsExpression(expr)
    return e.evaluate(QgsExpressionContext())


check(ev("arabic_digits('12.5 م')") == "١٢٫٥ م", "arabic_digits()")
check(ev("latin_digits('١٢٣')") == "123", "latin_digits()")
check(ev("arabic_label('شارع')") == at.label_text("شارع"), "arabic_label()")
check(ev("to_hijri(to_date('2024-03-11'))") == "1445/09/01 هـ", "to_hijri()")
check(ev("to_hijri(to_date('2024-03-11'), 'dd-mm-yyyy')") == "01-09-1445 هـ", "to_hijri() with a pattern")
check(ev("from_hijri('١٤٤٥/٠٩/٠١ هـ')") == QDate(2024, 3, 11), "from_hijri()")
check(ev("from_hijri('not a date')") is None, "from_hijri() of nonsense is NULL")

# ---- label fixer ---------------------------------------------------------------------------------


def line_layer(text, placement, spacing=0.0, family="Arial", expression=False):
    lyr = QgsVectorLayer("LineString?crs=EPSG:3857&field=name:string", "roads", "memory")
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 0, 100 50, 200 0)"))
    f.setAttributes([text])
    lyr.dataProvider().addFeatures([f])
    s = QgsPalLayerSettings()
    s.fieldName = "upper(\"name\")" if expression else "name"
    s.isExpression = expression
    s.placement = placement
    fmt = QgsTextFormat()
    font = QFont(family)
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    fmt.setFont(font)
    s.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
    lyr.setLabelsEnabled(True)
    return lyr


P = Qgis.LabelPlacement
lyr = line_layer("شارع الملك فهد", P.Curved)
check(arabic_labels.problems(lyr) == [(0, "curved")], "curved Arabic labels detected")
changes = arabic_labels.fix_layer(lyr)
check(lyr.labeling().settings().fieldName == 'arabic_label("name")' and lyr.labeling().settings().isExpression,
      f"curved labels wrapped in arabic_label() ({changes})")
check(arabic_labels.problems(lyr) == [] and arabic_labels.fix_layer(lyr) == [], "fixing twice changes nothing")
check(arabic_labels.unfix_layer(lyr) == 1 and lyr.labeling().settings().fieldName == '"name"', "unfix restores")
lyr2 = line_layer("حي النخيل", P.PerimeterCurved, expression=True)
arabic_labels.fix_layer(lyr2)
check(lyr2.labeling().settings().fieldName == 'arabic_label(upper("name"))', "expressions are wrapped as they are")
check(arabic_labels.problems(line_layer("Main Road", P.Curved)) == [], "Latin curved labels are not touched")
check(arabic_labels.problems(line_layer("شارع", P.Line)) == [], "parallel Arabic labels are already right")
sp = line_layer("شارع", P.Line, spacing=4, family="Wingdings")
check(sorted(w for _i, w in arabic_labels.problems(sp)) == ["font", "spacing"], "letter spacing and font detected")
arabic_labels.fix_layer(sp)
font = sp.labeling().settings().format().font()
check(abs(font.letterSpacing()) < 1e-9 and arabic_labels.font_has_arabic(font),
      f"spacing removed and an Arabic font chosen ({font.family()})")
auto = line_layer("طريق الملك", P.Curved)
arabic_labels.set_auto(True)
arabic_labels._check_layers([auto], announce=False)
check(auto.labeling().settings().fieldName.startswith("arabic_label("), "automatic fix on a layer as it is added")
arabic_labels.set_auto(False)
off = line_layer("طريق الملك", P.Curved)
arabic_labels._check_layers([off], announce=False)
check(off.labeling().settings().fieldName == "name", "automatic fix can be switched off")
arabic_labels.set_auto(True)
arabic_labels.unregister_functions()
check(not QgsExpression.isFunctionName("arabic_label"), "functions can be unregistered")
arabic_labels.register_functions()
check(all(QgsExpression.isFunctionName(n) for n in arabic_labels.FUNCTIONS), "and registered again")
ann = QgsAnnotationLayer("ann", QgsAnnotationLayer.LayerOptions(QgsCoordinateTransformContext()))
ann.addItem(QgsAnnotationLineTextItem("شارع الأمير", QgsLineString([QgsPoint(0, 0), QgsPoint(10, 5)])))
check(arabic_labels.fix_annotations(ann) == 1 and arabic_labels.fix_annotations(ann) == 0,
      "text along a line (annotation) fixed once")

# ---- Arabic text doctor --------------------------------------------------------------------------
# a shapefile written in cp1256 whose .cpg was lost: QGIS reads its text as Latin-1 gibberish
fields = QgsFields()
fields.append(QgsField("name", QMetaType.Type.QString, len=60))
fields.append(QgsField("code", QMetaType.Type.QString, len=20))
src = os.path.join(TMP, "plots.shp")
opts = QgsVectorFileWriter.SaveVectorOptions()
opts.driverName = "ESRI Shapefile"
opts.fileEncoding = "CP1256"
w = QgsVectorFileWriter.create(src, fields, QgsWkbTypes.Type.Point, QgsProject.instance().crs(),
                               QgsCoordinateTransformContext(), opts)
names = ["مخطط الروضة", "قطعة 12", "شارع الملك عبدالعزيز", "Main road"]  # cp1256 has no ٠-٩
for i, n in enumerate(names):
    f = QgsFeature(fields)
    f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
    f.setAttributes([n, f"A{i}"])
    w.addFeature(f)
del w
for ext in (".cpg", ".CPG"):
    if os.path.exists(os.path.join(TMP, "plots" + ext)):
        os.remove(os.path.join(TMP, "plots" + ext))
with open(os.path.join(TMP, "plots.dbf"), "r+b") as fh:  # no language driver either
    fh.seek(29)
    fh.write(b"\x00")
lost = QgsVectorLayer(src, "plots", "ogr")
read_as = [f["name"] for f in lost.getFeatures()]
check(read_as[0] != names[0], f"without .cpg the shapefile reads as gibberish ({read_as[0]})")
check(detect_dbf_encoding(os.path.join(TMP, "plots.dbf"))[0] == "cp1256", "DBF code page detected: cp1256")
_idx, raw = read_dbf_text(os.path.join(TMP, "plots.dbf"))
check(raw[0][0].decode("cp1256") == names[0] and len(raw) == 4, "raw DBF text read by record number")
res = processing.run("biruni:arabictextdoctor", {"INPUT": lost, "OUTPUT": "memory:", "CHANGES": "memory:"},
                     context=ctx, feedback=fb)
out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
check([f["name"] for f in out.getFeatures()] == names, "doctor: Arabic back from the right code page")
check(res["DETECTED_ENCODING"] == "cp1256" and res["FIX_COUNT"] == 3, f"3 values fixed ({res['FIX_COUNT']})")
chg = ctx.getMapLayer(res["CHANGES"]) if isinstance(res["CHANGES"], str) else res["CHANGES"]
check(chg.featureCount() == 3 and "1256" in next(chg.getFeatures())["fix"], "changes table lists each fix")
cpg = write_cpg(src, "cp1256")
check(open(cpg).read() == "1256", "cpg written")
again = QgsVectorLayer(src, "plots2", "ogr")
check([f["name"] for f in again.getFeatures()] == names, "with the .cpg every program reads the Arabic")

mem = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string&field=n:integer", "mem", "memory")
vals = [at.visual("مدرسة البنات"), at.visual(at.shape("حي النخيل")), "شـارع ١٢", "Main road",
        "مسجد".encode("utf-8").decode("cp1252")]
feats = []
for i, v in enumerate(vals):
    f = QgsFeature(mem.fields())
    f.setAttributes([v, i])
    feats.append(f)
mem.dataProvider().addFeatures(feats)
res = processing.run("biruni:arabictextdoctor", {"INPUT": mem, "DIGITS": 1, "TATWEEL": True, "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
got = [f["name"] for f in out.getFeatures()]
check(got == ["مدرسة البنات", "حي النخيل", "شارع 12", "Main road", "مسجد"],
      f"doctor: back-to-front, presentation forms, digits, tatweel, mojibake ({got})")

# ---- Hijri dates tool ----------------------------------------------------------------------------
hl = QgsVectorLayer("NoGeometry?field=d:string", "deeds", "memory")
feats = []
for v in ("١٤٤٥/٠٩/٠١ هـ", "1-9-1445", "1446/3/20H", "", "abc", "1300/1/1"):
    f = QgsFeature(hl.fields())
    f.setAttributes([v])
    feats.append(f)
hl.dataProvider().addFeatures(feats)
res = processing.run("biruni:hijridates", {"INPUT": hl, "FIELD": "d", "OUTPUT": "memory:"}, context=ctx, feedback=fb)
out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
g = [f[res["FIELD_NAME"]] for f in out.getFeatures()]
check(g[:3] == [QDate(2024, 3, 11), QDate(2024, 3, 11), QDate(2024, 9, 23)], f"Hijri text → Gregorian dates ({g[:3]})")
check(res["CONVERTED"] == 4 and res["FAILED"] == 1 and res["APPROXIMATE"] == 1,
      f"counts: converted, not understood, approximate ({res['CONVERTED']}, {res['FAILED']}, {res['APPROXIMATE']})")
gl = QgsVectorLayer("NoGeometry?field=d:date", "dates", "memory")
f = QgsFeature(gl.fields())
f.setAttributes([QDate(2024, 9, 23)])
gl.dataProvider().addFeatures([f])
res = processing.run("biruni:hijridates", {"INPUT": gl, "FIELD": "d", "ARABIC": True, "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
out = ctx.getMapLayer(res["OUTPUT"]) if isinstance(res["OUTPUT"], str) else res["OUTPUT"]
check(next(out.getFeatures())[res["FIELD_NAME"]] == "١٤٤٦/٠٣/٢٠ هـ", "date field → Hijri text with Arabic digits")

shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
