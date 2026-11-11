"""arcpy geometry objects on top of OGR/GEOS: Point, Array, Geometry, PointGeometry, Multipoint, Polyline, Polygon,
Extent and SpatialReference."""
import json
import math
import os
import re

from osgeo import ogr, osr

from ._base import ExecuteError, unsupported

_TRAD = osr.OAMS_TRADITIONAL_GIS_ORDER

# ---------------------------------------------------------------- units

LINEAR_UNITS = {
    "meters": 1.0, "meter": 1.0, "metres": 1.0, "m": 1.0, "kilometers": 1000.0, "kilometer": 1000.0, "km": 1000.0,
    "centimeters": 0.01, "millimeters": 0.001, "decimeters": 0.1, "feet": 0.3048, "foot": 0.3048,
    "internationalfeet": 0.3048, "feetint": 0.3048, "usfeet": 1200 / 3937, "feetus": 1200 / 3937,
    "ussurveyfeet": 1200 / 3937, "yards": 0.9144, "miles": 1609.344, "mile": 1609.344, "statutemiles": 1609.344,
    "nauticalmiles": 1852.0, "inches": 0.0254, "points": 0.0254 / 72,
}
AREA_UNITS = {
    "squaremeters": 1.0, "squarekilometers": 1e6, "hectares": 1e4, "ares": 100.0, "acres": 4046.8564224,
    "squarefeet": 0.09290304, "squarefeetus": (1200 / 3937) ** 2, "squareyards": 0.83612736,
    "squaremiles": 2589988.110336, "squarecentimeters": 1e-4, "squaremillimeters": 1e-6, "squareinches": 0.00064516,
    "squaredecimeters": 0.01,
}


def _ukey(unit):
    return re.sub(r"[\s_\-]", "", str(unit or "")).lower()


def linear_factor(unit):
    """Meters per unit for an arcpy unit name; None for unknown/degrees."""
    k = _ukey(unit)
    if k in LINEAR_UNITS:
        return LINEAR_UNITS[k]
    if k.startswith("us") and ("survey" in k or "feet" in k):
        return 1200 / 3937
    return None


def area_factor(unit):
    k = _ukey(unit)
    if k in AREA_UNITS:
        return AREA_UNITS[k]
    if k.startswith("square") and linear_factor(k[6:]):
        return linear_factor(k[6:]) ** 2
    return None


def parse_linear(value):
    """'100 Meters' / 100 / '100' -> (100.0, 'meters' or None); field names give (None, name)."""
    if value is None or value == "":
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip()
    m = re.match(r"^\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([A-Za-z_ ]*)\s*$", text)
    if not m:
        return None, text
    unit = m.group(2).strip() or None
    if unit and _ukey(unit) in ("unknown", "decimaldegrees"):
        unit = None if _ukey(unit) == "unknown" else "degrees"
    return float(m.group(1)), unit


# ---------------------------------------------------------------- spatial reference

_ESRI_NAMES = {
    "wgs1984": 4326, "gcswgs1984": 4326, "wgs84": 4326, "gcswgs84": 4326, "worldgeodeticsystem1984": 4326,
    "webmercator": 3857, "wgs1984webmercatorauxiliarysphere": 3857, "webmercatorauxiliarysphere": 3857,
    "wgs1984webmercator": 3857, "nad1983": 4269, "gcsnorthamerican1983": 4269, "etrs1989": 4258,
    "ainelabd1970": 4204, "gcsainelabd1970": 4204, "egypt1907": 4229, "gcsegypt1907": 4229,
}


def _name_to_epsg(name):
    key = re.sub(r"[\s_\-]", "", name).lower()
    if key in _ESRI_NAMES:
        return _ESRI_NAMES[key]
    m = re.match(r"^(wgs1984|wgs84|nad1983|etrs1989|ainelabd1970)utmzone(\d{1,2})([ns])$", key)
    if m:
        datum, zone, hemi = m.group(1), int(m.group(2)), m.group(3)
        if datum in ("wgs1984", "wgs84"):
            return (32600 if hemi == "n" else 32700) + zone
        if datum == "nad1983" and hemi == "n":
            return 26900 + zone
        if datum == "etrs1989" and hemi == "n":
            return 25800 + zone
        if datum == "ainelabd1970" and hemi == "n":
            return 20400 + zone
    return None


