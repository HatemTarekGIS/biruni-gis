"""Pure geometry of the Modify Features tools (no map, no layers, no Qt widgets).

Every function takes QgsGeometry objects in one planar CRS (the map's) and returns new geometries; the
inputs are never changed. Functions raise ``GeometryError`` with a sentence for the user when the operation
cannot be done (radius too big, nothing to cut …); validity of the results is checked by ``usable``.

Curves: functions that walk vertices (clean-up, align, fillet of a line …) densify curves first; the
fillet itself can return a true circular arc (``curves=True``).
"""
import math

from qgis.core import (Qgis, QgsAbstractGeometry, QgsCircularString, QgsCompoundCurve, QgsCurvePolygon,
                       QgsGeometry, QgsGeometryUtils, QgsLineString, QgsPoint, QgsPointXY, QgsWkbTypes)

from ..i18n import tr
from . import construct, fill


class GeometryError(ValueError):
    """An operation that cannot be done; the message is for the user."""


EPS = 1e-9


# ---- small helpers ------------------------------------------------------------------------------------
def _xy(p):
    return (p.x(), p.y())


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def _unit(v):
    d = math.hypot(v[0], v[1])
    return (v[0] / d, v[1] / d) if d else (0.0, 0.0)


def _line(pts):
    return QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])


def _poly(rings):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in r] for r in rings])


def keep_z(out, like):
    """``out`` with the Z and M values of ``like`` when ``out`` lost them (it was rebuilt from x, y only).
    A vertex ``like`` also has keeps its values; a new vertex gets them interpolated on the nearest segment
    of ``like`` - so surveyed heights are not written as 0."""
    if out is None or out.isNull() or out.isEmpty() or like is None or like.isNull() or like.isEmpty():
        return out
    src = like.constGet()
    hz, hm = src.is3D(), src.isMeasure()
    res = QgsGeometry(out)
    geom = res.get()
    need_z, need_m = hz and not geom.is3D(), hm and not geom.isMeasure()
    if not (need_z or need_m):
        return out
    known = {}
    for v in like.vertices():
        known.setdefault((v.x(), v.y()), v)
    if need_z:
        geom.addZValue(0)
    if need_m:
        geom.addMValue(0)
    for i, v in enumerate(list(res.vertices())):
        k = known.get((v.x(), v.y()))
        if k is None:
            k = QgsGeometryUtils.closestPoint(src, QgsPoint(v.x(), v.y()))
        ok, vid = res.vertexIdFromVertexNr(i)
        if ok:
            geom.moveVertex(vid, QgsPoint(v.x(), v.y(), k.z() if need_z else v.z(), k.m() if need_m else v.m()))
    return res


def flat(g):
    """``g`` with its curves turned into straight segments (a copy)."""
    g = QgsGeometry(g)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g = QgsGeometry(g.constGet().segmentize())
    return g


def kind(g):
    t = g.type()
    if t == Qgis.GeometryType.Point:
        return "point"
    if t == Qgis.GeometryType.Line:
        return "line"
    if t == Qgis.GeometryType.Polygon:
        return "polygon"
    return ""


def usable(g, want=None):
    """True when ``g`` can be written: not empty, valid (GEOS), with length / area, of kind ``want``."""
    if g is None or g.isNull() or g.isEmpty():
        return False
    k = kind(g)
    if want and k != want:
        return False
    if k == "line" and g.length() <= EPS:
        return False
    if k == "polygon" and g.area() <= EPS * EPS:
        return False
    try:
        return flat(g).isGeosValid()
    except Exception:  # noqa: BLE001 - a geometry GEOS cannot read is not usable
        return False


def vertex_count(g):
    return g.constGet().nCoordinates() if g is not None and not g.isNull() else 0


def parts(g):
    """The single parts of ``g`` as QgsGeometry (a copy each)."""
    if g is None or g.isEmpty():
        return []
    return [QgsGeometry(p.clone()) for p in g.constParts()]


def _structure(g):
    """(kind, [part]) where a part is [ring/line as [(x, y)]] - polygons have rings, lines one list."""
    g = flat(g)
    k = kind(g)
    if k == "line":
        lines = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        return k, [[[_xy(p) for p in ln]] for ln in lines]
    if k == "polygon":
        polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        return k, [[[_xy(p) for p in ring] for ring in poly] for poly in polys]
    raise GeometryError(tr("This tool works on lines and polygons."))


