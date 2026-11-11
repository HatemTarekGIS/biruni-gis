"""Tiny translation layer for Biruni's own strings.

QGIS translates its own UI through its .qm files (selected by the
``locale/userLocale`` setting). Biruni strings are English in code and looked up
in ``i18n/<code>.json`` (``pt_BR.json``, then ``pt.json``); a missing entry falls
back to English.
"""
import json
import os
from collections import namedtuple

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import QDate, QLocale, Qt
from qgis.PyQt.QtWidgets import QApplication

_HERE = os.path.dirname(__file__)
_RTL = {"ar", "fa", "ur", "he", "ckb", "ps"}

Language = namedtuple("Language", "code name rtl qgis_locale english_name")

# Order is the order shown in menus. code = the value stored in locale/userLocale.
LANGUAGE_INFO = [
    Language("en_US", "English", False, "en_US", "English"),
    Language("ar", "العربية", True, "ar", "Arabic"),
    Language("es", "Español", False, "es", "Spanish"),
    Language("pt_BR", "Português (Brasil)", False, "pt_BR", "Portuguese (Brazil)"),
    Language("fr", "Français", False, "fr", "French"),
    Language("tr", "Türkçe", False, "tr", "Turkish"),
    Language("fa", "فارسی", True, "fa", "Persian"),
]
# (code, native name) pairs - kept for the menus and the start page.
LANGUAGES = [(lng.code, lng.name) for lng in LANGUAGE_INFO]
HELP_TRANSLATE_URL = "https://github.com/HatemTarekGIS/biruni-gis/tree/main/app/plugins/biruni/i18n"

_strings = {}
_lang = "en"
_code = "en_US"

_NATIVE_DIGITS = {"ar": "٠١٢٣٤٥٦٧٨٩", "fa": "۰۱۲۳۴۵۶۷۸۹", "ur": "۰۱۲۳۴۵۶۷۸۹"}
_DECIMAL = {"ar": "٫", "fa": "٫", "ur": "٫"}
_SEPARATORS = {  # (decimal point, thousands)
    "en": (".", ","), "ar": (".", ","), "fa": (".", ","), "es": (",", "."), "pt": (",", "."),
    "fr": (",", " "), "tr": (",", "."), "de": (",", "."), "it": (",", "."), "ru": (",", " "),
}


def locale_setting():
    s = QgsSettings()
    if s.value("locale/overrideFlag", False, type=bool):
        return s.value("locale/userLocale", "en_US") or "en_US"
    return QLocale.system().name()


def language_info(code=None):
    """The Language entry for a locale code (exact, then by language, e.g. pt_PT → pt_BR); English if none."""
    code = code or _code
    for lng in LANGUAGE_INFO:
        if lng.code == code:
            return lng
    base = code.replace("-", "_").split("_")[0].lower()
    for lng in LANGUAGE_INFO:
        if lng.code.split("_")[0].lower() == base:
            return lng
    return LANGUAGE_INFO[0]


def translation_files(code):
    """The JSON files tried for a locale code, most specific first: pt_BR → [pt_BR.json, pt.json]."""
    code = code.replace("-", "_")
    names = [code]
    base = code.split("_")[0]
    if base != code:
        names.append(base)
    if base.lower() != base:
        names.append(base.lower())
    return [os.path.join(_HERE, "i18n", f"{n}.json") for n in dict.fromkeys(names)]


def load_strings(code):
    """Merged strings for a locale code: the specific file wins over the general one."""
    merged = {}
    for path in reversed(translation_files(code)):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                merged.update(json.load(f))
    return merged


def init(code=None):
    global _lang, _strings, _code
    loc = (code or locale_setting() or "en_US").replace("-", "_")
    _lang = loc.split("_")[0].lower()
    info = language_info(loc)
    # a language Biruni lists (pt_PT → pt_BR strings); otherwise keep the locale as it is
    _code = info.code if info.code.split("_")[0].lower() == _lang else loc
    _strings = {} if _lang == "en" else load_strings(_code)
    want = Qt.LayoutDirection.RightToLeft if is_rtl() else Qt.LayoutDirection.LeftToRight
    if QApplication.instance() is not None and QApplication.layoutDirection() != want:
        QApplication.setLayoutDirection(want)


def tr(text):
    return _strings.get(text, text)


def search_synonyms():
    """{English text: [other wordings]} for the current language - an older or everyday wording of a label
    stays findable in the search after the label itself was changed (i18n/synonyms/<lang>.json)."""
    path = os.path.join(_HERE, "i18n", "synonyms", f"{_lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def lang():
    return _lang


def is_rtl(code=None):
    return (code.replace("-", "_").split("_")[0].lower() if code else _lang) in _RTL


def current_code():
    return language_info(locale_setting() or "en_US").code


def set_language(code):
    s = QgsSettings()
    s.setValue("locale/userLocale", language_info(code).qgis_locale)
    s.setValue("locale/overrideFlag", True)
    s.sync()  # on disk now: a restart that goes wrong must not lose the choice
    # The engine reads the language from the user's own settings file at start, before the global defaults are
    # known, and QgsSettings leaves out values equal to those defaults (overrideFlag=true is one): without the
    # flag in the file the engine takes the Windows language and writes it over the choice. So write both
    # straight into the file.
    from qgis.PyQt.QtCore import QSettings

    path = s.fileName()
    if path:
        raw = QSettings(path, QSettings.Format.IniFormat)
        raw.setValue("locale/userLocale", language_info(code).qgis_locale)
        raw.setValue("locale/overrideFlag", True)
        raw.sync()


def has_qgis_translation(code):
    """True when the engine ships a QGIS translation (.qm) for this language (Persian does not: QGIS's own
    menus and dialogs stay in English there, Biruni's are translated)."""
    lng = language_info(code)
    if lng.code == "en_US":
        return True
    folder = QgsApplication.i18nPath()
    return any(os.path.exists(os.path.join(folder, f"qgis_{c}.qm"))
               for c in (lng.qgis_locale, lng.qgis_locale.split("_")[0]))


# ---- numbers and dates in the interface language ------------------------------------------
def native_digits(text, code=None):
    """Latin digits written with the language's own digits (Arabic-Indic for Arabic, Persian for Persian);
    unchanged for other languages."""
    base = (code or _code).replace("-", "_").split("_")[0].lower()
    digits = _NATIVE_DIGITS.get(base)
    if not digits or text is None:
        return text
    return str(text).translate(str.maketrans("0123456789", digits))


def format_number(value, decimals=2, code=None, native=False):
    """A number with the language's decimal and thousands separators (1,234.5 / 1.234,5 / 1 234,5);
    native=True also writes Arabic or Persian digits."""
    base = (code or _code).replace("-", "_").split("_")[0].lower()
    point, thousands = _SEPARATORS.get(base, (".", ","))
    text = f"{value:,.{decimals}f}"
    text = text.replace(",", "\0").replace(".", point).replace("\0", thousands)
    if native and base in _NATIVE_DIGITS:
        text = native_digits(text.replace(".", _DECIMAL[base]), base)
    return text


def format_date(date, code=None, native=False):
    """A short date (QDate, datetime.date or ISO text) in the language's usual order."""
    if not isinstance(date, QDate):
        import datetime

        if isinstance(date, str):
            date = datetime.date.fromisoformat(date[:10])
        date = QDate(date.year, date.month, date.day)
    lng = language_info(code)
    text = QLocale(lng.qgis_locale).toString(date, QLocale.FormatType.ShortFormat)
    if not native:
        text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    return native_digits(text, lng.code) if native else text
