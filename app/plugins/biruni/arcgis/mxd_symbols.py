"""Symbology of ArcMap documents and layer files (.mxd / .mxt / .lyr).

ArcMap persists each layer's renderer and symbols as ArcObjects, one after
another: a class id (GUID), an object id, a version and the object's fields.
An object written once is referred to later by its class id and object id
only. The layouts below were worked out by comparing hundreds of real
documents with what ArcGIS itself makes of them; they are turned into CIM
JSON (the dictionaries ArcGIS Pro writes) so the shared converter in cim.py
styles the layers:

    renderers  - simple, unique values (up to three fields), class breaks
    symbols    - simple / cartographic / multi-layer lines, simple / multi-layer
                 fills (solid, hatched, hollow), simple / character / multi-layer
                 markers; other symbols keep their main colour
    colours    - every colour model (stored as CIELAB), transparency, "no colour"
    labels     - expression, font, size, colour, and whether labels are shown

Pure Python (no QGIS), like mxd.py."""
import math
import struct
import uuid


def _g(s):
    return uuid.UUID(s).bytes_le


_CLASSES = {
    # colours
    "RgbColor": "7EE9C496-D123-11D0-8383-080009B996CC", "HsvColor": "7EE9C492-D123-11D0-8383-080009B996CC",
    "CmykColor": "7EE9C497-D123-11D0-8383-080009B996CC", "GrayColor": "7EE9C495-D123-11D0-8383-080009B996CC",
    "HlsColor": "7EE9C493-D123-11D0-8383-080009B996CC",
    # line symbols
    "SimpleLineSymbol": "7914E5F9-C892-11D0-8BB6-080009EE4E41",
    "CartographicLineSymbol": "7914E5FB-C892-11D0-8BB6-080009EE4E41",
    "MultiLayerLineSymbol": "7914E5FA-C892-11D0-8BB6-080009EE4E41",
    "HashLineSymbol": "7914E5FC-C892-11D0-8BB6-080009EE4E41",
    "MarkerLineSymbol": "7914E5FD-C892-11D0-8BB6-080009EE4E41",
    "PictureLineSymbol": "22C8C5A1-84FC-11D4-834D-0080C79F0371",
    # fill symbols
    "SimpleFillSymbol": "7914E603-C892-11D0-8BB6-080009EE4E41",
    "MultiLayerFillSymbol": "7914E604-C892-11D0-8BB6-080009EE4E41",
    "LineFillSymbol": "7914E606-C892-11D0-8BB6-080009EE4E41",
    "MarkerFillSymbol": "7914E608-C892-11D0-8BB6-080009EE4E41",
    "PictureFillSymbol": "D842B082-330C-11D2-9168-0000F87808EE",
    "GradientFillSymbol": "7914E609-C892-11D0-8BB6-080009EE4E41",
    # marker symbols
    "SimpleMarkerSymbol": "7914E5FE-C892-11D0-8BB6-080009EE4E41",
    "CharacterMarkerSymbol": "7914E600-C892-11D0-8BB6-080009EE4E41",
    "PictureMarkerSymbol": "7914E602-C892-11D0-8BB6-080009EE4E41",
    "MultiLayerMarkerSymbol": "7914E5FF-C892-11D0-8BB6-080009EE4E41",
    "ArrowMarkerSymbol": "88539431-E06E-11D1-B277-0000F878229E",
    # renderers and legends
    "SimpleRenderer": "F3435801-5779-11D0-98BF-00805F7CED21",
    "UniqueValueRenderer": "C3346D29-B2BC-11D1-8817-080009EC732A",
    "ClassBreaksRenderer": "AE5F7EA2-8B48-11D0-8356-080009B996CC",
    "ProportionalSymbolRenderer": "4EAB568E-8F9C-11D2-AB21-00C04FA334B3",
    "DotDensityRenderer": "9C7776BA-0421-11D4-9F7C-00C04F6BC709",
    "ChartRenderer": "4F17939A-C490-11D3-9F7A-00C04F6BC709",
    "BiUniqueValueRenderer": "B899CCD3-CD1C-11D2-9F25-00C04F6BC709",
    "ScaleDependentRenderer": "207C19F5-ED81-11D0-8BBA-080009EE4E41",
    "LegendGroup": "167C5EA2-AF20-11D1-8817-080009EC732A",
    "LegendClass": "167C5EA3-AF20-11D1-8817-080009EC732A",
    "Template": "41093A71-CCE1-11D0-BFAA-0080C7E24280",
    # labels
    "AnnotateLayerPropertiesCollection": "1D5849F3-0D33-11D2-A26F-080009B6F22B",
    "LabelEngineLayerProperties": "01004145-0D1C-11D2-A26F-080009B6F22B",
    "MaplexLabelEngineLayerProperties": "20664808-0D1C-11D2-A26F-080009B6F22B",
    "TextSymbol": "B65A3E74-2993-11D1-9A43-0080C7EC5C96",
    "StdFont": "0BE35203-8F91-11CE-9DE3-00AA004BB851",
}
_COLORS = {"RgbColor", "HsvColor", "CmykColor", "GrayColor", "HlsColor"}
_LINES = {"SimpleLineSymbol", "CartographicLineSymbol", "MultiLayerLineSymbol", "HashLineSymbol", "MarkerLineSymbol",
          "PictureLineSymbol"}
