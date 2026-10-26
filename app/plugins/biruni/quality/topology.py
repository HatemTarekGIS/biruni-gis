"""Topology checks on plain {feature id: QgsGeometry} dicts.

Shared by the "Topology Rules" tool and the project health scan. Every
function only reads geometries and returns what it found; nothing is edited.
"""
from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsPolygon, QgsRectangle, QgsSpatialIndex

from ..algorithms.base import polygonal

EPS = 1e-9


def collect(features, feedback=None, start=0, span=100, total=0):
    """(geometries by id, spatial index) of the non-empty geometries in ``features``."""
    geoms = {}
    index = QgsSpatialIndex()
    for i, f in enumerate(features):
        if feedback is not None:
            if feedback.isCanceled():
                break
            if total and i % 500 == 0:
                feedback.setProgress(start + span * i / total)
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        geoms[f.id()] = g
        index.addFeature(f.id(), g.boundingBox())
    return geoms, index


def _near(p, tol):
    t = max(tol, EPS)
    return QgsRectangle(p.x() - t, p.y() - t, p.x() + t, p.y() + t)


def _cancelled(feedback):
    return feedback is not None and feedback.isCanceled()


# ---- polygons --------------------------------------------------------------
def find_overlaps(geoms, index, min_area=0.0, feedback=None, max_checks=None, relative=0.0):
    """[(fid_a, fid_b, overlap polygon)] for every pair of polygons whose shared
    area is at least ``min_area`` (and ``relative`` x the smaller polygon)."""
    out = []
    checks = 0
    valid = {}
    for n, (fid, g) in enumerate(geoms.items()):
        if _cancelled(feedback) or (max_checks is not None and checks >= max_checks):
            break
        g = valid.setdefault(fid, g if g.isGeosValid() else g.makeValid())
        engine = QgsGeometry.createGeometryEngine(g.constGet())
        engine.prepareGeometry()
        for other in index.intersects(g.boundingBox()):
            if other <= fid or other not in geoms:
                continue
            checks += 1
            og = valid.setdefault(other, geoms[other] if geoms[other].isGeosValid() else geoms[other].makeValid())
            if not engine.intersects(og.constGet()) or engine.touches(og.constGet()):
                continue
            inter = polygonal(g.intersection(og))
            if inter is None:
                continue
            area = inter.area()
            if area < max(min_area, EPS) or (relative and area < relative * min(g.area(), og.area())):
                continue
            out.append((fid, other, inter))
    return out


def find_gaps(geoms, min_area=0.0, max_gap=0.0):
    """Holes enclosed by the union of the polygons (each a QgsGeometry)."""
    if not geoms:
        return []
    valid = [g if g.isGeosValid() else g.makeValid() for g in geoms.values()]
    union = QgsGeometry.unaryUnion(valid)
    parts = union.asGeometryCollection() if union.isMultipart() else [union]
    out = []
    for part in parts:
        poly = part.constGet()
        if poly is None or not hasattr(poly, "numInteriorRings"):
            continue
        for r in range(poly.numInteriorRings()):
            hole = QgsPolygon()
            hole.setExteriorRing(poly.interiorRing(r).clone())
            hg = QgsGeometry(hole)
            area = hg.area()
            if area < max(min_area, EPS) or (max_gap > 0 and area > max_gap):
                continue
            out.append(hg)
    return out


def invalid_details(geom):
    """[(QgsPointXY or None, message)] - empty when the geometry is valid."""
    if geom.isGeosValid():
        return []
    errs = geom.validateGeometry(Qgis.GeometryValidationEngine.Geos)
    out = [(e.where() if e.hasWhere() else None, e.what()) for e in errs]
    return out or [(None, "invalid geometry")]


def uncovered(polys, cover, cover_index, min_area=0.0, feedback=None):
    """[(fid, uncovered part)] of polygons not fully covered by ``cover`` polygons."""
    out = []
    for fid, g in polys.items():
        if _cancelled(feedback):
            break
        g = g if g.isGeosValid() else g.makeValid()
        cands = [cover[c] for c in cover_index.intersects(g.boundingBox()) if c in cover]
        rest = g
        if cands:
            cu = QgsGeometry.unaryUnion([c if c.isGeosValid() else c.makeValid() for c in cands])
            rest = g.difference(cu)
        rest = polygonal(rest)
        if rest is not None and rest.area() >= max(min_area, EPS):
            out.append((fid, rest))
    return out


