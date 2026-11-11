"""Map Studio themes: one click restyles the map layers for a purpose (technical sheet, atlas, minimal,
dark presentation, black & white print, high contrast) - classes are kept, colours come from palettes that
stay readable for colour-blind readers, every layer gets a style for its role (roads, water, buildings,
parcels, boundaries, green areas, contours) and labels get a matching Arabic/Latin font pair with a halo.

Applying a theme stores each layer's previous style, so undo_theme() puts it back exactly.

Colour science used by the map doctor too: colour-vision-deficiency simulation (Machado, Oliveira and
Fernandes 2009, severity 1.0, applied in linear RGB) and the CIE76 colour difference in CIELAB (D65)."""
import math
import re

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsGradientColorRamp,
                       QgsGradientStop, QgsGraduatedSymbolRenderer, QgsLinePatternFillSymbolLayer,
                       QgsLineSymbol, QgsMapLayerStyle, QgsMarkerSymbol, QgsPalLayerSettings, QgsProject,
                       QgsRuleBasedRenderer, QgsSingleSymbolRenderer, QgsTextBufferSettings, QgsTextFormat,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase

from ..i18n import tr

BACKUP_KEY = "biruni/theme-backup"
THEME_KEY = "biruni/theme"
PROJECT_SCOPE = "biruni"

THEMES = {
    "technical": "Technical / engineering",
    "atlas": "Atlas (soft natural colours)",
    "minimal": "Minimal (light and muted)",
    "dark": "Dark presentation",
    "bw": "Black & white print (patterns instead of colours)",
    "brand": "Brand colours (from your logo)",
    "contrast": "High contrast (easy to read)",
}

# ---- palettes -----------------------------------------------------------------------------------------------------
OKABE_ITO = ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#999999"]
TOL_MUTED = ["#CC6677", "#332288", "#DDCC77", "#117733", "#88CCEE", "#882255", "#44AA99", "#999933", "#AA4499",
             "#DDDDDD"]
TOL_LIGHT = ["#77AADD", "#EE8866", "#EEDD88", "#FFAABB", "#99DDFF", "#44BB99", "#BBCC33", "#AAAA00", "#DDDDDD"]
SEQUENTIAL = {
    "viridis": ["#440154", "#3B528B", "#21918C", "#5EC962", "#FDE725"],
    "ylgnbu": ["#FFFFD9", "#C7E9B4", "#41B6C4", "#225EA8", "#081D58"],
    "blues": ["#F7FBFF", "#C6DBEF", "#6BAED6", "#2171B5", "#08306B"],
    "magma": ["#2C115F", "#721F81", "#B73779", "#F1605D", "#FCFDBF"],
    "greys": ["#F0F0F0", "#BDBDBD", "#969696", "#636363", "#252525"],
    "cividis": ["#00204D", "#31446B", "#666970", "#958F78", "#CBBA69", "#FFEA46"],
}