class SpatialReference:
    """arcpy.SpatialReference(wkid | name | .prj path | WKT)."""

    def __init__(self, item=None, vcs=None, text=None):
        self._srs = osr.SpatialReference()
        self._srs.SetAxisMappingStrategy(_TRAD)
        self._empty = True
        if item is not None and item != "":
            self._load(item)
        elif text:
            self.loadFromString(text)

    # construction -----------------------------------------------------
    @classmethod
    def _from_osr(cls, srs):
        sr = cls()
        if srs is not None:
            sr._srs = srs.Clone()
            sr._srs.SetAxisMappingStrategy(_TRAD)
            sr._empty = False
        return sr

    def _load(self, item):
        if isinstance(item, SpatialReference):
            self._srs = item._srs.Clone()
            self._empty = item._empty
            self._srs.SetAxisMappingStrategy(_TRAD)
            return
        if isinstance(item, osr.SpatialReference):
            self._srs = item.Clone()
            self._srs.SetAxisMappingStrategy(_TRAD)
            self._empty = False
            return
        if hasattr(item, "spatialReference") and not isinstance(item, str):
            self._load(item.spatialReference)
            return
        text = str(item).strip()
        ok = False
        if isinstance(item, int) or re.fullmatch(r"\d+", text):
            ok = self._import_code(int(text))
        elif os.path.isfile(text) and text.lower().endswith(".prj"):
            with open(text, encoding="utf-8", errors="replace") as fh:
                ok = self._import_text(fh.read())
        elif re.match(r"^\s*(PROJCS|GEOGCS|PROJCRS|GEOGCRS|GEODCRS|COMPD_CS|COMPOUNDCRS|BOUNDCRS|\+proj|EPSG:|ESRI:)",
                      text, re.I):
            ok = self._import_text(text)
        else:
            code = _name_to_epsg(text)
            if code:
                ok = self._import_code(code)
            if not ok:
                ok = self._import_dataset(text)
            if not ok:
                ok = self._import_text(text)
        if not ok:
            raise RuntimeError(f"ERROR 999999: Invalid spatial reference: {text}")
        self._empty = False

    def _import_code(self, code):
        srs = osr.SpatialReference()
        for attempt in (lambda: srs.ImportFromEPSG(code), lambda: srs.SetFromUserInput(f"ESRI:{code}")):
            try:
                if attempt() == 0:
                    self._srs = srs
                    self._srs.SetAxisMappingStrategy(_TRAD)
                    return True
            except Exception:
                srs = osr.SpatialReference()
        if code in (102100, 102113):
            return self._import_code(3857)
        return False

    def _import_text(self, text):
        srs = osr.SpatialReference()
        try:
            if srs.SetFromUserInput(text) == 0:
                self._srs = srs
                self._srs.SetAxisMappingStrategy(_TRAD)
                return True
        except Exception:
            pass
        return False

    def _import_dataset(self, text):
        try:
            from . import data
            if not data.Exists(text):
                return False
            sr = data.Describe(text).spatialReference
            if sr is None or sr._empty:
                return False
            self._srs = sr._srs.Clone()
            self._srs.SetAxisMappingStrategy(_TRAD)
            return True
        except Exception:
            return False

    def loadFromString(self, string):  # noqa: N802
        text = str(string).split(";")[0] if not str(string).lstrip().startswith(("PROJCRS", "GEOGCRS")) else string
        if not self._import_text(text):
            raise RuntimeError("ERROR 999999: Invalid spatial reference string")
        self._empty = False

    def create(self):
        pass

    def createFromFile(self, prj_file, vcs=None):  # noqa: N802
        self._load(prj_file)

    def setFalseOriginAndUnits(self, *a):  # noqa: N802
        pass

    def setDomain(self, *a):  # noqa: N802
        pass

    def exportToString(self):  # noqa: N802
        if self._empty:
            return "{B286C06B-0879-11D2-AACA-00C04FA33C20};-450359962737.05 -450359962737.05 10000;#;#;0.001;#;#;" \
                   "IsHighPrecision"
        c = self._srs.Clone()
        try:
            c.MorphToESRI()
        except Exception:
            pass
        return c.ExportToWkt()

    def exportToWKT(self):  # noqa: N802
        return self._srs.ExportToWkt() if not self._empty else ""

    # properties -------------------------------------------------------
    @property
    def name(self):
        if self._empty:
            return "Unknown"
        n = self._srs.GetName() or "Unknown"
        return n.replace(" / ", "_").replace(" ", "_")

    @property
    def type(self):
        if self._empty:
            return "Unknown"
        return "Projected" if self._srs.IsProjected() else "Geographic"

    def _code(self, geog=False):
        if self._empty:
            return 0
        s = self._srs.CloneGeogCS() if geog else self._srs.Clone()
        key = "GEOGCS" if geog or s.IsGeographic() else "PROJCS"
        code = s.GetAuthorityCode(key) or s.GetAuthorityCode(None)
        if not code:
            try:
                s.AutoIdentifyEPSG()
                code = s.GetAuthorityCode(None)
            except Exception:
                code = None
        if not code:
            try:
                matches = s.FindMatches()
                if matches and matches[0][1] >= 90:
                    code = matches[0][0].GetAuthorityCode(None)
            except Exception:
                code = None
        try:
            return int(code)
        except (TypeError, ValueError):
            return 0

    @property
    def factoryCode(self):  # noqa: N802
        return self._code()

    @property
    def PCSCode(self):  # noqa: N802
        return self._code() if self.type == "Projected" else 0

    @property
    def GCSCode(self):  # noqa: N802
        return self._code(geog=True)

    @property
    def PCSName(self):  # noqa: N802
        return self.name if self.type == "Projected" else ""

    @property
    def GCSName(self):  # noqa: N802
        return self.GCS.name

    @property
    def GCS(self):  # noqa: N802
        if self._empty:
            return SpatialReference()
        return SpatialReference._from_osr(self._srs.CloneGeogCS())

    @property
    def datumName(self):  # noqa: N802
        d = self._srs.GetAttrValue("DATUM") or ""
        return d if d.startswith("D_") else "D_" + d.replace(" ", "_") if d else ""

    @property
    def spheroidName(self):  # noqa: N802
        return (self._srs.GetAttrValue("SPHEROID") or self._srs.GetAttrValue("ELLIPSOID") or "").replace(" ", "_")

    @property
    def semiMajorAxis(self):  # noqa: N802
        return self._srs.GetSemiMajor()

    @property
    def semiMinorAxis(self):  # noqa: N802
        return self._srs.GetSemiMinor()

    @property
    def flattening(self):
        inv = self._srs.GetInvFlattening()
        return 1.0 / inv if inv else 0.0

    @property
    def linearUnitName(self):  # noqa: N802
        if self._empty or not self._srs.IsProjected():
            return ""
        u = (self._srs.GetLinearUnitsName() or "").lower()
        return {"metre": "Meter", "meter": "Meter", "foot": "Foot", "us survey foot": "Foot_US",
                "foot_us": "Foot_US"}.get(u, u.title().replace(" ", "_"))

    @property
    def metersPerUnit(self):  # noqa: N802
        return self._srs.GetLinearUnits() if not self._empty and self._srs.IsProjected() else 0.0

    @property
    def angularUnitName(self):  # noqa: N802
        return "Degree" if not self._empty else ""

    @property
    def radiansPerUnit(self):  # noqa: N802
        return math.pi / 180

    @property
    def projectionName(self):  # noqa: N802
        return (self._srs.GetAttrValue("PROJECTION") or "").title().replace(" ", "_")

    @property
    def projectionCode(self):  # noqa: N802
        return 0

    def _param(self, name):
        try:
            return self._srs.GetProjParm(name, 0.0)
        except Exception:
            return 0.0

    @property
    def centralMeridian(self):  # noqa: N802
        return self._param(osr.SRS_PP_CENTRAL_MERIDIAN)

    @property
    def falseEasting(self):  # noqa: N802
        return self._param(osr.SRS_PP_FALSE_EASTING)

    @property
    def falseNorthing(self):  # noqa: N802
        return self._param(osr.SRS_PP_FALSE_NORTHING)

    @property
    def scaleFactor(self):  # noqa: N802
        return self._param(osr.SRS_PP_SCALE_FACTOR)

    @property
    def latitudeOfOrigin(self):  # noqa: N802
        return self._param(osr.SRS_PP_LATITUDE_OF_ORIGIN)

    @property
    def VCS(self):  # noqa: N802
        return None

    @property
    def domain(self):
        return ""

    @property
    def XYTolerance(self):  # noqa: N802
        return 0.001 if self.type == "Projected" else 8.98315284119521e-09

    @property
    def XYResolution(self):  # noqa: N802
        return 0.0001 if self.type == "Projected" else 8.98315284119521e-10

    @property
    def isHighPrecision(self):  # noqa: N802
        return True

    @property
    def usage(self):
        return ""

    @property
    def remarks(self):
        return ""

    @property
    def abbreviation(self):
        return ""

    @property
    def alias(self):
        return ""

    def __eq__(self, other):
        if not isinstance(other, SpatialReference):
            return NotImplemented
        if self._empty or other._empty:
            return self._empty and other._empty
        return bool(self._srs.IsSame(other._srs))

    __hash__ = object.__hash__

    def __repr__(self):
        return f"<SpatialReference object '{self.name}'>"


