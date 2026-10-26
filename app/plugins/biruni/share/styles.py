"""QGIS symbology reduced to the few properties web maps and KML understand:
stroke/fill colour, opacity, width, marker size and dashes.

Any renderer works (single symbol, categorized, graduated, rule-based...): the
style of a feature is the one its renderer draws it with, and the legend is the
renderer's own legend."""
import json

from qgis.core import (Qgis, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFillSymbol,
                       QgsLineSymbol, QgsMarkerSymbol, QgsRenderContext, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import Qt

PX_PER_MM = 96 / 25.4
DEFAULT = {"color": "#3388ff", "weight": 2, "opacity": 1.0, "fillColor": "#3388ff", "fillOpacity": 0.25,
           "radius": 6, "dashArray": None, "stroke": True, "fill": True}


def geometry_kind(layer):
    t = layer.geometryType()
    if t == Qgis.GeometryType.Point:
        return "point"
    if t == Qgis.GeometryType.Line:
        return "line"
    if t == Qgis.GeometryType.Polygon:
        return "polygon"
    return None


def _px(value, unit):
    if unit == Qgis.RenderUnit.Pixels:
        return value
    if unit == Qgis.RenderUnit.Points:
        return value * 96 / 72
    if unit == Qgis.RenderUnit.Millimeters:
        return value * PX_PER_MM
    if unit == Qgis.RenderUnit.Inches:
        return value * 96
    return None  # map units / percentages: no fixed screen size


def _hex(color):
    return color.name()  # #rrggbb


def _dash(pen_style):
    return {Qt.PenStyle.DashLine: "8 5", Qt.PenStyle.DotLine: "2 4", Qt.PenStyle.DashDotLine: "8 4 2 4",
            Qt.PenStyle.DashDotDotLine: "8 4 2 4 2 4"}.get(pen_style)


def symbol_style(symbol):
    """Leaflet path options for a QGIS symbol (first symbol layer)."""
    s = dict(DEFAULT)
    if symbol is None:
        return s
    op = symbol.opacity()
    sl = symbol.symbolLayer(0) if symbol.symbolLayerCount() else None
    base = symbol.color()
    if isinstance(symbol, QgsFillSymbol):
        fill, stroke, width, pen = base, base.darker(130), 0.26 * PX_PER_MM, Qt.PenStyle.SolidLine
        has_fill = True
        if sl is not None and hasattr(sl, "fillColor"):
            fill = sl.fillColor()
            if hasattr(sl, "brushStyle") and sl.brushStyle() == Qt.BrushStyle.NoBrush:
                has_fill = False
        if sl is not None and hasattr(sl, "strokeColor"):
            stroke = sl.strokeColor()
            w = _px(sl.strokeWidth(), sl.strokeWidthUnit()) if hasattr(sl, "strokeWidthUnit") else None
            width = w if w is not None else width
            if width == 0:  # "hairline" in QGIS
                width = 1
            if hasattr(sl, "strokeStyle"):
                pen = sl.strokeStyle()
        s.update(fillColor=_hex(fill), fillOpacity=round(fill.alphaF() * op if has_fill else 0, 3),
                 color=_hex(stroke), opacity=round(stroke.alphaF() * op, 3), weight=round(width, 2),
                 stroke=pen != Qt.PenStyle.NoPen, fill=has_fill, dashArray=_dash(pen))
    elif isinstance(symbol, QgsLineSymbol):
        color, width, pen = base, 0.26 * PX_PER_MM, Qt.PenStyle.SolidLine
        if sl is not None and hasattr(sl, "width"):
            color = sl.color()
            w = _px(sl.width(), sl.widthUnit()) if hasattr(sl, "widthUnit") else None
            width = w if w is not None else width
            if hasattr(sl, "penStyle"):
                pen = sl.penStyle()
        s.update(color=_hex(color), opacity=round(color.alphaF() * op, 3), weight=round(max(width, 1), 2),
                 fill=False, fillOpacity=0, dashArray=_dash(pen))
    elif isinstance(symbol, QgsMarkerSymbol):
        fill, stroke, size, width = base, base.darker(150), 2 * PX_PER_MM, 1
        if sl is not None and hasattr(sl, "fillColor"):
            fill = sl.fillColor()
        if sl is not None and hasattr(sl, "strokeColor"):
            stroke = sl.strokeColor()
        if sl is not None and hasattr(sl, "strokeWidth"):
            w = _px(sl.strokeWidth(), sl.strokeWidthUnit()) if hasattr(sl, "strokeWidthUnit") else None
            width = w if w else 1
        sz = _px(symbol.size(), symbol.sizeUnit())
        size = sz if sz is not None else size
        s.update(fillColor=_hex(fill), fillOpacity=round(fill.alphaF() * op, 3), color=_hex(stroke),
                 opacity=round(stroke.alphaF() * op, 3), weight=round(width, 2), radius=round(max(size / 2, 2), 2))
    return s


class LayerStyler:
    """Per-feature style index + legend for one layer, deduplicating styles."""

    def __init__(self, layer):
        self.layer = layer
        self.renderer = layer.renderer().clone() if layer.renderer() else None
        self.styles = []
        self._index = {}
        self.ctx = QgsRenderContext()
        self.ctx.setExpressionContext(self._expr_context())
        self._started = False

    def _expr_context(self):
        c = QgsExpressionContext()
        c.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(self.layer))
        return c

    def __enter__(self):
        if self.renderer is not None:
            self.renderer.startRender(self.ctx, self.layer.fields())
            self._started = True
        return self

    def __exit__(self, *exc):
        if self._started:
            self.renderer.stopRender(self.ctx)
            self._started = False

    def _add(self, style):
        key = json.dumps(style, sort_keys=True)
        if key not in self._index:
            self._index[key] = len(self.styles)
            self.styles.append(style)
        return self._index[key]

    def style_index(self, feature):
        """Index into self.styles, or None when the renderer does not draw it."""
        if self.renderer is None:
            return self._add(dict(DEFAULT))
        self.ctx.expressionContext().setFeature(feature)
        sym = self.renderer.symbolForFeature(feature, self.ctx)
        if sym is None:
            return None
        return self._add(symbol_style(sym))

    def legend(self):
        if self.renderer is None:
            return []
        items = []
        for it in self.renderer.legendSymbolItems():
            if it.symbol() is None:
                continue
            items.append({"label": it.label(), "style": symbol_style(it.symbol())})
        return items


def label_expression(layer):
    """The label expression of a layer with simple labelling switched on."""
    if not layer.labelsEnabled() or not isinstance(layer.labeling(), QgsVectorLayerSimpleLabeling):
        return None
    st = layer.labeling().settings()
    if not st.fieldName:
        return None
    return st.fieldName if st.isExpression else QgsExpression.quotedColumnRef(st.fieldName)


class ExpressionEvaluator:
    def __init__(self, layer, expression):
        self.expr = QgsExpression(expression) if expression else None
        self.ctx = QgsExpressionContext()
        self.ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        if self.expr is not None:
            self.expr.prepare(self.ctx)

    def __call__(self, feature):
        if self.expr is None:
            return None
        self.ctx.setFeature(feature)
        v = self.expr.evaluate(self.ctx)
        return None if v is None or v == "" or self.expr.hasEvalError() else str(v)
