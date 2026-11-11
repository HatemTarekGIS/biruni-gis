"""Helpers shared by the geoprocessing tools: spatial index, statistics, distances, field-calculator expressions."""
import datetime
import math
import random
import re
import statistics

from osgeo import ogr, osr

from . import geometry as G
from ._base import ExecuteError, fail


# ---------------------------------------------------------------- spatial index

class Index:
    """Bounding-box index of (key, ogr geometry); the engine's R-tree when present, a sorted scan otherwise."""

    def __init__(self, items):
        self.items = {}
        self._si = None
        try:
            from qgis.core import QgsRectangle, QgsSpatialIndex
            self._si = QgsSpatialIndex()
            self._rect = QgsRectangle
        except Exception:
            self._si = None
        for key, g in items:
            if g is None or g.IsEmpty():
                continue
            env = g.GetEnvelope()
            self.items[key] = (g, env)
            if self._si is not None:
                self._si.addFeature(int(key), self._rect(env[0], env[2], env[1], env[3]))

    def candidates(self, g, pad=0.0):
        if g is None or g.IsEmpty():
            return []
        x0, x1, y0, y1 = g.GetEnvelope()
        x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
        if self._si is not None:
            return [k for k in self._si.intersects(self._rect(x0, y0, x1, y1)) if k in self.items]
        out = []
        for k, (_g, e) in self.items.items():
            if e[0] <= x1 and e[1] >= x0 and e[2] <= y1 and e[3] >= y0:
                out.append(k)
        return out

    def geom(self, key):
        return self.items[key][0]


def union_all(geoms):
    geoms = [g for g in geoms if g is not None and not g.IsEmpty()]
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0].Clone()
    coll = ogr.Geometry(ogr.wkbGeometryCollection)
    for g in geoms:
        coll.AddGeometry(g)
    try:
        return coll.UnaryUnion()
    except Exception:
        out = geoms[0].Clone()
        for g in geoms[1:]:
            out = out.Union(g)
        return out


def dim_of_type(geom_type):
    flat = ogr.GT_Flatten(geom_type)
    if flat in (ogr.wkbPoint, ogr.wkbMultiPoint):
        return 0
    if flat in (ogr.wkbLineString, ogr.wkbMultiLineString):
        return 1
    if flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        return 2
    return -1


def multi_type(dim):
    return {0: ogr.wkbMultiPoint, 1: ogr.wkbMultiLineString, 2: ogr.wkbMultiPolygon}.get(dim, ogr.wkbUnknown)


def keep_dim(g, dim):
    if g is None or g.IsEmpty():
        return None
    out = G._pick_dim(g, dim)
    return out if out is not None and not out.IsEmpty() else None


# ---------------------------------------------------------------- distances

def distance_in_units(value, srs, fields=None):
    """'100 Meters' -> distance in the layer's units (degrees approximated for geographic data).
    Returns (number or None, field name or None)."""
    num, unit = G.parse_linear(value)
    if num is None:
        if unit and fields is not None:
            return None, unit
        return None, None
    if unit is None or srs is None:
        return num, None
    if unit == "degrees":
        return num, None
    f = G.linear_factor(unit)
    if f is None:
        return num, None
    meters = num * f
    if srs.IsProjected():
        return meters / (srs.GetLinearUnits() or 1.0), None
    return meters / 111320.0, None


def geodesic_buffer(g, meters, srs, options=None):
    """Buffer a geometry in geographic coordinates by a distance in meters (local azimuthal projection)."""
    c = g.Centroid()
    aeqd = osr.SpatialReference()
    aeqd.SetAE(c.GetY(), c.GetX(), 0, 0)
    aeqd.SetWellKnownGeogCS("WGS84")
    aeqd.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    there = osr.CoordinateTransformation(srs, aeqd)
    back = osr.CoordinateTransformation(aeqd, srs)
    h = g.Clone()
    h.Transform(there)
    b = buffer(h, meters, options)
    b.Transform(back)
    return b


def buffer(g, d, options=None):
    if options:
        try:
            return g.Buffer(float(d), options)
        except Exception:
            pass
    return g.Buffer(float(d), 30)


# ---------------------------------------------------------------- statistics

