"""QGIS styling -> CIM (ArcGIS Pro Cartographic Information Model): the inverse of cim.py.

Symbols (simple/hatch/marker/gradient fills, solid and dashed strokes with
offsets, simple/font/picture markers, markers along lines and inside
polygons), renderers (single symbol, categorized -> unique values, graduated
-> class breaks), labels (field or expression -> Arcade, font, size, colour,
halo, scale range, rule-based -> one label class per rule with a SQL where
clause), raster colourizers and coordinate systems.

Every function returns plain JSON-ready dicts and records what it had to
approximate with warn() - never an exception for a style it does not know."""
import base64
import json
import re

from qgis.core import (Qgis, QgsExpression,QgsExpressionNodeBinaryOperator,
                       QgsExpressionNodeColumnRef, QgsExpressionNodeCondition, QgsExpressionNodeFunction,
                       QgsExpressionNodeInOperator, QgsExpressionNodeLiteral, QgsExpressionNodeUnaryOperator,
                       QgsMarkerSymbol, QgsRenderContext, QgsRuleBasedLabeling, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from qgis.PyQt.QtGui import QColor

from ..i18n import tr

MM_TO_PT = 72 / 25.4


# ---- units and colours --------------------------------------------------------
def to_pt(value, unit=None):
    """A QGIS size in its render unit -> points (what CIM symbols use)."""
    v = float(value or 0)
    if unit in (None, Qgis.RenderUnit.Millimeters):
        return round(v * MM_TO_PT, 4)
    if unit == Qgis.RenderUnit.Points:
        return round(v, 4)
    if unit == Qgis.RenderUnit.Pixels:
        return round(v * 0.75, 4)
    if unit == Qgis.RenderUnit.Inches:
        return round(v * 72, 4)
    return round(v * MM_TO_PT, 4)  # map units / percent: no page equivalent, keep the number


def cim_color(c, opacity=1.0):
    """QColor -> CIMRGBColor (alpha 0-255 -> 0-100)."""
    q = QColor(c) if not isinstance(c, QColor) else c
    a = q.alpha() * max(0.0, min(1.0, opacity)) * 100 / 255
    return {"type": "CIMRGBColor", "values": [q.red(), q.green(), q.blue(), round(a, 2)]}


def ref(symbol):
    return {"type": "CIMSymbolReference", "symbol": symbol}


def spatial_reference(crs):
    """QgsCoordinateReferenceSystem -> CIM spatialReference (wkid when it has one, else ESRI WKT)."""
    if crs is None or not crs.isValid():
        return None
    auth, _, code = crs.authid().partition(":")
    if auth in ("EPSG", "ESRI") and code.isdigit():
        return {"wkid": int(code), "latestWkid": int(code)}
    wkt = crs.toWkt(Qgis.CrsWktVariant.Wkt1Esri)
    return {"wkt": wkt} if wkt else None


def _has(flags, flag):
    try:
        return bool(flags & flag)
    except TypeError:
        return int(getattr(flags, "value", flags)) & int(getattr(flag, "value", flag)) != 0


# ---- expressions: QGIS -> Arcade --------------------------------------------
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARCADE_FUNCS = {"upper": "Upper", "lower": "Lower", "title": "Proper", "round": "Round", "abs": "Abs",
                 "sqrt": "Sqrt", "floor": "Floor", "ceil": "Ceil", "to_string": "Text", "to_real": "Number",
                 "left": "Left", "right": "Right", "trim": "Trim", "length": "Count", "if": "IIf",
                 "coalesce": "DefaultValue", "concat": None}
_BIN = {"boOr": "||", "boAnd": "&&", "boEQ": "==", "boNE": "!=", "boLE": "<=", "boGE": ">=", "boLT": "<",
        "boGT": ">", "boPlus": "+", "boMinus": "-", "boMul": "*", "boDiv": "/", "boMod": "%", "boConcat": "+",
        "boIs": "==", "boIsNot": "!="}


class _Untranslatable(Exception):
    pass


def arcade_field(name):
    return f"$feature.{name}" if _IDENT.match(name or "") else "$feature[%s]" % json.dumps(name, ensure_ascii=False)


def _arcade(node):
    if isinstance(node, QgsExpressionNodeColumnRef):
        return arcade_field(node.name())
    if isinstance(node, QgsExpressionNodeLiteral):
        v = node.value()
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(str(v), ensure_ascii=False)
    if isinstance(node, QgsExpressionNodeBinaryOperator):
        op = _BIN.get(node.op().name)
        if op is None:
            if node.op().name == "boPow":
                return f"Pow({_arcade(node.opLeft())}, {_arcade(node.opRight())})"
            raise _Untranslatable(node.op().name)
        return f"{_arcade(node.opLeft())} {op} {_arcade(node.opRight())}"
    if isinstance(node, QgsExpressionNodeUnaryOperator):
        inner = _arcade(node.operand())
        return f"!({inner})" if node.op().name == "uoNot" else f"-{inner}"
    if isinstance(node, QgsExpressionNodeFunction):
        fn = QgsExpression.Functions()[node.fnIndex()].name().lower()
        args = [_arcade(a) for a in (node.args().list() if node.args() else [])]
        if fn == "concat":
            return "(" + " + ".join(args) + ")" if args else '""'
        if fn == "to_int" and args:
            return f"Floor(Number({args[0]}))"
        if fn not in _ARCADE_FUNCS:
            raise _Untranslatable(fn)
        return f"{_ARCADE_FUNCS[fn]}({', '.join(args)})"
    if isinstance(node, QgsExpressionNodeCondition):
        parts = []
        for wt in node.conditions():
            parts += [_arcade(wt.whenExp()), _arcade(wt.thenExp())]
        parts.append(_arcade(node.elseExp()) if node.elseExp() else "null")
        return f"When({', '.join(parts)})"
    if isinstance(node, QgsExpressionNodeInOperator):
        left = _arcade(node.node())
        items = [_arcade(n) for n in node.list().list()]
        test = " || ".join(f"{left} == {i}" for i in items) or "false"
        return f"!({test})" if node.isNotIn() else f"({test})"
    raise _Untranslatable(type(node).__name__)


def to_arcade(text, is_expression=True):
    """A QGIS label field / expression -> (Arcade expression, exact)."""
    text = (text or "").strip()
    if not text:
        return None, True
    if not is_expression:
        return arcade_field(text), True
    e = QgsExpression(text)
    if e.hasParserError() or e.rootNode() is None:
        return None, False
    try:
        out = _arcade(e.rootNode())
    except _Untranslatable:
        cols = sorted(e.referencedColumns())
        return (arcade_field(cols[0]) if cols else None), False
    if out.startswith("(") and out.endswith(")") and out.count("(") == 1:
        out = out[1:-1]
    return out, True


def to_sql(expr):
    """A QGIS filter expression -> SQL where clause for ArcGIS (exact when it is plain SQL)."""
    text = (expr or "").strip()
    if not text:
        return "", True
    e = QgsExpression(text)
    if e.hasParserError():
        return text, False  # provider SQL QGIS cannot parse (e.g. native OGR SQL): keep as written
    exact = not re.search(r"\b[a-z_][a-z0-9_]*\s*\(", text, re.IGNORECASE) or re.fullmatch(
        r"[^()]*\b(upper|lower)\s*\([^()]*\)[^()]*", text, re.IGNORECASE) is not None
    return text, exact


# ---- symbol layers ---------------------------------------------------------
_CAP = {Qt.PenCapStyle.FlatCap: "Butt", Qt.PenCapStyle.SquareCap: "Square", Qt.PenCapStyle.RoundCap: "Round"}
_JOIN = {Qt.PenJoinStyle.MiterJoin: "Miter", Qt.PenJoinStyle.BevelJoin: "Bevel", Qt.PenJoinStyle.RoundJoin: "Round",
         Qt.PenJoinStyle.SvgMiterJoin: "Miter"}
_PEN_DASH = {Qt.PenStyle.DashLine: [4, 2], Qt.PenStyle.DotLine: [1, 2], Qt.PenStyle.DashDotLine: [4, 2, 1, 2],
             Qt.PenStyle.DashDotDotLine: [4, 2, 1, 2, 1, 2]}
_BRUSH_HATCH = {Qt.BrushStyle.HorPattern: [0], Qt.BrushStyle.VerPattern: [90], Qt.BrushStyle.CrossPattern: [0, 90],
                Qt.BrushStyle.BDiagPattern: [45], Qt.BrushStyle.FDiagPattern: [135],
                Qt.BrushStyle.DiagCrossPattern: [45, 135]}


def _stroke(color, width_pt, opacity=1.0, cap="Round", join="Round", dashes=None, offset=0.0):
    d = {"type": "CIMSolidStroke", "enable": True, "capStyle": cap, "joinStyle": join, "lineStyle3D": "Strip",
         "miterLimit": 10, "width": round(width_pt, 4), "color": cim_color(color, opacity)}
    effects = []
    if dashes:
        effects.append({"type": "CIMGeometricEffectDashes", "dashTemplate": [round(x, 4) for x in dashes],
                        "lineDashEnding": "NoConstraint"})
    if offset:
        effects.append({"type": "CIMGeometricEffectOffset", "method": "Square", "offset": round(offset, 4),
                        "option": "Fast"})
    if effects:
        d["effects"] = effects
    return d


def _solid_fill(color, opacity=1.0):
    return {"type": "CIMSolidFill", "enable": True, "color": cim_color(color, opacity)}


def _poly(layers):
    return {"type": "CIMPolygonSymbol", "symbolLayers": layers, "angleAlignment": "Map"}


def _pen_dashes(style, width_pt):
    tpl = _PEN_DASH.get(style)
    return [x * max(width_pt, 1.0) for x in tpl] if tpl else None


# marker shapes as the reader recognises them (cim.Converter._vector_marker)
def _regular(n, r=5.0, start=90.0, inner=None):
    import math

    pts = []
    for i in range(n * (2 if inner else 1)):
        rad = inner if inner and i % 2 else r
        a = math.radians(start + i * 360.0 / (n * (2 if inner else 1)))
        pts.append([round(rad * math.cos(a), 6), round(rad * math.sin(a), 6)])
    return pts + [pts[0]]


_CIRCLE = {"curveRings": [[[0, 5], {"a": [[0, 5], [0, 0], 0, 1]}]]}
_SHAPES = {
    Qgis.MarkerShape.Circle: _CIRCLE,
    Qgis.MarkerShape.Square: {"rings": [[[-5, -5], [-5, 5], [5, 5], [5, -5], [-5, -5]]]},
    Qgis.MarkerShape.Diamond: {"rings": [[[0, -5], [-5, 0], [0, 5], [5, 0], [0, -5]]]},
    Qgis.MarkerShape.Triangle: {"rings": [[[-5, -4], [0, 5], [5, -4], [-5, -4]]]},
    Qgis.MarkerShape.EquilateralTriangle: {"rings": [[[-5, -4], [0, 5], [5, -4], [-5, -4]]]},
    Qgis.MarkerShape.Pentagon: {"rings": [_regular(5)]},
    Qgis.MarkerShape.Hexagon: {"rings": [_regular(6)]},
    Qgis.MarkerShape.Star: {"rings": [_regular(5, 5, 90, 2.1)]},
    Qgis.MarkerShape.CrossFill: {"rings": [[[-1.5, 5], [1.5, 5], [1.5, 1.5], [5, 1.5], [5, -1.5], [1.5, -1.5],
                                            [1.5, -5], [-1.5, -5], [-1.5, -1.5], [-5, -1.5], [-5, 1.5], [-1.5, 1.5],
                                            [-1.5, 5]]]},
    Qgis.MarkerShape.Cross: {"paths": [[[-5, 0], [5, 0]], [[0, -5], [0, 5]]]},
    Qgis.MarkerShape.Cross2: {"paths": [[[-5, -5], [5, 5]], [[-5, 5], [5, -5]]]},
}


class SymbolWriter:
    """QGIS symbols, renderers and labels -> CIM dicts; collects warnings."""

    def __init__(self):
        self.warnings = []

    def warn(self, text):
        if text and text not in self.warnings:
            self.warnings.append(text)

    # -- markers --
    def _marker_layer(self, sl, opacity=1.0):
        """One QGIS marker symbol layer -> CIM marker symbol layer."""
        t = sl.layerType()
        size = to_pt(sl.size(), sl.sizeUnit()) if hasattr(sl, "size") else 6.0
        out = None
        if t == "SimpleMarker":
            shape = sl.shape()
            geom = _SHAPES.get(shape)
            if geom is None:
                self.warn(tr("some point shapes are written as circles"))
                geom = _CIRCLE
            sw = to_pt(sl.strokeWidth(), sl.strokeWidthUnit()) if sl.strokeWidth() else 0.5
            has_stroke = sl.strokeStyle() != Qt.PenStyle.NoPen
            if "paths" in geom:  # lines only: the stroke colour draws the shape
                sym = {"type": "CIMLineSymbol", "symbolLayers": [_stroke(sl.strokeColor(), sw, opacity)]}
            else:
                layers = [_stroke(sl.strokeColor(), sw, opacity)] if has_stroke else []
                layers.append(_solid_fill(sl.color(), opacity))
                sym = _poly(layers)
            out = {"type": "CIMVectorMarker", "enable": True, "anchorPointUnits": "Relative",
                   "dominantSizeAxis3D": "Y", "size": size, "billboardMode3D": "FaceNearPlane",
                   "frame": {"xmin": -5, "ymin": -5, "xmax": 5, "ymax": 5},
                   "markerGraphics": [{"type": "CIMMarkerGraphic", "geometry": geom, "symbol": sym}],
                   "scaleSymbolsProportionally": True, "respectFrame": True}
        elif t == "FontMarker":
            ch = sl.character() or "A"
            out = {"type": "CIMCharacterMarker", "enable": True, "anchorPointUnits": "Relative",
                   "dominantSizeAxis3D": "Y", "size": size, "billboardMode3D": "FaceNearPlane",
                   "characterIndex": ord(ch[0]), "fontFamilyName": sl.fontFamily() or "Arial",
                   "fontStyleName": sl.fontStyle() or "Regular", "fontType": "Unspecified", "scaleX": 1,
                   "symbol": _poly([_solid_fill(sl.color(), opacity)]), "scaleSymbolsProportionally": True,
                   "respectFrame": True}
        else:
            png = self._marker_png(sl)
            if png is None:
                return None
            if t not in ("RasterMarker", "SvgMarker"):
                self.warn(tr("some point symbols are written as pictures"))
            out = {"type": "CIMPictureMarker", "enable": True, "anchorPointUnits": "Relative",
                   "dominantSizeAxis3D": "Y", "size": size, "billboardMode3D": "FaceNearPlane",
                   "invertBackfaceTexture": True, "scaleX": 1, "textureFilter": "Picture",
                   "url": "data:image/png;base64," + png}
        if sl.angle():
            out["rotation"] = -float(sl.angle())
        off = sl.offset() if hasattr(sl, "offset") else None
        if off is not None and (off.x() or off.y()):
            out["offsetX"] = to_pt(off.x(), sl.offsetUnit())
            out["offsetY"] = -to_pt(off.y(), sl.offsetUnit())
        return out

    def _marker_png(self, sl):
        """Base64 PNG of a marker symbol layer (embedded pictures are reused as they are)."""
        path = sl.path() if hasattr(sl, "path") else ""
        if sl.layerType() == "RasterMarker" and path.startswith("base64:"):
            return path[7:]
        try:
            img = QgsMarkerSymbol([sl.clone()]).asImage(QSize(64, 64))
        except Exception:  # noqa: BLE001
            return None
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return base64.b64encode(bytes(ba)).decode()

    def _placement(self, ml):
        """Placement of the markers of a QGIS marker line."""
        pl = ml.placements()
        if _has(pl, Qgis.MarkerLinePlacement.Interval):
            return {"type": "CIMMarkerPlacementAlongLineSameSize", "angleToLine": bool(ml.rotateSymbols()),
                    "placementTemplate": [to_pt(ml.interval(), ml.intervalUnit()) or 10]}
        first, last = _has(pl, Qgis.MarkerLinePlacement.FirstVertex), _has(pl, Qgis.MarkerLinePlacement.LastVertex)
        if first or last:
            where = "Both" if first and last else "JustBegin" if first else "JustEnd"
            return {"type": "CIMMarkerPlacementAtExtremities", "angleToLine": bool(ml.rotateSymbols()),
                    "extremityPlacement": where}
        if _has(pl, Qgis.MarkerLinePlacement.CentralPoint):
            return {"type": "CIMMarkerPlacementAtRatioPositions", "angleToLine": bool(ml.rotateSymbols()),
                    "positionArray": [0.5]}
        return {"type": "CIMMarkerPlacementOnVertices", "angleToLine": bool(ml.rotateSymbols()),
                "placeOnRegularVertices": True}

    def _sub_markers(self, sub, placement, opacity):
        out = []
        for sl in reversed(list(sub.symbolLayers()) if sub else []):
            if not sl.enabled():
                continue
            m = self._marker_layer(sl, opacity * (sub.opacity() if sub else 1))
            if m is not None:
                m["markerPlacement"] = placement
                out.append(m)
        return out

    def _line_layers(self, sl, opacity):
        """A QGIS line symbol layer -> list of CIM symbol layers (top first)."""
        t = sl.layerType()
        if t == "SimpleLine":
            if sl.penStyle() == Qt.PenStyle.NoPen:
                return []
            w = to_pt(sl.width(), sl.widthUnit()) if sl.width() else 0.7
            dashes = None
            if sl.useCustomDashPattern():
                dashes = [to_pt(v, sl.customDashPatternUnit()) for v in sl.customDashVector()]
            elif sl.penStyle() != Qt.PenStyle.SolidLine:
                dashes = _pen_dashes(sl.penStyle(), w)
            off = -to_pt(sl.offset(), sl.offsetUnit()) if sl.offset() else 0.0
            return [_stroke(sl.color(), w, opacity, _CAP.get(sl.penCapStyle(), "Round"),
                            _JOIN.get(sl.penJoinStyle(), "Round"), dashes, off)]
        if t in ("MarkerLine", "HashLine"):
            if t == "HashLine":
                self.warn(tr("hashed lines are written as solid lines"))
                return [_stroke(sl.color(), 0.7, opacity)]
            return self._sub_markers(sl.subSymbol(), self._placement(sl), opacity)
        self.warn(tr("{kind} lines are written as solid lines").format(kind=t))
        return [_stroke(sl.color(), 0.7, opacity)]

    def _fill_layers(self, sl, opacity):
        t = sl.layerType()
        if t == "SimpleFill":
            out = []
            if sl.strokeStyle() != Qt.PenStyle.NoPen:
                w = to_pt(sl.strokeWidth(), sl.strokeWidthUnit()) if sl.strokeWidth() else 0.7
                out.append(_stroke(sl.strokeColor(), w, opacity, "Round", _JOIN.get(sl.penJoinStyle(), "Round"),
                                   _pen_dashes(sl.strokeStyle(), w)))
            bs = sl.brushStyle()
            if bs == Qt.BrushStyle.SolidPattern:
                out.append(_solid_fill(sl.color(), opacity))
            elif bs in _BRUSH_HATCH:
                for angle in _BRUSH_HATCH[bs]:
                    out.append({"type": "CIMHatchFill", "enable": True, "rotation": angle, "separation": 5,
                                "lineSymbol": {"type": "CIMLineSymbol",
                                               "symbolLayers": [_stroke(sl.color(), 0.5, opacity)]}})
            elif bs != Qt.BrushStyle.NoBrush:
                self.warn(tr("some fill patterns are written as solid fills"))
                out.append(_solid_fill(sl.color(), opacity))
            return out
        if t == "LinePatternFill":
            sub = sl.subSymbol()
            line = self.line_symbol(sub) if sub else {"type": "CIMLineSymbol", "symbolLayers": [
                _stroke(sl.color(), 0.5, opacity)]}
            return [{"type": "CIMHatchFill", "enable": True, "rotation": float(sl.lineAngle()),
                     "separation": to_pt(sl.distance(), sl.distanceUnit()) or 5, "lineSymbol": line}]
        if t == "PointPatternFill":
            place = {"type": "CIMMarkerPlacementInsidePolygon", "gridType": "Fixed", "clipping": "ClipAtBoundary",
                     "stepX": to_pt(sl.distanceX(), sl.distanceXUnit()) or 16,
                     "stepY": to_pt(sl.distanceY(), sl.distanceYUnit()) or 16}
            return self._sub_markers(sl.subSymbol(), place, opacity)
        if t == "CentroidFill":
            return self._sub_markers(sl.subSymbol(), {"type": "CIMMarkerPlacementPolygonCenter",
                                                      "method": "OnPolygon"}, opacity)
        if t == "GradientFill":
            self.warn(tr("gradient fills are written as two-colour gradients"))
            return [{"type": "CIMGradientFill", "enable": True, "angle": float(sl.angle()),
                     "gradientMethod": "Linear", "gradientSize": 100, "gradientSizeUnits": "Relative",
                     "gradientType": "Discrete", "interval": 100,
                     "colorRamp": {"type": "CIMLinearContinuousColorRamp",
                                   "fromColor": cim_color(sl.color(), opacity),
                                   "toColor": cim_color(sl.color2(), opacity)}}]
        if t in ("SimpleLine", "MarkerLine", "HashLine"):
            return self._line_layers(sl, opacity)
        self.warn(tr("{kind} fills are written as solid fills").format(kind=t))
        return [_solid_fill(sl.color(), opacity)]

    # -- whole symbols --
    def marker_symbol(self, sym):
        layers = []
        for sl in reversed(list(sym.symbolLayers())):
            if not sl.enabled():
                continue
            if sl.layerType() == "GeometryGenerator":
                self.warn(tr("geometry generator symbols are not written"))
                continue
            m = self._marker_layer(sl, sym.opacity())
            if m is not None:
                layers.append(m)
        d = {"type": "CIMPointSymbol", "symbolLayers": layers, "haloSize": 1, "scaleX": 1,
             "angleAlignment": "Display"}
        return d

    def line_symbol(self, sym):
        layers = []
        for sl in reversed(list(sym.symbolLayers())):
            if not sl.enabled():
                continue
            if sl.layerType() == "GeometryGenerator":
                self.warn(tr("geometry generator symbols are not written"))
                continue
            layers += self._line_layers(sl, sym.opacity())
        return {"type": "CIMLineSymbol", "symbolLayers": layers}

    def fill_symbol(self, sym):
        layers = []
        for sl in reversed(list(sym.symbolLayers())):
            if not sl.enabled():
                continue
            if sl.layerType() == "GeometryGenerator":
                self.warn(tr("geometry generator symbols are not written"))
                continue
            layers += self._fill_layers(sl, sym.opacity())
        return _poly(layers)

    def symbol(self, sym):
        """QgsSymbol -> CIMSymbolReference."""
        if sym is None:
            return None
        t = sym.type()
        if t == Qgis.SymbolType.Marker:
            return ref(self.marker_symbol(sym))
        if t == Qgis.SymbolType.Line:
            return ref(self.line_symbol(sym))
        return ref(self.fill_symbol(sym))

    # -- renderers --
    def _field_of(self, attr, what):
        """The field a renderer classifies on; expressions are reduced to their first field."""
        if not attr:
            return None
        e = QgsExpression(attr)
        if not e.hasParserError() and isinstance(e.rootNode(), QgsExpressionNodeColumnRef):
            return e.rootNode().name()
        if e.hasParserError() or attr.strip('"') == attr:
            return attr.strip('"')
        cols = sorted(e.referencedColumns())
        self.warn(tr("{what} by an expression is written by its first field").format(what=what))
        return cols[0] if cols else None

    def renderer(self, r):
        """QgsFeatureRenderer -> CIM renderer dict (None when nothing can be drawn)."""
        if r is None:
            return None
        t = r.type()
        if t == "singleSymbol":
            return {"type": "CIMSimpleRenderer", "patch": "Default", "symbol": self.symbol(r.symbol())}
        if t == "categorizedSymbol":
            return self._unique(r)
        if t == "graduatedSymbol":
            return self._breaks(r)
        if t in ("pointDisplacement", "pointCluster", "invertedPolygonRenderer", "mergedFeatureRenderer") \
                and r.embeddedRenderer() is not None:
            self.warn(tr("{kind} drawing is written as its inner symbology").format(kind=t))
            return self.renderer(r.embeddedRenderer())
        if t == "nullSymbol":
            self.warn(tr("layer without symbols is written with a default symbol"))
            return None
        syms = r.symbols(QgsRenderContext())
        if t == "RuleRenderer":
            self.warn(tr("rule-based symbology is written as a single symbol"))
        else:
            self.warn(tr("{kind} symbology is written as a single symbol").format(kind=t))
        return {"type": "CIMSimpleRenderer", "patch": "Default", "symbol": self.symbol(syms[0])} if syms else None

    @staticmethod
    def _value_text(v):
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            return "<Null>"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    def _unique(self, r):
        field = self._field_of(r.classAttribute(), tr("Categorized symbology"))
        classes, default = [], None
        for cat in r.categories():
            v = cat.value()
            if v == "" or (isinstance(v, list) and not v):
                default = cat
                continue
            values = v if isinstance(v, list) else [v]
            classes.append({"type": "CIMUniqueValueClass", "editable": True, "label": cat.label() or str(values[0]),
                            "patch": "Default", "symbol": self.symbol(cat.symbol()),
                            "values": [{"type": "CIMUniqueValue", "fieldValues": [self._value_text(x)]}
                                       for x in values],
                            "visible": bool(cat.renderState())})
        d = {"type": "CIMUniqueValueRenderer", "fields": [field] if field else [],
             "groups": [{"type": "CIMUniqueValueGroup", "classes": classes, "heading": field or ""}],
             "useDefaultSymbol": default is not None, "polygonSymbolColorTarget": "Fill"}
        if default is not None:
            d["defaultSymbol"] = self.symbol(default.symbol())
            d["defaultLabel"] = default.label() or "<all other values>"
            d["defaultSymbolPatch"] = "Default"
        return d

    def _breaks(self, r):
        field = self._field_of(r.classAttribute(), tr("Graduated symbology"))
        ranges = list(r.ranges())
        breaks = [{"type": "CIMClassBreak", "label": rg.label(), "patch": "Default",
                   "symbol": self.symbol(rg.symbol()), "upperBound": rg.upperValue()} for rg in ranges]
        size = getattr(r, "graduatedMethod", lambda: None)() == Qgis.GraduatedMethod.Size
        if any(not rg.renderState() for rg in ranges):
            self.warn(tr("hidden classes are shown in ArcGIS Pro"))
        return {"type": "CIMClassBreaksRenderer", "barrierWeight": "None",
                "classBreakType": "GraduatedSymbol" if size else "GraduatedColor",
                "classificationMethod": "Manual", "field": field or "", "heading": field or "",
                "minimumBreak": ranges[0].lowerValue() if ranges else 0, "breaks": breaks,
                "showInAscendingOrder": True, "polygonSymbolColorTarget": "Fill"}

    # -- labels --
    def text_symbol(self, fmt):
        """QgsTextFormat -> CIMTextSymbol."""
        font = fmt.font()
        style = font.styleName() or " ".join(s for s, on in (("Bold", font.bold()), ("Italic", font.italic())) if on)
        size = fmt.size()
        if fmt.sizeUnit() != Qgis.RenderUnit.Points:
            size = to_pt(size, fmt.sizeUnit())
        d = {"type": "CIMTextSymbol", "blockProgression": "TTB", "depth3D": 1, "extrapolateBaselines": True,
             "fontEffects": "Normal", "fontEncoding": "Unicode", "fontFamilyName": font.family() or "Tahoma",
             "fontStyleName": style or "Regular", "fontType": "Unspecified", "height": round(size, 3),
             "hinting": "Default", "horizontalAlignment": "Center", "kerning": True, "letterWidth": 100,
             "ligatures": True, "lineGapType": "ExtraLeading",
             "symbol": _poly([_solid_fill(fmt.color(), fmt.opacity())]), "textCase": "Normal",
             "textDirection": "LTR", "verticalAlignment": "Center", "verticalGlyphOrientation": "Right",
             "wordSpacing": 100, "billboardMode3D": "FaceNearPlane"}
        buf = fmt.buffer()
        if buf.enabled() and buf.size() > 0:
            d["haloSize"] = round(to_pt(buf.size(), buf.sizeUnit()), 3)
            d["haloSymbol"] = _poly([_solid_fill(buf.color(), buf.opacity())])
        if fmt.background().enabled() or fmt.shadow().enabled():
            self.warn(tr("label backgrounds and shadows are not written"))
        return d

    def _label_class(self, settings, name, geom, where=None):
        arcade, exact = to_arcade(settings.fieldName, settings.isExpression)
        if not arcade:
            self.warn(tr("a label expression could not be written"))
            return None
        if not exact:
            self.warn(tr("a label expression could not be written exactly: it shows one field"))
        feature_type = {Qgis.GeometryType.Line: "Line", Qgis.GeometryType.Polygon: "Polygon"}.get(geom, "Point")
        lc = {"type": "CIMLabelClass", "name": name, "expressionTitle": "Custom", "expression": arcade,
              "expressionEngine": "Arcade", "featuresToLabel": "AllVisibleFeatures", "priority": -1,
              "textSymbol": ref(self.text_symbol(settings.format())), "useCodedValue": True, "visibility": True,
              "iD": -1,
              "maplexLabelPlacementProperties": {
                  "type": "CIMMaplexLabelPlacementProperties", "featureType": feature_type,
                  "pointPlacementMethod": "AroundPoint", "linePlacementMethod": "OffsetCurvedFromLine",
                  "polygonPlacementMethod": "HorizontalInPolygon", "primaryOffset": 1,
                  "primaryOffsetUnit": "Point", "canRemoveOverlappingLabel": True},
              "standardLabelPlacementProperties": {
                  "type": "CIMStandardLabelPlacementProperties", "featureType": feature_type,
                  "featureWeight": "None", "labelWeight": "High", "numLabelsOption": "OneLabelPerName",
                  "pointPlacementMethod": "AroundPoint", "rotationType": "Arithmetic",
                  "polygonPlacementMethod": "AlwaysHorizontal"}}
        if where:
            lc["whereClause"] = where
        if settings.scaleVisibility:
            lc["minimumScale"], lc["maximumScale"] = float(settings.minimumScale), float(settings.maximumScale)
        return lc

    def label_classes(self, layer):
        lab = layer.labeling()
        if lab is None:
            return []
        geom = layer.geometryType()
        if isinstance(lab, QgsVectorLayerSimpleLabeling):
            lc = self._label_class(lab.settings(), "Class 1", geom)
            return [lc] if lc else []
        if isinstance(lab, QgsRuleBasedLabeling):
            out = []

            def walk(rule, filters):
                for child in rule.children():
                    if not child.active():
                        continue
                    f = filters + ([child.filterExpression()] if child.filterExpression() else [])
                    if child.settings() is not None:
                        joined = f[0] if len(f) == 1 else " AND ".join(f"({x})" for x in f)
                        where, exact = to_sql(joined) if f else ("", True)
                        if not exact:
                            self.warn(tr("a label rule filter may need checking in ArcGIS Pro"))
                        lc = self._label_class(child.settings(), child.description() or f"Class {len(out) + 1}",
                                               geom, where)
                        if lc:
                            out.append(lc)
                    walk(child, f)
                    if child.minimumScale() or child.maximumScale():
                        self.warn(tr("label rule scale ranges are not written"))

            walk(lab.rootRule(), [])
            return out
        self.warn(tr("this labelling is not written"))
        return []

    # -- rasters --
    def colorizer(self, layer):
        """QgsRasterRenderer -> CIM raster colorizer (None: let ArcGIS Pro choose)."""
        d = self._colorizer(layer)
        if d is not None and not d.get("bandIndex"):
            d.pop("bandIndex", None)  # band 1 is ArcGIS's default: Pro does not write it either
        return d

    def _colorizer(self, layer):
        r = layer.renderer()
        if r is None:
            return None
        t = r.type()
        if t == "multibandcolor":
            return {"type": "CIMRasterRGBColorizer", "redBandIndex": max(r.redBand() - 1, 0),
                    "greenBandIndex": max(r.greenBand() - 1, 0), "blueBandIndex": max(r.blueBand() - 1, 0),
                    "stretchType": "MinimumMaximum"}
        if t == "paletted":
            classes = [{"type": "CIMRasterUniqueValueClass", "values": [self._value_text(c.value)],
                        "label": c.label or self._value_text(c.value), "color": cim_color(c.color)}
                       for c in r.classes()]
            return {"type": "CIMRasterUniqueValueColorizer", "bandIndex": max(r.inputBand() - 1, 0),
                    "groups": [{"type": "CIMRasterUniqueValueGroup", "classes": classes}]}
        if t == "singlebandpseudocolor":
            fn = r.shader().rasterShaderFunction()
            items = fn.colorRampItemList()
            kind = fn.colorRampType()
            band = max(r.inputBand() - 1, 0)
            if kind == Qgis.ShaderInterpolationMethod.Discrete:
                return {"type": "CIMRasterClassifyColorizer", "bandIndex": band, "classificationMethod": "Manual",
                        "classBreaks": [{"type": "CIMRasterClassBreak", "upperBound": i.value,
                                         "label": i.label or f"{i.value:g}", "color": cim_color(i.color)}
                                        for i in items]}
            if kind == Qgis.ShaderInterpolationMethod.Exact:
                return {"type": "CIMRasterUniqueValueColorizer", "bandIndex": band,
                        "groups": [{"type": "CIMRasterUniqueValueGroup", "classes": [
                            {"type": "CIMRasterUniqueValueClass", "values": [self._value_text(i.value)],
                             "label": i.label or self._value_text(i.value), "color": cim_color(i.color)}
                            for i in items]}]}
            parts = [{"type": "CIMLinearContinuousColorRamp", "fromColor": cim_color(a.color),
                      "toColor": cim_color(b.color)} for a, b in zip(items, items[1:])]
            ramp = parts[0] if len(parts) == 1 else {"type": "CIMMultipartColorRamp", "colorRamps": parts,
                                                     "weights": [1] * len(parts)}
            if any(abs((b.value - a.value) - (items[-1].value - items[0].value) / max(len(parts), 1)) > 1e-6
                   for a, b in zip(items, items[1:])):
                self.warn(tr("unevenly spaced colour stops are spread evenly"))
            return {"type": "CIMRasterStretchColorizer", "bandIndex": band, "stretchType": "MinimumMaximum",
                    "useCustomStretchMinMax": True, "customStretchMin": r.classificationMin(),
                    "customStretchMax": r.classificationMax(), "colorRamp": ramp} if parts else None
        if t == "singlebandgray":
            ce = r.contrastEnhancement()
            d = {"type": "CIMRasterStretchColorizer", "bandIndex": max(r.inputBand() - 1, 0),
                 "stretchType": "MinimumMaximum",
                 "colorRamp": {"type": "CIMLinearContinuousColorRamp", "fromColor": cim_color(QColor("black")),
                               "toColor": cim_color(QColor("white"))}}
            if ce is not None:
                d.update(useCustomStretchMinMax=True, customStretchMin=ce.minimumValue(),
                         customStretchMax=ce.maximumValue())
            return d
        self.warn(tr("{kind} raster drawing is left to ArcGIS Pro's default").format(kind=t))
        return None