def as_sr(value):
    """Anything arcpy accepts as a coordinate system -> SpatialReference (or None)."""
    if value is None or value == "" or value == "#":
        return None
    if isinstance(value, SpatialReference):
        return value
    return SpatialReference(value)


def osr_of(value):
    sr = as_sr(value)
    return None if sr is None or sr._empty else sr._srs


def transform(srs_from, srs_to):
    if srs_from is None or srs_to is None or srs_from.IsSame(srs_to):
        return None
    a, b = srs_from.Clone(), srs_to.Clone()
    a.SetAxisMappingStrategy(_TRAD)
    b.SetAxisMappingStrategy(_TRAD)
    return osr.CoordinateTransformation(a, b)


# ---------------------------------------------------------------- point and array

class Point:
    def __init__(self, X=None, Y=None, Z=None, M=None, ID=None):  # noqa: N803
        self.X = 0.0 if X is None else float(X)
        self.Y = 0.0 if Y is None else float(Y)
        self.Z = None if Z is None else float(Z)
        self.M = None if M is None else float(M)
        self.ID = 0 if ID is None else int(ID)

    def clone(self, point_object=None):
        return Point(self.X, self.Y, self.Z, self.M, self.ID)

    def equals(self, second_point):
        return isinstance(second_point, Point) and self.X == second_point.X and self.Y == second_point.Y

    def _pg(self):
        return PointGeometry(self)

    def within(self, second_geometry, relation=None):
        return self._pg().within(second_geometry)

    def contains(self, second_geometry, relation=None):
        return self._pg().contains(second_geometry)

    def disjoint(self, second_geometry):
        return self._pg().disjoint(second_geometry)

    def touches(self, second_geometry):
        return self._pg().touches(second_geometry)

    def overlaps(self, second_geometry):
        return self._pg().overlaps(second_geometry)

    def crosses(self, second_geometry):
        return self._pg().crosses(second_geometry)

    def __eq__(self, other):
        return self.equals(other)

    __hash__ = object.__hash__

    def __iter__(self):
        return iter((self.X, self.Y))

    def __repr__(self):
        z = "NaN" if self.Z is None else _num(self.Z)
        m = "NaN" if self.M is None else _num(self.M)
        return f"{_num(self.X)} {_num(self.Y)} {z} {m}"


def _num(v):
    return repr(float(v)).rstrip("0").rstrip(".") if v is not None else "NaN"


def _as_point(v):
    if v is None or isinstance(v, Point):
        return v
    if isinstance(v, PointGeometry):
        return v.firstPoint
    if isinstance(v, (list, tuple)) and len(v) >= 2 and all(isinstance(c, (int, float)) or c is None for c in v):
        return Point(*v[:4])
    return v


class Array:
    def __init__(self, items=None):
        self._items = []
        self._i = 0
        if items is not None:
            if isinstance(items, (Point, Array)):
                items = [items]
            for it in items:
                self.add(it)

    def _conv(self, v):
        v = _as_point(v)
        if isinstance(v, (list, tuple)):
            return Array(v)
        return v

    def add(self, value):
        self._items.append(self._conv(value))

    append = add

    def extend(self, items):
        for it in items:
            self.add(it)

    def insert(self, index, value):
        self._items.insert(index, self._conv(value))

    def remove(self, index):
        del self._items[index]

    def removeAll(self):  # noqa: N802
        self._items = []

    def replace(self, index, value):
        self._items[index] = self._conv(value)

    def getObject(self, index):  # noqa: N802
        return self._items[index]

    def clone(self):
        return Array([it.clone() if hasattr(it, "clone") else it for it in self._items])

    def next(self):
        if self._i < len(self._items):
            self._i += 1
            return self._items[self._i - 1]
        return None

    def reset(self):
        self._i = 0

    @property
    def count(self):
        return len(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]

    def __setitem__(self, i, v):
        self._items[i] = self._conv(v)

    def __iter__(self):
        return iter(list(self._items))

    def __repr__(self):
        return f"<Array {self._items!r}>"


# ---------------------------------------------------------------- geometry

_DIM = {"point": 0, "multipoint": 0, "polyline": 1, "polygon": 2}


def _flat(g):
    return ogr.GT_Flatten(g.GetGeometryType())


def _kind_of(g):
    t = _flat(g)
    if t == ogr.wkbPoint:
        return "point"
    if t == ogr.wkbMultiPoint:
        return "multipoint"
    if t in (ogr.wkbLineString, ogr.wkbMultiLineString, ogr.wkbCircularString, ogr.wkbCompoundCurve,
             ogr.wkbMultiCurve):
        return "polyline"
    if t in (ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbCurvePolygon, ogr.wkbMultiSurface):
        return "polygon"
    return "collection"


