"""Symbol libraries for Biruni maps: survey, utilities, road signs and places (POIs).

Every symbol is an SVG with QGIS colour parameters (param(fill), param(outline), param(outline-width)),
so one file serves any colour. Survey, utility and road-sign symbols are drawn here; place symbols put a
Tabler icon (MIT licence, bundled in _vendor) on a coloured disc. The SVGs live in resources/symbols/ and
are written once by generate() (also called on demand), and install_style() adds them to the QGIS style
library under the tag "Biruni".
"""
import os
import re

from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLinePatternFillSymbolLayer,
                       QgsMarkerSymbol, QgsRendererCategory, QgsRuleBasedRenderer, QgsSimpleMarkerSymbolLayer,
                       QgsStyle, QgsSvgMarkerSymbolLayer, QgsSymbol)
from qgis.PyQt.QtGui import QColor

from .. import arabic_text
from ..i18n import tr

HERE = os.path.dirname(os.path.dirname(__file__))
SYMBOL_DIR = os.path.join(HERE, "resources", "symbols")
TABLER_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "_vendor", "package", "icons", "outline"))
TAG = "Biruni"

GROUPS = {"survey": "Survey", "utility": "Utilities", "road": "Road signs and markings", "poi": "Places"}

_F = 'fill="param(fill) {fill}" stroke="param(outline) {line}" stroke-width="param(outline-width) 1.2"'
_L = 'fill="none" stroke="param(outline) {line}" stroke-width="param(outline-width) 1.2"'
_D = 'fill="param(outline) {line}" stroke="none"'

# id -> (group, English label, default fill, default outline, svg body (24×24))
DRAWN = {
    "benchmark": ("survey", "Bench mark", "#ffffff", "#000000",
                  f'<path d="M12 3L21 19H3Z" {_F}/><circle cx="12" cy="13.5" r="2" {_D}/>'),
    "control_point": ("survey", "Control point", "#ffffff", "#000000",
                      f'<circle cx="12" cy="12" r="8" {_F}/><path d="M12 2v20M2 12h20" {_L}/>'
                      f'<circle cx="12" cy="12" r="1.6" {_D}/>'),
    "boundary_marker": ("survey", "Boundary marker (beacon)", "#ffffff", "#c0392b",
                        f'<rect x="5" y="5" width="14" height="14" {_F}/><path d="M5 5l14 14M19 5L5 19" {_L}/>'),
    "traverse_station": ("survey", "Traverse station", "#ffffff", "#000000",
                         f'<path d="M12 4L20 18H4Z" {_F}/><circle cx="12" cy="13" r="3" {_L}/>'),
    "spot_height": ("survey", "Spot height", "#000000", "#000000",
                    f'<path d="M8 8l8 8M16 8l-8 8" {_L}/><circle cx="12" cy="12" r="1.5" {_D}/>'),
    "well": ("survey", "Well", "#9fd3f5", "#1f5f8b",
             f'<circle cx="12" cy="12" r="8" {_F}/><circle cx="12" cy="12" r="3.5" {_L}/>'),
    "water_valve": ("utility", "Water valve", "#ffffff", "#1f5f8b",
                    f'<circle cx="12" cy="12" r="9" {_F}/><path d="M5 8l14 8V8L5 16Z" {_F}/>'),
    "hydrant": ("utility", "Fire hydrant", "#e74c3c", "#7b1d14",
                f'<rect x="8" y="7" width="8" height="13" rx="1" {_F}/><path d="M6 11h12M12 3v4" {_L}/>'),
    "manhole": ("utility", "Manhole", "#ffffff", "#333333",
                f'<circle cx="12" cy="12" r="9" {_F}/><path d="M6 9h12M5 12h14M6 15h12" {_L}/>'),
    "sewer": ("utility", "Sewer node", "#8d6e63", "#3e2723",
              f'<circle cx="12" cy="12" r="9" {_F}/><path d="M7 12h10M13 8l4 4-4 4" {_L}/>'),
    "electric_pole": ("utility", "Electric pole", "#ffffff", "#e67e22",
                      f'<circle cx="12" cy="12" r="9" {_F}/><path d="M12 5v14M7 8h10" {_L}/>'),
    "transformer": ("utility", "Transformer", "#f9e79f", "#b9770e",
                    f'<rect x="4" y="4" width="16" height="16" {_F}/><path d="M7 12l2-4 2 8 2-8 2 8 2-4" {_L}/>'),
    "telecom": ("utility", "Telecom", "#ffffff", "#6c3483",
                f'<circle cx="12" cy="12" r="9" {_F}/><path d="M12 9v9M9 18h6M8.5 6.5a5 5 0 0 1 7 0M6.5 4.5a8 8 0 0 1 11 0" {_L}/>'),
    "stop": ("road", "Stop sign", "#c0392b", "#ffffff",
             f'<path d="M8 2h8l6 6v8l-6 6H8l-6-6V8Z" {_F}/>'),
    "give_way": ("road", "Give way", "#ffffff", "#c0392b",
                 f'<path d="M3 4h18L12 21Z" {_F}/>'),
    "no_entry": ("road", "No entry", "#c0392b", "#ffffff",
                 f'<circle cx="12" cy="12" r="10" {_F}/><rect x="5" y="10" width="14" height="4" fill="#ffffff"/>'),
    "speed_limit": ("road", "Speed limit", "#ffffff", "#c0392b",
                    f'<circle cx="12" cy="12" r="9" fill="param(fill) #ffffff" stroke="param(outline) #c0392b" '
                    f'stroke-width="3"/>'),
    "arrow_marking": ("road", "Arrow road marking", "#ffffff", "#333333",
                      f'<path d="M10 22V10H6l6-8 6 8h-4v12Z" {_F}/>'),
    "crossing": ("road", "Pedestrian crossing", "#333333", "#ffffff",
                 f'<rect x="2" y="2" width="20" height="20" {_F}/><path d="M5 4v16M9 4v16M13 4v16M17 4v16" '
                 f'stroke="#ffffff" stroke-width="2"/>'),
}

