"""Coordinate grids and graticules for print layouts, right for every market.

A projected grid (UTM or the project's metric system) and a geographic
graticule (degrees-minutes-seconds) can be drawn together; a map that crosses
two UTM zones can keep one zone (extended), show each zone's grid on its own
side, or fall back to a geographic graticule. Annotations are written the
local way: hemisphere letters (N/S/L/O in Portuguese, N/S/E/O in Spanish and
French, ش ج ق غ in Arabic, ش ج خ ب in Persian…), thousands separators and
Arabic-Indic or Persian digits. Intervals are picked from the scale.
"""
import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsLayoutItemLabel,
                       QgsLayoutItemMapGrid, QgsLayoutPoint, QgsLayoutSize, QgsPointXY, QgsProject,
                       QgsTextFormat)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QFont

from .i18n import tr

G = QgsLayoutItemMapGrid

# style id -> description (shown in the map sheet dialog)
STYLES = {
    "auto": "Automatic (metric grid on projected maps, degrees on geographic maps)",
    "projected": "Metric grid (UTM / project system)",
    "geographic": "Geographic graticule (degrees, minutes, seconds)",
    "both": "Metric grid with a geographic frame (topographic sheet)",
    "ticks": "Frame ticks only (no lines across the map)",
    "none": "No grid",
}
# what to do when the map crosses a UTM zone boundary
ZONE_MODES = {
    "extended": "One zone, extended over the boundary",
    "per_zone": "Each zone's grid on its own side",
    "geographic": "Geographic graticule instead",
}

# hemisphere letters: (north, south, east, west)
GRID_LETTERS = {
    "en": ("N", "S", "E", "W"), "pt": ("N", "S", "L", "O"), "es": ("N", "S", "E", "O"),
    "fr": ("N", "S", "E", "O"), "it": ("N", "S", "E", "O"), "de": ("N", "S", "O", "W"),
    "nl": ("N", "Z", "O", "W"), "tr": ("K", "G", "D", "B"), "id": ("U", "S", "T", "B"),
    "ru": ("С", "Ю", "В", "З"), "uk": ("Пн", "Пд", "Сх", "Зх"), "ar": ("ش", "ج", "ق", "غ"),
    "fa": ("ش", "ج", "خ", "ب"), "ur": ("ش", "ج", "م", "مغ"),
}
# (thousands separator, decimal separator)
SEPARATORS = {
    "en": (",", "."), "pt": (".", ","), "es": (".", ","), "it": (".", ","), "de": (".", ","), "nl": (".", ","),
    "id": (".", ","), "tr": (".", ","), "fr": (" ", ","), "ru": (" ", ","), "uk": (" ", ","),
    "ar": ("٬", "٫"), "fa": ("٬", "٫"), "ur": (",", "."),
}
DIGITS = {"ar": "٠١٢٣٤٥٦٧٨٩", "fa": "۰۱۲۳۴۵۶۷۸۹", "ur": "۰۱۲۳۴۵۶۷۸۹"}
RTL = {"ar", "fa", "ur", "he"}
LRM = "‎"

# geographic intervals in degrees: 1", 2", 5", 10", 15", 30", 1', 2', 5', 10', 15', 30', 1°, 2°, 5°, 10°, 15°, 30°
GEO_INTERVALS = [1 / 3600, 2 / 3600, 5 / 3600, 10 / 3600, 15 / 3600, 30 / 3600, 1 / 60, 2 / 60, 5 / 60, 10 / 60,
                 15 / 60, 30 / 60, 1, 2, 5, 10, 15, 30]


def _lang(lang):
    return (lang or "en").split("_")[0].lower()


def nice_interval(raw):
    """1, 2, 2.5, 5 × 10ⁿ at or above ``raw``."""
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag + 1e-9:
            return m * mag
    return 10 * mag


def projected_interval(span, lines=5):
    """A round interval (map units) giving about ``lines`` grid lines across ``span``."""
    return nice_interval(span / max(lines, 1))