_FILLS = {"SimpleFillSymbol", "MultiLayerFillSymbol", "LineFillSymbol", "MarkerFillSymbol", "PictureFillSymbol",
          "GradientFillSymbol"}
_MARKERS = {"SimpleMarkerSymbol", "CharacterMarkerSymbol", "PictureMarkerSymbol", "MultiLayerMarkerSymbol",
            "ArrowMarkerSymbol"}
_SYMBOLS = _LINES | _FILLS | _MARKERS
_RENDERERS = {"SimpleRenderer", "UniqueValueRenderer", "ClassBreaksRenderer", "ProportionalSymbolRenderer",
              "DotDensityRenderer", "ChartRenderer", "BiUniqueValueRenderer", "ScaleDependentRenderer"}
_GUIDS = {_g(v): k for k, v in _CLASSES.items() if v}
_NULL_GUID = b"\0" * 16


class _Bad(Exception):
    """The bytes do not have the expected layout."""


# ---- colours ------------------------------------------------------------------------------------------------------
# ArcObjects keep every colour as CIELAB; its RGB conversion uses gamma 1.8 and this matrix (fitted on real
# documents against ArcGIS's own RGB values, exact to the unit)
_LAB_TO_RGB = ((2.80507504, -1.28898848, -0.51611991),
               (-1.03180675, 1.99122396, 0.04055236),
               (0.08110791, -0.26930611, 1.1881494))


def lab_to_rgb(L, a, b):
    def finv(t):
        return t ** 3 if t > 6 / 29 else 3 * (6 / 29) ** 2 * (t - 4 / 29)

    fy = (L + 16) / 116
    xyz = (finv(fy + a / 500), finv(fy), finv(fy - b / 200))
    out = []
    for row in _LAB_TO_RGB:
        v = sum(m * c for m, c in zip(row, xyz))
        out.append(int(round(max(0.0, min(1.0, v)) ** (1 / 1.8) * 255)))
    return out


def _cim_color(rgba):
    r, g, b, a = rgba
    return {"type": "CIMRGBColor", "values": [r, g, b, a]}


# ---- CIM building -------------------------------------------------------------------------------------------------
_DASHES = {1: [6, 3], 2: [1, 2], 3: [6, 3, 1, 3], 4: [6, 3, 1, 3, 1, 3]}   # esriSLS Dash, Dot, DashDot, DashDotDot
_CAPS = {0: "Butt", 1: "Round", 2: "Square"}
_JOINS = {0: "Miter", 1: "Round", 2: "Bevel"}


def _stroke(color, width, dashes=None, cap="Round", join="Round"):
    sl = {"type": "CIMSolidStroke", "enable": True, "capStyle": cap, "joinStyle": join, "miterLimit": 10,
          "width": round(width, 4), "color": _cim_color(color)}
    if dashes:
        sl["effects"] = [{"type": "CIMGeometricEffectDashes", "dashTemplate": [round(x, 4) for x in dashes]}]
    return sl


def _solid_fill(color):
    return {"type": "CIMSolidFill", "enable": True, "color": _cim_color(color)}


_SQUARE = [[[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]]]
_DIAMOND = [[[0, -1], [-1, 0], [0, 1], [1, 0], [0, -1]]]


