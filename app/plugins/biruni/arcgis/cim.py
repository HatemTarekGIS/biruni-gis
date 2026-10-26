"""CIM (ArcGIS Pro Cartographic Information Model) -> QGIS styling.

Covers what most real projects use: colours in every CIM colour model, solid /
hatch / dot fills, strokes with dashes and offsets, vector / character /
picture markers (also along lines and inside polygons), simple, unique-value
and class-breaks renderers, labels (Arcade, VBScript, Python and JScript
expressions), definition queries, transparency, scale ranges, field aliases.
Anything else gets a sensible default and a warning - never an exception."""
import re
import warnings

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsExpression, QgsFillSymbol, QgsFontMarkerSymbolLayer,
                       QgsGraduatedSymbolRenderer, QgsLinePatternFillSymbolLayer, QgsLineSymbol,
                       QgsMarkerLineSymbolLayer, QgsMarkerSymbol, QgsPalLayerSettings, QgsPointPatternFillSymbolLayer,
                       QgsRasterMarkerSymbolLayer, QgsRendererCategory, QgsRendererRange, QgsRuleBasedLabeling,
                       QgsSimpleFillSymbolLayer, QgsSimpleLineSymbolLayer, QgsSimpleMarkerSymbolLayer,
                       QgsSingleSymbolRenderer, QgsSymbol, QgsTextBufferSettings, QgsTextFormat,
                       QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QPointF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase

from ..i18n import tr

PT = Qgis.RenderUnit.Points
POINT, LINE, POLYGON = Qgis.GeometryType.Point, Qgis.GeometryType.Line, Qgis.GeometryType.Polygon


# ---- colours ---------------------------------------------------------------
def _lab_to_rgb(L, a, b):
    """CIE L*a*b* (D65) -> sRGB 0-255."""
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def finv(t):
        return t ** 3 if t ** 3 > 0.008856 else (t - 16 / 116) / 7.787

    x, y, z = 0.95047 * finv(fx), 1.0 * finv(fy), 1.08883 * finv(fz)
    rgb = (3.2406 * x - 1.5372 * y - 0.4986 * z, -0.9689 * x + 1.8758 * y + 0.0415 * z,
           0.0557 * x - 0.2040 * y + 1.0570 * z)

    def gamma(c):
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        return max(0, min(255, round(c * 255)))

    return [gamma(c) for c in rgb]


def color(c, default=None):
    """A CIM colour object -> QColor (alpha 0-100 -> 0-255); default when absent."""
    if not isinstance(c, dict) or not c.get("values"):
        return QColor(default) if default is not None else None
    t, v = c.get("type", "CIMRGBColor"), list(c["values"])
    try:
        if t == "CIMHSVColor":
            q = QColor.fromHsvF((v[0] % 360) / 360, v[1] / 100, v[2] / 100)
            alpha = v[3] if len(v) > 3 else 100
        elif t == "CIMHSLColor":
            q = QColor.fromHslF((v[0] % 360) / 360, v[1] / 100, v[2] / 100)
            alpha = v[3] if len(v) > 3 else 100
        elif t == "CIMCMYKColor":
            q = QColor.fromCmykF(v[0] / 100, v[1] / 100, v[2] / 100, v[3] / 100)
            alpha = v[4] if len(v) > 4 else 100
        elif t == "CIMLABColor":
            q = QColor(*_lab_to_rgb(v[0], v[1], v[2]))
            alpha = v[3] if len(v) > 3 else 100
        elif t == "CIMGrayColor":
            g = max(0, min(255, round(v[0])))
            q = QColor(g, g, g)
            alpha = v[1] if len(v) > 1 else 100
        else:  # CIMRGBColor and unknown models stored as RGB
            q = QColor(*[max(0, min(255, round(x))) for x in v[:3]])
            alpha = v[3] if len(v) > 3 else 100
    except (TypeError, ValueError, IndexError):
        return QColor(default) if default is not None else None
    q.setAlpha(max(0, min(255, round(alpha * 255 / 100))))
    return q


# ---- expressions -----------------------------------------------------------
_FUNCS = {"upper": "upper", "ucase": "upper", "lower": "lower", "lcase": "lower", "round": "round",
          "text": "to_string", "str": "to_string", "cstr": "to_string", "proper": "title", "iif": "if",
          "abs": "abs", "sqrt": "sqrt", "left": "left", "right": "right", "trim": "trim", "isempty": "is_empty_or_null",
          "floor": "floor", "ceil": "ceil", "number": "to_real", "int": "to_int", "float": "to_real"}