# theme → settings
SPEC = {
    "technical": dict(background="#FFFFFF", qualitative=TOL_MUTED, sequential="greys", fill_alpha=0.35,
                      outline="#3A3A3A", outline_w=0.18, line="#202020", line_w=0.25, label="#1E1E1E",
                      halo="#FFFFFF", halo_w=0.6, label_size=8.0, bold=False,
                      roles=dict(road=("#5A5A5A", 0.5), water=("#9EC9E2", "#3F7FA8"), building=("#D9D9D9", "#4D4D4D"),
                                 parcel=("#FFFFFF", "#202020"), boundary=("#202020", 0.6), green=("#DCEBD3", "#6B8F5E"),
                                 contour=("#A0785A", 0.18), rail=("#303030", 0.4))),
    "atlas": dict(background="#F6F2E8", qualitative=TOL_LIGHT, sequential="ylgnbu", fill_alpha=0.85,
                  outline="#FFFFFF", outline_w=0.2, line="#6E5B4B", line_w=0.4, label="#3B3226", halo="#FFFDF7",
                  halo_w=0.8, label_size=8.5, bold=False,
                  roles=dict(road=("#E8B04D", 0.8), water=("#A9D3EA", "#5E9CC2"), building=("#D8C7B4", "#A48C74"),
                             parcel=("#FBF6EA", "#B9A68E"), boundary=("#8C5A86", 0.7), green=("#BFDDA5", "#86B26A"),
                             contour=("#B48A63", 0.2), rail=("#6B6B6B", 0.5))),
    "minimal": dict(background="#FAFAFA", qualitative=TOL_LIGHT, sequential="blues", fill_alpha=0.6,
                    outline="#FFFFFF", outline_w=0.15, line="#9AA4AE", line_w=0.3, label="#4A5560", halo="#FAFAFA",
                    halo_w=0.7, label_size=8.0, bold=False,
                    roles=dict(road=("#C9CED3", 0.6), water=("#D6E6F2", "#B4CDE0"), building=("#E4E4E4", "#D0D0D0"),
                               parcel=("#FFFFFF", "#C2C8CE"), boundary=("#8D96A0", 0.4), green=("#E3EEDC", "#C9DCBE"),
                               contour=("#CDBBA8", 0.15), rail=("#B0B0B0", 0.3))),
    "dark": dict(background="#16191D", qualitative=OKABE_ITO, sequential="magma", fill_alpha=0.75,
                 outline="#16191D", outline_w=0.2, line="#8FA3B5", line_w=0.35, label="#ECEFF2", halo="#16191D",
                 halo_w=0.8, label_size=8.5, bold=False,
                 roles=dict(road=("#7C8792", 0.6), water=("#1F3A52", "#2E5A80"), building=("#30363D", "#4A525B"),
                            parcel=("#20252B", "#5C6773"), boundary=("#E6A84A", 0.6), green=("#24392C", "#3F6A4D"),
                            contour=("#6E5A48", 0.18), rail=("#9AA3AD", 0.4))),
    "bw": dict(background="#FFFFFF", qualitative=None, sequential="greys", fill_alpha=1.0, outline="#000000",
               outline_w=0.2, line="#000000", line_w=0.3, label="#000000", halo="#FFFFFF", halo_w=0.6,
               label_size=8.0, bold=False,
               roles=dict(road=("#000000", 0.5), water=("#FFFFFF", "#000000"), building=("#BFBFBF", "#000000"),
                          parcel=("#FFFFFF", "#000000"), boundary=("#000000", 0.7), green=("#FFFFFF", "#000000"),
                          contour=("#555555", 0.15), rail=("#000000", 0.5))),
    "contrast": dict(background="#FFFFFF", qualitative=OKABE_ITO, sequential="cividis", fill_alpha=0.9,
                     outline="#000000", outline_w=0.35, line="#000000", line_w=0.6, label="#000000",
                     halo="#FFFFFF", halo_w=1.2, label_size=10.0, bold=True,
                     roles=dict(road=("#000000", 0.9), water=("#56B4E9", "#0072B2"), building=("#E69F00", "#000000"),
                                parcel=("#FFFFFF", "#000000"), boundary=("#D55E00", 1.0), green=("#009E73", "#000000"),
                                contour=("#7F4F24", 0.3), rail=("#000000", 0.8))),
}
SPEC["brand"] = dict(SPEC["minimal"], qualitative=TOL_LIGHT)  # qualitative = the saved brand palette, at apply time

ARABIC_FONTS = ["Noto Kufi Arabic", "Noto Naskh Arabic", "Noto Sans Arabic", "Segoe UI", "Tahoma", "Arial"]
LATIN_FONTS = ["Noto Sans", "Segoe UI", "Calibri", "Arial", "Helvetica"]