STAT_TYPES = ("SUM", "MEAN", "MIN", "MAX", "RANGE", "STD", "COUNT", "FIRST", "LAST", "MEDIAN", "VARIANCE",
              "UNIQUE", "CONCATENATE", "MODE")


def parse_stats(statistics_fields):
    """[['POP', 'SUM'], ...] or 'POP SUM;AREA MEAN' -> [(field, STAT)]."""
    if not statistics_fields:
        return []
    if isinstance(statistics_fields, str):
        items = [s.strip().split() for s in statistics_fields.split(";") if s.strip()]
    else:
        items = [list(x) if isinstance(x, (list, tuple)) else str(x).split() for x in statistics_fields]
    out = []
    for it in items:
        if len(it) < 2:
            fail(728, f"Statistics field {it} needs a field name and a statistic type")
        stat = str(it[1]).upper()
        if stat not in STAT_TYPES:
            fail(800, f"The value is not a member of {' | '.join(STAT_TYPES)}: {it[1]}")
        out.append((str(it[0]), stat))
    return out


def compute_stat(stat, values, sep=", "):
    if stat == "FIRST":
        return values[0] if values else None
    if stat == "LAST":
        return values[-1] if values else None
    vals = [v for v in values if v is not None]
    if stat == "COUNT":
        return len(vals)
    if stat == "UNIQUE":
        return len(set(vals))
    if stat == "CONCATENATE":
        return sep.join(str(v) for v in vals)
    if not vals:
        return None
    if stat == "MIN":
        return min(vals)
    if stat == "MAX":
        return max(vals)
    if stat == "MODE":
        return statistics.mode(vals)
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    if not nums:
        return None
    if stat == "SUM":
        return sum(nums)
    if stat == "MEAN":
        return sum(nums) / len(nums)
    if stat == "RANGE":
        return max(nums) - min(nums)
    if stat == "MEDIAN":
        return statistics.median(nums)
    if stat == "STD":
        return statistics.stdev(nums) if len(nums) > 1 else 0.0
    if stat == "VARIANCE":
        return statistics.variance(nums) if len(nums) > 1 else 0.0
    return None


def stat_defn(stat, field, src_defn):
    name = f"{stat}_{field}"
    if stat in ("COUNT", "UNIQUE"):
        return ogr.FieldDefn(name, ogr.OFTInteger)
    if stat == "CONCATENATE":
        fd = ogr.FieldDefn(name, ogr.OFTString)
        fd.SetWidth(8000)
        return fd
    if stat in ("FIRST", "LAST", "MIN", "MAX", "MODE") and src_defn is not None:
        fd = ogr.FieldDefn(name, src_defn.GetType())
        fd.SetWidth(src_defn.GetWidth())
        fd.SetPrecision(src_defn.GetPrecision())
        return fd
    return ogr.FieldDefn(name, ogr.OFTReal)


# ---------------------------------------------------------------- field calculator

class _CI:
    """Case-insensitive attribute access for !shape.area! style tokens."""

    def __init__(self, obj):
        self._o = obj

    def __getattr__(self, name):
        o = object.__getattribute__(self, "_o")
        for attr in dir(o):
            if attr.lower() == name.lower():
                v = getattr(o, attr)
                return _CI(v) if isinstance(v, (G.Point, G.Extent, G.Geometry)) else v
        raise AttributeError(name)

    def __float__(self):
        return float(self._o)


def _shape_token(chain, geom):
    """'shape.area@squaremeters' -> value."""
    unit = None
    if "@" in chain:
        chain, unit = chain.split("@", 1)
    parts = chain.split(".")[1:]
    if geom is None:
        return None
    if unit and parts and parts[-1].lower() in ("area", "geodesicarea"):
        method = "GEODESIC" if parts[-1].lower() == "geodesicarea" else "PLANAR"
        return geom.getArea(method, unit)
    if unit and parts and parts[-1].lower() in ("length", "geodesiclength"):
        method = "GEODESIC" if parts[-1].lower() == "geodesiclength" else "PLANAR"
        return geom.getLength(method, unit)
    v = _CI(geom)
    for p in parts:
        v = getattr(v, p)
    return v._o if isinstance(v, _CI) else v