# id -> (Tabler icon, English label, default disc colour, keywords (English and Arabic))
POIS = {
    "mosque": ("building-mosque", "Mosque", "#1e8449", ("mosque", "masjid", "jamia", "مسجد", "جامع", "مصلى")),
    "church": ("building-church", "Church", "#5d6d7e", ("church", "cathedral", "كنيسة", "كاتدرائية")),
    "school": ("school", "School", "#d68910", ("school", "kindergarten", "مدرسة", "روضة", "حضانة", "ابتدائية",
                                               "متوسطة", "ثانوية")),
    "university": ("book", "University / college", "#a04000", ("university", "college", "institute", "جامعة",
                                                              "كلية", "معهد")),
    "hospital": ("building-hospital", "Hospital", "#c0392b", ("hospital", "مستشفى", "مستشفي")),
    "clinic": ("stethoscope", "Clinic / health centre", "#e74c3c", ("clinic", "health center", "health centre",
                                                                    "مستوصف", "عيادة", "مركز صحي", "وحدة صحية")),
    "pharmacy": ("pill", "Pharmacy", "#27ae60", ("pharmacy", "chemist", "صيدلية")),
    "fuel": ("gas-station", "Fuel station", "#2c3e50", ("fuel", "gas station", "petrol", "محطة وقود", "بنزينة",
                                                         "محطة بنزين")),
    "police": ("shield", "Police", "#1f3a93", ("police", "شرطة", "قسم شرطة", "مخفر")),
    "fire": ("flame", "Fire station", "#e67e22", ("fire station", "civil defence", "civil defense", "دفاع مدني",
                                                  "مطافي", "اطفاء", "إطفاء")),
    "park": ("trees", "Park / garden", "#229954", ("park", "garden", "حديقة", "منتزه", "متنزه")),
    "market": ("shopping-cart", "Market / supermarket", "#8e44ad", ("market", "supermarket", "hypermarket",
                                                                      "سوق", "سوبر ماركت", "هايبر", "بقالة")),
    "shop": ("building-store", "Shop / mall", "#9b59b6", ("shop", "store", "mall", "محل", "متجر", "مول",
                                                          "مركز تجاري")),
    "bank": ("building-bank", "Bank", "#117a65", ("bank", "atm", "بنك", "مصرف", "صراف")),
    "hotel": ("bed", "Hotel", "#2e86c1", ("hotel", "motel", "فندق", "شقق فندقية")),
    "restaurant": ("tools-kitchen-2", "Restaurant", "#cb4335", ("restaurant", "مطعم")),
    "cafe": ("coffee", "Café", "#6e2c00", ("cafe", "café", "coffee", "مقهى", "كافيه", "كوفي")),
    "parking": ("parking", "Parking", "#2471a3", ("parking", "car park", "موقف", "مواقف", "باركنج")),
    "bus": ("bus", "Bus stop / station", "#1a5276", ("bus", "حافلة", "باص", "اتوبيس", "أتوبيس", "موقف حافلات")),
    "airport": ("plane", "Airport", "#566573", ("airport", "airfield", "مطار")),
    "train": ("train", "Railway station", "#424949", ("railway", "train", "metro", "قطار", "سكة حديد", "مترو")),
    "port": ("anchor", "Port", "#1b4f72", ("port", "harbour", "harbor", "ميناء", "مرفأ")),
    "government": ("building", "Government office", "#515a5a", ("government", "ministry", "municipality",
                                                                "amana", "baladiya", "وزارة", "امانة", "أمانة",
                                                                "بلدية", "محافظة", "إمارة", "امارة", "حكومي")),
    "stadium": ("building-stadium", "Stadium / sports", "#28b463", ("stadium", "sports", "ملعب", "استاد",
                                                                    "صالة رياضية", "نادي")),
    "post": ("mailbox", "Post office", "#b9770e", ("post office", "بريد")),
    "water": ("droplet", "Water", "#2e86c1", ("water tank", "reservoir", "خزان", "مياه")),
    "pin": ("map-pin", "Place", "#7f8c8d", ()),
}