def _marker_graphic(shape, fill, outline, outline_width):
    """CIMVectorMarker graphic for a simple marker shape (0 circle, 1 square, 2 cross, 3 x, 4 diamond)."""
    if shape in (2, 3):
        paths = ([[[-1, 0], [1, 0]], [[0, -1], [0, 1]]] if shape == 2 else
                 [[[-1, -1], [1, 1]], [[-1, 1], [1, -1]]])
        sym = {"type": "CIMLineSymbol", "symbolLayers": [_stroke(fill, max(outline_width, 1))]}
        return {"type": "CIMMarkerGraphic", "geometry": {"paths": paths}, "symbol": sym}
    layers = []
    if outline is not None:
        layers.append(_stroke(outline, outline_width))
    if fill is not None:
        layers.append(_solid_fill(fill))
    geom = ({"curveRings": [[[0, 1], {"a": [[0, 1], [0, 0], 0, 1]}]]} if shape == 0 else
            {"rings": _DIAMOND if shape == 4 else _SQUARE})
    return {"type": "CIMMarkerGraphic", "geometry": geom, "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": layers}}


def cim_symbol(kind, layers):
    t = {"line": "CIMLineSymbol", "fill": "CIMPolygonSymbol", "marker": "CIMPointSymbol"}[kind]
    return {"type": "CIMSymbolReference", "symbol": {"type": t, "symbolLayers": layers}}


def _mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