def python_expression(expr, field_names, shape_name):
    """Turn '!a! * 2 + !shape.area!' into Python using __row / __geom."""
    lowered = {n.lower(): n for n in field_names}
    shp = (shape_name or "shape").lower()

    def repl(m):
        tok = m.group(1)
        head = tok.split(".")[0].split("@")[0].lower()
        if head in (shp, "shape") and (("." in tok) or "@" in tok):
            return f"__shape({tok!r})"
        if tok.lower() in lowered:
            return f"__row[{lowered[tok.lower()]!r}]"
        if tok.lower() in ("shape_area", "shape.starea()"):
            return "__shape('shape.area')"
        if tok.lower() in ("shape_length", "shape.stlength()", "shape_leng"):
            return "__shape('shape.length')"
        if head in (shp, "shape"):
            return "__shape('shape.firstPoint')"
        raise ExecuteError(f"ERROR 000539: Invalid field {tok}")
    return re.sub(r"!([^!\s]+)!", repl, expr)


_ARCADE_FUNCS = {
    "Round": "__a_round", "Upper": "__a_upper", "Lower": "__a_lower", "Concatenate": "__a_concat", "Left": "__a_left",
    "Right": "__a_right", "Mid": "__a_mid", "Text": "__a_text", "Number": "__a_number", "IIf": "__a_iif",
    "IsEmpty": "__a_isempty", "Abs": "abs", "Floor": "__a_floor", "Ceil": "__a_ceil", "Sqrt": "__a_sqrt",
    "Pow": "pow", "Max": "__a_max", "Min": "__a_min", "Area": "__a_area", "Length": "__a_length",
    "Geometry": "__a_geometry", "Trim": "__a_trim", "Replace": "__a_replace", "Count": "len",
    "DefaultValue": "__a_default", "Proper": "__a_proper", "Find": "__a_find", "Now": "__a_now",
    "Centroid": "__a_centroid", "Boolean": "bool", "Random": "random.random", "Log": "math.log",
    "Exp": "math.exp", "Sin": "math.sin", "Cos": "math.cos", "Tan": "math.tan", "Atan2": "math.atan2",
    "Decode": "__a_decode", "When": "__a_when", "AreaGeodetic": "__a_area_geo", "LengthGeodetic": "__a_length_geo",
}


