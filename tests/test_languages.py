"""The interface languages: every i18n/<code>.json is valid and in step with ar.json, placeholders
survive translation, RTL languages flip the layout, and pt_PT falls back to the pt_BR strings.

Run:  engine\\bin\\python-qgis.bat tests\\test_languages.py
"""
import json
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtCore import QDate, Qt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.join(HERE, "..", "app", "plugins")
I18N = os.path.join(PLUGINS, "biruni", "i18n")
sys.path.insert(0, PLUGINS)

app = QgsApplication([], True)
app.initQgis()
from biruni import i18n  # noqa: E402

failures = []
MIN_COVERAGE = 0.90  # other work adds English strings faster than translations: below this the test fails
PLACE = re.compile(r"\{[^{}]*\}")
TAG = re.compile(r"</?[a-zA-Z][^<>]*>")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def load(name):
    with open(os.path.join(I18N, name), encoding="utf-8") as f:
        return json.load(f)


source = load("ar.json")  # its keys are the English source strings
check(len(source) > 1000, f"ar.json lists the English strings ({len(source)})")

# ---- every language file ------------------------------------------------------------------
for lng in i18n.LANGUAGE_INFO:
    if lng.code == "en_US":
        continue
    name = lng.code + ".json"
    path = os.path.join(I18N, name)
    check(os.path.exists(path), f"{name} exists")
    if not os.path.exists(path):
        continue
    try:
        data = load(name)
    except ValueError as e:
        check(False, f"{name} is valid JSON ({e})")
        continue
    check(isinstance(data, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()),
          f"{name}: an object of text → text")
    missing = [k for k in source if k not in data]
    unused = [k for k in data if k not in source]
    cover = 1 - len(missing) / len(source)
    print(f"     {name}: {len(source) - len(missing)}/{len(source)} ({cover:.1%}), {len(missing)} missing")
    check(cover >= MIN_COVERAGE, f"{name} covers at least {MIN_COVERAGE:.0%} of the strings")
    check(not unused, f"{name}: no strings that are no longer in the program {unused[:3]}")
    empty = [k for k, v in data.items() if not v.strip()]
    check(not empty, f"{name}: no empty translations {empty[:3]}")
    bad = [k for k, v in data.items() if sorted(PLACE.findall(k)) != sorted(PLACE.findall(v))]
    check(not bad, f"{name}: every {{placeholder}} kept {bad[:3]}")
    bad = [k for k, v in data.items() if sorted(TAG.findall(k)) != sorted(TAG.findall(v))]
    check(not bad, f"{name}: HTML tags kept {bad[:3]}")
    if lng.code == "ar":  # hand-written: البيروني and its own line breaks are deliberate
        continue
    bad = [k for k, v in data.items() if len(k) <= 60 and k.count("\n") != v.count("\n")]
    check(not bad, f"{name}: two-line button labels keep their line break {bad[:3]}")
    bad = [k for k, v in data.items() if "Biruni" in k and "Biruni" not in v]
    check(not bad, f"{name}: the name Biruni is not translated {bad[:3]}")
    if lng.code == "fa":
        bad = [k for k, v in data.items() if ("ي" in v or "ك" in v) and "ي" not in k and "ك" not in k]
        check(not bad, f"fa.json: Persian ی and ک, not Arabic ي and ك {bad[:3]}")

# ---- the language list --------------------------------------------------------------------
codes = [lng.code for lng in i18n.LANGUAGE_INFO]
check(codes[0] == "en_US" and len(set(codes)) == len(codes), "English first, no language twice")
check(i18n.LANGUAGES == [(lng.code, lng.name) for lng in i18n.LANGUAGE_INFO], "LANGUAGES stays (code, name) pairs")
for lng in i18n.LANGUAGE_INFO:
    check(lng.rtl == i18n.is_rtl(lng.code), f"{lng.code}: rtl flag matches is_rtl()")
check(i18n.is_rtl("ar") and i18n.is_rtl("fa") and i18n.is_rtl("ur") and i18n.is_rtl("he"), "ar, fa, ur, he are RTL")
check(not any(i18n.is_rtl(c) for c in ("en_US", "es", "pt_BR", "fr", "tr")), "es, pt_BR, fr, tr are LTR")
check(i18n.HELP_TRANSLATE_URL.startswith("https://"), "help-translate link")

# ---- fallback -----------------------------------------------------------------------------
check(i18n.language_info("pt_PT").code == "pt_BR", "pt_PT uses Português (Brasil)")
check(i18n.language_info("fr_CA").code == "fr", "fr_CA uses Français")
check(i18n.language_info("de_DE").code == "en_US", "an unlisted language falls back to English")
files = [os.path.basename(p) for p in i18n.translation_files("pt_BR")]
check(files == ["pt_BR.json", "pt.json"], f"pt_BR looks in pt_BR.json, then pt.json ({files})")
check(i18n.load_strings("pt_PT") == {} or "Save" in i18n.load_strings("pt_PT"), "pt_PT loads without error")

# ---- switching the language sets the strings and the layout direction ---------------------
i18n.init("fa")
check(i18n.lang() == "fa" and i18n.tr("Save") not in ("", "Save"), "fa: strings are translated")
check(app.layoutDirection() == Qt.LayoutDirection.RightToLeft, "fa: the layout runs right to left")
i18n.init("es")
check(i18n.tr("Save") == load("es.json").get("Save", "Save"), "es: strings come from es.json")
check(app.layoutDirection() == Qt.LayoutDirection.LeftToRight, "es: the layout runs left to right")
i18n.init("pt_PT")
check(i18n.tr("Save") == load("pt_BR.json").get("Save", "Save"), "pt_PT: the pt_BR strings are used")
i18n.init("ar")
check(app.layoutDirection() == Qt.LayoutDirection.RightToLeft, "ar: right to left again")
i18n.init("en_US")
check(i18n.tr("Save") == "Save" and app.layoutDirection() == Qt.LayoutDirection.LeftToRight, "English: source text, LTR")
check(i18n.tr("a string nobody translated") == "a string nobody translated", "a missing string shows the English")

# ---- numbers and dates --------------------------------------------------------------------
check(i18n.format_number(1234.5, 1, "en_US") == "1,234.5", "English 1,234.5")
check(i18n.format_number(1234.5, 1, "es") == "1.234,5", "Spanish 1.234,5")
check(i18n.format_number(1234.5, 1, "tr") == "1.234,5", "Turkish 1.234,5")
check(i18n.format_number(1234.5, 1, "fr").endswith("234,5") and "," in i18n.format_number(1.5, 1, "fr"),
      "French decimal comma")
check(i18n.format_number(1234.5, 1, "fa", native=True) == "۱,۲۳۴٫۵", "Persian digits ۱,۲۳۴٫۵")
check(i18n.native_digits("2025", "ar") == "٢٠٢٥" and i18n.native_digits("2025", "es") == "2025", "native digits")
d = i18n.format_date(QDate(2025, 3, 11), "en_US")
check("3" in d and "11" in d, f"short date ({d})")
check(i18n.format_date("2025-03-11", "fr").startswith("11"), "French date: day first")
check(any(c in i18n.format_date("2025-03-11", "fa", native=True) for c in "۰۱۲۳۴۵۶۷۸۹"), "Persian date digits")
i18n.has_qgis_translation("fa")  # must not raise; QGIS ships no Persian .qm
check(i18n.has_qgis_translation("en_US"), "English needs no .qm")

app.exitQgis()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