# ---- the stream ---------------------------------------------------------------------------------------------------
class _Stream:
    def __init__(self, b):
        self.b = b
        self.at = {}            # offset -> class name of every known class id
        for g, name in _GUIDS.items():
            i = b.find(g)
            while i >= 0:
                self.at[i] = name
                i = b.find(g, i + 1)
        self.first = {}         # (class, object id) -> offset where the object is written in full
        for p in sorted(self.at):
            if p + 20 <= len(b):
                self.first.setdefault((self.at[p], self.u32(p + 16)), p)
        self.cache = {}

    # -- primitives --
    def u16(self, p):
        if p + 2 > len(self.b):
            raise _Bad("end")
        return struct.unpack_from("<H", self.b, p)[0]

    def u32(self, p):
        if p + 4 > len(self.b):
            raise _Bad("end")
        return struct.unpack_from("<I", self.b, p)[0]

    def f64(self, p):
        if p + 8 > len(self.b):
            raise _Bad("end")
        v = struct.unpack_from("<d", self.b, p)[0]
        if not math.isfinite(v):
            raise _Bad("not a number")
        return v

    def bstr(self, p):
        """Length-prefixed UTF-16 string -> (text, end)."""
        n = self.u32(p)
        if n % 2 or n > 65536 or p + 4 + n > len(self.b) or (n and self.b[p + 2 + n:p + 4 + n] != b"\0\0"):
            raise _Bad("string")
        if n == 0:
            return "", p + 4
        try:
            return self.b[p + 4:p + 2 + n].decode("utf-16-le"), p + 4 + n
        except UnicodeDecodeError as e:
            raise _Bad("string") from e

    def head(self, p, kinds=None):
        """Object at p -> (class, offset of its full copy, is reference)."""
        name = self.at.get(p)
        if name is None or (kinds is not None and name not in kinds):
            raise _Bad(f"expected {sorted(kinds)[:3] if kinds else 'object'} at {p}")
        first = self.first.get((name, self.u32(p + 16)), p)
        return name, first, first != p

    def objects(self, start, end, kinds):
        return [p for p in sorted(k for k in self.at if start <= k < end) if self.at[p] in kinds]

    # -- colours --
    def color(self, p):
        """-> ((r, g, b, alpha 0-100) or None for "no colour", end)."""
        name, first, ref = self.head(p, _COLORS)
        if ref:
            return self.color(first)[0], p + 20
        ver = self.u16(p + 20)
        if ver > 1:   # CMYK colours keep their own components
            return self._cmyk(p, ver)
        L, a, b = self.f64(p + 25), self.f64(p + 33), self.f64(p + 41)
        if not (-1 <= L <= 101 and -200 <= a <= 200 and -200 <= b <= 200):
            raise _Bad("colour")
        transparent = self.b[p + 50]
        alpha = round((255 - transparent) / 255 * 100)
        if transparent == 255:
            return None, p + 51
        return tuple(lab_to_rgb(L, a, b)) + (alpha,), p + 51

    def _cmyk(self, p, ver):
        # version 4: cyan, magenta, yellow and black in percent, then two flags
        if ver != 4 or p + 30 > len(self.b):
            raise _Bad("colour")
        k, y, c, m = (min(100, x) / 100 for x in self.b[p + 22:p + 26])
        if self.b[p + 27] == 255:
            return None, p + 30
        rgb = (round(255 * (1 - c) * (1 - k)), round(255 * (1 - m) * (1 - k)), round(255 * (1 - y) * (1 - k)))
        return rgb + (round((255 - self.b[p + 27]) / 255 * 100),), p + 30

    # -- symbols --
    def symbol(self, p):
        """Symbol object at p -> (kind 'line'|'fill'|'marker', [CIM symbol layers], end)."""
        name, first, ref = self.head(p, _SYMBOLS)
        if ref:
            if first not in self.cache:
                self.cache[first] = self.symbol(first)
            kind, layers, _end = self.cache[first]
            return kind, layers, p + 20
        fn = getattr(self, "_" + name, None)
        if fn is None:
            raise _Bad(f"{name} is not read")
        out = fn(p)
        self.cache[p] = out
        return out

    def _SimpleLineSymbol(self, p):
        color, q = self.color(p + 22)
        width, style = self.f64(q), self.u32(q + 8)
        layers = [] if color is None or style == 5 else [_stroke(color, width, _DASHES.get(style))]
        return "line", layers, q + 20

    def _CartographicLineSymbol(self, p):
        cap, join, width = self.u32(p + 22), self.u32(p + 26), self.f64(p + 30)
        color, q = self.color(p + 47)
        dashes = None
        if self.at.get(q) == "Template":
            name, first, ref = self.head(q, {"Template"})
            t = first
            interval, n = self.f64(t + 22), self.u32(t + 30)
            if n > 1000:
                raise _Bad("template")
            pattern = [self.f64(t + 34 + 8 * k) * interval for k in range(2 * n)]
            if any(pattern[1::2]):
                dashes = pattern
            q = q + 20 if ref else t + 34 + 16 * n
        elif self.b[q:q + 16] == _NULL_GUID:
            q += 16
        else:
            raise _Bad("template")
        end = q + 16 + 4 + 13 + 8
        if end > len(self.b):
            raise _Bad("end")
        layers = [] if color is None else [_stroke(color, width, dashes, _CAPS.get(cap, "Round"),
                                                   _JOINS.get(join, "Round"))]
        return "line", layers, end

    def _layers(self, q, count):
        """count symbol layers from q, then the visible and locked flags -> ([(kind, layers)], end)."""
        if count > 100:
            raise _Bad("layer count")
        parts = []
        for _k in range(count):
            kind, layers, q = self.symbol(q)
            parts.append((kind, layers))
        visible = [self.u32(q + 4 * k) for k in range(count)]
        q += 8 * count
        for _k in range(count):   # newer documents add a (usually empty) name per layer
            try:
                _name, q = self.bstr(q)
            except _Bad:
                break
        return [part for part, v in zip(parts, visible) if v], q

    def _MultiLayerLineSymbol(self, p):
        parts, end = self._layers(p + 34, self.u32(p + 30))
        return "line", [sl for _k, layers in parts for sl in layers], end

    def _optional_symbol(self, q):
        """A symbol object, or nothing (16 zero bytes) -> (layers, end)."""
        if self.b[q:q + 16] == _NULL_GUID:
            return [], q + 16
        _kind, layers, q = self.symbol(q)
        return layers, q

    def _SimpleFillSymbol(self, p):
        outline, q = self._optional_symbol(p + 22)
        color, q = self.color(q)
        style = self.u32(q + 4)
        layers = [sl for sl in outline if sl.get("width", 1) > 0]   # ArcMap's 0-width outlines are not drawn
        if color is not None and style != 1:      # 1: hollow
            if style in (0,) or style > 7:
                layers.append(_solid_fill(color))
            else:  # hatches: 2 horizontal, 3 vertical, 4 forward diagonal, 5 backward diagonal, 6 cross, 7 diagonal cross
                angles = {2: [0], 3: [90], 4: [45], 5: [135], 6: [0, 90], 7: [45, 135]}[style]
                for ang in angles:
                    layers.append({"type": "CIMHatchFill", "enable": True, "rotation": ang, "separation": 5,
                                   "lineSymbol": cim_symbol("line", [_stroke(color, 0.5)])["symbol"]})
        return "fill", layers, q + 12

    def _MultiLayerFillSymbol(self, p):
        _color, q = self.color(p + 30)
        parts, end = self._layers(q + 4, self.u32(q))
        return "fill", [sl for _k, layers in parts for sl in layers], end

    def _SimpleMarkerSymbol(self, p):
        color, q = self.color(p + 22)
        size, style = self.f64(q), self.u32(q + 8)
        angle, dx, dy = self.f64(q + 20), self.f64(q + 28), self.f64(q + 36)
        has_outline = self.b[q + 44]
        outline_width = self.f64(q + 45)
        outline, end = self.color(q + 53)
        if not has_outline:
            outline = None
        if not (0 <= size < 1000):
            raise _Bad("size")
        sl = {"type": "CIMVectorMarker", "enable": True, "size": round(size, 4),
              "markerGraphics": [_marker_graphic(style, color, outline, outline_width)]}
        if angle:
            sl["rotation"] = round(angle, 4)
        if dx or dy:
            sl["offsetX"], sl["offsetY"] = round(dx, 4), round(dy, 4)
        return "marker", [sl], end + 2

    def _font(self, p):
        """StdFont object at p -> (family, size in points, end)."""
        name, first, ref = self.head(p, {"StdFont"})
        if ref:
            fam, size, _e = self._font(first)
            return fam, size, p + 20
        size = self.u32(p + 26) / 10000
        n = self.b[p + 30]
        try:
            fam = self.b[p + 31:p + 31 + n].decode("latin-1")
        except UnicodeDecodeError as e:
            raise _Bad("font") from e
        return fam, size, p + 31 + n

    def _CharacterMarkerSymbol(self, p):
        color, q = self.color(p + 22)
        index, angle, size = self.u32(q), self.f64(q + 4), self.f64(q + 12)
        dx, dy = self.f64(q + 20), self.f64(q + 28)
        ver = self.u16(p + 20)
        if ver >= 3:   # the font name, a LOGFONT-like block (29 bytes) and, from version 4, the font object
            family, end = self.bstr(q + 62)
            if ver >= 4:
                f = next((k for k in range(end + 25, end + 40) if self.at.get(k) == "StdFont"), None)
                if f is None:
                    raise _Bad("font")
                fam2, _size, end = self._font(f)
                family = family or fam2
            else:
                end += 29
        else:          # version 2: the font object, then the drawing mode and a flag
            family, _size, end = self._font(q + 52)
            end += 10
        sl = {"type": "CIMCharacterMarker", "enable": True, "fontFamilyName": family or "Arial",
              "characterIndex": index, "size": round(size, 4),
              "symbol": cim_symbol("fill", [] if color is None else [_solid_fill(color)])["symbol"]}
        if angle:
            sl["rotation"] = round(angle, 4)
        if dx or dy:
            sl["offsetX"], sl["offsetY"] = round(dx, 4), round(dy, 4)
        return "marker", [sl], end

    _PICTURE = _g("0BE35204-8F91-11CE-9DE3-00AA004BB851")    # StdPicture

    def _picture(self, p, limit):
        """The image of a picture symbol from p -> (mime type or None, bytes, end of the image)."""
        g = self.b.find(self._PICTURE, p, p + 24)
        if g >= 0:
            if self.b[g + 20:g + 24] != b"lt\0\0":    # the same picture again
                return None, b"", g + 20
            n = self.u32(g + 24)
            data = self.b[g + 28:g + 28 + n]
            return _mime(data), data, g + 28 + n
        if self.u32(p) == 2 and 0 < self.u32(p + 8) < 50000000:
            n = self.u32(p + 8)
            data = self.b[p + 12:p + 12 + n]
            return _mime(data), data, p + 12 + n
        z = self.b.find(_NULL_GUID, p, p + 24)
        if z < 0:
            raise _Bad("picture")
        return None, b"", z + 16

    def _picture_colours(self, q):
        """After the image: 16 reserved bytes, the background colour, the transparent colour (or nothing)."""
        c = next((k for k in range(q, q + 40) if self.at.get(k) in _COLORS), None)
        if c is None:
            raise _Bad("picture colours")
        background, q = self.color(c)
        if self.b[q:q + 16] == _NULL_GUID:
            return background, None, q + 16
        transparent, q = self.color(q)
        return background, transparent, q

    def _PictureMarkerSymbol(self, p):
        mime, data, q = self._picture(p + 22, None)
        _bg, _tr, q = self._picture_colours(q)
        size, angle, dx, dy = self.f64(q), self.f64(q + 8), self.f64(q + 16), self.f64(q + 24)
        end = q + 48 + 4 + 13
        if not data or mime is None:
            raise _Bad("picture")
        import base64
        sl = {"type": "CIMPictureMarker", "enable": True, "size": round(size, 4),
              "url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}
        if angle:
            sl["rotation"] = round(angle, 4)
        if dx or dy:
            sl["offsetX"], sl["offsetY"] = round(dx, 4), round(dy, 4)
        return "marker", [sl], end

    def _LineFillSymbol(self, p):
        q = p + 22
        if self.b[q:q + 16] == _NULL_GUID:
            q += 16
        elif self.at.get(q) in _COLORS:
            _c, q = self.color(q)
        _kind, line, q = self.symbol(q)
        outline, q = self._optional_symbol(q)
        angle, offset, sep = self.f64(q), self.f64(q + 8), self.f64(q + 16)
        if not (0 < sep < 10000 and -1e4 < angle < 1e4):
            raise _Bad("line fill")
        layers = list(outline)
        if line:
            layers.append({"type": "CIMHatchFill", "enable": True, "rotation": round(angle, 4),
                           "separation": round(sep, 4), "offsetX": round(offset, 4),
                           "lineSymbol": {"type": "CIMLineSymbol", "symbolLayers": line}})
        return "fill", layers, q + 24

    def _ArrowMarkerSymbol(self, p):
        color, q = self.color(p + 22)
        size = self.f64(q)
        if not 0 <= size < 1000:
            raise _Bad("size")
        graphic = {"type": "CIMMarkerGraphic", "geometry": {"rings": [[[-1, -1], [1, 0], [-1, 1], [-1, -1]]]},
                   "symbol": {"type": "CIMPolygonSymbol",
                              "symbolLayers": [] if color is None else [_solid_fill(color)]}}
        return "marker", [{"type": "CIMVectorMarker", "enable": True, "size": round(size, 4),
                           "markerGraphics": [graphic]}], q + 58

    def _MultiLayerMarkerSymbol(self, p):
        _color, q = self.color(p + 62)
        _kind, _mask, q = self.symbol(q + 12)      # the mask (halo) fill symbol
        parts, end = self._layers(q + 4, self.u32(q))
        return "marker", [sl for _k, layers in parts for sl in layers], end

    # -- symbols that are not read in full: their main colour --
    def approximate(self, p, end):
        """(kind, layers) for a symbol at p whose layout is not known: its first colour in [p, end)."""
        name = self.at.get(p)
        kind = "line" if name in _LINES else "fill" if name in _FILLS else "marker" if name in _MARKERS else None
        if kind is None:
            return None
        color = None
        for c in self.objects(p, end, _COLORS):
            try:
                color = self.color(c)[0]
            except (_Bad, IndexError):
                continue
            if color is not None:
                break
        if color is None:
            return kind, []
        if kind == "line":
            return kind, [_stroke(color, 1)]
        if kind == "fill":
            return kind, [_solid_fill(color)]
        return kind, [{"type": "CIMVectorMarker", "enable": True, "size": 6,
                       "markerGraphics": [_marker_graphic(0, color, None, 0)]}]

    def symbol_in(self, p, end):
        """Symbol at p, read in full or approximated -> (kind, layers, exact, end or None)."""
        try:
            kind, layers, e = self.symbol(p)
            if e > end:
                raise _Bad("past the end")
            return kind, layers, True, e
        except (_Bad, IndexError, KeyError, ValueError):
            got = self.approximate(p, end)
            if got is None:
                return None, [], False, None
            return got[0], got[1], False, None


# ---- renderers ----------------------------------------------------------------------------------------------------
def _legend(st, start, end):
    """Legend groups and classes in [start, end) -> ([[(label, kind, layers, exact)]] per group, offset after the
    last class's label (or of the last class when it could not be read))."""
    groups, cur, after = [], None, start
    marks = st.objects(start, end, {"LegendGroup", "LegendClass"})
    for k, p in enumerate(marks):
        nxt = marks[k + 1] if k + 1 < len(marks) else end
        name, first, ref = st.head(p)
        if name == "LegendGroup":
            cur = []
            groups.append(cur)
            continue
        if cur is None:
            cur = []
            groups.append(cur)
        src = first if ref else p
        nxt_src = nxt if not ref else next((m for m in sorted(st.at) if m > src and st.at[m] in
                                            ("LegendClass", "LegendGroup")), end)
        kind, layers, exact, e = st.symbol_in(src + 22, nxt_src)
        label, after = "", p
        if e is not None:
            try:
                label, after = st.bstr(e)
                _desc, after = st.bstr(after)
            except _Bad:
                pass
        cur.append((label, kind, layers, exact))
    return groups, after


def _simple(st, p, end):
    kind, layers, exact, _e = st.symbol_in(p + 22, end)
    if kind is None:
        return None, False
    return {"type": "CIMSimpleRenderer", "symbol": cim_symbol(kind, layers)}, exact


def _unique(st, p, end):
    n = st.u32(p + 22)
    if not 0 < n <= 3:
        raise _Bad("field count")
    q, fields = p + 26, []
    for _k in range(n):
        f, q = st.bstr(q)
        fields.append(f)
        q += 3
    delim, q = st.bstr(q)
    default = None
    if st.at.get(q) in _SYMBOLS:
        lg = next((m for m in sorted(st.at) if m > q and st.at[m] == "LegendGroup"), end)
        kind, layers, exact, e = st.symbol_in(q, lg)
        default = (kind, layers)
    groups, last = _legend(st, q, end)
    # the value table follows the legend: count, then (value, group, class, 0)
    values = None
    for s in range(last, min(end, last + 200000)):
        try:
            cnt = st.u32(s)
            if not 0 < cnt <= 100000:
                continue
            r, vals = s + 4, []
            for _k in range(cnt):
                v, r = st.bstr(r)
                g, c = st.u32(r), st.u32(r + 4)
                r += 12
                if g >= len(groups) or c >= len(groups[g]):
                    raise _Bad("index")
                vals.append((v, g, c))
            # every class of the groups that hold values has at least one value
            used = {(g, c) for _v, g, c in vals}
            # (the default symbol has a class of its own: a group of its own, or the first class of the only group)
            need = range(1 if default is not None and len(groups) > 1 else 0, len(groups))
            missing = [(g, c) for g in need for c in range(len(groups[g])) if (g, c) not in used]
            if len(missing) > (1 if default is not None and len(groups) == 1 else 0):
                continue
            values = vals
            break
        except _Bad:
            continue
    if values is None:
        raise _Bad("value table")
    classes, by_class, exact_all = [], {}, True
    for v, g, c in values:
        key = (g, c)
        if key not in by_class:
            label, kind, layers, exact = groups[g][c]
            exact_all &= exact
            cls = {"type": "CIMUniqueValueClass", "label": label or v,
                   "symbol": cim_symbol(kind or "fill", layers), "values": [], "visible": True}
            by_class[key] = cls
            classes.append(cls)
        parts = v.split(delim) if n > 1 and delim else [v]
        by_class[key]["values"].append({"type": "CIMUniqueValue", "fieldValues": parts})
    r = {"type": "CIMUniqueValueRenderer", "fields": fields, "fieldDelimiter": delim or ",",
         "groups": [{"type": "CIMUniqueValueGroup", "classes": classes}]}
    used = {(g, c) for _v, g, c in values}
    rest = [(g, c) for g in range(len(groups)) for c in range(len(groups[g])) if (g, c) not in used]
    if default is not None and default[0] is not None and rest:
        g, c = rest[0]
        r.update(useDefaultSymbol=True, defaultSymbol=cim_symbol(*default),
                 defaultLabel=groups[g][c][0] or "<all other values>")
    return r, exact_all


def _breaks(st, p, end):
    field, q = st.bstr(p + 22)
    _ramp, q = st.bstr(q)
    n = st.u32(q)
    if not 0 < n <= 1000:
        raise _Bad("break count")
    upper = [st.f64(q + 4 + 8 * k) for k in range(n)]
    lower = [st.f64(q + 4 + 8 * (n + k)) for k in range(n)]
    groups, _after = _legend(st, q, end)
    classes = [c for g in groups for c in g]
    if len(classes) < n:
        raise _Bad("legend")
    out, exact_all = [], True
    for k in range(n):
        label, kind, layers, exact = classes[k]
        exact_all &= exact
        out.append({"type": "CIMClassBreak", "label": label, "upperBound": upper[k],
                    "symbol": cim_symbol(kind or "fill", layers)})
    return {"type": "CIMClassBreaksRenderer", "field": field, "minimumBreak": lower[0], "breaks": out,
            "classBreakType": "GraduatedColor"}, exact_all


def read_renderer(st, start, end):
    """First renderer in [start, end) -> (CIM renderer or None, exact, ArcMap renderer name)."""
    found = st.objects(start, end, _RENDERERS)
    if not found:
        return None, False, ""
    p = found[0]
    name, first, ref = st.head(p)
    if ref:
        p = first
    stop = min(end, next((k for k in st.objects(p + 16, end, {"AnnotateLayerPropertiesCollection"})), end))
    try:
        if name == "SimpleRenderer":
            r, exact = _simple(st, p, stop)
        elif name == "UniqueValueRenderer":
            r, exact = _unique(st, p, stop)
        elif name == "ClassBreaksRenderer":
            r, exact = _breaks(st, p, stop)
        else:
            # other renderers: the first symbol they draw with
            sym = st.objects(p, stop, _SYMBOLS)
            r, exact = (_simple(st, sym[0] - 22, stop)[0] if sym else None), False
    except (_Bad, IndexError, KeyError, ValueError):
        sym = st.objects(p, stop, _SYMBOLS)
        r, exact = (_simple(st, sym[0] - 22, stop)[0] if sym else None), False
    return r, exact, name


# ---- labels -------------------------------------------------------------------------------------------------------
_ENGINES = {_g("AA157207-E079-11D2-9F48-00C04F6BC6A5"): "VBScript", _g("AA157208-E079-11D2-9F48-00C04F6BC6A5"): "JScript",
            _g("BB6721A2-D81A-45E3-7FEF-884DB2B2A905"): "Python"}
_LABEL_PROPS = {"LabelEngineLayerProperties", "MaplexLabelEngineLayerProperties"}
_FIELD_INFO = _g("A2BAAE2D-969B-11D2-AE77-080009EC732A")
_TWO_EMPTY = bytes.fromhex("020000000000020000000000")


def read_labels(st, start, end):
    """Label classes in [start, end) -> [CIMLabelClass]."""
    out = []
    props = [p for p in st.objects(start, end, _LABEL_PROPS) if not st.head(p)[2]]
    for k, p in enumerate(props):
        try:
            expr, q = st.bstr(p + 22)
        except _Bad:
            continue
        nxt = props[k + 1] if k + 1 < len(props) else end
        ts = next((t for t in st.objects(q, nxt, {"TextSymbol"})), None)
        strings, s = [], q
        while ts is not None and s < ts:      # ... the class name and its SQL query come last before the text
            try:
                t, s = st.bstr(s)
                strings.append(t)
            except _Bad:
                s += 1
        name, where = (strings[-2], strings[-1]) if len(strings) >= 2 else ("Default", "")
        text = {"type": "CIMTextSymbol", "fontFamilyName": "Arial", "height": 8,
                "symbol": cim_symbol("fill", [_solid_fill((0, 0, 0, 100))])["symbol"]}
        if ts is not None:
            try:
                color = st.color(ts + 22)[0]
                if color is not None:
                    text["symbol"] = cim_symbol("fill", [_solid_fill(color)])["symbol"]
            except _Bad:
                pass
            f = next((t for t in st.objects(ts, nxt, {"StdFont"})), None)
            if f is not None:
                try:
                    fam, size, _e = st._font(f)
                    text["fontFamilyName"], text["height"] = fam or "Arial", round(size, 2) or 8
                    weight = st.u16(st.first.get(("StdFont", st.u32(f + 16)), f) + 24)
                    if weight >= 600:
                        text["fontStyleName"] = "Bold"
                except _Bad:
                    pass
        tail = st.b[p:min(nxt, p + 4000)]
        engine = next((e for g, e in _ENGINES.items() if g in tail), "VBScript")
        lc = {"type": "CIMLabelClass", "name": name or "Default", "expression": expr, "expressionEngine": engine,
              "visibility": True, "textSymbol": {"type": "CIMSymbolReference", "symbol": text}}
        if where.strip():
            lc["whereClause"] = where
        out.append(lc)
    return out


def labels_shown(st, start, end):
    """The layer's "Label features" check box: two flag bytes 22 bytes before two empty strings that come
    right before the layer's field list."""
    f = st.b.find(_FIELD_INFO, start, end)
    if f < 0:
        return False
    p = st.b.rfind(_TWO_EMPTY, max(start, f - 400), f)
    return p >= 22 and st.b[p - 22:p - 20] == bytes((255, 255))


# ---- entry point --------------------------------------------------------------------------------------------------
class SymbolReader:
    """Reads renderers and labels of the layers of one stream (mxd.py gives each layer's byte range)."""

    def __init__(self, data):
        self.st = _Stream(data)

    def layer(self, start, end):
        """-> dict(renderer=CIM or None, exact=bool, kind=ArcMap renderer name, labelClasses=[...],
        labelVisibility=bool)."""
        renderer, exact, kind = read_renderer(self.st, start, end)
        try:
            labels = read_labels(self.st, start, end)
            shown = labels_shown(self.st, start, end)
        except (_Bad, IndexError, ValueError):
            labels, shown = [], False
        return {"renderer": renderer, "exact": exact, "kind": kind, "labelClasses": labels,
                "labelVisibility": shown and bool(labels)}
