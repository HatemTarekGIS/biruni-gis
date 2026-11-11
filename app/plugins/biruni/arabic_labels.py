"""Arabic labels that come out right - kept for code and projects that use it; the work is done for
every writing system in script_labels (labels) and calendars (Hijri and other calendar functions).

Expression functions (group "Biruni"): arabic_label(text), arabic_digits(text), latin_digits(text),
to_hijri(date[, pattern]), from_hijri(text) - and, from script_labels and calendars, shape_label,
native_digits, wrap_label, thai_words, unify_letters, to_jalali, from_jalali, to_calendar, from_calendar.
"""
from qgis.core import QgsExpression

from . import arabic_text as at  # noqa: F401 - used by callers as arabic_labels.at
from . import calendars, script_labels
from .script_labels import (AUTO_KEY, CURVED, GROUP, _check_layers, _label_values, _settings_list,  # noqa: F401
                            arabic_font, fix_annotations, fix_layer, fix_project, font_has_arabic, install,
                            is_auto, problems, set_auto, unfix_layer, uninstall)

FUNCTIONS = ("arabic_label", "arabic_digits", "latin_digits", "to_hijri", "from_hijri")
ARABIC_FONTS = script_labels.BY_ID["arabic"]["fonts"]


def register_functions():
    """Register (again) every Biruni label and calendar function that is missing."""
    script_labels.register_functions()
    calendars.register_functions()


def unregister_functions():
    for name in FUNCTIONS:
        if QgsExpression.isFunctionName(name):
            QgsExpression.unregisterFunction(name)