def _rebuild(k, parts_, multi):
    if k == "line":
        lines = [p[0] for p in parts_ if len(p[0]) >= 2]
        if not lines:
            return QgsGeometry()
        if multi or len(lines) > 1:
            return QgsGeometry.fromMultiPolylineXY([[QgsPointXY(*q) for q in ln] for ln in lines])
        return _line(lines[0])
    polys = [p for p in parts_ if p and len(p[0]) >= 4]
    if not polys:
        return QgsGeometry()
    polys = [[r for r in p if len(r) >= 4] for p in polys]
    if multi or len(polys) > 1:
        return QgsGeometry.fromMultiPolygonXY([[[QgsPointXY(*q) for q in r] for r in p] for p in polys])
    return _poly(polys[0])


def map_vertices(g, fn):
    """Apply ``fn(points, closed) -> points`` to every line and ring of ``g`` (densified)."""
    k, ps = _structure(g)
    out = []
    for part in ps:
        out.append([fn(list(r), k == "polygon") for r in part])
    return keep_z(_rebuild(k, out, g.isMultipart()), g)


# ---- alignment ----------------------------------------------------------------------------------------
def move(g, dx, dy):
    out = QgsGeometry(g)
    out.translate(dx, dy)
    return out


def rotate(g, angle, pivot):
    """Rotate clockwise by ``angle`` degrees around ``pivot`` (x, y)."""
    out = QgsGeometry(g)
    out.rotate(angle, QgsPointXY(*pivot))
    return out


def scale(g, factor, pivot):
    """Scale by ``factor`` (> 0) around ``pivot``."""
    if factor <= 0:
        raise GeometryError(tr("The scale factor must be more than 0."))
    from qgis.PyQt.QtGui import QTransform

    px, py = pivot
    out = QgsGeometry(g)
    out.get().transform(QTransform(factor, 0, 0, factor, px * (1 - factor), py * (1 - factor)))
    return out


def centre(g):
    c = g.centroid()
    if c.isNull() or c.isEmpty():
        c = g.pointOnSurface()
    return _xy(c.asPoint())


def transform(g, dx=0.0, dy=0.0, angle=0.0, factor=1.0, pivot=None):
    """Scale, then rotate (clockwise degrees), both around ``pivot`` (the centre when None), then move."""
    pivot = pivot if pivot is not None else centre(g)
    out = QgsGeometry(g)
    if abs(factor - 1.0) > 1e-15:
        out = scale(out, factor, pivot)
    if abs(angle) > 1e-15:
        out = rotate(out, angle, pivot)
    if dx or dy:
        out = move(out, dx, dy)
    return out


def repeat_copies(g, count, dx=0.0, dy=0.0, dangle=0.0, pivot=None):
    """``count`` copies: copy i is turned i·``dangle`` degrees (clockwise) around ``pivot`` (the feature's
    centre when None) and moved i·(dx, dy)."""
    if count < 1:
        raise GeometryError(tr("Make at least one copy."))
    if not (dx or dy or dangle):
        raise GeometryError(tr("Copies would lie on top of the original: give a distance or an angle."))
    pivot = pivot if pivot is not None else centre(g)
    return [transform(g, dx * i, dy * i, dangle * i, 1.0, pivot) for i in range(1, int(count) + 1)]


def align_edge(g, target, tolerance):
    """Snap the vertices of ``g`` that lie within ``tolerance`` of the ``target`` line(s) onto it, and add the
    target's own vertices between two snapped vertices, so the edge follows the target exactly.
    Returns (geometry, number of vertices moved)."""
    tparts = fill._parts(target)
    if not tparts:
        raise GeometryError(tr("The target must be a line or a polygon edge."))
    moved = [0]

    def nearest(p):
        pg = QgsGeometry.fromPointXY(QgsPointXY(*p))
        best = None
        for i, t in enumerate(tparts):
            d = t.distance(pg)
            if best is None or d < best[0]:
                best = (d, i)
        return best

    def fn(pts, closed):
        ring = pts[:-1] if closed and len(pts) > 1 and pts[0] == pts[-1] else pts
        snapped = []
        for p in ring:
            d, i = nearest(p)
            if d <= tolerance:
                t = tparts[i]
                q = _xy(t.nearestPoint(QgsGeometry.fromPointXY(QgsPointXY(*p))).asPoint())
                if _dist(p, q) > EPS:
                    moved[0] += 1
                snapped.append((q, i, t.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(*q)))))
            else:
                snapped.append((p, None, None))
        out = []
        n = len(snapped)
        for j in range(n):
            q, i, s = snapped[j]
            out.append(q)
            nxt = j + 1 if j + 1 < n else (0 if closed else None)
            if nxt is None:
                continue
            q2, i2, s2 = snapped[nxt]
            if i is None or i2 is None or i != i2 or abs(s2 - s) <= EPS:
                continue
            tl = tparts[i].asPolyline()
            acc, between = 0.0, []
            for k_ in range(len(tl)):
                if k_:
                    acc += tl[k_].distance(tl[k_ - 1])
                if min(s, s2) + EPS < acc < max(s, s2) - EPS:
                    between.append((acc, _xy(tl[k_])))
            between.sort(reverse=s2 < s)
            out.extend(b for _, b in between)
        clean = [out[0]] if out else []
        for p in out[1:]:
            if _dist(p, clean[-1]) > EPS:
                clean.append(p)
        if closed:
            if len(clean) > 1 and _dist(clean[0], clean[-1]) <= EPS:
                clean.pop()
            clean = clean + clean[:1]
        return clean

    res = map_vertices(g, fn)
    return res, moved[0]