def _pick_dim(g, dim):
    """Keep only the parts of an OGR geometry with the given dimension (0, 1, 2) as a multi geometry."""
    if g is None:
        return None
    kind = _kind_of(g)
    if kind != "collection":
        if _DIM[kind] == dim:
            return g
        return None
    multi = {0: ogr.wkbMultiPoint, 1: ogr.wkbMultiLineString, 2: ogr.wkbMultiPolygon}[dim]
    out = ogr.Geometry(multi)
    for i in range(g.GetGeometryCount()):
        part = g.GetGeometryRef(i)
        sub = _pick_dim(part, dim)
        if sub is None:
            continue
        if _flat(sub) in (ogr.wkbMultiPoint, ogr.wkbMultiLineString, ogr.wkbMultiPolygon):
            for j in range(sub.GetGeometryCount()):
                out.AddGeometry(sub.GetGeometryRef(j).Clone())
        else:
            out.AddGeometry(sub.Clone())
    return out if out.GetGeometryCount() else None


def _top_dim(g):
    if g is None or g.IsEmpty():
        return -1
    k = _kind_of(g)
    if k != "collection":
        return _DIM[k]
    return max((_top_dim(g.GetGeometryRef(i)) for i in range(g.GetGeometryCount())), default=-1)


def wrap(g, sr=None, hint=None):
    """An OGR geometry -> the right arcpy geometry class."""
    if g is None:
        return None
    if isinstance(sr, osr.SpatialReference):
        sr = SpatialReference._from_osr(sr)
    kind = _kind_of(g)
    if kind == "collection":
        d = _top_dim(g)
        if d < 0:
            kind = hint or "polygon"
            g = ogr.Geometry({"point": ogr.wkbPoint, "multipoint": ogr.wkbMultiPoint,
                              "polyline": ogr.wkbMultiLineString}.get(kind, ogr.wkbMultiPolygon))
        else:
            g = _pick_dim(g, d)
            kind = {0: "multipoint", 1: "polyline", 2: "polygon"}[d]
    cls = {"point": PointGeometry, "multipoint": Multipoint, "polyline": Polyline, "polygon": Polygon}[kind]
    obj = cls.__new__(cls)
    obj._g = g
    obj._sr = sr
    return obj