def geographic_interval(span_deg, lines=4):
    """A round angular interval (degrees, a whole number of seconds/minutes/degrees) for ``span_deg``."""
    target = span_deg / max(lines, 1)
    for iv in GEO_INTERVALS:
        if iv >= target - 1e-12:
            return iv
    return GEO_INTERVALS[-1]


def _digit_map(lang):
    """QGIS expression arrays that turn 0-9 into the language's digits (empty when Latin digits)."""
    d = DIGITS.get(_lang(lang))
    if not d:
        return None
    src = ",".join(f"'{k}'" for k in "0123456789")
    dst = ",".join(f"'{c}'" for c in d)
    return f"array({src})", f"array({dst})"


def _localise(expr, lang):
    """Wrap a text expression: digits of the language, and left-to-right marks after symbols so that
    right-to-left languages keep 30°15'10" in order."""
    lang = _lang(lang)
    dm = _digit_map(lang)
    if dm:
        expr = f"replace({expr}, {dm[0]}, {dm[1]})"
    if lang in RTL:
        expr = (f"replace({expr}, array('°', '''', '\"', '.', ','), "
                f"array('°'||char(8206), ''''||char(8206), '\"'||char(8206), '.'||char(8206), ','||char(8206)))")
    return expr


def _number(expr_value, decimals, lang):
    """A number in the language's grouping and decimal style (as a QGIS expression)."""
    thousands, dec = SEPARATORS.get(_lang(lang), SEPARATORS["en"])
    base = f"format_number({expr_value}, {int(decimals)}, 'en')"  # 1,234,567.89
    return (f"replace(replace(replace({base}, ',', '§'), '.', '{dec}'), '§', '{thousands}')"
            if (thousands, dec) != (",", ".") else base)


def projected_expression(lang="en", decimals=0, suffix=True):
    """Annotation expression for a metric grid: 7.420.000 N, 330.000 L (pt), ٣٣٠٬٠٠٠ ق (ar)…"""
    n, s, e, w = GRID_LETTERS.get(_lang(lang), GRID_LETTERS["en"])
    num = _number("abs(@grid_number)", decimals, lang)
    if suffix:
        hemi = (f"if(@grid_axis = 'x', if(@grid_number < 0, '{w}', '{e}'), "
                f"if(@grid_number < 0, '{s}', '{n}'))")
        text = f"{num} || ' ' || {hemi}"
    else:
        text = f"if(@grid_number < 0, '-', '') || {num}"
    return _localise(text, lang)


def geographic_expression(lang="en", interval=None, seconds_decimals=0):
    """Annotation expression for a graticule: 46°30'00" O (pt), 24°45' ش (ar)… Minutes or seconds are left
    out when the interval is a whole number of degrees or minutes."""
    lang = _lang(lang)
    n, s, e, w = GRID_LETTERS.get(lang, GRID_LETTERS["en"])
    _t, dec = SEPARATORS.get(lang, SEPARATORS["en"])
    show_min = interval is None or abs(interval - round(interval)) > 1e-9
    show_sec = interval is None or abs(interval * 60 - round(interval * 60)) > 1e-9
    sd = int(seconds_decimals) if show_sec else 0
    tot = f"round(abs(@grid_number) * 3600, {sd})"
    parts = "to_string(@d) || '°'"
    if show_min:
        parts += " || lpad(to_string(@m), 2, '0') || ''''"
    if show_sec:
        sec = f"format_number(@s, {sd}, 'en')"
        if dec != ".":
            sec = f"replace({sec}, '.', '{dec}')"
        parts += f" || lpad({sec}, {2 + (sd + 1 if sd else 0)}, '0') || '\"'"
    hemi = f"if(@grid_axis = 'x', if(@grid_number < 0, '{w}', '{e}'), if(@grid_number < 0, '{s}', '{n}'))"
    text = (f"with_variable('tot', {tot}, with_variable('d', floor(@tot / 3600), "
            f"with_variable('m', floor((@tot - @d * 3600) / 60), with_variable('s', @tot - @d * 3600 - @m * 60, "
            f"{parts} || ' ' || {hemi}))))")
    return _localise(text, lang)


# ---- zones -----------------------------------------------------------------------------------
def utm_zone(lon):
    return int(math.floor((lon + 180) / 6)) % 60 + 1