# ---- construct -----------------------------------------------------------------------------------------
def copy_parallel(g, distance, side="both", join="miter"):
    """Copies of a line at ``distance`` on its "left", "right" or "both" sides; for a polygon "left" is
    outside, "right" inside (a buffer with sharp corners)."""
    if distance <= 0:
        raise GeometryError(tr("The distance must be more than 0."))
    js = Qgis.JoinStyle.Round if join == "round" else Qgis.JoinStyle.Miter
    signs = {"left": [1], "right": [-1], "both": [1, -1]}[side]
    out = []
    k = kind(g)
    for s in signs:
        if k == "line":
            for part in parts(flat(g)):
                c = part.offsetCurve(s * distance, 8, js, 4.0)
                if c is not None and not c.isEmpty():
                    out.append(c)
        elif k == "polygon":
            c = flat(g).buffer(s * distance, 8, Qgis.EndCapStyle.Flat, js, 4.0)
            if c is not None and not c.isEmpty():
                out.append(c)
        else:
            raise GeometryError(tr("This tool works on lines and polygons."))
    return out


def points_along(line, spacing=None, count=None, include_ends=True):
    """Points along ``line`` every ``spacing`` or at ``count`` equal parts (reuses Create Features)."""
    out = []
    for part in parts(flat(line)) if kind(line) == "line" else fill._parts(line):
        ds = construct.along_distances(part.length(), spacing=None if count else spacing, parts=count,
                                       include_ends=include_ends)
        out.extend(part.interpolate(d) for d in ds)
    return out


def buffer(g, distance, segments=8):
    if abs(distance) <= EPS:
        raise GeometryError(tr("The distance must not be 0."))
    return flat(g).buffer(distance, segments)


def fillet_corner(a, v, b, radius):
    """(t1, mid, t2, centre) of the arc of ``radius`` tangent to segments v→a and v→b, or raises."""
    u1, u2 = _unit(_sub(a, v)), _unit(_sub(b, v))
    cosang = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
    ang = math.acos(cosang)
    if ang < 1e-9 or math.pi - ang < 1e-9:
        raise GeometryError(tr("There is no corner here to round."))
    t = radius / math.tan(ang / 2.0)
    if t > _dist(a, v) + 1e-9 or t > _dist(b, v) + 1e-9:
        raise GeometryError(tr("The radius is too big for the segments at this corner."))
    t1 = (v[0] + u1[0] * t, v[1] + u1[1] * t)
    t2 = (v[0] + u2[0] * t, v[1] + u2[1] * t)
    bis = _unit((u1[0] + u2[0], u1[1] + u2[1]))
    dc = radius / math.sin(ang / 2.0)
    c = (v[0] + bis[0] * dc, v[1] + bis[1] * dc)
    m = (c[0] - bis[0] * radius, c[1] - bis[1] * radius)
    return t1, m, t2, c


def _arc_points(t1, m, t2, chord):
    cs = QgsCircularString(QgsPoint(*t1), QgsPoint(*m), QgsPoint(*t2))
    ls = cs.curveToLine(max(chord, 1e-9), QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference)
    return [(ls.xAt(i), ls.yAt(i)) for i in range(ls.numPoints())]


def _curve(runs):
    """A QgsCompoundCurve / QgsLineString from [("line", [pts]) | ("arc", (a, m, b))]."""
    cc = QgsCompoundCurve()
    for what, data in runs:
        if what == "line":
            if len(data) >= 2:
                cc.addCurve(QgsLineString([QgsPoint(*p) for p in data]))
        else:
            cc.addCurve(QgsCircularString(*[QgsPoint(*p) for p in data]))
    return cc


