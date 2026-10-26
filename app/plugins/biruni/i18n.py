"""Tiny translation layer for Biruni's own strings.

QGIS translates its own UI through its .qm files (selected by the
``locale/userLocale`` setting). Biruni strings are English in code and looked up
in ``i18n/<lang>.json``; a missing entry falls back to English.
"""
import json
import os

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QLocale, Qt
from qgis.PyQt.QtWidgets import QApplication

_HERE = os.path.dirname(__file__)
_RTL = {"ar", "fa", "he", "ur"}

# (QGIS locale code, native name) - order is the order shown in menus.
LANGUAGES = [("en_US", "English"), ("ar", "العربية")]

_strings = {}
_lang = "en"


def locale_setting():
    s = QgsSettings()
    if s.value("locale/overrideFlag", False, type=bool):
        return s.value("locale/userLocale", "en_US") or "en_US"
    return QLocale.system().name()


def init():
    global _lang, _strings
    _lang = (locale_setting() or "en_US").split("_")[0].lower()
    _strings = {}
    path = os.path.join(_HERE, "i18n", f"{_lang}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _strings = json.load(f)
    if is_rtl() and QApplication.layoutDirection() != Qt.LayoutDirection.RightToLeft:
        QApplication.setLayoutDirection(Qt.LayoutDirection.RightToLeft)


def tr(text):
    return _strings.get(text, text)


def lang():
    return _lang


def is_rtl():
    return _lang in _RTL


def current_code():
    loc = locale_setting() or "en_US"
    for code, _ in LANGUAGES:
        if loc == code or loc.split("_")[0] == code.split("_")[0]:
            return code
    return "en_US"


def set_language(code):
    s = QgsSettings()
    s.setValue("locale/userLocale", code)
    s.setValue("locale/overrideFlag", True)