def map_lonlat_range(layout_map):
    """(min lon, max lon, min lat, max lat) of what the map shows."""
    poly = layout_map.visibleExtentPolygon()
    xf = QgsCoordinateTransform(layout_map.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    pts = [xf.transform(QgsPointXY(p)) for p in poly]
    lons = [p.x() for p in pts]
    lats = [p.y() for p in pts]
    return min(lons), max(lons), min(lats), max(lats)


def map_zones(layout_map):
    """UTM zone numbers the map covers, west to east."""
    lo, hi, _a, _b = map_lonlat_range(layout_map)
    z0, z1 = utm_zone(lo), utm_zone(hi)
    return list(range(z0, z1 + 1)) if z1 >= z0 else [z0, z1]


def utm_like(base, zone, south):
    """The UTM zone ``zone`` on the datum of ``base`` (a UTM system of the same datum keeps its datum)."""
    proj = base.toProj() if base.isValid() else ""
    if "+proj=utm" in proj:
        import re

        p = re.sub(r"\+zone=\d+", f"+zone={zone}", proj)
        p = p.replace(" +south", "")
        if south:
            p = p.replace("+proj=utm", "+proj=utm +south")
        crs = QgsCoordinateReferenceSystem.fromProj(p)
        if crs.isValid():
            return crs
    return QgsCoordinateReferenceSystem(f"EPSG:{(32700 if south else 32600) + zone}")


def geographic_like(base):
    g = base.toGeographicCrs() if base.isValid() and hasattr(base, "toGeographicCrs") else None
    return g if g is not None and g.isValid() else QgsCoordinateReferenceSystem("EPSG:4326")


def _metric_crs(layout_map):
    """The CRS for a metric grid: the map's own when projected (not Web Mercator), else the UTM zone of its
    centre."""
    crs = layout_map.crs()
    if crs.isValid() and not crs.isGeographic() and crs.authid() not in ("EPSG:3857", "EPSG:900913"):
        return crs
    lo, hi, la, lb = map_lonlat_range(layout_map)
    return utm_like(QgsCoordinateReferenceSystem("EPSG:4326"), utm_zone((lo + hi) / 2), (la + lb) / 2 < 0)


def _extent_in(layout_map, crs):
    xf = QgsCoordinateTransform(layout_map.crs(), crs, QgsProject.instance())
    pts = [xf.transform(QgsPointXY(p)) for p in layout_map.visibleExtentPolygon()]
    xs = [p.x() for p in pts]
    ys = [p.y() for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


# ---- building grids ----------------------------------------------------------------------------
def _text_format(size, color="#1E252B", lang="en"):
    f = QFont("Segoe UI")
    f.setPointSizeF(size)
    fmt = QgsTextFormat()
    fmt.setFont(f)
    fmt.setSize(size)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(color))
    return fmt


def _sides():
    return (G.BorderSide.Left, G.BorderSide.Right, G.BorderSide.Top, G.BorderSide.Bottom)


def _new_grid(layout_map, name, crs, interval, expr, size, lang):
    grid = G(name, layout_map)
    layout_map.grids().addGrid(grid)
    grid.setCrs(crs)
    grid.setUnits(G.GridUnit.MapUnits)
    grid.setIntervalX(interval)
    grid.setIntervalY(interval)
    grid.setAnnotationEnabled(True)
    grid.setAnnotationFormat(G.AnnotationFormat.CustomFormat)
    grid.setAnnotationExpression(expr)
    grid.setAnnotationTextFormat(_text_format(size, lang=lang))
    for side in (G.BorderSide.Left, G.BorderSide.Right):
        grid.setAnnotationDirection(G.AnnotationDirection.VerticalDescending, side)
    for side in (G.BorderSide.Top, G.BorderSide.Bottom):
        grid.setAnnotationDirection(G.AnnotationDirection.Horizontal, side)
    for side in _sides():
        grid.setAnnotationPosition(G.AnnotationPosition.OutsideMapFrame, side)
    if abs(layout_map.mapRotation()) > 0.01:
        grid.setRotatedTicksEnabled(True)
        grid.setRotatedAnnotationsEnabled(True)
    return grid


def _metric_grid(layout_map, crs, lang, interval, line_style, size, name="biruni-grid-metric"):
    x0, x1, y0, y1 = _extent_in(layout_map, crs)
    iv = interval or projected_interval(max(x1 - x0, y1 - y0))
    grid = _new_grid(layout_map, name, crs, iv, projected_expression(lang), size, lang)
    if line_style == "ticks":
        grid.setStyle(G.GridStyle.FrameAndAnnotationsOnly)
        grid.setFrameStyle(G.FrameStyle.InteriorExteriorTicks)
        grid.setFrameWidth(1.6)
    elif line_style == "lines":
        grid.setStyle(G.GridStyle.Lines)
        grid.setGridLineColor(QColor(30, 37, 43, 110))
        grid.setGridLineWidth(0.12)
        grid.setFrameStyle(G.FrameStyle.Zebra)
        grid.setFrameWidth(1.2)
    else:  # crosses
        grid.setStyle(G.GridStyle.LineCrosses)
        grid.setCrossLength(2.0)
        grid.setFrameStyle(G.FrameStyle.Zebra)
        grid.setFrameWidth(1.2)
    return grid


def _geo_grid(layout_map, crs, lang, interval, frame_only, size, name="biruni-grid-geographic"):
    x0, x1, y0, y1 = _extent_in(layout_map, crs)
    iv = interval or geographic_interval(max(x1 - x0, y1 - y0))
    grid = _new_grid(layout_map, name, crs, iv, geographic_expression(lang, iv), size, lang)
    if frame_only:
        grid.setStyle(G.GridStyle.FrameAndAnnotationsOnly)
        grid.setFrameStyle(G.FrameStyle.ExteriorTicks)
        grid.setFrameWidth(2.0)
    else:
        grid.setStyle(G.GridStyle.Lines)
        grid.setGridLineColor(QColor(90, 100, 110, 150))
        grid.setGridLineWidth(0.15)
        grid.setFrameStyle(G.FrameStyle.LineBorder)
    return grid


def add_grid(layout_map, style="auto", lang="en", zone_mode="extended", interval=None, geo_interval=None,
             size=6.0):
    """Add the grid(s) of ``style`` (see STYLES) to a layout map; returns the list of grids added.

    zone_mode (see ZONE_MODES) applies when the metric grid would be UTM and the map crosses a zone boundary.
    ``interval`` (map units of the grid) and ``geo_interval`` (degrees) override the automatic choice."""
    lang = _lang(lang)
    if style == "none":
        return []
    crs = layout_map.crs()
    if style == "auto":
        style = "geographic" if crs.isGeographic() else "projected"
    grids = []
    metric_crs = _metric_crs(layout_map) if style in ("projected", "both", "ticks") else None
    zones = map_zones(layout_map) if metric_crs is not None else []
    crosses = len(zones) > 1 and "+proj=utm" in (metric_crs.toProj() if metric_crs else "")
    if crosses and zone_mode == "geographic":
        style = "geographic"
        metric_crs = None
    if style in ("projected", "both", "ticks"):
        line = "ticks" if style == "ticks" else "crosses"
        south = map_lonlat_range(layout_map)[2] + map_lonlat_range(layout_map)[3] < 0
        if crosses and zone_mode == "per_zone":
            west, east = zones[0], zones[-1]
            gw = _metric_grid(layout_map, utm_like(metric_crs, west, south), lang, interval, line, size,
                              f"biruni-grid-zone-{west}")
            ge = _metric_grid(layout_map, utm_like(metric_crs, east, south), lang, interval, line, size,
                              f"biruni-grid-zone-{east}")
            # each zone annotates its own half of the frame; the east zone's lines in a second colour
            gw.setAnnotationDisplay(G.DisplayMode.HideAll, G.BorderSide.Right)
            ge.setAnnotationDisplay(G.DisplayMode.HideAll, G.BorderSide.Left)
            for g in (gw, ge):
                g.setAnnotationDisplay(G.DisplayMode.HideAll, G.BorderSide.Top)
            ge.setGridLineColor(QColor(200, 70, 60, 140))
            ge.setFrameStyle(G.FrameStyle.NoFrame)
            grids += [gw, ge]
        else:
            grids.append(_metric_grid(layout_map, metric_crs, lang, interval, line, size))
        if style == "both":
            # metric labels inside, the geographic graticule on the outer frame
            for side in _sides():
                grids[-1].setAnnotationPosition(G.AnnotationPosition.InsideMapFrame, side)
            grids[-1].setFrameStyle(G.FrameStyle.NoFrame)
            grids.append(_geo_grid(layout_map, geographic_like(crs), lang, geo_interval, True, size))
    if style == "geographic":
        grids.append(_geo_grid(layout_map, geographic_like(crs), lang, geo_interval, False, size))
    layout_map.updateBoundingRect()
    layout_map.refresh()
    return grids


def remove_grids(layout_map):
    """Remove the grids this module added (names starting with biruni-grid)."""
    for g in list(layout_map.grids().asList()):
        if g.name().startswith("biruni-grid"):
            layout_map.grids().removeGrid(g.id())


# ---- corner coordinates -----------------------------------------------------------------------
def corner_texts(layout_map, lang="en", crs=None, geographic=False):
    """Texts of the four map corners (top-left, top-right, bottom-right, bottom-left)."""
    lang = _lang(lang)
    crs = crs or (geographic_like(layout_map.crs()) if geographic else _metric_crs(layout_map))
    xf = QgsCoordinateTransform(layout_map.crs(), crs, QgsProject.instance())
    polygon = layout_map.visibleExtentPolygon()  # keep it alive: its points are views into it
    poly = [QgsPointXY(p.x(), p.y()) for p in polygon][:4]
    from qgis.core import QgsExpression, QgsExpressionContext

    template = geographic_expression(lang, None, 1) if crs.isGeographic() else projected_expression(lang, 0)
    out = []
    for p in poly:
        q = xf.transform(QgsPointXY(p))
        parts = []
        for axis, val in (("x", q.x()), ("y", q.y())):
            # the same text as the grid labels, with the corner's values written into the expression (a context
            # scope rebuilt in a loop lost its variables after the first evaluations)
            expr = template.replace("@grid_number", f"({val!r})").replace("@grid_axis", f"'{axis}'")
            parts.append(str(QgsExpression(expr).evaluate(QgsExpressionContext())))
        out.append("\n".join(parts[::-1]))  # northing / latitude first, as on survey sheets
    return out


def add_corner_labels(layout, layout_map, lang="en", geographic=False, size=5.5):
    """Coordinates written just outside the four corners of the map frame; returns the labels."""
    texts = corner_texts(layout_map, lang, geographic=geographic)
    r = layout_map.sceneBoundingRect() if abs(layout_map.mapRotation()) > 0.01 else None
    x, y = layout_map.positionWithUnits().x(), layout_map.positionWithUnits().y()
    w, h = layout_map.sizeWithUnits().width(), layout_map.sizeWithUnits().height()
    if r is not None:
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
    spots = [(x - 22, y - 8, Qt.AlignmentFlag.AlignLeft), (x + w + 1, y - 8, Qt.AlignmentFlag.AlignLeft),
             (x + w + 1, y + h + 1, Qt.AlignmentFlag.AlignLeft), (x - 22, y + h + 1, Qt.AlignmentFlag.AlignLeft)]
    labels = []
    for (lx, ly, align), text in zip(spots, texts):
        lab = QgsLayoutItemLabel(layout)
        lab.setText(text)
        lab.setTextFormat(_text_format(size, "#3A444D", lang))
        lab.setHAlign(align)
        lab.setMarginX(0)
        lab.setMarginY(0)
        layout.addLayoutItem(lab)
        lab.attemptMove(QgsLayoutPoint(lx, ly))
        lab.attemptResize(QgsLayoutSize(21, 7))
        lab.setId("biruni-corner")
        labels.append(lab)
    return labels


def style_label(style):
    return tr(STYLES.get(style, style))


def zone_mode_label(mode):
    return tr(ZONE_MODES.get(mode, mode))