def fillet(g, vertex, radius, curves=False, chord=0.01):
    """Round the corner at ``vertex`` (index in a single line, or in the outer ring of a single polygon)
    with an arc of ``radius``. A true arc when ``curves``, else straight segments within ``chord``."""
    if radius <= 0:
        raise GeometryError(tr("The radius must be more than 0."))
    if g.isMultipart() and len(parts(g)) > 1:
        raise GeometryError(tr("Explode the multipart feature first."))
    k = kind(g)
    if k == "line":
        pts = [_xy(p) for p in flat(g).asPolyline()] if not g.isMultipart() else \
            [_xy(p) for p in flat(g).asMultiPolyline()[0]]
        if not 0 < vertex < len(pts) - 1:
            raise GeometryError(tr("Click a corner between two segments, not an end."))
        t1, m, t2, _c = fillet_corner(pts[vertex - 1], pts[vertex], pts[vertex + 1], radius)
        before = pts[:vertex] + ([t1] if _dist(t1, pts[vertex - 1]) > EPS else [])
        after = ([t2] if _dist(t2, pts[vertex + 1]) > EPS else []) + pts[vertex + 1:]
        if curves:
            runs = [("line", before), ("arc", (t1, m, t2)), ("line", after)]
            return keep_z(QgsGeometry(_curve(runs)), g)
        arc = _arc_points(t1, m, t2, chord)
        return keep_z(_line(before[:-1] + arc + after[1:]) if len(before) and len(after)
                      else _line(before + arc + after), g)
    if k == "polygon":
        poly = (flat(g).asMultiPolygon()[0] if g.isMultipart() else flat(g).asPolygon())
        ring = [_xy(p) for p in poly[0]][:-1]
        n = len(ring)
        if vertex >= n + 1 or n < 3:
            raise GeometryError(tr("Click a corner of the outer edge."))
        i = vertex % n
        a, v, b = ring[i - 1], ring[i], ring[(i + 1) % n]
        t1, m, t2, _c = fillet_corner(a, v, b, radius)
        rest = ring[i + 1:] + ring[:i]           # b … a
        path = [t2] + [p for p in rest] + [t1]
        path = [p for j, p in enumerate(path) if j == 0 or _dist(p, path[j - 1]) > EPS]
        holes = [[_xy(p) for p in r] for r in poly[1:]]
        if curves:
            cp = QgsCurvePolygon()
            cp.setExteriorRing(_curve([("line", path), ("arc", (t1, m, t2))]))
            for h in holes:
                cp.addInteriorRing(QgsLineString([QgsPoint(*p) for p in h]))
            return keep_z(QgsGeometry(cp), g)
        arc = _arc_points(t1, m, t2, chord)
        outer = path + arc[1:]
        return keep_z(_poly([outer] + holes), g)
    raise GeometryError(tr("This tool works on lines and polygons."))


def closest_vertex(g, p):
    """Index of the vertex of ``g`` nearest to (x, y), and its distance."""
    pt, at, _b, _a, d2 = g.closestVertex(QgsPointXY(*p))
    return at, math.sqrt(d2) if d2 >= 0 else float("inf")


# ---- divide -------------------------------------------------------------------------------------------
def split_by_line(g, blade):
    """Pieces of ``g`` cut by the ``blade`` line (None when the line does not cut it)."""
    pts = [QgsPointXY(p) for p in flat(blade).asPolyline()] if not blade.isMultipart() else \
        [QgsPointXY(p) for p in flat(blade).asMultiPolyline()[0]]
    if len(pts) < 2:
        raise GeometryError(tr("Draw a line with at least two points."))
    work = flat(g)
    res, new, _topo = work.splitGeometry(pts, False)
    if res != Qgis.GeometryOperationResult.Success or not new:
        return None
    return [work] + list(new)


def _line_parts(g):
    if kind(g) != "line":
        raise GeometryError(tr("This tool works on lines."))
    ps = parts(g)
    if len(ps) != 1:
        raise GeometryError(tr("Explode the multipart feature first."))
    return ps[0]


def substrings(line, cuts):
    """Pieces of a single line between the sorted distances ``cuts`` (ends excluded automatically)."""
    part = _line_parts(line)
    length = part.length()
    marks = [0.0] + sorted(d for d in set(cuts) if EPS < d < length - EPS) + [length]
    curve = part.constGet()
    out = []
    for a, b in zip(marks, marks[1:]):
        if b - a > EPS:
            out.append(QgsGeometry(curve.curveSubstring(a, b)))
    return out


def split_into_parts(line, count):
    if int(count) < 2:
        raise GeometryError(tr("Split into at least 2 parts."))
    length = _line_parts(line).length()
    return substrings(line, [length * i / int(count) for i in range(1, int(count))])


def split_every(line, distance):
    if distance <= 0:
        raise GeometryError(tr("The distance must be more than 0."))
    length = _line_parts(line).length()
    if distance >= length - EPS:
        raise GeometryError(tr("The distance is longer than the line."))
    return substrings(line, [distance * i for i in range(1, int(math.floor(length / distance)) + 1)])