def arcade_expression(expr, field_names):
    """A single-line Arcade expression -> Python. Multi-statement scripts are not translated."""
    text = expr.strip()
    if text.startswith("return "):
        text = text[7:]
    text = text.rstrip(";").strip()
    if re.search(r"\b(var|for|if|function|while)\b", text) or ";" in text or "\n" in text:
        raise ExecuteError("ERROR 999999: arcpy CalculateField ARCADE scripts with statements are "
                           "not available in Biruni yet (single expressions only).")
    lowered = {n.lower(): n for n in field_names}
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < len(text) and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            lit = text[i + 1:j]
            out.append(repr(lit))
            i = j + 1
            continue
        m = re.match(r"\$feature\.(\w+)|\$feature\[\s*(['\"])(.+?)\2\s*\]", text[i:])
        if m:
            name = m.group(1) or m.group(3)
            real = lowered.get(name.lower())
            if real is None:
                raise ExecuteError(f"ERROR 002717: Invalid Arcade expression: field {name} not found")
            out.append(f"__row[{real!r}]")
            i += m.end()
            continue
        if text.startswith("$feature", i):
            out.append("__feature")
            i += 8
            continue
        m = re.match(r"[A-Za-z_]\w*", text[i:])
        if m:
            w = m.group(0)
            out.append({"true": "True", "false": "False", "null": "None", "PI": "math.pi"}.get(
                w, _ARCADE_FUNCS.get(w, w)))
            i += m.end()
            continue
        if text.startswith("&&", i):
            out.append(" and ")
            i += 2
            continue
        if text.startswith("||", i):
            out.append(" or ")
            i += 2
            continue
        if c == "!" and not text.startswith("!=", i):
            out.append(" not ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _arcade_ns(geom_getter):
    def a_round(v, n=0):
        return None if v is None else round(float(v), int(n))

    def a_text(v, fmt=None):
        if v is None:
            return ""
        return str(v)

    def a_number(v, fmt=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return float("nan")

    def a_concat(values, sep=""):
        if not isinstance(values, (list, tuple)):
            values = [values]
        return str(sep).join("" if v is None else str(v) for v in values)

    def a_area(feat, unit="square-meters"):
        g = geom_getter()
        return None if g is None else g.getArea("PLANAR", str(unit).replace("-", ""))

    def a_area_geo(feat, unit="square-meters"):
        g = geom_getter()
        return None if g is None else g.getArea("GEODESIC", str(unit).replace("-", ""))

    def a_length(feat, unit="meters"):
        g = geom_getter()
        return None if g is None else g.getLength("PLANAR", str(unit).replace("-", ""))

    def a_length_geo(feat, unit="meters"):
        g = geom_getter()
        return None if g is None else g.getLength("GEODESIC", str(unit).replace("-", ""))

    def a_decode(v, *pairs):
        for k in range(0, len(pairs) - 1, 2):
            if v == pairs[k]:
                return pairs[k + 1]
        return pairs[-1] if len(pairs) % 2 else None

    def a_when(*pairs):
        for k in range(0, len(pairs) - 1, 2):
            if pairs[k]:
                return pairs[k + 1]
        return pairs[-1] if len(pairs) % 2 else None

    def a_centroid(feat):
        g = geom_getter()
        return None if g is None else g.centroid

    return {
        "__a_round": a_round, "__a_upper": lambda s: None if s is None else str(s).upper(),
        "__a_lower": lambda s: None if s is None else str(s).lower(), "__a_concat": a_concat,
        "__a_left": lambda s, n: str(s or "")[:int(n)], "__a_right": lambda s, n: str(s or "")[-int(n):] if n else "",
        "__a_mid": lambda s, start, n=None: str(s or "")[int(start):(int(start) + int(n)) if n is not None else None],
        "__a_text": a_text, "__a_number": a_number, "__a_iif": lambda c, a, b: a if c else b,
        "__a_isempty": lambda v: v is None or v == "", "__a_floor": lambda v, n=0: math.floor(float(v)),
        "__a_ceil": lambda v, n=0: math.ceil(float(v)), "__a_sqrt": lambda v: math.sqrt(float(v)),
        "__a_max": lambda *v: max(v[0]) if len(v) == 1 and isinstance(v[0], (list, tuple)) else max(v),
        "__a_min": lambda *v: min(v[0]) if len(v) == 1 and isinstance(v[0], (list, tuple)) else min(v),
        "__a_area": a_area, "__a_length": a_length, "__a_area_geo": a_area_geo, "__a_length_geo": a_length_geo,
        "__a_geometry": lambda f: geom_getter(), "__a_trim": lambda s: None if s is None else str(s).strip(),
        "__a_replace": lambda s, a, b, all_=True: str(s or "").replace(str(a), str(b)) if all_ else
        str(s or "").replace(str(a), str(b), 1),
        "__a_default": lambda v, d: d if v is None or v == "" else v,
        "__a_proper": lambda s, *_: None if s is None else str(s).title(),
        "__a_find": lambda sub, s, start=0: str(s or "").find(str(sub), int(start)),
        "__a_now": datetime.datetime.now, "__a_decode": a_decode, "__a_when": a_when, "__a_centroid": a_centroid,
    }


def vb_expression(expr, field_names):
    """Very simple VB expressions: [Field] references and & concatenation."""
    lowered = {n.lower(): n for n in field_names}

    def repl(m):
        n = lowered.get(m.group(1).lower())
        if n is None:
            raise ExecuteError(f"ERROR 000539: Invalid field {m.group(1)}")
        return f"__row[{n!r}]"
    py = re.sub(r"\[([^\]]+)\]", repl, expr)
    if re.search(r"\b(Dim|If|Then|End|Set)\b", py):
        raise ExecuteError("ERROR 999999: arcpy CalculateField VB code blocks are not available in Biruni yet.")
    py = py.replace("&", "+")
    return py


def base_namespace():
    from . import _arcpy as arcpy
    return {"math": math, "datetime": datetime, "random": random, "re": re, "arcpy": arcpy, "statistics": statistics}


def arcade_namespace(geom_getter):
    ns = base_namespace()
    ns.update(_arcade_ns(geom_getter))
    return ns
