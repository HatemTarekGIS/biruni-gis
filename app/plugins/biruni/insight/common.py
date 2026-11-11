"""Shared plumbing of the insight tools: a layer read once into memory (geometries in one coordinate system,
attributes, a spatial index, a point inside every feature), value keys that compare names the way people
write them, neighbours, and metre <-> layer unit conversion.

Reading once and working on plain dicts is what keeps 20,000 parcels in seconds: every later question is a
dict lookup or an index query instead of another pass over the provider.
"""
import math
import re
from collections import Counter

from qgis.core import (Qgis, QgsCoordinateTransform, QgsDistanceArea, QgsFeatureRequest, QgsGeometry, QgsProject,
                       QgsRectangle, QgsSpatialIndex, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from .. import arabic_text as at

MAX_FEATURES = 250000
METRES_PER_DEGREE = 111320.0
_TRAILING_NUMBER = re.compile(r"^(.*?)(\d+)$")


def is_empty(v):
    return v is None or (hasattr(v, "isNull") and v.isNull()) or (isinstance(v, str) and v.strip() == "") or \
        str(v) == "NULL"


def value_key(v):
    """The matching key of a value: «حي الياسمين», "الياسمين " and "الياسمين" give one key, 12.0 and 12 too."""
    if is_empty(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    return at.normalise(str(v), strip_prefixes=True)


def number(v):
    """A float from a number or a text holding one (Arabic digits too); None otherwise."""
    if is_empty(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    n = at.parse_number(s)
    if n is None:
        return None
    # "12 m" is a number, "Block 12" is not: the number must be (nearly) the whole text
    digits, _ = at.light_normalise(s)
    return n if len(re.sub(r"[\d.,\s-]", "", digits)) <= 2 else None


def split_number(v):
    """(prefix, number, width) of an id like 101, "A-101" or "١٠٤"; None when it does not end with digits."""
    if is_empty(v) or isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = int(v)
    s, _ = at.light_normalise(str(v).strip())
    m = _TRAILING_NUMBER.match(s)
    if not m:
        return None
    return m.group(1), int(m.group(2)), len(m.group(2))


def field_kind(fld):
    """"int", "real" or "text" (None for dates, blobs, lists... which these tools leave alone)."""
    t = fld.type()
    if t in (QMetaType.Type.Int, QMetaType.Type.LongLong, QMetaType.Type.UInt, QMetaType.Type.ULongLong):
        return "int"
    if t == QMetaType.Type.Double:
        return "real"
    if t == QMetaType.Type.QString:
        return "text"
    return None


def is_geographic(crs):
    return crs.isValid() and crs.isGeographic()


def to_units(metres, crs):
    """A distance in metres expressed in the units of ``crs`` (degrees are taken at the equator - good enough
    for tolerances)."""
    return metres / METRES_PER_DEGREE if is_geographic(crs) else metres


def to_metres(units, crs):
    return units * METRES_PER_DEGREE if is_geographic(crs) else units


def geometry_type(layer):
    return layer.geometryType() if isinstance(layer, QgsVectorLayer) else None


def inner_point(g):
    """A point that lies on the feature: the point itself, the middle of a line, a point inside a polygon."""
    t = g.type()
    if t == Qgis.GeometryType.Point:
        return g.asPoint() if not g.isMultipart() else g.centroid().asPoint()
    if t == Qgis.GeometryType.Line:
        return g.interpolate(g.length() / 2).asPoint()
    p = g.pointOnSurface()
    return p.asPoint() if not p.isNull() else g.centroid().asPoint()


class Snapshot:
    """One vector layer read into memory, its geometries in ``crs`` (the layer's own by default)."""

    def __init__(self, layer, crs=None, feedback=None, limit=MAX_FEATURES, geometry=True):
        self.layer = layer
        self.layer_id = layer.id()
        self.name = layer.name()
        self.crs = crs if crs is not None and crs.isValid() else layer.crs()
        self.gtype = layer.geometryType()
        self.fields = [f.name() for f in layer.fields()]
        self.kinds = {f.name(): field_kind(f) for f in layer.fields()}
        self.attrs = {}          # fid -> list of attribute values
        self.geoms = {}          # fid -> QgsGeometry (in self.crs)
        self.points = {}         # fid -> QgsPointXY on the feature
        self.index = QgsSpatialIndex()
        self.truncated = False
        self._engines = {}
        self._neighbours = None
        xform = None
        if geometry and self.crs != layer.crs() and layer.crs().isValid():
            xform = QgsCoordinateTransform(layer.crs(), self.crs, QgsProject.instance())
        req = QgsFeatureRequest()
        if not geometry:
            req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
        total = max(1, layer.featureCount())
        for i, f in enumerate(layer.getFeatures(req)):
            if i >= limit:
                self.truncated = True
                break
            if feedback is not None and i % 1000 == 0 and feedback.isCanceled():
                break
            fid = f.id()
            self.attrs[fid] = f.attributes()
            if not geometry:
                continue
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            if xform is not None:
                g = QgsGeometry(g)
                try:
                    g.transform(xform)
                except Exception:  # noqa: BLE001 - a point outside the target system is left out
                    continue
            self.geoms[fid] = g
            self.index.addFeature(fid, g.boundingBox())
        self.count = len(self.attrs)
        self.total = total

    # ---- values -------------------------------------------------------------
    def column(self, field):
        """{fid: value} of the non-empty values of ``field``."""
        i = self.fields.index(field)
        return {fid: a[i] for fid, a in self.attrs.items() if not is_empty(a[i])}

    def value(self, fid, field):
        return self.attrs[fid][self.fields.index(field)]

    # ---- geometry ----------------------------------------------------------
    def point(self, fid):
        p = self.points.get(fid)
        if p is None and fid in self.geoms:
            p = self.points[fid] = inner_point(self.geoms[fid])
        return p

    def engine(self, fid):
        """A prepared geometry engine (fast repeated contains / intersects), made once per feature."""
        e = self._engines.get(fid)
        if e is None:
            g = self.geoms[fid]
            if not g.isGeosValid():
                g = g.makeValid()
                self.geoms[fid] = g
            e = QgsGeometry.createGeometryEngine(g.constGet())
            e.prepareGeometry()
            self._engines[fid] = e
        return e

    def containing(self, point):
        """Ids of the polygons that contain ``point``."""
        rect = QgsRectangle(point.x(), point.y(), point.x(), point.y())
        pg = QgsGeometry.fromPointXY(point)     # kept alive while the engine reads it
        return [fid for fid in self.index.intersects(rect) if self.engine(fid).contains(pg.constGet())]

    def near(self, geom, distance):
        """Ids whose bounding box lies within ``distance`` of the bounding box of ``geom``."""
        r = QgsRectangle(geom.boundingBox())
        r.grow(distance)
        return self.index.intersects(r)

    def typical_size(self):
        """Median square root of the polygon areas (or median feature extent): the scale of one feature."""
        sizes = []
        for n, g in enumerate(self.geoms.values()):
            if n >= 2000:
                break
            if self.gtype == Qgis.GeometryType.Polygon:
                sizes.append(math.sqrt(max(g.area(), 0)))
            else:
                b = g.boundingBox()
                sizes.append(max(b.width(), b.height()))
        sizes = sorted(s for s in sizes if s > 0)
        return sizes[len(sizes) // 2] if sizes else to_units(10, self.crs)

    def neighbours(self):
        """{fid: [neighbour fids]}: polygons that touch (within a hair), points and lines by their nearest 6
        within 3x the typical spacing. Computed once."""
        if self._neighbours is not None:
            return self._neighbours
        out = {}
        if self.gtype == Qgis.GeometryType.Polygon:
            tol = max(self.typical_size() * 0.02, to_units(0.05, self.crs))
            for fid, g in self.geoms.items():
                e = self.engine(fid)
                cands = [o for o in self.near(g, tol) if o != fid]
                out[fid] = [o for o in cands if e.distance(self.geoms[o].constGet()) <= tol]
        else:
            pidx = QgsSpatialIndex()
            for fid in self.geoms:
                p = self.point(fid)
                pidx.addFeature(fid, QgsRectangle(p.x(), p.y(), p.x(), p.y()))
            nearest = {}
            for fid in self.geoms:
                p = self.point(fid)
                nearest[fid] = [o for o in pidx.nearestNeighbor(p, 7) if o != fid][:6]
            gaps = sorted(self.point(fid).distance(self.point(n[0])) for fid, n in nearest.items() if n)
            reach = 3 * gaps[len(gaps) // 2] if gaps else 0
            for fid, ns in nearest.items():
                p = self.point(fid)
                out[fid] = [o for o in ns if p.distance(self.point(o)) <= reach]
        self._neighbours = out
        return out


def measurer(crs):
    """area(g) / length(g) in square metres / metres: planar in a projected system, on the ellipsoid in degrees."""
    if not is_geographic(crs):
        return (lambda g: g.area()), (lambda g: g.length())
    da = QgsDistanceArea()
    da.setSourceCrs(crs, QgsProject.instance().transformContext())
    da.setEllipsoid(QgsProject.instance().ellipsoid() if QgsProject.instance().ellipsoid() not in ("", "NONE")
                    else "EPSG:7030")
    return da.measureArea, da.measureLength


def vector_layers(project=None):
    project = project or QgsProject.instance()
    return [lyr for lyr in project.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial()]


def text_fields(snap):
    return [f for f in snap.fields if snap.kinds.get(f) in ("text", "int")]


def distinct(values):
    return Counter(value_key(v) for v in values if not is_empty(v))


def nice_floor(x):
    """The largest of 0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15, 20 ... not above x (0 below 0.25)."""
    steps = [0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100]
    best = 0.0
    for s in steps:
        if s <= x + 1e-9:
            best = s
    return best


def fmt(x, decimals=2):
    """A short number for messages: 12, 12.5, 0.75 (no trailing zeros)."""
    if x is None:
        return ""
    s = f"{x:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _edits(a, b, limit):
    """Levenshtein distance of two short texts, stopping early above ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def same_name(ka, kb, similar=0.85):
    """True when two value keys are one name written differently: close by arabic_text.similarity, or one
    slip of the pen apart (a letter more, less or swapped - «الياسمن» for «الياسمين»)."""
    if not ka or not kb or ka == kb:
        return bool(ka) and ka == kb
    if at.similarity_keys(ka, kb) >= similar:
        return True
    a, b = ka.replace(" ", ""), kb.replace(" ", "")
    if a.isdigit() or b.isdigit():
        return False
    n = min(len(a), len(b))
    return n >= 4 and _edits(a, b, 2 if n >= 8 else 1) <= (2 if n >= 8 else 1)


def point_rect(p, r=0.0):
    return QgsRectangle(p.x() - r, p.y() - r, p.x() + r, p.y() + r)


__all__ = ["Snapshot", "value_key", "number", "split_number", "is_empty", "measurer"]