def split_at_point(line, p):
    part = _line_parts(line)
    d = part.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(*p)))
    if d <= EPS or d >= part.length() - EPS:
        raise GeometryError(tr("Click inside the line, not on its end."))
    return substrings(line, [d])


def boundary_point(poly, p):
    """The point of the outline of ``poly`` nearest to (x, y)."""
    best = None
    pg = QgsGeometry.fromPointXY(QgsPointXY(*p))
    for ring in fill._parts(poly):
        q = ring.nearestPoint(pg).asPoint()
        d = _dist(p, _xy(q))
        if best is None or d < best[0]:
            best = (d, _xy(q))
    return best[1] if best else None


def two_point_cut(poly, p1, p2):
    """Cut a polygon along the straight line between two points on its outline (the clicks are moved onto
    the outline). Returns the pieces (their areas add up to the original)."""
    if kind(poly) != "polygon":
        raise GeometryError(tr("This tool works on polygons."))
    polys = parts(flat(poly))
    mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    mg_ = QgsGeometry.fromPointXY(QgsPointXY(*mid))
    k = min(range(len(polys)), key=lambda i: polys[i].distance(mg_))
    rings = [[_xy(p) for p in r] for r in polys[k].asPolygon()]
    outer = rings[0][:-1]

    def insert(ring, q):
        best = None
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            d = _line([a, b]).distance(QgsGeometry.fromPointXY(QgsPointXY(*q)))
            if best is None or d < best[0]:
                best = (d, i)
        for j, v in enumerate(ring):
            if _dist(v, q) <= EPS * 10:
                return best[0], j
        ring.insert(best[1] + 1, q)
        return best[0], best[1] + 1

    snap = []
    for p in (p1, p2):
        q = boundary_point(QgsGeometry.fromPolygonXY([[QgsPointXY(*v) for v in outer + outer[:1]]]), p)
        snap.append(q)
    q1, q2 = snap
    if q1 is None or q2 is None or _dist(q1, q2) <= EPS:
        raise GeometryError(tr("Click two different points on the polygon's edge."))
    insert(outer, q1)
    insert(outer, q2)
    i1 = min(range(len(outer)), key=lambda i: _dist(outer[i], q1))
    i2 = min(range(len(outer)), key=lambda i: _dist(outer[i], q2))
    n = len(outer)
    path1 = [outer[(i1 + j) % n] for j in range((i2 - i1) % n + 1)]
    path2 = [outer[(i2 + j) % n] for j in range((i1 - i2) % n + 1)]
    if len(path1) < 3 or len(path2) < 3:
        raise GeometryError(tr("The two points are on the same side: nothing to cut."))
    pieces = [_poly([path1 + path1[:1]]), _poly([path2 + path2[:1]])]
    for hole in rings[1:]:
        hg = _poly([hole])
        pieces = [single_if_possible(pc.difference(hg)) for pc in pieces]
    if not all(usable(pc, "polygon") for pc in pieces) or \
            abs(sum(pc.area() for pc in pieces) - polys[k].area()) > 1e-6 * max(1.0, polys[k].area()):
        raise GeometryError(tr("The straight line between the two points leaves the polygon."))
    others = [pp for i, pp in enumerate(polys) if i != k]
    if others:   # a multipart polygon: the other parts stay with the first piece
        pieces[0] = QgsGeometry.unaryUnion([pieces[0]] + others)
    return [keep_z(pc, poly) for pc in pieces]


def planarize(lines):
    """Split every line at its crossings with the others: [(index of the source line, piece)]."""
    flats = [flat(g) for g in lines]
    out = []
    for i, g in enumerate(flats):
        if kind(g) != "line":
            continue
        for part in parts(g):
            cuts = []
            for j, h in enumerate(flats):
                if j == i or not part.boundingBox().intersects(h.boundingBox()):
                    continue
                x = part.intersection(h)
                if x.isEmpty():
                    continue
                for v in x.vertices():
                    cuts.append(part.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(v.x(), v.y()))))
            for piece in substrings(part, cuts):
                out.append((i, piece))
    return out


def explode(g):
    ps = parts(g)
    if len(ps) < 2:
        raise GeometryError(tr("The feature has only one part."))
    return ps


# ---- reshape ------------------------------------------------------------------------------------------
def reshape(g, sketch):
    """``g`` reshaped by a line that starts and ends on it (None when the line does not reshape it)."""
    pl = flat(sketch).asPolyline()
    if len(pl) < 2:
        raise GeometryError(tr("Draw a line with at least two points."))
    out = flat(g)
    res = out.reshapeGeometry(QgsLineString([QgsPoint(p.x(), p.y()) for p in pl]))
    if res != Qgis.GeometryOperationResult.Success:
        return None
    return out


