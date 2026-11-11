"""Smart legend items for print layouts: a histogram of the class distribution, nested circles for
proportional symbols, a 3×3 bivariate square, and a legend that lists only what is inside the map frame.

Every function adds ordinary layout items (a picture drawn as SVG, labels, a QGIS legend) so the result can
be moved and edited in the layout designer like anything else."""
import html
import math
import os
import tempfile

from qgis.core import (QgsFeatureRequest, QgsGraduatedSymbolRenderer, QgsLayoutItemLegend, QgsLayoutItemPicture,
                       QgsLayoutPoint, QgsLayoutSize, QgsLegendStyle, QgsVectorLayer)

from ..i18n import tr

_DIR = os.path.join(tempfile.gettempdir(), "biruni-legends")


def _svg_path(name):
    os.makedirs(_DIR, exist_ok=True)
    return os.path.join(_DIR, f"{name}-{os.getpid()}-{abs(hash(name)) % 100000}.svg")


def _picture(layout, svg_text, x, y, w, h, name, dpi=300):
    """The SVG is kept next to a 300 dpi PNG of it; the PNG goes on the page because the QGIS SVG cache drops
    <text> elements (values and axis labels would vanish)."""
    from qgis.PyQt.QtCore import QByteArray
    from qgis.PyQt.QtGui import QImage, QPainter
    from qgis.PyQt.QtSvg import QSvgRenderer

    path = _svg_path(name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg_text)
    img = QImage(max(1, round(w / 25.4 * dpi)), max(1, round(h / 25.4 * dpi)), QImage.Format.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    QSvgRenderer(QByteArray(svg_text.encode("utf-8"))).render(p)
    p.end()
    png = path[:-4] + ".png"
    img.save(png)
    pic = QgsLayoutItemPicture(layout)
    pic.setPicturePath(png, QgsLayoutItemPicture.Format.FormatRaster)
    pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
    layout.addLayoutItem(pic)
    pic.attemptMove(QgsLayoutPoint(x, y))
    pic.attemptResize(QgsLayoutSize(w, h))
    pic.setId(name)
    return pic


def _numbers(layer, field, limit=200000):
    req = QgsFeatureRequest().setSubsetOfAttributes([field], layer.fields()).setFlags(
        QgsFeatureRequest.Flag.NoGeometry)
    out = []
    for i, f in enumerate(layer.getFeatures(req)):
        if i >= limit:
            break
        v = f[field]
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _fmt(v):
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


# ---- histogram legend --------------------------------------------------------------------------------------
def histogram_svg(values, ranges, width=60.0, height=40.0):
    """SVG of the value distribution (bars) coloured by the classes of a graduated renderer.
    ranges: [(lower, upper, '#rrggbb', label)]."""
    if not values or not ranges:
        return None
    lo, hi = min(r[0] for r in ranges), max(r[1] for r in ranges)
    if hi <= lo:
        return None
    bins = 30
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        if lo <= v <= hi:
            counts[min(bins - 1, int((v - lo) / step))] += 1
    top = max(counts) or 1
    plot_h = height - 8
    bw = width / bins
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm" '
             f'viewBox="0 0 {width} {height}">']
    for i, c in enumerate(counts):
        mid = lo + (i + 0.5) * step
        colour = next((r[2] for r in ranges if r[0] <= mid <= r[1]), "#999999")
        h = plot_h * c / top
        parts.append(f'<rect x="{i * bw:.3f}" y="{plot_h - h:.3f}" width="{bw * 0.92:.3f}" height="{h:.3f}" '
                     f'fill="{colour}"/>')
    parts.append(f'<line x1="0" y1="{plot_h}" x2="{width}" y2="{plot_h}" stroke="#555" stroke-width="0.2"/>')
    for r in ranges:                                   # class breaks as ticks with values
        x = (r[0] - lo) / (hi - lo) * width
        parts.append(f'<line x1="{x:.2f}" y1="{plot_h}" x2="{x:.2f}" y2="{plot_h + 1.5}" stroke="#555" '
                     f'stroke-width="0.2"/>')
        parts.append(f'<text x="{x:.2f}" y="{height - 1}" font-size="2.6" font-family="Segoe UI" '
                     f'text-anchor="middle" fill="#333">{html.escape(_fmt(r[0]))}</text>')
    parts.append(f'<text x="{width:.2f}" y="{height - 1}" font-size="2.6" font-family="Segoe UI" '
                 f'text-anchor="end" fill="#333">{html.escape(_fmt(hi))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def graduated_ranges(layer):
    r = layer.renderer() if isinstance(layer, QgsVectorLayer) else None
    if not isinstance(r, QgsGraduatedSymbolRenderer):
        return None, []
    return r.classAttribute(), [(rg.lowerValue(), rg.upperValue(), rg.symbol().color().name(), rg.label())
                                for rg in r.ranges()]


def add_histogram_legend(layout, layer, x, y, width=60.0, height=40.0):
    """A histogram of a graduated layer's values in its class colours. Returns the picture item or None."""
    field, ranges = graduated_ranges(layer)
    if not field:
        return None
    svg = histogram_svg(_numbers(layer, field), ranges, width, height)
    return _picture(layout, svg, x, y, width, height, "biruni-histogram") if svg else None


# ---- nested circles ----------------------------------------------------------------------------------------
def nice_values(vmax, n=3):
    """Round reference values for a size legend: e.g. 12345 → [10000, 5000, 1000]."""
    if vmax <= 0:
        return []
    mag = 10 ** math.floor(math.log10(vmax))
    top = max(m * mag for m in (1, 2, 5, 10) if m * mag <= vmax) if vmax >= mag else vmax
    vals = [top]
    for f in (0.5, 0.1, 0.02)[: n - 1]:
        vals.append(top * f)
    return [v for v in vals if v > 0]


def nested_circles_svg(values, max_value, max_diameter_mm, colour="#4C78A8"):
    """Proportional (area-true) nested circles with leader lines and values, as SVG."""
    d_max = max_diameter_mm
    w, h = d_max + 30, d_max + 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">']
    base = h - 1
    cx = d_max / 2 + 1
    for v in values:
        r = d_max / 2 * math.sqrt(v / max_value)
        top = base - 2 * r
        parts.append(f'<circle cx="{cx:.2f}" cy="{base - r:.2f}" r="{r:.2f}" fill="{colour}" fill-opacity="0.18" '
                     f'stroke="{colour}" stroke-width="0.3"/>')
        parts.append(f'<line x1="{cx:.2f}" y1="{top:.2f}" x2="{d_max + 4:.2f}" y2="{top:.2f}" stroke="#666" '
                     f'stroke-width="0.15" stroke-dasharray="0.6,0.4"/>')
        parts.append(f'<text x="{d_max + 5:.2f}" y="{top + 1:.2f}" font-size="2.8" font-family="Segoe UI" '
                     f'fill="#333">{html.escape(_fmt(v))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def add_nested_circles(layout, values_max, x, y, max_diameter_mm=20.0, colour="#4C78A8", title=None):
    vals = nice_values(values_max)
    if not vals:
        return None
    pic = _picture(layout, nested_circles_svg(vals, values_max, max_diameter_mm, colour), x, y,
                   max_diameter_mm + 30, max_diameter_mm + 2, "biruni-circles")
    return pic


# ---- bivariate square --------------------------------------------------------------------------------------
BIVARIATE_3x3 = [  # rows: variable B low→high, columns: variable A low→high (Joshua Stevens' pink-blue scheme)
    ["#e8e8e8", "#e4acac", "#c85a5a"],
    ["#b0d5df", "#ad9ea5", "#985356"],
    ["#64acbe", "#627f8c", "#574249"],
]


def bivariate_svg(label_a, label_b, colours=BIVARIATE_3x3, size=24.0):
    cell = size / 3
    pad = 8
    w = h = size + pad + 2
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">']
    for row in range(3):
        for col in range(3):
            parts.append(f'<rect x="{pad + col * cell:.2f}" y="{(2 - row) * cell:.2f}" width="{cell:.2f}" '
                         f'height="{cell:.2f}" fill="{colours[row][col]}"/>')
    parts.append(f'<text x="{pad + size / 2:.2f}" y="{size + 5:.2f}" font-size="2.8" font-family="Segoe UI" '
                 f'text-anchor="middle" fill="#333">{html.escape(label_a)} →</text>')
    parts.append(f'<text x="5" y="{size / 2:.2f}" font-size="2.8" font-family="Segoe UI" text-anchor="middle" '
                 f'fill="#333" transform="rotate(-90 5 {size / 2:.2f})">{html.escape(label_b)} →</text>')
    parts.append("</svg>")
    return "".join(parts)


def add_bivariate_legend(layout, label_a, label_b, x, y, size=24.0, colours=BIVARIATE_3x3):
    return _picture(layout, bivariate_svg(label_a, label_b, colours, size), x, y, size + 10, size + 10,
                    "biruni-bivariate")


# ---- legend of what is in the frame -----------------------------------------------------------------------
def add_frame_legend(layout, layout_map, x, y, title=None):
    """A QGIS legend linked to the map that shows only layers and classes visible inside the frame."""
    leg = QgsLayoutItemLegend(layout)
    leg.setTitle(title if title is not None else tr("Legend"))
    leg.setLinkedMap(layout_map)
    leg.setLegendFilterByMapEnabled(True)       # classes with features in the frame only
    leg.setAutoUpdateModel(True)
    from .auto_layout import _fmt as text_format

    for part, size, bold in ((QgsLegendStyle.Style.Title, 9, True), (QgsLegendStyle.Style.Group, 8, True),
                             (QgsLegendStyle.Style.Subgroup, 8, True), (QgsLegendStyle.Style.SymbolLabel, 7.5, False)):
        st = leg.style(part)
        st.setTextFormat(text_format(size, bold))       # the default offscreen font has no proper glyph map
        leg.setStyle(part, st)
    layout.addLayoutItem(leg)
    leg.attemptMove(QgsLayoutPoint(x, y))
    leg.adjustBoxSize()
    return leg
