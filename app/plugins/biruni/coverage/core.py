"""The parcel coverage doctor: a polygon layer that should tile an area (parcels, blocks, zones) checked and
cleaned as one fabric.

Found (``examine``):
- gaps: holes between the polygons narrower than the gap width (a courtyard or a lake wider than it is no gap);
- overlaps: the area two polygons both cover;
- edges: the boundary pieces that do not match their neighbour's vertex for vertex (a shared side drawn twice,
  a few millimetres apart): the cause of the slivers that come back after every edit;
- slivers: polygons narrower everywhere than the sliver width;
- invalid: shapes that are not valid polygons (they are left out of the rest, fix them first).

Fixed (``clean``): every shared side made one and the same, the overlaps given to one neighbour, the narrow gaps
filled - with GEOS's coverage cleaner (Shapely 2.2). ``simplify`` takes vertices out while the neighbours keep
sharing their sides exactly. Both write into the layer's edits as one command: one Undo takes them back, nothing
is saved (the save, and the spatial rules that check it, stay the user's).

The polygon with an arc is measured on its arc's points; it is never rewritten (its arc would become segments):
the cleaner leaves it and says so. The older "Find overlaps and gaps" (algorithms/overlaps_gaps.py) lists
overlaps and holes by area; this one goes by width, finds the edges, and mends.
"""
from qgis.core import (QgsFeatureRequest, QgsGeometry, QgsUnitTypes, QgsVectorLayer, QgsWkbTypes)

from ..i18n import tr


class CoverageError(Exception):
    pass


def _shapely():
    import shapely

    if not hasattr(shapely, "coverage_clean"):
        raise CoverageError("shapely")
    return shapely


def metres_per_unit(layer):
    """How many metres one unit of the layer's coordinates is (about, for degrees)."""
    crs = layer.crs()
    if crs.isGeographic():
        return 111320.0
    f = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), QgsUnitTypes.DistanceUnit.DistanceMeters)
    return f if f and f > 0 else 1.0


class Problem:
    def __init__(self, kind, geom, fids, size=0.0, width=None):
        self.kind = kind          # gap, overlap, edge, sliver, invalid
        self.geom = geom          # shapely geometry, in the layer's coordinates
        self.fids = list(fids)
        self.size = size          # m² (gap, overlap, sliver, invalid) or m (edge)
        self.width = width        # m, for gaps and slivers

    def text(self):
        ids = ", ".join(str(f) for f in self.fids)
        if self.kind == "gap":
            return tr("A gap {w} m wide ({a} m²) between parcels {f}: filled by «Clean».").format(
                w=_r(self.width), a=_r(self.size), f=ids)
        if self.kind == "overlap":
            return tr("Parcels {f} overlap by {a} m²: «Clean» gives it to one of them.").format(f=ids, a=_r(self.size))
        if self.kind == "edge":
            return tr("{l} m of the boundary of parcel {f} do not match the neighbour's vertices: «Clean» makes the "
                      "shared side one.").format(l=_r(self.size), f=ids)
        if self.kind == "sliver":
            return tr("Parcel {f} is a sliver, {w} m wide at most ({a} m²): merge it with a neighbour or delete it.").format(
                f=ids, w=_r(self.width), a=_r(self.size))
        return tr("Parcel {f} is not a valid shape: fix it first (it is left out of the check and the cleaning).").format(f=ids)


def _r(v):
    if v is None:
        return "?"
    return f"{v:.3f}" if abs(v) < 1 else f"{v:.2f}" if abs(v) < 100 else f"{v:,.0f}"


class Report:
    def __init__(self):
        self.problems = []
        self.features = 0
        self.curved = []          # fids of polygons with arcs (checked on their points, never rewritten)
        self.unit = 1.0

    def count(self, kind):
        return sum(1 for p in self.problems if p.kind == kind)

    @property
    def clean(self):
        return not self.problems


