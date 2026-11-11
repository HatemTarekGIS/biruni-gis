"""Labels in every writing system (script_labels) and calendars (calendars, biruni:convertdates).

Run:  engine\\bin\\python-qgis.bat tests\\test_scripts_calendars.py
"""
import datetime
import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()  # settings of this run stay out of the user's profile

from qgis.core import (Qgis, QgsApplication, QgsExpression, QgsExpressionContext, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsPalLayerSettings, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                       QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QDate  # noqa: E402
from qgis.PyQt.QtGui import QFont  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import arabic_labels, calendars, script_labels as sl  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def ev(expr):
    return QgsExpression(expr).evaluate(QgsExpressionContext())


# ---- scripts ---------------------------------------------------------------------------------------
check(sl.main_script(["خیابان ولیعصر"]) == "arabic" and sl.main_script(["רחוב הרצל 12"]) == "hebrew"
      and sl.main_script(["ถนนสุขุมวิท"]) == "thai" and sl.main_script(["महात्मा गांधी"]) == "devanagari"
      and sl.main_script(["中山路"]) == "cjk" and sl.main_script(["세종대로"]) == "korean"
      and sl.main_script(["Main Road 12"]) is None, "scripts detected (Latin is not a problem script)")
check(sl.unshapeable_letters("خیابان ولیعصر کوچه") == set() and sl.unshapeable_letters("شاہراہ ٹاؤن ے") == set(),
      "Persian and Urdu letters all have joined forms")
check(sl.unshapeable_letters("شەقامی سەڵاح ڕێگا") == {"ڕ", "ڵ", "ێ"}
      and sl.unshapeable_letters("کابل ښار ډیر") == {"ښ", "ډ"}, "Kurdish/Pashto letters without joined forms found")
shaped = sl.shape_text("کوچه")
check(shaped != "کوچه" and all("ﭐ" <= c <= "﻿" for c in shaped), f"Persian shaped for curves ({shaped})")
check(sl.shape_text("ڵەک") == "ڵەک", "unshapeable text left as it is")
check(sl.shape_text("רחוב 12") == "12 בוחר", "Hebrew in display order, numbers still left to right")
check(sl.shape_text("Main Road") == "Main Road" and sl.shape_text("ถนน") == "ถนน", "other scripts untouched")
check(sl.to_native_digits("1403/05/20", "persian") == "۱۴۰۳/۰۵/۲۰" and sl.to_native_digits("12.5", "arabic") == "١٢٫٥"
      and sl.to_native_digits("2567", "thai") == "๒๕๖๗" and sl.to_native_digits("42", "bengali") == "৪২"
      and sl.to_native_digits("7", "devanagari") == "७", "native digits")
check(sl.to_latin_digits("۱۴۰۳ ١٢٫٥ ๒๕๖๗ ৪২ ७") == "1403 12.5 2567 42 7", "any digits back to 0-9")
check(sl.unify("كيان", "persian") == "کیان" and sl.unify("کیان", "arabic") == "كيان", "Arabic/Persian ي ك unified")
words = sl.split_thai("ถนนสุขุมวิทซอยสิบเอ็ดกรุงเทพมหานคร")
check(words == ["ถนน", "สุขุมวิท", "ซอย", "สิบเอ็ด", "กรุงเทพมหานคร"], f"Thai words found ({'|'.join(words)})")
w2 = sl.split_thai("ถนนเจริญกรุงใหม่ABC ซอยพิเศษ")
check("".join(w2) == "ถนนเจริญกรุงใหม่ABC ซอยพิเศษ" and "ถนน" in w2, f"unknown Thai kept whole ({'|'.join(w2)})")
check(all(not c.startswith(("ั", "ิ", "่", "์", "ะ", "า")) for c in sl.split_thai("ศักดิ์สิทธิ์ทดสอบคำแปลก")),
      "Thai words never start with a mark or following vowel")
check(sl.wrap_text("ถนนสุขุมวิทซอยสิบเอ็ดกรุงเทพมหานคร", 10) == "ถนนสุขุมวิท\nซอยสิบเอ็ด\nกรุงเทพมหานคร",
      "Thai wrapped between words")
check(sl.wrap_text("King Fahd Road North", 10) == "King Fahd\nRoad North", "Latin wrapped at spaces")

# expression functions
check(ev("shape_label('רחוב 12')") == "12 בוחר" and ev("native_digits('1403', 'persian')") == "۱۴۰۳"
      and ev("latin_digits('۱۲')") == "12" and ev("wrap_label('ถนนสุขุมวิทซอยสิบเอ็ด', 8)") == "ถนนสุขุมวิท\nซอยสิบเอ็ด"
      and ev("thai_words('ถนนสุขุมวิท', ' ')") == "ถนน สุขุมวิท" and ev("unify_letters('كيان')") == "کیان",
      "label expression functions work")
check(ev("arabic_label('شارع')") == sl.at.label_text("شارع") and ev("arabic_digits('12')") == "١٢",
      "older Arabic functions unchanged")

# layers
P = Qgis.LabelPlacement


def layer(text, placement, family="Segoe UI", kind="LineString", wrap=0):
    wkt = {"LineString": "LINESTRING(0 0, 100 50, 200 0)", "Point": "POINT(1 1)"}[kind]
    lyr = QgsVectorLayer(f"{kind}?crs=EPSG:3857&field=name:string", "l", "memory")
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    f.setAttributes([text])
    lyr.dataProvider().addFeatures([f])
    s = QgsPalLayerSettings()
    s.fieldName = "name"
    s.placement = placement
    s.autoWrapLength = wrap
    fmt = QgsTextFormat()
    fmt.setFont(QFont(family))
    s.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
    lyr.setLabelsEnabled(True)
    return lyr


def settings(lyr):
    return lyr.labeling().settings()


fa = layer("خیابان ولیعصر", P.Curved)
check(sl.problems(fa) == [(0, "curved")] and sl.fix_layer(fa) and settings(fa).fieldName == 'arabic_label("name")',
      "Persian curved labels shaped")
ku = layer("شەقامی سەڵاح", P.Curved)
ch = sl.fix_layer(ku)
check(sl.problems(ku) == [] and settings(ku).placement == P.Line and settings(ku).fieldName == "name"
      and not settings(ku).isExpression and "ڵ" in ch[0], "Kurdish with ڵ switched to straight labels")
kw = layer("شەقامی سەڵاح", P.Curved)
s = QgsPalLayerSettings(settings(kw))
s.fieldName, s.isExpression = 'arabic_label("name")', True
kw.setLabeling(QgsVectorLayerSimpleLabeling(s))
sl.fix_layer(kw)
check(settings(kw).placement == P.Line and settings(kw).fieldName == "name", "a wrapped layer that cannot shape is undone")
he = layer("רחוב הרצל", P.Curved)
sl.fix_layer(he)
check(settings(he).fieldName == 'shape_label("name")', "Hebrew curved labels put in display order")
un = layer("שכונה", P.Curved)
sl.fix_layer(un)
s = QgsPalLayerSettings(settings(un))
s.placement = P.Horizontal
un.setLabeling(QgsVectorLayerSimpleLabeling(s))
check(sl.problems(un) == [(0, "unneeded")], "curved function on a horizontal label noticed")
sl.fix_layer(un)
check(settings(un).fieldName == "name" and not settings(un).isExpression, "and taken off")
th = layer("ถนนสุขุมวิทซอยสิบเอ็ด", P.OverPoint, family="Tahoma", kind="Point", wrap=12)
sl.fix_layer(th)
check(settings(th).fieldName == 'wrap_label("name", 12)' and settings(th).autoWrapLength == 0,
      "Thai automatic wrap replaced by wrapping between words")
hi = layer("महात्मा गांधी मार्ग", P.Curved, family="Segoe UI")
check(all(w != "curved" for _i, w in sl.problems(hi)), "Indic curved labels need no shaping (they draw right)")
check(sl.problems(layer("Main Road", P.Curved)) == [], "Latin labels untouched")
check(sl.font_covers("Segoe UI", "arabic") and sl.font_covers("Segoe UI", "hebrew")
      and not sl.font_covers("Wingdings", "arabic"), "font coverage by writing system")
wd = layer("ถนน", P.OverPoint, family="Wingdings", kind="Point")
sl.fix_layer(wd)
check(sl.font_covers(settings(wd).format().font(), "thai"), f"Thai font chosen ({settings(wd).format().font().family()})")
cj = layer("中山路", P.OverPoint, kind="Point")
check(sl.set_vertical(cj) == 1 and settings(cj).format().orientation() == Qgis.TextOrientation.Vertical,
      "vertical labels switched on")
check(all(s["fonts"] and s["sample"] for s in sl.SCRIPTS) and len({s["id"] for s in sl.SCRIPTS}) == len(sl.SCRIPTS),
      "every script has a sample and recommended fonts")
check(arabic_labels.fix_layer is sl.fix_layer and arabic_labels.install is sl.install, "arabic_labels delegates")

# ---- calendars -------------------------------------------------------------------------------------
D = datetime.date
check(calendars.gregorian_to_jalali(D(2024, 8, 10)) == (1403, 5, 20)
      and calendars.gregorian_to_jalali(D(2024, 3, 20)) == (1403, 1, 1)
      and calendars.gregorian_to_jalali(D(2023, 3, 21)) == (1402, 1, 1)
      and calendars.gregorian_to_jalali(D(2025, 3, 20)) == (1403, 12, 30)
      and calendars.gregorian_to_jalali(D(2021, 3, 20)) == (1399, 12, 30), "Jalali dates (Nowruz, leap Esfand 30)")
check(calendars.jalali_leap(1403) and calendars.jalali_leap(1399) and not calendars.jalali_leap(1402),
      "Jalali leap years")
check(calendars.gregorian_to_ethiopian(D(2024, 9, 11)) == (2017, 1, 1)
      and calendars.gregorian_to_ethiopian(D(2023, 9, 12)) == (2016, 1, 1)
      and calendars.gregorian_to_ethiopian(D(2024, 9, 27)) == (2017, 1, 17)
      and calendars.gregorian_to_ethiopian(D(2024, 1, 7)) == (2016, 4, 28), "Ethiopian dates (new year, Meskel, Genna)")
ok = True
d = D(1940, 1, 1)
while d <= D(2077, 12, 31):
    for cal in ("jalali", "ethiopian", "buddhist", "hijri"):
        ymd, _ex = calendars.from_gregorian(d, cal)
        if calendars.to_gregorian(cal, *ymd)[0] != d:
            ok = False
    d += datetime.timedelta(days=1)
check(ok, "every day 1940-2077 converts there and back in all calendars")
check(calendars.from_gregorian(D(2024, 3, 11), "hijri")[0] == (1445, 9, 1)
      and calendars.from_gregorian(D(2024, 1, 15), "buddhist")[0] == (2567, 1, 15), "Hijri and Buddhist")
check(calendars.parse_date("۱۴۰۳/۰۵/۲۰") == ("jalali", (1403, 5, 20))
      and calendars.parse_date("١٤٤٥/٠٩/٠١ هـ") == ("hijri", (1445, 9, 1))
      and calendars.parse_date("15/01/2567 พ.ศ.") == ("buddhist", (2567, 1, 15))
      and calendars.parse_date("2017/01/01 ዓ.ም") == ("ethiopian", (2017, 1, 1))
      and calendars.parse_date("11/3/2024") == ("gregorian", (2024, 3, 11))
      and calendars.parse_date("20 مرداد 1403") == ("jalali", (1403, 5, 20)), "calendar found from the text")
check(calendars.format_date("jalali", (1403, 5, 20), digits="native") == "۱۴۰۳/۰۵/۲۰ ه‍.ش"
      and calendars.format_date("jalali", (1403, 5, 20), "dd MMMM yyyy", suffix=False) == "20 مرداد 1403",
      "Jalali written with Persian digits and month names")
r = calendars.convert("1403/05/20", "gregorian", "jalali")
check(r is not None and r[0] == D(2024, 8, 10), "Jalali → Gregorian")
check(calendars.for_country("SA") == ["hijri"] and calendars.for_country("IR") == ["jalali"]
      and calendars.for_country("TH") == ["buddhist"] and calendars.for_country("ET") == ["ethiopian"]
      and calendars.for_country("EG") == [], "calendars by country")
check(ev("to_jalali(to_date('2024-08-10'))") == "1403/05/20 ه‍.ش" and ev("from_jalali('۱۴۰۳/۰۵/۲۰')") == QDate(2024, 8, 10)
      and ev("to_calendar(to_date('2024-09-11'), 'ethiopian')") == "2017/01/01 ዓ.ም"
      and ev("from_calendar('2567/01/15', 'buddhist')") == QDate(2024, 1, 15)
      and ev("to_hijri(to_date('2024-03-11'))") == "1445/09/01 هـ", "calendar expression functions")

# re-registering after unregistering must work (and must not crash on exit)
arabic_labels.unregister_functions()
calendars.unregister_functions()
sl.unregister_functions()
check(not QgsExpression.isFunctionName("to_jalali") and not QgsExpression.isFunctionName("shape_label"),
      "functions unregistered")
arabic_labels.register_functions()
check(all(QgsExpression.isFunctionName(n) for n in sl.FUNCTIONS.names() + calendars.FUNCTIONS.names()),
      "and registered again")
check(ev("to_jalali(to_date('2024-03-20'))") == "1403/01/01 ه‍.ش" and ev("shape_label('רחוב')") == "בוחר",
      "and they still work")

# ---- the tool --------------------------------------------------------------------------------------
tab = QgsVectorLayer("None?field=d:string", "dates", "memory")
vals = ["۱۴۰۳/۰۵/۲۰", "1403/05/20 ه.ش", "2024-08-10", "", "not a date", "١٤٤٦/٠٢/٠٦ هـ"]
feats = []
for v in vals:
    f = QgsFeature(tab.fields())
    f.setAttributes([v])
    feats.append(f)
tab.dataProvider().addFeatures(feats)
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
res = processing.run("biruni:convertdates", {"INPUT": tab, "FIELD": "d", "SOURCE": 0, "TARGET": 0,
                                             "OUTPUT": "memory:"}, context=ctx, feedback=QgsProcessingFeedback())
out = res["OUTPUT"] if not isinstance(res["OUTPUT"], str) else ctx.getMapLayer(res["OUTPUT"])
got = [f["d_gregorian"] for f in out.getFeatures()]
check(got[0] == QDate(2024, 8, 10) and got[1] == QDate(2024, 8, 10) and got[2] == QDate(2024, 8, 10)
      and got[5] == QDate(2024, 8, 10) and res["CONVERTED"] == 4 and res["FAILED"] == 1,
      f"mixed Jalali/Hijri/Gregorian column converted to Gregorian ({got}, {res['CONVERTED']}/{res['FAILED']})")
res2 = processing.run("biruni:convertdates", {"INPUT": tab, "FIELD": "d", "SOURCE": 0, "TARGET": 2, "DIGITS": True,
                                              "OUTPUT": "memory:"}, context=ctx, feedback=QgsProcessingFeedback())
out2 = res2["OUTPUT"] if not isinstance(res2["OUTPUT"], str) else ctx.getMapLayer(res2["OUTPUT"])
check([f["d_jalali"] for f in out2.getFeatures()][2] == "۱۴۰۳/۰۵/۲۰ ه‍.ش", "Gregorian → Jalali with Persian digits")
check(QgsApplication.processingRegistry().algorithmById("biruni:hijridates") is not None, "the Hijri tool is still there")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