def _ray_hit(origin, direction, target):
    """Nearest forward intersection (distance, point) of a ray with the segments of ``target``."""
    best = None
    ox, oy = origin
    dx, dy = direction
    for t in fill._parts(target):
        pl = t.asPolyline()
        for a, b in zip(pl, pl[1:]):
            ex, ey = b.x() - a.x(), b.y() - a.y()
            den = dx * ey - dy * ex
            if abs(den) < 1e-15:
                continue
            wx, wy = a.x() - ox, a.y() - oy
            s = (wx * ey - wy * ex) / den        # along the ray
            u = (wx * dy - wy * dx) / den        # along the segment
            if s > 1e-9 and -1e-12 <= u <= 1 + 1e-12 and (best is None or s < best[0]):
                best = (s, (ox + dx * s, oy + dy * s))
    return best


def extend_line(line, target, end="end"):
    """Extend the ``end`` ("start" / "end") of a single line straight on until it meets ``target``."""
    part = _line_parts(flat(line))
    pts = [_xy(p) for p in part.asPolyline()]
    if end == "start":
        pts.reverse()
    direction = _unit(_sub(pts[-1], pts[-2]))
    hit = _ray_hit(pts[-1], direction, target)
    if hit is None:
        raise GeometryError(tr("The line does not reach the target when extended."))
    pts.append(hit[1])
    if end == "start":
        pts.reverse()
    return keep_z(_line(pts), line)


def nearest_end(line, p):
    pl = _line_parts(flat(line)).asPolyline()
    return "start" if _dist(_xy(pl[0]), p) <= _dist(_xy(pl[-1]), p) else "end"


def trim_line(line, target, p):
    """Cut a single line where it crosses ``target`` and drop the piece nearest to the click ``p``.
    Returns the remaining pieces."""
    part = _line_parts(flat(line))
    x = part.intersection(flat(target))
    cuts = [part.lineLocatePoint(QgsGeometry.fromPointXY(QgsPointXY(v.x(), v.y()))) for v in x.vertices()] \
        if not x.isEmpty() else []
    pieces = substrings(part, cuts)
    if len(pieces) < 2:
        raise GeometryError(tr("The line does not cross the target."))
    pg = QgsGeometry.fromPointXY(QgsPointXY(*p))
    drop = min(range(len(pieces)), key=lambda i: pieces[i].distance(pg))
    return [pc for i, pc in enumerate(pieces) if i != drop]


def vertex_rows(g):
    """[(flat vertex index, x, y)] of ``g`` without the repeated closing vertex of rings."""
    out = []
    geom = g.constGet()
    for i, v in enumerate(g.vertices()):
        ok, vid = g.vertexIdFromVertexNr(i)
        if ok and kind(g) == "polygon":
            ring_n = geom.vertexCount(vid.part, vid.ring)
            if vid.vertex == ring_n - 1:
                continue
        out.append((i, v.x(), v.y()))
    return out


def move_vertices(g, moves):
    """``g`` with vertices moved: ``moves`` = {flat index: (x, y)} (ring ends follow their start)."""
    out = QgsGeometry(g)
    for i, (x, y) in moves.items():
        if not out.moveVertex(x, y, i):
            raise GeometryError(tr("Vertex {n} cannot be moved.").format(n=i + 1))
    return out


def generalize(g, tolerance):
    """Douglas-Peucker simplification."""
    if tolerance <= 0:
        raise GeometryError(tr("The tolerance must be more than 0."))
    return flat(g).simplify(tolerance)


def smooth(g, iterations=1, offset=0.25):
    return flat(g).smooth(max(1, int(iterations)), max(0.01, min(0.5, offset)))


def shared_edge_reshape(a, b, sketch, tolerance=1e-6):
    """Move the boundary shared by polygons ``a`` and ``b`` along ``sketch`` (which starts and ends on the
    shared boundary): ``a`` is reshaped and ``b`` gets exactly what is left of the pair, so the two keep
    touching without a gap or an overlap. Returns (new a, new b)."""
    na = reshape(a, sketch)
    if na is None or not usable(na, "polygon"):
        raise GeometryError(tr("The line must start and end on the shared edge."))
    union = QgsGeometry.unaryUnion([flat(a), flat(b)])
    outside = na.difference(union)
    if not outside.isEmpty() and outside.area() > max(tolerance, 1e-9) * max(1.0, na.length()):
        raise GeometryError(tr("The new edge leaves the two polygons; draw it between them."))
    na = na.intersection(union)
    nb = union.difference(na)
    na, nb = single_if_possible(na), single_if_possible(nb)
    if not usable(na, "polygon") or not usable(nb, "polygon"):
        raise GeometryError(tr("The result would not be a valid polygon."))
    return na, nb