def _load(layer, only=None):
    """(fids, shapely array, curved fids, invalid fids) of the polygons of the layer (or of ``only``)."""
    import numpy as np

    sh = _shapely()
    if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != QgsWkbTypes.GeometryType.PolygonGeometry:
        raise CoverageError("not polygons")
    req = QgsFeatureRequest().setNoAttributes()
    if only is not None:
        req.setFilterFids(list(only))
    fids, geoms, curved, invalid = [], [], [], []
    for f in layer.getFeatures(req):
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        if QgsWkbTypes.isCurvedType(g.wkbType()):    # a curved type holds straight polygons too (GeoPackage)
            if g.constGet().hasCurvedSegments():
                curved.append(f.id())
            g = QgsGeometry(g.constGet().segmentize())
        s = sh.from_wkb(bytes(g.asWkb()))
        if s is None or s.is_empty or s.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        if not s.is_valid:
            invalid.append((f.id(), s))
            continue
        fids.append(f.id())
        geoms.append(s)
    return fids, np.array(geoms, dtype=object), curved, invalid


def _polys(g):
    sh = _shapely()
    return [p for p in sh.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon") and not p.is_empty]


def _width(sh, poly):
    """Twice the radius of the largest circle inside: how wide a gap or a sliver is."""
    try:
        return 2.0 * sh.maximum_inscribed_circle(poly).length
    except Exception:  # noqa: BLE001 - a degenerate shape
        return 0.0


def examine(layer, gap_width=0.5, sliver_width=0.2, only=None):
    """The problems of the layer's coverage (widths in metres). Returns a Report."""
    sh = _shapely()
    rep = Report()
    u = metres_per_unit(layer)
    rep.unit = u
    fids, arr, curved, invalid = _load(layer, only)
    rep.features = len(fids) + len(invalid)
    rep.curved = curved
    for fid, s in invalid:
        rep.problems.append(Problem("invalid", s, [fid], abs(s.area) * u * u))
    if not len(arr):
        return rep
    gw, sw = gap_width / u, sliver_width / u
    tiny = 1e-6 / (u * u)        # a square millimetre
    for fid, e in zip(fids, sh.coverage_invalid_edges(arr, gap_width=gw)):
        if e is not None and not e.is_empty:
            rep.problems.append(Problem("edge", e, [fid], e.length * u))
    tree = sh.STRtree(arr)
    a_i, b_i = tree.query(arr, predicate="intersects")
    for i, j in zip(a_i, b_i):
        if i >= j:
            continue
        inter = sh.intersection(arr[i], arr[j])
        parts = _polys(inter)
        area = sum(p.area for p in parts)
        if area > tiny:
            rep.problems.append(Problem("overlap", sh.union_all(parts), [fids[i], fids[j]], area * u * u))
    union = sh.union_all(arr)
    for poly in _polys(union):
        for ring in poly.interiors:
            hole = sh.Polygon(ring)
            if hole.area <= tiny:
                continue
            w = _width(sh, hole)
            if w <= gw:
                near = [fids[k] for k in tree.query(hole, predicate="touches")] or \
                    [fids[k] for k in tree.query(hole.buffer(gw), predicate="intersects")]
                rep.problems.append(Problem("gap", hole, sorted(set(near)), hole.area * u * u, w * u))
    slivers = set()
    for fid, s in zip(fids, arr):
        w = _width(sh, s)
        if w < sw:
            rep.problems.append(Problem("sliver", s, [fid], s.area * u * u, w * u))
            slivers.add(fid)
    # a sliver's own sides are all near another side: the sliver says it once
    rep.problems = [p for p in rep.problems if not (p.kind == "edge" and p.fids[0] in slivers)]
    return rep


def _qgs(s):
    g = QgsGeometry()
    g.fromWkb(bytes(_shapely().to_wkb(s)))
    return g


def _to_layer_geom(layer, s):
    """A shapely polygon as a geometry the layer takes (multi or single, Z/M kept as zero), or None."""
    g = _qgs(s)
    if g.isNull() or g.isEmpty():
        return None
    t = layer.wkbType()
    if QgsWkbTypes.isMultiType(t):
        g.convertToMultiType()
    elif g.isMultipart():
        if g.constGet().numGeometries() != 1:
            return None
        g.convertToSingleType()
    if QgsWkbTypes.isCurvedType(t):
        g = QgsGeometry(g.constGet().toCurveType())
    a = g.get()
    if QgsWkbTypes.hasZ(t) and not QgsWkbTypes.hasZ(a.wkbType()):
        a.addZValue(0)
    if QgsWkbTypes.hasM(t) and not QgsWkbTypes.hasM(a.wkbType()):
        a.addMValue(0)
    return g


class Result:
    def __init__(self):
        self.changed = 0
        self.curved = 0            # polygons with arcs left as they are
        self.invalid = 0           # invalid polygons left out
        self.split = 0             # polygons the cleaning would cut in two pieces, in a single-part layer: left
        self.emptied = 0           # polygons the cleaning would leave with nothing: left


def _write(layer, fids, before, after, curved, note):
    sh = _shapely()
    res = Result()
    res.curved = len(curved)
    changes = {}
    for fid, old, new in zip(fids, before, after):
        if fid in curved:
            continue
        if new is None or new.is_empty:
            res.emptied += 1
            continue
        if sh.equals_exact(sh.normalize(old), sh.normalize(new), 0.0):
            continue
        g = _to_layer_geom(layer, new)
        if g is None:
            res.split += 1
            continue
        changes[fid] = g
    if not changes:
        return res
    if not layer.isEditable() and not layer.startEditing():
        raise CoverageError("not editable")
    layer.beginEditCommand(note)
    try:
        for fid, g in changes.items():
            layer.changeGeometry(fid, g)
    except Exception:
        layer.destroyEditCommand()
        raise
    layer.endEditCommand()
    layer.triggerRepaint()
    res.changed = len(changes)
    return res


def clean(layer, gap_width=0.5, snapping=None, only=None, merge="longest_border"):
    """Make the layer a clean coverage, in its edits (widths in metres; ``snapping`` None = chosen from the extent,
    0 = no snapping). Returns a Result."""
    sh = _shapely()
    u = metres_per_unit(layer)
    fids, arr, curved, invalid = _load(layer, only)
    if not len(arr):
        r = Result()
        r.invalid = len(invalid)
        return r
    out = sh.coverage_clean(arr, gap_width=gap_width / u,
                            snapping_distance=-1 if snapping is None else snapping / u, merge_strategy=merge)
    r = _write(layer, fids, arr, out, set(curved), tr("Coverage cleaned"))
    r.invalid = len(invalid)
    return r


def simplify(layer, tolerance, keep_outline=False, only=None):
    """Fewer vertices, the shared sides still shared exactly (``tolerance`` in metres; ``keep_outline`` leaves the
    outer boundary of the fabric as it is). The layer should be clean first. Returns a Result."""
    sh = _shapely()
    u = metres_per_unit(layer)
    fids, arr, curved, invalid = _load(layer, only)
    if not len(arr):
        return Result()
    out = sh.coverage_simplify(arr, tolerance / u, simplify_boundary=not keep_outline)
    r = _write(layer, fids, arr, out, set(curved), tr("Coverage simplified"))
    r.invalid = len(invalid)
    return r


def problem_layers(report, layer):
    """Memory layers of the problems: (polygons: gaps, overlaps, slivers, invalid; lines: edges), each feature
    with its kind, the parcels, its size and the explanation."""
    from qgis.core import QgsFeature, QgsField, QgsVectorLayer
    from qgis.PyQt.QtCore import QMetaType

    crs = layer.crs().authid() or layer.crs().toWkt()
    out = []
    for geom_type, kinds, title in (("MultiPolygon", ("gap", "overlap", "sliver", "invalid"), tr("Coverage problems: {l}")),
                                    ("MultiLineString", ("edge",), tr("Unmatched edges: {l}"))):
        probs = [p for p in report.problems if p.kind in kinds]
        if not probs:
            continue
        ml = QgsVectorLayer(f"{geom_type}?crs={crs}", title.format(l=layer.name()), "memory")
        pr = ml.dataProvider()
        pr.addAttributes([QgsField("kind", QMetaType.Type.QString), QgsField("parcels", QMetaType.Type.QString),
                          QgsField("size", QMetaType.Type.Double), QgsField("note", QMetaType.Type.QString)])
        ml.updateFields()
        feats = []
        for p in probs:
            f = QgsFeature(ml.fields())
            g = _qgs(p.geom)
            g.convertToMultiType()
            f.setGeometry(g)
            f.setAttributes([p.kind, ",".join(str(x) for x in p.fids), round(p.size, 4), p.text()])
            feats.append(f)
        pr.addFeatures(feats)
        out.append(ml)
    return out