def to_ogr(value, target_srs=None):
    """Geometry / Point / Extent / (x, y) / WKT / GeoJSON dict -> OGR geometry, in target_srs when both are known."""
    if value is None:
        return None
    if isinstance(value, ogr.Geometry):
        return value.Clone()
    src_srs = None
    if isinstance(value, Geometry):
        g = value._g.Clone()
        src_srs = value._sr._srs if value._sr is not None and not value._sr._empty else None
    elif isinstance(value, Point):
        g = ogr.Geometry(ogr.wkbPoint25D if value.Z is not None else ogr.wkbPoint)
        if value.Z is not None:
            g.AddPoint(value.X, value.Y, value.Z)
        else:
            g.AddPoint_2D(value.X, value.Y)
    elif isinstance(value, Extent):
        g = value.polygon._g.Clone()
        src_srs = value._sr._srs if value._sr is not None and not value._sr._empty else None
    elif isinstance(value, (tuple, list)) and len(value) >= 2 and all(isinstance(c, (int, float)) for c in value[:2]):
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(float(value[0]), float(value[1]))
    elif isinstance(value, dict):
        g = _from_json_dict(value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        g = ogr.CreateGeometryFromWkb(bytes(value))
    elif isinstance(value, str):
        text = value.strip()
        g = _from_json_dict(json.loads(text)) if text.startswith("{") else ogr.CreateGeometryFromWkt(text)
    else:
        raise TypeError(f"Cannot use {type(value).__name__} as a geometry")
    if g is not None and target_srs is not None and src_srs is not None:
        ct = transform(src_srs, target_srs)
        if ct is not None:
            g.Transform(ct)
    return g


def _from_json_dict(d):
    if "type" in d and "coordinates" in d or d.get("type") == "GeometryCollection":
        return ogr.CreateGeometryFromJson(json.dumps(d))
    if "x" in d and "y" in d:
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(float(d["x"]), float(d["y"]))
        return g
    if "points" in d:
        g = ogr.Geometry(ogr.wkbMultiPoint)
        for p in d["points"]:
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(p[0], p[1])
            g.AddGeometry(pt)
        return g
    if "paths" in d:
        g = ogr.Geometry(ogr.wkbMultiLineString)
        for path in d["paths"]:
            ln = ogr.Geometry(ogr.wkbLineString)
            for p in path:
                ln.AddPoint_2D(p[0], p[1])
            g.AddGeometry(ln)
        return g
    if "rings" in d:
        return _rings_to_polygon([[(p[0], p[1]) for p in r] for r in d["rings"]])
    raise ValueError("Unrecognised JSON geometry")


def _ring_geom(coords, has_z):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for c in coords:
        if has_z:
            ring.AddPoint(c[0], c[1], c[2] if len(c) > 2 and c[2] is not None else 0.0)
        else:
            ring.AddPoint_2D(c[0], c[1])
    if coords and (coords[0][0] != coords[-1][0] or coords[0][1] != coords[-1][1]):
        c = coords[0]
        if has_z:
            ring.AddPoint(c[0], c[1], c[2] if len(c) > 2 and c[2] is not None else 0.0)
        else:
            ring.AddPoint_2D(c[0], c[1])
    return ring


def _rings_to_polygon(rings, has_z=False):
    """Rings in any order/orientation -> (multi)polygon: a ring inside an odd number of larger rings is a hole."""
    items = []
    for coords in rings:
        if len(coords) < 3:
            continue
        ring = _ring_geom(coords, has_z)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        items.append((abs(poly.GetArea()), ring, poly))
    items.sort(key=lambda t: -t[0])
    shells = []  # [poly_with_holes, outer_poly, depth]
    for _area, ring, poly in items:
        probe = ogr.Geometry(ogr.wkbPoint)
        p = ring.GetPoint(0)
        probe.AddPoint_2D(p[0], p[1])
        containers = [s for s in shells if s[1].Contains(probe) or s[1].Intersects(probe)]
        depth = len(containers)
        if depth % 2 == 1:
            innermost = containers[-1]
            innermost[0].AddGeometry(ring.Clone())
            shells.append([None, poly, depth])
        else:
            shell = ogr.Geometry(ogr.wkbPolygon25D if has_z else ogr.wkbPolygon)
            shell.AddGeometry(ring.Clone())
            shells.append([shell, poly, depth])
    polys = [s[0] for s in shells if s[0] is not None]
    if len(polys) == 1:
        return polys[0]
    mp = ogr.Geometry(ogr.wkbMultiPolygon25D if has_z else ogr.wkbMultiPolygon)
    for p in polys:
        mp.AddGeometry(p)
    return mp


def _split_parts(inputs):
    """An arcpy Array (of Points, or of Arrays) -> list of parts, each a list of rings split at None points."""
    if inputs is None:
        return []
    if isinstance(inputs, Point):
        inputs = Array([inputs])
    if isinstance(inputs, (list, tuple)):
        inputs = Array(inputs)
    items = list(inputs)
    if items and all(isinstance(it, Array) for it in items if it is not None):
        groups = [list(it) for it in items if it is not None]
    else:
        groups = [items]
    parts = []
    for grp in groups:
        rings, cur = [], []
        for p in grp:
            p = _as_point(p)
            if p is None:
                if cur:
                    rings.append(cur)
                cur = []
                continue
            if isinstance(p, Array):  # deeper nesting: flatten
                for q in p:
                    q = _as_point(q)
                    if q is not None:
                        cur.append((q.X, q.Y, q.Z))
                continue
            cur.append((p.X, p.Y, p.Z))
        if cur:
            rings.append(cur)
        if rings:
            parts.append(rings)
    return parts


def _points_of(g):
    """All vertices of an OGR geometry as (x, y, z|None) tuples."""
    out = []
    n = g.GetGeometryCount()
    if n:
        for i in range(n):
            out.extend(_points_of(g.GetGeometryRef(i)))
        return out
    has_z = g.Is3D()
    for i in range(g.GetPointCount()):
        p = g.GetPoint(i)
        out.append((p[0], p[1], p[2] if has_z else None))
    return out


def _mk_point(t):
    return Point(t[0], t[1], t[2])


class Geometry:
    """Base class; `arcpy.Geometry("polygon", array, sr)` also works."""

    _kind = None

    def __init__(self, geometry=None, inputs=None, spatial_reference=None, has_z=False, has_m=False):
        self._g = ogr.Geometry(ogr.wkbGeometryCollection)
        self._sr = None
        if isinstance(geometry, str):
            target = {"point": PointGeometry, "multipoint": Multipoint, "polyline": Polyline,
                      "polygon": Polygon}.get(geometry.lower())
            if target is None:
                unsupported(f"Geometry('{geometry}')")
            if target is PointGeometry and isinstance(inputs, Array) and len(inputs):
                inputs = inputs[0]
            tmp = target(inputs, spatial_reference, has_z, has_m)
            self.__class__ = target
            self._g, self._sr = tmp._g, tmp._sr

    # basic properties -----------------------------------------------------------
    @property
    def type(self):
        return self._kind

    @property
    def spatialReference(self):  # noqa: N802
        return self._sr

    @property
    def isMultipart(self):  # noqa: N802
        return self.partCount > 1

    @property
    def hasZ(self):  # noqa: N802
        return bool(self._g.Is3D())

    @property
    def hasM(self):  # noqa: N802
        return bool(self._g.IsMeasured())

    @property
    def hasCurves(self):  # noqa: N802
        return bool(self._g.HasCurveGeometry())

    @property
    def area(self):
        return self._g.GetArea() if self._kind == "polygon" else 0.0

    @property
    def length(self):
        if self._kind == "polygon":
            b = self._g.GetBoundary() if hasattr(self._g, "GetBoundary") else self._g.Boundary()
            return b.Length() if b is not None else 0.0
        if self._kind == "polyline":
            return self._g.Length()
        return 0.0

    @property
    def length3D(self):  # noqa: N802
        return self.length

    @property
    def centroid(self):
        if self._g.IsEmpty():
            return None
        c = self._g.Centroid()
        if self._kind == "polygon" and not self._g.Intersects(c):
            c = self._g.PointOnSurface()
        return Point(c.GetX(), c.GetY())

    @property
    def trueCentroid(self):  # noqa: N802
        if self._g.IsEmpty():
            return None
        c = self._g.Centroid()
        return Point(c.GetX(), c.GetY())

    @property
    def labelPoint(self):  # noqa: N802
        if self._g.IsEmpty():
            return None
        c = self._g.PointOnSurface() if self._kind == "polygon" else self._g.Centroid()
        return Point(c.GetX(), c.GetY())

    @property
    def firstPoint(self):  # noqa: N802
        pts = _points_of(self._g)
        return _mk_point(pts[0]) if pts else None

    @property
    def lastPoint(self):  # noqa: N802
        pts = _points_of(self._g)
        return _mk_point(pts[-1]) if pts else None

    @property
    def extent(self):
        if self._g.IsEmpty():
            return Extent(spatial_reference=self._sr)
        x0, x1, y0, y1 = self._g.GetEnvelope()
        return Extent(x0, y0, x1, y1, spatial_reference=self._sr)

    @property
    def hullRectangle(self):  # noqa: N802
        e = self.extent
        return f"{e.XMin} {e.YMin} {e.XMin} {e.YMax} {e.XMax} {e.YMax} {e.XMax} {e.YMin}"

    def _parts(self):
        t = _flat(self._g)
        if t in (ogr.wkbMultiPoint, ogr.wkbMultiLineString, ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
            return [self._g.GetGeometryRef(i) for i in range(self._g.GetGeometryCount())]
        return [self._g] if not self._g.IsEmpty() else []

    @property
    def partCount(self):  # noqa: N802
        return len(self._parts())

    @property
    def pointCount(self):  # noqa: N802
        return len(_points_of(self._g))

    @property
    def WKT(self):  # noqa: N802
        g = self._g.Clone()
        if self._kind == "polygon":
            g = ogr.ForceToMultiPolygon(g)
        elif self._kind == "polyline":
            g = ogr.ForceToMultiLineString(g)
        return g.ExportToIsoWkt()

    @property
    def WKB(self):  # noqa: N802
        return bytearray(self._g.ExportToIsoWkb())

    @property
    def JSON(self):  # noqa: N802
        sr = {"wkid": self._sr.factoryCode} if self._sr is not None and not self._sr._empty else None
        d = {}
        if self._kind == "point":
            d = {"x": self._g.GetX(), "y": self._g.GetY()}
        elif self._kind == "multipoint":
            d = {"points": [[p[0], p[1]] for p in _points_of(self._g)]}
        elif self._kind == "polyline":
            d = {"paths": [[[p[0], p[1]] for p in _points_of(part)] for part in self._parts()]}
        elif self._kind == "polygon":
            rings = []
            for part in self._parts():
                for i in range(part.GetGeometryCount()):
                    rings.append([[p[0], p[1]] for p in _points_of(part.GetGeometryRef(i))])
            d = {"rings": rings}
        if sr:
            d["spatialReference"] = sr
        return json.dumps(d)

    @property
    def __geo_interface__(self):
        return json.loads(self._g.ExportToJson())

    # iteration -------------------------------------------------------------------
    def _part_array(self, part):
        if self._kind == "polygon":
            pts = []
            for i in range(part.GetGeometryCount()):
                if i:
                    pts.append(None)
                pts.extend(_mk_point(p) for p in _points_of(part.GetGeometryRef(i)))
            return Array(pts)
        return Array([_mk_point(p) for p in _points_of(part)])

    def getPart(self, index=None):  # noqa: N802
        parts = self._parts()
        if self._kind == "point":
            return self.firstPoint
        if self._kind == "multipoint":
            pts = [_mk_point(p) for p in _points_of(self._g)]
            return Array(pts) if index is None else pts[index]
        if index is None:
            return Array([self._part_array(p) for p in parts])
        return self._part_array(parts[index])

    def __iter__(self):
        if self._kind == "multipoint":
            return iter([_mk_point(p) for p in _points_of(self._g)])
        if self._kind == "point":
            return iter([self.firstPoint])
        return iter([self._part_array(p) for p in self._parts()])

    # relations -------------------------------------------------------------------
    def _other(self, other):
        srs = self._sr._srs if self._sr is not None and not self._sr._empty else None
        return to_ogr(other, srs)

    def contains(self, second_geometry, relation=None):
        return bool(self._g.Contains(self._other(second_geometry)))

    def within(self, second_geometry, relation=None):
        o = self._other(second_geometry)
        if relation and str(relation).upper() == "PROPER":
            return bool(self._g.Within(o)) and not self._g.Touches(o)
        return bool(self._g.Within(o))

    def crosses(self, second_geometry):
        return bool(self._g.Crosses(self._other(second_geometry)))

    def disjoint(self, second_geometry):
        return bool(self._g.Disjoint(self._other(second_geometry)))

    def equals(self, second_geometry):
        return bool(self._g.Equals(self._other(second_geometry)))

    def overlaps(self, second_geometry):
        return bool(self._g.Overlaps(self._other(second_geometry)))

    def touches(self, second_geometry):
        return bool(self._g.Touches(self._other(second_geometry)))

    def distanceTo(self, other):  # noqa: N802
        return float(self._g.Distance(self._other(other)))

    # operations ------------------------------------------------------------------
    def _wrap(self, g, hint=None):
        return wrap(g, self._sr, hint)

    def buffer(self, distance):
        return self._wrap(self._g.Buffer(float(distance), 30), "polygon")

    def intersect(self, other, dimension=None):
        g = self._g.Intersection(self._other(other))
        if dimension is not None:
            d = {1: 0, 2: 1, 4: 2}.get(int(dimension), 2)
            picked = _pick_dim(g, d) if g is not None else None
            hint = {0: "multipoint", 1: "polyline", 2: "polygon"}[d]
            if picked is None:
                return self._wrap(ogr.Geometry({0: ogr.wkbMultiPoint, 1: ogr.wkbMultiLineString,
                                                2: ogr.wkbMultiPolygon}[d]), hint)
            if d == 0 and _flat(picked) == ogr.wkbPoint:
                mp = ogr.Geometry(ogr.wkbMultiPoint)
                mp.AddGeometry(picked)
                picked = mp
            return self._wrap(picked, hint)
        return self._wrap(g, self._kind)

    def union(self, other):
        return self._wrap(self._g.Union(self._other(other)), self._kind)

    def difference(self, other):
        return self._wrap(self._g.Difference(self._other(other)), self._kind)

    def symmetricDifference(self, other):  # noqa: N802
        return self._wrap(self._g.SymDifference(self._other(other)), self._kind)

    def clip(self, envelope):
        return self._wrap(self._g.Intersection(self._other(envelope)), self._kind)

    def convexHull(self):  # noqa: N802
        return self._wrap(self._g.ConvexHull())

    def boundary(self):
        b = self._g.GetBoundary() if hasattr(self._g, "GetBoundary") else self._g.Boundary()
        return self._wrap(b)

    def generalize(self, max_offset):
        return self._wrap(self._g.SimplifyPreserveTopology(float(max_offset)), self._kind)

    def densify(self, method, distance, deviation=None):
        g = self._g.Clone()
        g.Segmentize(float(distance))
        return self._wrap(g, self._kind)

    def projectAs(self, spatial_reference, transformation_name=None):  # noqa: N802
        sr = as_sr(spatial_reference)
        if sr is None or sr._empty or self._sr is None or self._sr._empty:
            return wrap(self._g.Clone(), sr if sr is not None else self._sr, self._kind)
        g = self._g.Clone()
        ct = transform(self._sr._srs, sr._srs)
        if ct is not None:
            g.Transform(ct)
        return wrap(g, sr, self._kind)

    def cut(self, cutter):
        unsupported("Geometry.cut")

    def getGeohash(self, precision=8):  # noqa: N802
        unsupported("Geometry.getGeohash")

    # measures --------------------------------------------------------------------
    def _local(self):
        """This geometry in a local equal-area / equidistant projection centred on it (meters)."""
        g = self._g.Clone()
        src = self._sr._srs if self._sr is not None and not self._sr._empty else None
        if src is None:
            return None
        geo = osr.SpatialReference()
        geo.ImportFromEPSG(4326)
        geo.SetAxisMappingStrategy(_TRAD)
        ct = transform(src, geo)
        if ct is not None:
            g.Transform(ct)
        c = g.Centroid()
        return g, c.GetX(), c.GetY(), geo

    def _geodesic(self, what):
        loc = self._local()
        if loc is None:
            return None
        g, lon, lat, geo = loc
        proj = osr.SpatialReference()
        if what == "area":
            proj.SetLAEA(lat, lon, 0, 0)
        else:
            proj.SetAE(lat, lon, 0, 0)
        proj.SetWellKnownGeogCS("WGS84")
        proj.SetAxisMappingStrategy(_TRAD)
        g.Transform(osr.CoordinateTransformation(geo, proj))
        w = wrap(g, None, self._kind)
        return w.area if what == "area" else w.length

    def _meters_per_unit(self):
        if self._sr is None or self._sr._empty or not self._sr._srs.IsProjected():
            return None
        return self._sr._srs.GetLinearUnits()

    def getArea(self, method="PLANAR", units=None):  # noqa: N802
        method = (method or "PLANAR").upper()
        mpu = self._meters_per_unit()
        if method == "PLANAR" and (mpu is not None or not units):
            area_m = self.area * (mpu or 1.0) ** 2 if units else self.area
            if not units:
                return area_m
        else:
            area_m = self._geodesic("area")
            if area_m is None:
                area_m = self.area
        f = area_factor(units) if units else 1.0
        return area_m / f if f else area_m

    def getLength(self, method="PLANAR", units=None):  # noqa: N802
        method = (method or "PLANAR").upper()
        mpu = self._meters_per_unit()
        if method == "PLANAR" and (mpu is not None or not units):
            if not units:
                return self.length
            len_m = self.length * (mpu or 1.0)
        else:
            len_m = self._geodesic("length")
            if len_m is None:
                len_m = self.length
        f = linear_factor(units) if units else 1.0
        return len_m / f if f else len_m

    # linear referencing (planar) --------------------------------------------------------
    def _line_pts(self):
        return [(p[0], p[1]) for p in _points_of(self._parts()[0])] if self._parts() else []

    def positionAlongLine(self, value, use_percentage=False):  # noqa: N802
        pts = self._line_pts()
        total = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        d = float(value) * total if use_percentage else float(value)
        d = max(0.0, min(total, d))
        run = 0.0
        for a, b in zip(pts, pts[1:]):
            seg = math.dist(a, b)
            if run + seg >= d and seg > 0:
                t = (d - run) / seg
                return PointGeometry(Point(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])), self._sr)
            run += seg
        return PointGeometry(Point(*pts[-1]), self._sr) if pts else None

    def queryPointAndDistance(self, in_point, as_percentage=False):  # noqa: N802
        p = to_ogr(in_point)
        px, py = p.GetX(), p.GetY()
        pts = self._line_pts()
        best = None
        run = 0.0
        total = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        for a, b in zip(pts, pts[1:]):
            seg = math.dist(a, b)
            t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - a[0]) * (b[0] - a[0]) + (py - a[1]) * (b[1] - a[1]))
                                                   / seg ** 2))
            q = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            dist = math.dist(q, (px, py))
            cross = (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
            if best is None or dist < best[2]:
                best = (q, run + t * seg, dist, cross < 0)
            run += seg
        if best is None:
            return None
        along = best[1] / total if as_percentage and total else best[1]
        return PointGeometry(Point(*best[0]), self._sr), along, best[2], best[3]

    def measureOnLine(self, in_point, as_percentage=False):  # noqa: N802
        r = self.queryPointAndDistance(in_point, as_percentage)
        return r[1] if r else 0.0

    def segmentAlongLine(self, start_measure, end_measure, use_percentage=False):  # noqa: N802
        pts = self._line_pts()
        total = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        s = float(start_measure) * (total if use_percentage else 1)
        e = float(end_measure) * (total if use_percentage else 1)
        out = [self.positionAlongLine(s).firstPoint]
        run = 0.0
        for a, b in zip(pts, pts[1:]):
            run += math.dist(a, b)
            if s < run < e:
                out.append(Point(*b))
        out.append(self.positionAlongLine(e).firstPoint)
        return Polyline(Array(out), self._sr)

    def angleAndDistanceTo(self, other, method="GEODESIC"):  # noqa: N802
        a = self.firstPoint if self._kind == "point" else self.centroid
        o = to_ogr(other, self._sr._srs if self._sr is not None and not self._sr._empty else None)
        dx, dy = o.Centroid().GetX() - a.X, o.Centroid().GetY() - a.Y
        return (math.degrees(math.atan2(dx, dy)), math.hypot(dx, dy))

    def pointFromAngleAndDistance(self, angle, distance, method="GEODESIC"):  # noqa: N802
        a = self.firstPoint if self._kind == "point" else self.centroid
        r = math.radians(float(angle))
        return PointGeometry(Point(a.X + float(distance) * math.sin(r), a.Y + float(distance) * math.cos(r)),
                             self._sr)

    # misc ------------------------------------------------------------------------
    def __eq__(self, other):
        if isinstance(other, Geometry):
            return bool(self._g.Equals(other._g))
        return NotImplemented

    __hash__ = object.__hash__

    def __repr__(self):
        return f"<{type(self).__name__} object at {hex(id(self))}>"

    def __str__(self):
        return self.WKT

    def __reduce__(self):
        return (FromWKT, (self._g.ExportToWkt(), self._sr))


class PointGeometry(Geometry):
    _kind = "point"

    def __init__(self, inputs=None, spatial_reference=None, has_z=False, has_m=False):
        p = _as_point(inputs)
        if isinstance(p, Array):
            p = p[0] if len(p) else None
        g = ogr.Geometry(ogr.wkbPoint)
        if p is not None:
            if p.Z is not None or has_z:
                g = ogr.Geometry(ogr.wkbPoint25D)
                g.AddPoint(p.X, p.Y, p.Z or 0.0)
            else:
                g.AddPoint_2D(p.X, p.Y)
        self._g = g
        self._sr = as_sr(spatial_reference)

    @property
    def centroid(self):
        return self.firstPoint

    @property
    def length(self):
        return 0.0


class Multipoint(Geometry):
    _kind = "multipoint"

    def __init__(self, inputs=None, spatial_reference=None, has_z=False, has_m=False):
        g = ogr.Geometry(ogr.wkbMultiPoint)
        for p in (inputs or []):
            p = _as_point(p)
            if p is None:
                continue
            pt = ogr.Geometry(ogr.wkbPoint)
            pt.AddPoint_2D(p.X, p.Y)
            g.AddGeometry(pt)
        self._g = g
        self._sr = as_sr(spatial_reference)


class Polyline(Geometry):
    _kind = "polyline"

    def __init__(self, inputs=None, spatial_reference=None, has_z=False, has_m=False):
        parts = _split_parts(inputs)
        z = has_z or any(c[2] is not None for part in parts for ring in part for c in ring)
        lines = []
        for part in parts:
            for ring in part:
                ln = ogr.Geometry(ogr.wkbLineString25D if z else ogr.wkbLineString)
                for c in ring:
                    if z:
                        ln.AddPoint(c[0], c[1], c[2] or 0.0)
                    else:
                        ln.AddPoint_2D(c[0], c[1])
                lines.append(ln)
        if len(lines) == 1:
            g = lines[0]
        else:
            g = ogr.Geometry(ogr.wkbMultiLineString25D if z else ogr.wkbMultiLineString)
            for ln in lines:
                g.AddGeometry(ln)
        self._g = g
        self._sr = as_sr(spatial_reference)


class Polygon(Geometry):
    _kind = "polygon"

    def __init__(self, inputs=None, spatial_reference=None, has_z=False, has_m=False):
        parts = _split_parts(inputs)
        z = has_z or any(c[2] is not None for part in parts for ring in part for c in ring)
        rings = [ring for part in parts for ring in part]
        self._g = _rings_to_polygon(rings, z) if rings else ogr.Geometry(ogr.wkbPolygon)
        self._sr = as_sr(spatial_reference)


class Extent:
    def __init__(self, XMin=None, YMin=None, XMax=None, YMax=None, ZMin=None, ZMax=None,  # noqa: N803
                 MMin=None, MMax=None, spatial_reference=None):
        nan = float("nan")
        self.XMin = nan if XMin is None else float(XMin)
        self.YMin = nan if YMin is None else float(YMin)
        self.XMax = nan if XMax is None else float(XMax)
        self.YMax = nan if YMax is None else float(YMax)
        self.ZMin, self.ZMax, self.MMin, self.MMax = ZMin, ZMax, MMin, MMax
        self._sr = as_sr(spatial_reference)

    @property
    def spatialReference(self):  # noqa: N802
        return self._sr

    @property
    def width(self):
        return self.XMax - self.XMin

    @property
    def height(self):
        return self.YMax - self.YMin

    @property
    def lowerLeft(self):  # noqa: N802
        return Point(self.XMin, self.YMin)

    @property
    def lowerRight(self):  # noqa: N802
        return Point(self.XMax, self.YMin)

    @property
    def upperLeft(self):  # noqa: N802
        return Point(self.XMin, self.YMax)

    @property
    def upperRight(self):  # noqa: N802
        return Point(self.XMax, self.YMax)

    @property
    def polygon(self):
        return Polygon(Array([Point(self.XMin, self.YMin), Point(self.XMin, self.YMax), Point(self.XMax, self.YMax),
                              Point(self.XMax, self.YMin), Point(self.XMin, self.YMin)]), self._sr)

    @property
    def JSON(self):  # noqa: N802
        return json.dumps({"xmin": self.XMin, "ymin": self.YMin, "xmax": self.XMax, "ymax": self.YMax})

    def contains(self, second_geometry, relation=None):
        return self.polygon.contains(second_geometry)

    def within(self, second_geometry, relation=None):
        return self.polygon.within(second_geometry)

    def overlaps(self, second_geometry):
        return self.polygon.overlaps(second_geometry)

    def disjoint(self, second_geometry):
        return self.polygon.disjoint(second_geometry)

    def touches(self, second_geometry):
        return self.polygon.touches(second_geometry)

    def crosses(self, second_geometry):
        return self.polygon.crosses(second_geometry)

    def equals(self, second_geometry):
        return self.polygon.equals(second_geometry)

    def projectAs(self, spatial_reference, transformation_name=None):  # noqa: N802
        return self.polygon.projectAs(spatial_reference).extent

    def __repr__(self):
        def f(v):
            return "NaN" if v is None or (isinstance(v, float) and math.isnan(v)) else _num(v)
        return " ".join(f(v) for v in (self.XMin, self.YMin, self.XMax, self.YMax, self.ZMin, self.ZMax,
                                       self.MMin, self.MMax))

    __str__ = __repr__


# ---------------------------------------------------------------- module-level helpers

def FromWKT(wkt_string, spatial_reference=None):  # noqa: N802
    return wrap(ogr.CreateGeometryFromWkt(wkt_string), as_sr(spatial_reference))


def FromWKB(wkb_string, spatial_reference=None):  # noqa: N802
    return wrap(ogr.CreateGeometryFromWkb(bytes(wkb_string)), as_sr(spatial_reference))


def FromGeohash(geohash_string):  # noqa: N802
    unsupported("FromGeohash")


def FromCoordString(*a, **k):  # noqa: N802
    unsupported("FromCoordString")


def AsShape(geojson_struct, esri_json=False):  # noqa: N802
    d = json.loads(geojson_struct) if isinstance(geojson_struct, str) else dict(geojson_struct)
    sr = None
    if esri_json and isinstance(d.get("spatialReference"), dict):
        wkid = d["spatialReference"].get("latestWkid") or d["spatialReference"].get("wkid")
        sr = SpatialReference(wkid) if wkid else None
    return wrap(_from_json_dict(d), sr)


def ensure_geometry(value, sr=None):
    """Any accepted geometry input -> arcpy Geometry object."""
    if isinstance(value, Geometry):
        return value
    if isinstance(value, Point):
        return PointGeometry(value, sr)
    if isinstance(value, Extent):
        return value.polygon
    return wrap(to_ogr(value), as_sr(sr))


__all__ = ["Point", "Array", "Geometry", "PointGeometry", "Multipoint", "Polyline", "Polygon", "Extent",
           "SpatialReference", "FromWKT", "FromWKB", "AsShape", "FromGeohash", "FromCoordString", "ExecuteError"]