# ---- lines -----------------------------------------------------------------
def _line_parts(g):
    return g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]


def find_dangles(geoms, index, tol=0.0, feedback=None):
    """[(fid, QgsPointXY)]: line ends that touch no other line (within ``tol``)."""
    out = []
    t = max(tol, EPS)
    for fid, g in geoms.items():
        if _cancelled(feedback):
            break
        parts = _line_parts(g)
        for pi, pts in enumerate(parts):
            if len(pts) < 2:
                continue
            if pts[0].distance(pts[-1]) <= t:  # closed ring
                continue
            for end, rest in ((pts[0], pts[1:]), (pts[-1], pts[:-1])):
                pg = QgsGeometry.fromPointXY(end)
                connected = False
                for other in index.intersects(_near(end, t)):
                    if other != fid and other in geoms and geoms[other].distance(pg) <= t:
                        connected = True
                        break
                if not connected:
                    # another part of the same feature, or the line itself away from this end
                    own = [QgsGeometry.fromPolylineXY(p) for k, p in enumerate(parts) if k != pi and len(p) > 1]
                    if len(rest) > 1:
                        own.append(QgsGeometry.fromPolylineXY(rest))
                    connected = any(o.distance(pg) <= t for o in own)
                if not connected:
                    out.append((fid, QgsPointXY(end)))
    return out


def find_line_overlaps(geoms, index, tol=0.0, feedback=None):
    """[(fid_a, fid_b, shared line)] where two lines run along each other."""
    out = []
    for fid, g in geoms.items():
        if _cancelled(feedback):
            break
        for other in index.intersects(g.boundingBox()):
            if other <= fid or other not in geoms:
                continue
            og = geoms[other]
            if not g.intersects(og):
                continue
            inter = g.intersection(og)
            parts = inter.asGeometryCollection() if inter.isMultipart() or inter.type() != Qgis.GeometryType.Line \
                else [inter]
            lines = [p for p in parts if p.type() == Qgis.GeometryType.Line and p.length() > max(tol, EPS)]
            if lines:
                shared = QgsGeometry.collectGeometry(lines) if len(lines) > 1 else lines[0]
                out.append((fid, other, shared))
    return out


def _key(p, nd=7):
    return (round(p.x(), nd), round(p.y(), nd))


def self_intersections(geom):
    """Points where a line (or the parts of a multi-line) crosses or touches itself."""
    if geom.isEmpty() or geom.isSimple():
        return []
    ends = {}
    for pts in _line_parts(geom):
        if len(pts) > 1:
            for p in (pts[0], pts[-1]):
                ends[_key(p)] = ends.get(_key(p), 0) + 1
    noded = QgsGeometry.unaryUnion([geom])
    counts = {}
    for pts in _line_parts(noded):
        if len(pts) > 1:
            for p in (pts[0], pts[-1]):
                k = _key(p)
                counts[k] = counts.get(k, 0) + 1
    out = []
    for k, c in counts.items():
        original = ends.get(k, 0)
        # an original end shows up once (twice for a closed ring); a crossing
        # splits the line into pieces meeting there
        if c - original >= 2 or (original == 1 and c >= 3):
            out.append(QgsPointXY(*k))
    return out


# ---- points ----------------------------------------------------------------
def points_outside(points, polys, poly_index, feedback=None):
    """Ids of points that are not inside (or on the edge of) any polygon."""
    out = []
    for fid, g in points.items():
        if _cancelled(feedback):
            break
        p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
        inside = any(polys[c].intersects(g) for c in poly_index.intersects(_near(p, 0)) if c in polys)
        if not inside:
            out.append(fid)
    return out


def points_off_lines(points, lines, line_index, tol=0.0, feedback=None):
    """[(fid, distance to the nearest line or None)] of points farther than ``tol`` from every line."""
    out = []
    t = max(tol, EPS)
    for fid, g in points.items():
        if _cancelled(feedback):
            break
        p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
        if any(lines[c].distance(g) <= t for c in line_index.intersects(_near(p, t)) if c in lines):
            continue
        near = [c for c in line_index.nearestNeighbor(p, 1) if c in lines]
        out.append((fid, min(lines[c].distance(g) for c in near) if near else None))
    return out