_NEWLINES = {"textformatting.newline", "vbnewline", "vbcrlf", "vblf", "vbcr"}
_COMPLEX = re.compile(r"(\bvar\b|\breturn\b|\bfunction\b|\bdef\b|\bfor\b|\bwhile\b|\bif\s*\(|;|\{|\bend\s+function\b)",
                      re.IGNORECASE)
_ARCADE_FIELD = re.compile(r"\$feature\s*(?:\.\s*([^\W\d]\w*)|\[\s*([\"'])(.*?)\2\s*\])")
_BRACKET_FIELD = re.compile(r"\[([^\[\]\"']+)\]")


def referenced_fields(expr):
    out = []
    for m in _ARCADE_FIELD.finditer(expr or ""):
        out.append(m.group(1) or m.group(3))
    for m in _BRACKET_FIELD.finditer(expr or ""):
        out.append(m.group(1).strip())
    return [f for i, f in enumerate(out) if f and f not in out[:i]]


def translate_expression(expr, engine="Arcade"):
    """ArcGIS label/expression -> (QGIS text, is_expression, exact).

    A lone field comes back as a plain field name. Code blocks (functions,
    variables, loops) cannot be translated: the first field they use is
    returned with exact=False."""
    src = (expr or "").strip()
    engine = (engine or "VBScript").lower()
    if not src:
        return None, False, True
    fields = referenced_fields(src)
    if _COMPLEX.search(src):
        return (fields[0] if fields else None), False, False
    out, i, n, has_str = [], 0, len(src), False
    while i < n:
        ch = src[i]
        if ch in "\"'" and not (engine == "vbscript" and ch == "'"):
            j, buf = i + 1, []
            while j < n:
                if src[j] == "\\" and engine != "vbscript" and j + 1 < n:
                    nxt = src[j + 1]
                    buf.append("\n" if nxt == "n" else nxt)
                    j += 2
                    continue
                if src[j] == ch:
                    if engine == "vbscript" and j + 1 < n and src[j + 1] == ch:
                        buf.append(ch)
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            lit = "".join(buf).replace("'", "''").replace("\n", "\\n")
            out.append(f"'{lit}'")
            has_str = True
            i = j + 1
            continue
        if src.startswith("$feature", i):
            m = _ARCADE_FIELD.match(src, i)
            if m:
                out.append('"%s"' % (m.group(1) or m.group(3)))
                i = m.end()
                continue
        if ch == "[" and engine != "arcade":
            m = _BRACKET_FIELD.match(src, i)
            if m:
                out.append('"%s"' % m.group(1).strip())
                i = m.end()
                continue
        if src.startswith("&&", i):
            out.append(" AND ")
            i += 2
            continue
        if src.startswith("||", i):
            out.append(" OR ")
            i += 2
            continue
        if src.startswith("==", i):
            out.append(" = ")
            i += 2
            continue
        if ch == "&" and engine == "vbscript":
            out.append("\x00")
            has_str = True
            i += 1
            continue
        m = re.match(r"[^\W\d][\w.]*", src[i:])
        if m:
            word = m.group(0)
            if word.lower() in _NEWLINES:
                out.append("'\\n'")
                has_str = True
            elif word.lower() in ("and", "or", "not") and engine in ("vbscript", "python"):
                out.append(f" {word.upper()} ")
            else:
                out.append(_FUNCS.get(word.lower(), word))
            i += len(word)
            continue
        out.append("\x00" if ch == "+" else ch)
        i += 1
    text = "".join(out).replace("\x00", " || " if has_str else " + ").strip()
    text = re.sub(r"\s{2,}", " ", text)
    lone = re.fullmatch(r'"([^"]+)"', text)
    if lone:
        return lone.group(1), False, True
    if QgsExpression(text).hasParserError():
        return (fields[0] if fields else None), False, False
    return text, True, True


# ---- symbols ---------------------------------------------------------------
def _sym(ref):
    """Unwrap a CIMSymbolReference."""
    if isinstance(ref, dict) and ref.get("type") == "CIMSymbolReference":
        return ref.get("symbol") or {}
    return ref or {}


def _enabled(layers):
    return [sl for sl in (layers or []) if isinstance(sl, dict) and sl.get("enable", True)]