# role keywords (English, Arabic, a few other languages) matched against layer and field names
ROLE_WORDS = {
    "road": r"road|street|highway|route|carriageway|lane|rue|calle|rua|strasse|straße|طريق|طرق|شارع|شوارع|محور",
    "water": r"water|river|lake|sea|canal|wadi|hydro|stream|reservoir|ميا?ه|نهر|بحر|بحيرة|قناة|وادي|ترعة|مصرف",
    "building": r"building|bldg|house|structure|footprint|edifice|bâtiment|edificio|gebäude|مبنى|مباني|منشآت|عمار",
    "parcel": r"parcel|plot|lot|cadast|property|land_?use|parcela|lote|flurst|قطع|قطعة|أراض|اراض|صك|مخطط",
    "boundary": r"boundar|border|admin|district|province|governorate|region|municipal|حدود|حي\b|أحياء|بلدية|محافظ|منطقة",
    "green": r"park|green|garden|forest|wood|vegetat|حديق|غاب|خضر|مزروع",
    "contour": r"contour|isoline|elev_?line|كنتور|خطوط_?الارتفاع",
    "rail": r"rail|metro|tram|train|سكة|قطار|مترو",
}


# ---- colour science -----------------------------------------------------------------------------------------------
_MACHADO = {
    "protan": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "deutan": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881)),
    "tritan": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900)),
}
DEFICIENCIES = tuple(_MACHADO)


def _rgb(color):
    if isinstance(color, QColor):
        return color.redF(), color.greenF(), color.blueF()
    c = QColor(color)
    return c.redF(), c.greenF(), c.blueF()


def _to_linear(v):
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _to_srgb(v):
    v = min(1.0, max(0.0, v))
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def simulate(color, deficiency):
    """The colour as seen with protanopia, deuteranopia or tritanopia (sRGB 0..1 tuple)."""
    lin = [_to_linear(v) for v in _rgb(color)]
    m = _MACHADO[deficiency]
    out = [sum(m[i][j] * lin[j] for j in range(3)) for i in range(3)]
    return tuple(_to_srgb(v) for v in out)


def to_lab(color):
    r, g, b = (_to_linear(v) for v in (_rgb(color) if not isinstance(color, tuple) else color))
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a, b):
    """CIE76 colour difference (about 2.3 = just noticeable; under 10 = easily confused on a map)."""
    la, lb = to_lab(a), to_lab(b)
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(la, lb)))