_ARABIC_HINTS = {k: [arabic_text.normalise(w) for w in v[3]] for k, v in POIS.items()}


def path(symbol_id):
    group = DRAWN[symbol_id][0] if symbol_id in DRAWN else "poi"
    return os.path.join(SYMBOL_DIR, group, f"{symbol_id}.svg")


def _svg(body):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
            'stroke-linecap="round" stroke-linejoin="round">' + body + "</svg>\n")


def _tabler_body(name):
    """The drawing of a Tabler outline icon (its own frame path removed)."""
    with open(os.path.join(TABLER_DIR, f"{name}.svg"), encoding="utf-8") as f:
        text = f.read()
    inner = text[text.index(">", text.index("<svg")) + 1:text.rindex("</svg>")]
    return re.sub(r'<path stroke="none" d="M0 0h24v24H0z" fill="none"\s*/>', "", inner).strip()


def generate(force=False):
    """Write every SVG into resources/symbols (skips existing files unless force). Returns the count written."""
    n = 0
    for sid, (group, _label, fill, line, body) in DRAWN.items():
        p = path(sid)
        if force or not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(_svg(body.format(fill=fill, line=line) if "{" in body else body))
            n += 1
    for sid, (icon, _label, colour, _kw) in POIS.items():
        p = path(sid)
        if not force and os.path.exists(p):
            continue
        try:
            inner = _tabler_body(icon)
        except OSError:
            continue
        body = (f'<circle cx="12" cy="12" r="11.5" fill="param(fill) {colour}" '
                f'stroke="param(outline) #ffffff" stroke-width="param(outline-width) 1"/>'
                f'<g transform="translate(5.4 5.4) scale(0.55)" fill="none" stroke="param(outline) #ffffff" '
                f'stroke-width="2">{inner}</g>')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(_svg(body))
        n += 1
    return n


def default_colours(symbol_id):
    if symbol_id in DRAWN:
        return DRAWN[symbol_id][2], DRAWN[symbol_id][3]
    return POIS[symbol_id][2], "#ffffff"


def label(symbol_id):
    return tr(DRAWN[symbol_id][1] if symbol_id in DRAWN else POIS[symbol_id][1])


def all_ids():
    return list(DRAWN) + list(POIS)


def marker(symbol_id, size=6.0, fill=None, outline=None):
    """A marker symbol drawing one library SVG (colours default to the symbol's own)."""
    if not os.path.exists(path(symbol_id)):
        generate()
    f0, o0 = default_colours(symbol_id)
    layer = QgsSvgMarkerSymbolLayer(path(symbol_id), size)
    layer.setFillColor(QColor(fill or f0))
    layer.setStrokeColor(QColor(outline or o0))
    layer.setStrokeWidth(0.2)
    sym = QgsMarkerSymbol()
    sym.changeSymbolLayer(0, layer)
    return sym


HATCHES = {"diagonal": 45.0, "back_diagonal": -45.0, "horizontal": 0.0, "vertical": 90.0}


def hatch_fill(colour="#333333", angle=45.0, spacing=2.0, width=0.25, cross=False, outline="#333333"):
    """A hatched polygon symbol (print in black and white, or show 'planned' areas)."""
    sym = QgsFillSymbol.createSimple({"color": "255,255,255,0", "outline_color": outline, "outline_width": "0.3"})
    for a in ((angle, angle + 90.0) if cross else (angle,)):
        lp = QgsLinePatternFillSymbolLayer()
        lp.setLineAngle(a)
        lp.setDistance(spacing)
        lp.setColor(QColor(colour))
        lp.subSymbol().setWidth(width)
        sym.appendSymbolLayer(lp)
    return sym