class Converter:
    """Collects warnings while converting; one instance per document."""

    def __init__(self):
        self.warnings = []

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    # -- symbol layers --
    def _stroke(self, sl):
        line = QgsSimpleLineSymbolLayer(color(sl.get("color"), "#808080"), float(sl.get("width", 1)))
        line.setWidthUnit(PT)
        cap = {"Butt": Qt.PenCapStyle.FlatCap, "Square": Qt.PenCapStyle.SquareCap}.get(sl.get("capStyle"),
                                                                                       Qt.PenCapStyle.RoundCap)
        join = {"Miter": Qt.PenJoinStyle.MiterJoin, "Bevel": Qt.PenJoinStyle.BevelJoin}.get(sl.get("joinStyle"),
                                                                                           Qt.PenJoinStyle.RoundJoin)
        line.setPenCapStyle(cap)
        line.setPenJoinStyle(join)
        for fx in sl.get("effects") or []:
            t = fx.get("type")
            if t == "CIMGeometricEffectDashes":
                tpl = [float(x) for x in fx.get("dashTemplate") or [] if float(x) >= 0]
                if len(tpl) >= 2:
                    if len(tpl) % 2:
                        tpl.append(tpl[-1])
                    line.setUseCustomDashPattern(True)
                    line.setCustomDashVector(tpl)
                    line.setCustomDashPatternUnit(PT)
                else:
                    line.setPenStyle(Qt.PenStyle.DashLine)
            elif t == "CIMGeometricEffectOffset":
                line.setOffset(-float(fx.get("offset", 0)))
                line.setOffsetUnit(PT)
        return line

    def _fill(self, sl):
        t = sl.get("type")
        if t == "CIMSolidFill":
            f = QgsSimpleFillSymbolLayer()
            f.setColor(color(sl.get("color"), "#cccccc"))
            f.setStrokeStyle(Qt.PenStyle.NoPen)
            return f
        if t == "CIMHatchFill":
            hatch = QgsLinePatternFillSymbolLayer()
            hatch.setLineAngle(float(sl.get("rotation", 0)))
            hatch.setDistance(float(sl.get("separation", 5)))
            hatch.setDistanceUnit(PT)
            sub = self.line_symbol(_sym(sl.get("lineSymbol")))
            hatch.setSubSymbol(sub)
            return hatch
        if t == "CIMGradientFill":
            f = QgsSimpleFillSymbolLayer()
            ramp = sl.get("colorRamp") or {}
            c = color(ramp.get("fromColor") or (ramp.get("colorRamps") or [{}])[0].get("fromColor"), "#cccccc")
            f.setColor(c)
            f.setStrokeStyle(Qt.PenStyle.NoPen)
            self.warn(tr("gradient fills are shown as solid fills"))
            return f
        if t == "CIMPictureFill":
            self.warn(tr("picture fills are shown as a plain fill"))
            f = QgsSimpleFillSymbolLayer()
            f.setColor(QColor(200, 200, 200, 120))
            f.setStrokeStyle(Qt.PenStyle.NoPen)
            return f
        return None

    def _marker_layer(self, sl):
        """One CIM marker symbol layer -> QGIS marker symbol layer (or None)."""
        t = sl.get("type")
        size = float(sl.get("size", 6))
        out = None
        if t == "CIMVectorMarker":
            out = self._vector_marker(sl, size)
        elif t == "CIMCharacterMarker":
            family = sl.get("fontFamilyName") or "Arial"
            fill = self._polygon_colors(_sym(sl.get("symbol")))[0] or QColor("black")
            if family in QFontDatabase.families():
                out = QgsFontMarkerSymbolLayer(family, chr(int(sl.get("characterIndex", 65))), size, fill)
            else:
                self.warn(tr("font '{font}' is not installed: its markers are drawn as circles").format(font=family))
                out = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Circle, size * 0.6)
                out.setColor(fill)
                out.setStrokeColor(fill.darker(150))
        elif t == "CIMPictureMarker":
            url = sl.get("url") or ""
            if url.startswith("data:") and "base64," in url:
                out = QgsRasterMarkerSymbolLayer("base64:" + url.split("base64,", 1)[1], size)
            else:
                self.warn(tr("picture markers without embedded images are drawn as circles"))
                out = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Circle, size)
        if out is None:
            return None
        out.setSizeUnit(PT)
        if sl.get("rotation"):
            out.setAngle(-float(sl["rotation"]))
        if sl.get("offsetX") or sl.get("offsetY"):
            out.setOffset(QPointF(float(sl.get("offsetX", 0)), -float(sl.get("offsetY", 0))))
            out.setOffsetUnit(PT)
        return out

    def _polygon_colors(self, poly):
        """(fill QColor, stroke QColor, stroke width) of a CIMPolygon/Line symbol."""
        fill = stroke = None
        width = 0.7
        for sl in _enabled(poly.get("symbolLayers")):
            if sl.get("type") == "CIMSolidFill" and fill is None:
                fill = color(sl.get("color"))
            elif sl.get("type") == "CIMSolidStroke" and stroke is None:
                stroke = color(sl.get("color"))
                width = float(sl.get("width", 0.7))
        return fill, stroke, width

    def _vector_marker(self, sl, size):
        graphics = [g for g in sl.get("markerGraphics") or [] if isinstance(g, dict)]
        if not graphics:
            return QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Circle, size)
        g = graphics[0]
        geom = g.get("geometry") or {}
        sym = _sym(g.get("symbol"))
        fill, stroke, width = self._polygon_colors(sym)
        shape = Qgis.MarkerShape.Circle
        if "curveRings" in geom:
            shape = Qgis.MarkerShape.Circle
        elif geom.get("rings"):
            ring = [p for p in geom["rings"][0] if isinstance(p, list)]
            if len(ring) > 1 and ring[0][:2] == ring[-1][:2]:
                ring = ring[:-1]
            n = len(ring)
            if n == 3:
                shape = Qgis.MarkerShape.Triangle
            elif n == 4:
                xs = {round(p[0], 6) for p in ring}
                shape = Qgis.MarkerShape.Square if len(xs) == 2 else Qgis.MarkerShape.Diamond
            elif n == 5:
                shape = Qgis.MarkerShape.Pentagon
            elif n == 6:
                shape = Qgis.MarkerShape.Hexagon
            elif n == 10:
                shape = Qgis.MarkerShape.Star
            elif n == 12:
                shape = Qgis.MarkerShape.CrossFill
        elif geom.get("paths"):
            seg = geom["paths"][0]
            axis = len(seg) >= 2 and (abs(seg[0][0] - seg[-1][0]) < 1e-9 or abs(seg[0][1] - seg[-1][1]) < 1e-9)
            shape = Qgis.MarkerShape.Cross if axis else Qgis.MarkerShape.Cross2
            fill = None
        out = QgsSimpleMarkerSymbolLayer(shape, size)
        if fill is not None:
            out.setColor(fill)
        else:
            out.setColor(QColor(0, 0, 0, 0))
        if stroke is not None:
            out.setStrokeColor(stroke)
            out.setStrokeWidth(width)
            out.setStrokeWidthUnit(PT)
        else:
            out.setStrokeStyle(Qt.PenStyle.NoPen)
        return out

    # -- whole symbols --
    def marker_symbol(self, cim):
        layers = []
        for sl in reversed(_enabled(cim.get("symbolLayers"))):
            m = self._marker_layer(sl)
            if m is not None:
                layers.append(m)
        if not layers:
            if cim:
                self.warn(tr("some point symbols use a default marker"))
            return QgsMarkerSymbol.createSimple({"name": "circle", "color": "#3d85c6", "size": "2"})
        sym = QgsMarkerSymbol(layers)
        if cim.get("angle"):
            sym.setAngle(-float(cim["angle"]))
        return sym

    def _line_marker(self, sl):
        place = sl.get("markerPlacement") or {}
        marker = self._marker_layer(sl)
        if marker is None:
            return None
        ml = QgsMarkerLineSymbolLayer(True, 10)
        ml.setSubSymbol(QgsMarkerSymbol([marker]))
        pt = place.get("type", "")
        if pt == "CIMMarkerPlacementAtExtremities":
            where = place.get("extremityPlacement", "Both")
            flags = {"JustBegin": Qgis.MarkerLinePlacement.FirstVertex,
                     "JustEnd": Qgis.MarkerLinePlacement.LastVertex}.get(where)
            ml.setPlacements(flags if flags is not None else
                             Qgis.MarkerLinePlacement.FirstVertex | Qgis.MarkerLinePlacement.LastVertex)
        elif pt == "CIMMarkerPlacementOnVertices":
            ml.setPlacements(Qgis.MarkerLinePlacement.InnerVertices)
        elif pt == "CIMMarkerPlacementAtRatioPositions":
            ml.setPlacements(Qgis.MarkerLinePlacement.CentralPoint)
        else:
            tpl = [float(x) for x in place.get("placementTemplate") or [] if float(x) > 0]
            ml.setInterval(sum(tpl) if tpl else 20)
            ml.setIntervalUnit(PT)
            ml.setPlacements(Qgis.MarkerLinePlacement.Interval)
        ml.setRotateSymbols(bool(place.get("angleToLine", True)))
        return ml

    def line_symbol(self, cim):
        layers = []
        for sl in reversed(_enabled(cim.get("symbolLayers"))):
            t = sl.get("type")
            if t == "CIMSolidStroke":
                layers.append(self._stroke(sl))
            elif t in ("CIMVectorMarker", "CIMCharacterMarker", "CIMPictureMarker"):
                ml = self._line_marker(sl)
                if ml is not None:
                    layers.append(ml)
            elif t == "CIMPictureStroke" or t == "CIMGradientStroke":
                fake = dict(sl, type="CIMSolidStroke", color=(sl.get("colorRamp") or {}).get("fromColor"))
                layers.append(self._stroke(fake))
                self.warn(tr("picture/gradient strokes are shown as plain lines"))
        if not layers:
            if cim:
                self.warn(tr("some line symbols use a default line"))
            return QgsLineSymbol.createSimple({"color": "#3d85c6", "width": "0.5"})
        return QgsLineSymbol(layers)

    def fill_symbol(self, cim):
        layers = []
        for sl in reversed(_enabled(cim.get("symbolLayers"))):
            t = sl.get("type")
            if t == "CIMSolidStroke":
                layers.append(self._stroke(sl))
            elif t in ("CIMVectorMarker", "CIMCharacterMarker", "CIMPictureMarker"):
                marker = self._marker_layer(sl)
                if marker is None:
                    continue
                place = sl.get("markerPlacement") or {}
                if place.get("type") == "CIMMarkerPlacementPolygonCenter":
                    from qgis.core import QgsCentroidFillSymbolLayer
                    c = QgsCentroidFillSymbolLayer()
                    c.setSubSymbol(QgsMarkerSymbol([marker]))
                    layers.append(c)
                else:
                    p = QgsPointPatternFillSymbolLayer()
                    p.setDistanceX(float(place.get("stepX", 16)))
                    p.setDistanceY(float(place.get("stepY", 16)))
                    p.setDistanceXUnit(PT)
                    p.setDistanceYUnit(PT)
                    p.setSubSymbol(QgsMarkerSymbol([marker]))
                    layers.append(p)
            else:
                f = self._fill(sl)
                if f is not None:
                    layers.append(f)
        if not layers:
            if cim:
                self.warn(tr("some polygon symbols use a default fill"))
            return QgsFillSymbol.createSimple({"color": "#a6cee3", "outline_color": "#1f78b4"})
        return QgsFillSymbol(layers)

    def symbol(self, ref, geom_type):
        cim = _sym(ref)
        t = cim.get("type", "")
        # a symbol type that does not match the geometry (e.g. a marker for polygons)
        if geom_type == POINT or (t == "CIMPointSymbol" and geom_type not in (LINE, POLYGON)):
            return self.marker_symbol(cim if t == "CIMPointSymbol" else {})
        if geom_type == LINE:
            return self.line_symbol(cim if t == "CIMLineSymbol" else {})
        if geom_type == POLYGON:
            return self.fill_symbol(cim if t == "CIMPolygonSymbol" else {})
        return QgsSymbol.defaultSymbol(geom_type)

    # -- renderers --
    def renderer(self, r, geom_type):
        if not isinstance(r, dict) or geom_type not in (POINT, LINE, POLYGON):
            return None
        t = r.get("type")
        if t == "CIMSimpleRenderer":
            return QgsSingleSymbolRenderer(self.symbol(r.get("symbol"), geom_type))
        if t == "CIMUniqueValueRenderer":
            return self._unique(r, geom_type)
        if t == "CIMClassBreaksRenderer":
            return self._breaks(r, geom_type)
        self.warn(tr("{kind} symbology is not supported yet: a single symbol is used").format(kind=t or "?"))
        sym = r.get("symbol") or r.get("defaultSymbol")
        return QgsSingleSymbolRenderer(self.symbol(sym, geom_type) if sym else QgsSymbol.defaultSymbol(geom_type))

    def _unique(self, r, geom_type):
        fields = [f for f in r.get("fields") or [] if f]
        if not fields:
            return QgsSingleSymbolRenderer(self.symbol(r.get("defaultSymbol"), geom_type))
        delim = r.get("fieldDelimiter") or ","
        if len(fields) == 1:
            attr = fields[0]
        else:
            sep = QgsExpression.quotedString(delim)
            attr = "concat(" + f", {sep}, ".join(QgsExpression.quotedColumnRef(f) for f in fields) + ")"
        cats = []
        for group in r.get("groups") or []:
            for cls in group.get("classes") or []:
                values = []
                for v in cls.get("values") or []:
                    parts = ["" if x == "<Null>" else str(x) for x in v.get("fieldValues") or []]
                    values.append(delim.join(parts))
                if not values:
                    continue
                cats.append(QgsRendererCategory(values if len(values) > 1 else values[0],
                                                self.symbol(cls.get("symbol"), geom_type),
                                                cls.get("label") or values[0], bool(cls.get("visible", True))))
        if r.get("useDefaultSymbol") and r.get("defaultSymbol"):
            cats.append(QgsRendererCategory("", self.symbol(r.get("defaultSymbol"), geom_type),
                                            r.get("defaultLabel") or "<all other values>"))
        if r.get("visualVariables"):
            self.warn(tr("size/colour visual variables are not converted"))
        return QgsCategorizedSymbolRenderer(attr, cats)

    def _breaks(self, r, geom_type):
        field = r.get("field")
        if not field:
            return QgsSingleSymbolRenderer(QgsSymbol.defaultSymbol(geom_type))
        if r.get("normalizationType") not in (None, "Nothing") or r.get("normalizationField"):
            self.warn(tr("normalised class breaks are drawn without normalisation"))
        lower = float(r.get("minimumBreak", 0) or 0)
        ranges = []
        for b in r.get("breaks") or []:
            upper = float(b.get("upperBound", lower))
            ranges.append(QgsRendererRange(lower, upper, self.symbol(b.get("symbol"), geom_type),
                                           b.get("label") or f"{lower:g} - {upper:g}"))
            lower = upper
        return QgsGraduatedSymbolRenderer(field, ranges)

    # -- labels --
    def _text_format(self, ts):
        ts = _sym(ts)
        fmt = QgsTextFormat()
        font = QFont(ts.get("fontFamilyName") or "Tahoma")
        style = (ts.get("fontStyleName") or "").lower()
        font.setBold("bold" in style)
        font.setItalic("italic" in style or "oblique" in style)
        fmt.setFont(font)
        fmt.setSize(float(ts.get("height", 10)))
        fmt.setSizeUnit(PT)
        fill = self._polygon_colors(_sym(ts.get("symbol")))[0]
        fmt.setColor(fill or QColor("black"))
        halo = _sym(ts.get("haloSymbol"))
        if halo and float(ts.get("haloSize", 0) or 0) > 0:
            buf = QgsTextBufferSettings()
            buf.setEnabled(True)
            buf.setSize(float(ts["haloSize"]))
            buf.setSizeUnit(PT)
            buf.setColor(self._polygon_colors(halo)[0] or QColor("white"))
            fmt.setBuffer(buf)
        return fmt

    def _label_settings(self, lc, geom_type):
        text, is_expr, exact = translate_expression(lc.get("expression"), lc.get("expressionEngine"))
        if not text:
            return None
        if not exact:
            self.warn(tr("a label expression could not be translated exactly: it shows one field"))
        s = QgsPalLayerSettings()
        s.fieldName = text
        s.isExpression = is_expr
        s.setFormat(self._text_format(lc.get("textSymbol")))
        if geom_type == LINE:
            s.placement = Qgis.LabelPlacement.Line
        elif geom_type == POLYGON:
            s.placement = Qgis.LabelPlacement.Horizontal
        else:
            s.placement = Qgis.LabelPlacement.AroundPoint
        mn, mx = float(lc.get("minimumScale", 0) or 0), float(lc.get("maximumScale", 0) or 0)
        if mn or mx:
            s.scaleVisibility = True
            s.minimumScale, s.maximumScale = mn, mx
        return s

    def labeling(self, classes, geom_type):
        """Label classes -> (QgsAbstractVectorLayerLabeling or None)."""
        classes = [c for c in classes or [] if isinstance(c, dict) and c.get("visibility", True)]
        built = []
        for lc in classes:
            s = self._label_settings(lc, geom_type)
            if s is not None:
                built.append((lc, s))
        if not built:
            return None
        if len(built) == 1 and not built[0][0].get("whereClause"):
            return QgsVectorLayerSimpleLabeling(built[0][1])
        root = QgsRuleBasedLabeling.Rule(None)
        for lc, s in built:
            rule = QgsRuleBasedLabeling.Rule(s)
            rule.setDescription(lc.get("name") or "")
            if lc.get("whereClause"):
                rule.setFilterExpression(lc["whereClause"])
            root.appendChild(rule)
        return QgsRuleBasedLabeling(root)

    # -- whole layer --
    def apply_layer(self, layer, cim):
        """Style a valid QGIS layer from its CIM definition."""
        from qgis.core import QgsVectorLayer

        t = float(cim.get("transparency", 0) or 0)
        if t:
            layer.setOpacity(max(0.0, 1 - t / 100))
        mn, mx = float(cim.get("minScale", 0) or 0), float(cim.get("maxScale", 0) or 0)
        if mn or mx:
            layer.setScaleBasedVisibility(True)
            layer.setMinimumScale(mn)
            layer.setMaximumScale(mx)
        if not isinstance(layer, QgsVectorLayer):
            from qgis.core import QgsRasterLayer
            if isinstance(layer, QgsRasterLayer) and cim.get("colorizer"):
                self.apply_raster(layer, cim["colorizer"])
            return
        gt = layer.geometryType()
        table = cim.get("featureTable") or {}
        expr = table.get("definitionExpression")
        if expr and not layer.setSubsetString(expr):
            self.warn(tr("definition query not applied: {query}").format(query=expr))
        if table.get("displayField"):
            layer.setDisplayExpression(QgsExpression.quotedColumnRef(table["displayField"]))
        hidden = []
        for fd in table.get("fieldDescriptions") or []:
            idx = layer.fields().lookupField(fd.get("fieldName") or "")
            if idx < 0:
                continue
            alias = fd.get("alias")
            if alias and alias != fd.get("fieldName"):
                layer.setFieldAlias(idx, alias)
            if fd.get("visible") is False:
                hidden.append(layer.fields()[idx].name())
        if hidden:
            cfg = layer.attributeTableConfig()
            cols = cfg.columns()
            for c in cols:
                if c.name in hidden:
                    c.hidden = True
            cfg.setColumns(cols)
            layer.setAttributeTableConfig(cfg)
        renderer = self.renderer(cim.get("renderer"), gt)
        if renderer is not None:
            layer.setRenderer(renderer)
        labeling = self.labeling(cim.get("labelClasses"), gt)
        if labeling is not None:
            layer.setLabeling(labeling)
            layer.setLabelsEnabled(bool(cim.get("labelVisibility", False)))


    # -- rasters --
    def _ramp_colors(self, ramp):
        ramp = ramp or {}
        t = ramp.get("type", "")
        if t == "CIMMultipartColorRamp":
            out = []
            for part in ramp.get("colorRamps") or []:
                cols = self._ramp_colors(part)
                out.extend(cols if not out else cols[1:] if cols and out[-1] == cols[0] else cols)
            return out
        if t == "CIMFixedColorRamp":
            return [color(c) for c in ramp.get("colors") or [] if color(c) is not None]
        cols = [color(ramp.get("fromColor")), color(ramp.get("toColor"))]
        return [c for c in cols if c is not None]

    def apply_raster(self, layer, cz):
        from qgis.core import (QgsColorRampShader, QgsContrastEnhancement, QgsMultiBandColorRenderer,
                               QgsPalettedRasterRenderer, QgsRasterShader, QgsSingleBandGrayRenderer,
                               QgsSingleBandPseudoColorRenderer)

        provider = layer.dataProvider()
        t = cz.get("type")
        band = int(cz.get("bandIndex", 0) or 0) + 1
        if band > layer.bandCount():
            band = 1
        if t == "CIMRasterRGBColorizer":
            bands = [int(cz.get(k, i)) + 1 for i, k in enumerate(("redBandIndex", "greenBandIndex", "blueBandIndex"))]
            if layer.bandCount() >= 3 and all(1 <= b <= layer.bandCount() for b in bands) and bands != [1, 2, 3]:
                layer.setRenderer(QgsMultiBandColorRenderer(provider, *bands))
            return
        if t == "CIMRasterUniqueValueColorizer":
            classes = []
            for g in cz.get("groups") or []:
                for cls in g.get("classes") or []:
                    for v in cls.get("values") or []:
                        try:
                            classes.append(QgsPalettedRasterRenderer.Class(float(v), color(cls.get("color"), "gray"),
                                                                           cls.get("label") or str(v)))
                        except (TypeError, ValueError):
                            self.warn(tr("raster classes by text values are not converted"))
            if classes:
                layer.setRenderer(QgsPalettedRasterRenderer(provider, band, classes))
            return
        if t == "CIMRasterClassifyColorizer":
            items = [QgsColorRampShader.ColorRampItem(float(b.get("upperBound", 0)), color(b.get("color"), "gray"),
                                                      b.get("label") or "")
                     for b in cz.get("classBreaks") or []]
            if not items:
                return
            shader = QgsColorRampShader(items[0].value, items[-1].value)
            shader.setColorRampType(Qgis.ShaderInterpolationMethod.Discrete)
            shader.setColorRampItemList(items)
            rs = QgsRasterShader()
            rs.setRasterShaderFunction(shader)
            r = QgsSingleBandPseudoColorRenderer(provider, band, rs)
            r.setClassificationMin(items[0].value)
            r.setClassificationMax(items[-1].value)
            layer.setRenderer(r)
            return
        if t != "CIMRasterStretchColorizer":
            self.warn(tr("raster colours use the default stretch"))
            return
        mn, mx = self._stretch_range(layer, band, cz)
        if mn is None or mx <= mn:
            return
        cols = self._ramp_colors(cz.get("colorRamp"))
        if cz.get("invert"):
            cols.reverse()
        gray = len(cols) == 2 and all(c.saturation() == 0 for c in cols) and cols[0].value() < cols[1].value()
        if not cols or gray:
            r = QgsSingleBandGrayRenderer(provider, band)
            ce = QgsContrastEnhancement(provider.dataType(band))
            ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum)
            ce.setMinimumValue(mn)
            ce.setMaximumValue(mx)
            r.setContrastEnhancement(ce)
            layer.setRenderer(r)
            return
        n = len(cols)
        items = [QgsColorRampShader.ColorRampItem(mn + (mx - mn) * i / max(n - 1, 1), c, "") for i, c in enumerate(cols)]
        items[0].label, items[-1].label = f"{mn:g}", f"{mx:g}"
        shader = QgsColorRampShader(mn, mx)
        shader.setColorRampType(Qgis.ShaderInterpolationMethod.Linear)
        shader.setColorRampItemList(items)
        rs = QgsRasterShader()
        rs.setRasterShaderFunction(shader)
        r = QgsSingleBandPseudoColorRenderer(provider, band, rs)
        r.setClassificationMin(mn)
        r.setClassificationMax(mx)
        layer.setRenderer(r)

    def _stretch_range(self, layer, band, cz):
        """(min, max) of a stretch: ArcGIS's saved statistics, else the data's."""
        st = cz.get("stretchStats") or {}
        kind = cz.get("stretchType", "MinimumMaximum")
        if kind == "Custom" or cz.get("useCustomStretchMinMax"):
            lo, hi = cz.get("customStretchMin"), cz.get("customStretchMax")
            if lo is not None and hi is not None and float(hi) > float(lo):
                return float(lo), float(hi)
        if st.get("min") is None or st.get("max") is None:
            with warnings.catch_warnings():  # every overload warns in the QGIS 4 bindings
                warnings.simplefilter("ignore", DeprecationWarning)
                s = layer.dataProvider().bandStatistics(band, Qgis.RasterBandStatistic.All)
            st = {"min": s.minimumValue, "max": s.maximumValue, "mean": s.mean, "stddev": s.stdDev}
        lo, hi = float(st["min"]), float(st["max"])
        if kind == "StandardDeviations" and st.get("stddev"):
            n = float(cz.get("standardDeviationParam", 2) or 2)
            mean, sd = float(st.get("mean", (lo + hi) / 2)), float(st["stddev"])
            return max(lo, mean - n * sd), min(hi, mean + n * sd)
        if kind == "PercentMinimumMaximum":
            lo_p = float(cz.get("minPercent", 0.5) or 0) / 100
            hi_p = 1 - float(cz.get("maxPercent", 0.5) or 0) / 100
            try:
                cut = layer.dataProvider().cumulativeCut(band, lo_p, hi_p, layer.extent(), 250000)
                if cut and cut[1] > cut[0]:
                    return float(cut[0]), float(cut[1])
            except Exception:  # noqa: BLE001 - fall back to min/max
                pass
        return lo, hi


def crs_from_spatial_reference(sr):
    """CIM spatialReference -> QgsCoordinateReferenceSystem (invalid when unknown)."""
    from qgis.core import QgsCoordinateReferenceSystem

    if not isinstance(sr, dict):
        return QgsCoordinateReferenceSystem()
    for key in ("latestWkid", "wkid"):
        code = sr.get(key)
        if code:
            for auth in ("EPSG", "ESRI"):
                crs = QgsCoordinateReferenceSystem(f"{auth}:{int(code)}")
                if crs.isValid():
                    return crs
    if sr.get("wkt"):
        return QgsCoordinateReferenceSystem.fromWkt(sr["wkt"])
    return QgsCoordinateReferenceSystem()