def relative_luminance(color):
    r, g, b = (_to_linear(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    """WCAG contrast ratio (1..21); 4.5 is the usual minimum for small text."""
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def confusable_pairs(colors, threshold=10.0):
    """[(i, j, deficiency, ΔE)] of colours that are distinct for normal vision but look alike for a reader with
    a colour-vision deficiency (ΔE under the threshold after simulation)."""
    out = []
    cols = [QColor(c) if not isinstance(c, QColor) else c for c in colors]
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            if delta_e(cols[i], cols[j]) < threshold:
                continue  # already alike for everyone: not a colour-vision problem
            for d in DEFICIENCIES:
                de = delta_e(simulate(cols[i], d), simulate(cols[j], d))
                if de < threshold:
                    out.append((i, j, d, de))
                    break
    return out


def safe_qualitative(n):
    """n colours that stay apart for the three common colour-vision deficiencies (Okabe-Ito, then Paul Tol)."""
    base = OKABE_ITO[:7] if n <= 7 else (TOL_MUTED[:9] if n <= 9 else OKABE_ITO[:7] + TOL_MUTED)
    return [base[i % len(base)] for i in range(n)]


def ramp(name_or_stops):
    stops = SEQUENTIAL.get(name_or_stops, name_or_stops) if isinstance(name_or_stops, str) else name_or_stops
    first, last = QColor(stops[0]), QColor(stops[-1])
    mids = [QgsGradientStop(i / (len(stops) - 1), QColor(c)) for i, c in enumerate(stops[1:-1], 1)]
    return QgsGradientColorRamp(first, last, False, mids)


def ramp_colors(name, n):
    r = ramp(name)
    return [r.color(i / max(n - 1, 1)).name() for i in range(n)]


# ---- roles --------------------------------------------------------------------------------------------------------
def detect_role(layer):
    """road / water / building / parcel / boundary / green / contour / rail, or None, from the layer's name,
    source and field names and its geometry."""
    if not isinstance(layer, QgsVectorLayer):
        return None
    text = " ".join([layer.name(), layer.source().split("|")[0].split("/")[-1].split("\\")[-1]]
                    + [f.name() for f in layer.fields()]).lower()
    gt = layer.geometryType()
    allowed = {Qgis.GeometryType.Line: ("road", "water", "boundary", "contour", "rail"),
               Qgis.GeometryType.Polygon: ("water", "building", "parcel", "boundary", "green"),
               Qgis.GeometryType.Point: ()}.get(gt, ())
    name = layer.name().lower()
    for role in ("contour", "rail", "road", "water", "building", "parcel", "green", "boundary"):
        if role in allowed and re.search(ROLE_WORDS[role], name, re.I):
            return role
    for role in ("contour", "rail", "road", "water", "building", "parcel", "green", "boundary"):
        if role in allowed and re.search(ROLE_WORDS[role], text, re.I):
            return role
    return None


# ---- symbols ------------------------------------------------------------------------------------------------------
def _fill(color, outline, width, alpha=1.0):
    c = QColor(color)
    c.setAlphaF(alpha)
    return QgsFillSymbol.createSimple({"color": c.name(QColor.NameFormat.HexArgb), "outline_color": outline,
                                       "outline_width": f"{width}", "outline_width_unit": "MM"})


def _hatch(index, outline="#000000", width=0.2):
    """Black & white fills: a white base with a different line pattern per class."""
    sym = _fill("#FFFFFF", outline, width)
    angles = [45, 135, 0, 90, 30, 150, 60, 120]
    spacing = [1.6, 1.6, 2.2, 2.2, 1.2, 1.2, 2.8, 2.8]
    k = index % len(angles)
    if index >= 1:
        hatch = QgsLinePatternFillSymbolLayer()
        hatch.setLineAngle(angles[k])
        hatch.setDistance(spacing[k])
        hatch.setDistanceUnit(Qgis.RenderUnit.Millimeters)
        hatch.setLineWidth(0.15)
        hatch.setColor(QColor("#000000"))
        sym.appendSymbolLayer(hatch)
    return sym


def _line(color, width, dash=None):
    props = {"line_color": color, "line_width": f"{width}", "line_width_unit": "MM", "capstyle": "round",
             "joinstyle": "round"}
    if dash:
        props.update({"use_custom_dash": "1", "customdash": dash, "customdash_unit": "MM"})
    return QgsLineSymbol.createSimple(props)


_BW_DASHES = [None, "2;1", "0.5;1", "3;1;0.5;1", "1;1", "4;2"]
_BW_SHAPES = ["circle", "square", "triangle", "diamond", "cross", "star", "pentagon", "hexagon"]


def _marker(color, size, outline="#FFFFFF", shape="circle"):
    return QgsMarkerSymbol.createSimple({"name": shape, "color": color, "outline_color": outline,
                                         "size": f"{size}", "size_unit": "MM", "outline_width": "0.2"})


def _class_symbol(layer, spec, index, color, theme):
    gt = layer.geometryType()
    if gt == Qgis.GeometryType.Polygon:
        return _hatch(index, spec["outline"], spec["outline_w"]) if theme == "bw" else \
            _fill(color, spec["outline"], spec["outline_w"], spec["fill_alpha"])
    if gt == Qgis.GeometryType.Line:
        if theme == "bw":
            return _line("#000000", spec["line_w"], _BW_DASHES[index % len(_BW_DASHES)])
        return _line(color, max(spec["line_w"], 0.4))
    if theme == "bw":
        return _marker("#000000", 2.4, "#FFFFFF", _BW_SHAPES[index % len(_BW_SHAPES)])
    return _marker(color, 2.6 if theme == "contrast" else 2.2, "#000000" if theme == "contrast" else "#FFFFFF")


def _single_symbol(layer, spec, theme):
    gt = layer.geometryType()
    role = detect_role(layer)
    roles = spec["roles"]
    if gt == Qgis.GeometryType.Polygon:
        if role in ("water", "building", "parcel", "green"):
            fill, outline = roles[role]
            if theme == "bw" and role == "water":
                return _hatch(4, "#000000", spec["outline_w"])
            if theme == "bw" and role == "green":
                return _hatch(6, "#000000", spec["outline_w"])
            return _fill(fill, outline, spec["outline_w"])
        if role == "boundary":
            color, width = roles["boundary"]
            sym = _fill("#00000000", color, width)
            sym.symbolLayer(0).setBrushStyle(Qt.BrushStyle.NoBrush)
            return sym
        color = spec["qualitative"][0] if spec["qualitative"] else "#FFFFFF"
        return _hatch(0) if theme == "bw" else _fill(color, spec["outline"], spec["outline_w"], spec["fill_alpha"])
    if gt == Qgis.GeometryType.Line:
        if role in ("road", "boundary", "contour", "rail"):
            color, width = roles[role]
            dash = "3;1.5;0.5;1.5" if role == "boundary" else ("4;1" if role == "rail" else None)
            return _line(color, width, dash)
        if role == "water":
            return _line(roles["water"][1], 0.5)
        return _line(spec["line"], spec["line_w"])
    color = spec["qualitative"][1] if spec["qualitative"] else "#000000"
    return _marker(color, 2.2)


def _recolour_renderer(layer, spec, theme):
    r = layer.renderer()
    if isinstance(r, QgsCategorizedSymbolRenderer):
        r = r.clone()
        cats = r.categories()
        palette = spec["qualitative"] or ["#000000"]
        cols = safe_qualitative(len(cats)) if theme != "bw" and len(cats) > len(palette) else palette
        cols = [cols[i % len(cols)] for i in range(len(cats))]
        if theme != "bw":  # categories with a meaning (residential, park, water…) keep their usual colours
            from .palettes import classify, SEMANTIC_COLOURS

            meant = [classify(c.value()) for c in cats]
            if cats and sum(1 for m in meant if m) >= 0.6 * len(cats):
                rest = iter([c for c in palette if c not in SEMANTIC_COLOURS.values()] or palette)
                cols = [SEMANTIC_COLOURS[m] if m else next(rest, "#DDDDDD") for m in meant]
        for i, _cat in enumerate(cats):
            r.updateCategorySymbol(i, _class_symbol(layer, spec, i, cols[i], theme))
        layer.setRenderer(r)
        return "categorized"
    if isinstance(r, QgsGraduatedSymbolRenderer):
        r = r.clone()
        n = len(r.ranges())
        cols = ramp_colors(spec["sequential"], max(n, 2))
        for i in range(n):
            sym = _class_symbol(layer, spec, i, cols[i], theme if theme != "bw" else "technical")
            if theme == "bw" and layer.geometryType() == Qgis.GeometryType.Polygon:
                sym = _fill(cols[i], "#000000", spec["outline_w"])
            r.updateRangeSymbol(i, sym)
        layer.setRenderer(r)
        return "graduated"
    if isinstance(r, QgsRuleBasedRenderer):
        r = r.clone()
        rules = [x for x in r.rootRule().descendants() if x.symbol() is not None]
        cols = safe_qualitative(len(rules)) if spec["qualitative"] else ["#000000"] * len(rules)
        for i, rule in enumerate(rules):
            rule.setSymbol(_class_symbol(layer, spec, i, cols[i], theme))
        layer.setRenderer(r)
        return "rules"
    layer.setRenderer(QgsSingleSymbolRenderer(_single_symbol(layer, spec, theme)))
    return detect_role(layer) or "single"


# ---- labels -------------------------------------------------------------------------------------------------------
def _families():
    return set(QFontDatabase.families())


def font_pair():
    """(Arabic font, Latin font) installed here, in order of preference."""
    fams = _families()
    ar = next((f for f in ARABIC_FONTS if f in fams), None)
    try:
        from ..script_labels import font_covers, pick_font

        if ar is None or not font_covers(ar, "arabic"):
            ar = pick_font("arabic") or ar
    except Exception:  # noqa: BLE001
        pass
    la = next((f for f in LATIN_FONTS if f in fams), None)
    return ar or "Arial", la or "Arial"


def _theme_labels(layer, spec, theme):
    if not layer.labelsEnabled() or layer.labeling() is None:
        return False
    lab = layer.labeling()
    if not isinstance(lab, QgsVectorLayerSimpleLabeling):
        return False
    s = QgsPalLayerSettings(lab.settings())
    try:
        from ..script_labels import _label_values, detect_scripts

        scripts = detect_scripts(_label_values(layer, s, 20))
    except Exception:  # noqa: BLE001
        scripts = {}
    ar_font, la_font = font_pair()
    arabic = bool(scripts) and max(scripts, key=scripts.get) in ("arabic", "hebrew")
    fmt = QgsTextFormat(s.format())
    f = QFont(fmt.font())
    f.setFamily(ar_font if arabic else la_font)
    f.setBold(spec["bold"])
    fmt.setFont(f)
    fmt.setNamedStyle("")
    fmt.setSize(max(fmt.size() if fmt.sizeUnit() == Qgis.RenderUnit.Points else 0, spec["label_size"]))
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(spec["label"]))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(spec["halo_w"])
    buf.setSizeUnit(Qgis.RenderUnit.Millimeters)
    buf.setColor(QColor(spec["halo"]))
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    return True


# ---- public API ---------------------------------------------------------------------------------------------------
def theme_label(theme_id):
    return tr(THEMES.get(theme_id, theme_id))


def theme_background(theme_id):
    return SPEC[theme_id]["background"]


def apply_theme(layers, theme_id, purpose=None, project=None, background=True):
    """Restyle vector layers with a theme. The previous style of each layer is kept (undo_theme puts it back).
    Returns [(layer name, what was done)]."""
    if theme_id not in SPEC:
        raise ValueError(theme_id)
    spec = SPEC[theme_id]
    if theme_id == "brand":
        from .palettes import brand_palette

        spec = dict(spec, qualitative=brand_palette() or TOL_LIGHT)
    project = project or QgsProject.instance()
    done = []
    for layer in layers:
        if not isinstance(layer, QgsVectorLayer) or not layer.isSpatial():
            continue
        if layer.customProperty(BACKUP_KEY) is None:  # keep the user's own style, not an earlier theme
            style = QgsMapLayerStyle()
            style.readFromLayer(layer)
            layer.setCustomProperty(BACKUP_KEY, style.xmlData())
        what = _recolour_renderer(layer, spec, theme_id)
        labelled = _theme_labels(layer, spec, theme_id)
        layer.setCustomProperty(THEME_KEY, theme_id)
        layer.triggerRepaint()
        done.append((layer.name(), what + (" + labels" if labelled else "")))
    if background:
        old, ok = project.readEntry(PROJECT_SCOPE, "theme-background-backup")
        if not ok or not old:
            project.writeEntry(PROJECT_SCOPE, "theme-background-backup", project.backgroundColor().name())
        project.setBackgroundColor(QColor(spec["background"]))
    return done


def undo_theme(layers, project=None):
    """Put back the styles the layers had before the first theme was applied. Returns the number restored."""
    project = project or QgsProject.instance()
    n = 0
    for layer in layers:
        xml = layer.customProperty(BACKUP_KEY) if isinstance(layer, QgsVectorLayer) else None
        if not xml:
            continue
        QgsMapLayerStyle(xml).writeToLayer(layer)
        layer.removeCustomProperty(BACKUP_KEY)
        layer.removeCustomProperty(THEME_KEY)
        layer.triggerRepaint()
        n += 1
    old, ok = project.readEntry(PROJECT_SCOPE, "theme-background-backup")
    if ok and old:
        project.setBackgroundColor(QColor(old))
        project.removeEntry(PROJECT_SCOPE, "theme-background-backup")
    return n


def current_theme(layer):
    return layer.customProperty(THEME_KEY) if isinstance(layer, QgsVectorLayer) else None