def install_style(style=None):
    """Add every symbol (and the hatches) to the QGIS style library, tagged "Biruni" and with its group.
    Returns the number of symbols added or replaced."""
    style = style or QgsStyle.defaultStyle()
    generate()
    n = 0
    for sid in all_ids():
        name = f"Biruni - {DRAWN[sid][1] if sid in DRAWN else POIS[sid][1]}"
        group = DRAWN[sid][0] if sid in DRAWN else "poi"
        if style.addSymbol(name, marker(sid), True):
            style.tagSymbol(QgsStyle.StyleEntity.SymbolEntity, name, [TAG, GROUPS[group]])
            n += 1
    for hid, angle in HATCHES.items():
        name = f"Biruni - hatch {hid.replace('_', ' ')}"
        if style.addSymbol(name, hatch_fill(angle=angle), True):
            style.tagSymbol(QgsStyle.StyleEntity.SymbolEntity, name, [TAG, "Hatches"])
            n += 1
    return n


def symbol_for_value(text):
    """The place symbol id that suits a name or a type («مسجد الراجحي» → mosque, "Central Bank" → bank);
    None when nothing matches. Longer keywords win (a "health center" is a clinic, not a "center")."""
    if text is None:
        return None
    key = arabic_text.normalise(str(text))
    if not key:
        return None
    best, best_len = None, 0
    for sid, words in _ARABIC_HINTS.items():
        for w in words:
            if not w:
                continue
            if re.search(r"(^|\s)" + re.escape(w) + r"($|\s)", key) or (len(w) >= 4 and w in key):
                if len(w) > best_len:
                    best, best_len = sid, len(w)
    return best


def apply_icons(layer, field, size=6.0, fallback="pin"):
    """Categorized icons by the values of a field (each value gets its matching place symbol).
    Returns {value: symbol id}."""
    idx = layer.fields().indexOf(field)
    if idx < 0:
        raise ValueError(tr("The layer has no field {f}.").format(f=field))
    values = sorted({v for v in layer.uniqueValues(idx) if v not in (None, "") and str(v) != "NULL"}, key=str)
    chosen, cats = {}, []
    for v in values:
        sid = symbol_for_value(v) or fallback
        chosen[v] = sid
        cats.append(QgsRendererCategory(v, marker(sid, size), str(v)))
    cats.append(QgsRendererCategory(None, marker(fallback, size * 0.8), tr("Other")))
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
    layer.triggerRepaint()
    return chosen


def scale_dependent(layer, threshold=50000, dot_size=1.6, dot_colour="#555555", label_threshold=None):
    """Simple dots when zoomed out (scale 1:threshold and smaller), the full symbols when zoomed in; labels
    (if any) only appear below label_threshold (default: the same threshold)."""
    current = layer.renderer().clone() if layer.renderer() else None
    root = QgsRuleBasedRenderer.Rule(None)
    dot = QgsMarkerSymbol()
    dl = QgsSimpleMarkerSymbolLayer()
    dl.setSize(dot_size)
    dl.setColor(QColor(dot_colour))
    dl.setStrokeColor(QColor("#ffffff"))
    dl.setStrokeWidth(0.1)
    dot.changeSymbolLayer(0, dl)
    far = QgsRuleBasedRenderer.Rule(dot, 0, 0, "", tr("Zoomed out"))
    far.setMinimumScale(0)            # no limit when zooming out
    far.setMaximumScale(threshold)    # shown while the scale denominator is above the threshold
    root.appendChild(far)
    if isinstance(current, QgsCategorizedSymbolRenderer):
        near = QgsRuleBasedRenderer.Rule(None, 0, 0, "", tr("Zoomed in"))
        near.setMinimumScale(threshold)
        near.setMaximumScale(0)
        for cat in current.categories():
            if not cat.renderState():
                continue
            v = cat.value()
            if v is None or v == "" or str(v) == "NULL":
                expr = "ELSE"
            else:
                expr = f'"{current.classAttribute()}" = ' + (str(v) if isinstance(v, (int, float))
                                                             else "'" + str(v).replace("'", "''") + "'")
            near.appendChild(QgsRuleBasedRenderer.Rule(cat.symbol().clone(), 0, 0, expr, cat.label()))
    else:
        sym = (current.symbols(None)[0].clone() if current is not None and current.symbols(None)
               else QgsSymbol.defaultSymbol(layer.geometryType()))
        near = QgsRuleBasedRenderer.Rule(sym, 0, 0, "", tr("Zoomed in"))
        near.setMinimumScale(threshold)
        near.setMaximumScale(0)
    root.appendChild(near)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    lt = label_threshold or threshold
    if layer.labeling() is not None:
        lab = layer.labeling()
        settings = lab.settings() if hasattr(lab, "settings") else None
        if settings is not None:
            s = type(settings)(settings)
            s.scaleVisibility = True
            s.minimumScale = lt
            s.maximumScale = 0
            lab.setSettings(s)
    layer.triggerRepaint()
    return threshold