def single_if_possible(g):
    """A multi-geometry of one part as a single geometry; polygon collections reduced to their polygons."""
    if g is None or g.isEmpty():
        return g
    if g.type() == Qgis.GeometryType.Unknown or QgsWkbTypes.flatType(g.wkbType()) == Qgis.WkbType.GeometryCollection:
        polys = [p for p in parts(g) if kind(p) == "polygon"]
        g = QgsGeometry.unaryUnion(polys) if polys else QgsGeometry()
    ps = parts(g)
    if g.isMultipart() and len(ps) == 1:
        return ps[0]
    return g


# ---- combine ------------------------------------------------------------------------------------------
def merge_geometries(geoms):
    u = QgsGeometry.unaryUnion([flat(g) for g in geoms])
    if u.isEmpty():
        raise GeometryError(tr("Nothing to merge."))
    if kind(u) == "line":
        m = u.mergeLines()
        if m is not None and not m.isEmpty():
            u = m
    return single_if_possible(u)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def merge_attributes(rows, main=0, rules=None, sizes=None, sep=", "):
    """Attribute values of a merged feature. ``rows`` = [{field: value}] of the merged features, ``main`` =
    index of the feature whose values win, ``rules`` = {field: "main"|"first"|"largest"|"sum"|"mean"|"min"|
    "max"|"concat"|"empty"}, ``sizes`` = the features' lengths / areas (for "largest")."""
    rules = rules or {}
    fields = list(rows[main].keys()) if rows else []
    out = {}
    for f in fields:
        rule = rules.get(f, "main")
        vals = [r.get(f) for r in rows]
        present = [v for v in vals if v is not None and v != ""]
        nums = [n for n in (_num(v) for v in present) if n is not None]
        if rule == "first":
            out[f] = present[0] if present else None
        elif rule == "largest" and sizes:
            out[f] = vals[max(range(len(rows)), key=lambda i: sizes[i])]
        elif rule == "sum":
            out[f] = sum(nums) if nums else None
        elif rule == "mean":
            out[f] = sum(nums) / len(nums) if nums else None
        elif rule == "min":
            out[f] = min(nums) if nums else None
        elif rule == "max":
            out[f] = max(nums) if nums else None
        elif rule == "concat":
            seen = []
            for v in present:
                if str(v) not in seen:
                    seen.append(str(v))
            out[f] = sep.join(seen) if seen else None
        elif rule == "empty":
            out[f] = None
        else:
            out[f] = rows[main].get(f)
    return out


def clip(g, cutter, keep_inside=True):
    """The part of ``g`` inside (or, with keep_inside False, outside) the ``cutter`` polygon."""
    res = flat(g).intersection(flat(cutter)) if keep_inside else flat(g).difference(flat(cutter))
    res = single_if_possible(res)
    if res is None or res.isEmpty() or kind(res) != kind(g):
        return None
    return res


def gap_pieces(polys, max_width):
    """Gaps (holes and slivers) between polygons up to ``max_width`` wide: [(piece, [shared length with
    each polygon])]. Only gaps touching at least two of the polygons are returned."""
    if max_width <= 0:
        raise GeometryError(tr("The width must be more than 0."))
    polys = [flat(p) for p in polys]
    u = QgsGeometry.unaryUnion(polys)
    r = max_width / 2.0
    closed = u.buffer(r, 8, Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Miter, 10.0).buffer(
        -r, 8, Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Miter, 10.0)
    gaps = closed.difference(u)
    eps = max_width * 0.05
    out = []
    for piece in parts(gaps):
        if kind(piece) != "polygon" or piece.area() <= (max_width * 1e-3) ** 2:
            continue
        edge = QgsGeometry.unaryUnion(fill._parts(piece))
        shared = []
        for p in polys:
            if not piece.boundingBox().buffered(eps).intersects(p.boundingBox()):
                shared.append(0.0)
                continue
            x = edge.intersection(p.buffer(eps, 4))
            shared.append(x.length() if not x.isEmpty() else 0.0)
        if sum(1 for s in shared if s > eps * 2) >= 2:
            out.append((piece, shared))
    return out


def gap_fill(polys, max_width):
    """{index of polygon: new geometry} - every gap goes to the neighbour with the longest shared edge."""
    given = {}
    for piece, shared in gap_pieces(polys, max_width):
        i = max(range(len(shared)), key=lambda k: shared[k])
        given.setdefault(i, []).append(piece)
    out = {}
    for i, pieces in given.items():
        g = QgsGeometry.unaryUnion([flat(polys[i])] + pieces)
        out[i] = single_if_possible(g)
    return out


# ---- clean up -----------------------------------------------------------------------------------------
def _angle_at(a, v, b):
    u1, u2 = _unit(_sub(a, v)), _unit(_sub(b, v))
    if u1 == (0.0, 0.0) or u2 == (0.0, 0.0):
        return 180.0
    return math.degrees(math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))))


def remove_spikes(g, min_angle):
    """Remove the vertices where the outline turns back sharper than ``min_angle`` degrees. Returns
    (geometry, vertices removed)."""
    removed = [0]

    def fn(pts, closed):
        ring = pts[:-1] if closed else pts
        changed = True
        while changed and len(ring) > (3 if closed else 2):
            changed = False
            n = len(ring)
            idx = range(n) if closed else range(1, n - 1)
            for i in idx:
                a, v, b = ring[i - 1], ring[i], ring[(i + 1) % n]
                if _angle_at(a, v, b) < min_angle:
                    del ring[i]
                    removed[0] += 1
                    changed = True
                    break
        return ring + ring[:1] if closed else ring

    return map_vertices(g, fn), removed[0]


def remove_duplicate_vertices(g, tolerance):
    """Drop vertices closer than ``tolerance`` to the previous one (duplicates and tiny segments).
    Returns (geometry, vertices removed)."""
    removed = [0]

    def fn(pts, closed):
        ring = pts[:-1] if closed else pts
        keep = [ring[0]]
        for p in ring[1:]:
            if _dist(p, keep[-1]) <= tolerance:
                removed[0] += 1
                continue
            keep.append(p)
        if not closed and _dist(keep[-1], ring[-1]) > EPS:
            keep[-1] = ring[-1]        # a line keeps its own end
        if closed:
            while len(keep) > 3 and _dist(keep[-1], keep[0]) <= tolerance:
                keep.pop()
                removed[0] += 1
            return keep + keep[:1]
        return keep

    return map_vertices(g, fn), removed[0]


def close_line_gaps(lines, tolerance):
    """Snap the dangling ends of ``lines`` within ``tolerance``: onto another end, onto another line
    (undershoot), or back to the crossing they overshoot. Returns {index: new geometry}."""
    work = [[_xy(p) for p in flat(g).asPolyline()] if kind(g) == "line" and not g.isMultipart() else None
            for g in lines]
    geoms = [_line(p) if p and len(p) >= 2 else None for p in work]

    def degree(pt, me):
        n = 0
        for j, p in enumerate(work):
            if p is None:
                continue
            for e in (p[0], p[-1]):
                if _dist(e, pt) <= EPS and not (j == me and e is pt):
                    n += 1
        return n

    changed = set()
    for i, pts in enumerate(work):
        if pts is None or len(pts) < 2:
            continue
        for end in (0, -1):
            p = pts[end]
            if degree(p, i) > 1:
                continue
            best = None
            # 1. another end
            for j, q in enumerate(work):
                if q is None or j == i:
                    continue
                for e in (q[0], q[-1]):
                    d = _dist(p, e)
                    if EPS < d <= tolerance and (best is None or d < best[0]):
                        best = (d, e)
            if best is None:
                pg = QgsGeometry.fromPointXY(QgsPointXY(*p))
                # 2. overshoot: the last segment crosses another line within tolerance of its end
                nb = pts[1] if end == 0 else pts[-2]
                seg = _line([nb, p])
                for j, q in enumerate(geoms):
                    if q is None or j == i:
                        continue
                    x = seg.intersection(q)
                    if x.isEmpty():
                        continue
                    for v in x.vertices():
                        c = (v.x(), v.y())
                        d = _dist(c, p)
                        if EPS < d <= tolerance and (best is None or d < best[0]):
                            best = (d, c)
                # 3. undershoot: nearest point of another line
                if best is None:
                    for j, q in enumerate(geoms):
                        if q is None or j == i:
                            continue
                        d = q.distance(pg)
                        if EPS < d <= tolerance and (best is None or d < best[0]):
                            best = (d, _xy(q.nearestPoint(pg).asPoint()))
            if best is not None:
                pts[end] = best[1]
                geoms[i] = _line(pts)
                changed.add(i)
    return {i: keep_z(geoms[i], lines[i]) for i in changed}


# ---- attributes ---------------------------------------------------------------------------------------
def transfer_values(source, target_fields, mapping=None):
    """{target field: value} copied from the ``source`` row: by ``mapping`` {target: source} or, without one,
    every target field whose name matches a source field (letter case ignored)."""
    if mapping is None:
        low = {k.lower(): k for k in source}
        mapping = {t: low[t.lower()] for t in target_fields if t.lower() in low}
    return {t: source.get(s) for t, s in mapping.items() if t in target_fields and s in source}
