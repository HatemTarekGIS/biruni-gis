"""Polygons made from the linework around them (pure geometry, no map or layers).

* ``fill_face``: the face of the planar graph of lines / polygon edges that contains a point - "click
  inside an area enclosed by lines and get the polygon". Tiny gaps (a line that stops 5 cm short of
  another, two ends that do not meet) are closed first within a tolerance.
* ``auto_complete``: the area enclosed between a sketched line and existing polygons, without the
  existing polygons (ArcGIS Pro's "Auto-complete polygon").

All geometries are QgsGeometry in one CRS; curves are densified first.
"""
from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle, QgsWkbTypes


def _parts(geom):
    """Linestring parts (as QgsGeometry) of any line or polygon geometry; polygons give their rings."""
    if geom is None or geom.isEmpty():
        return []
    g = QgsGeometry(geom)
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g = QgsGeometry(g.constGet().segmentize())
    gt = g.type()
    out = []
    if gt == QgsWkbTypes.GeometryType.PolygonGeometry:
        polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
        for poly in polys:
            for ring in poly:
                if len(ring) > 1:
                    out.append(QgsGeometry.fromPolylineXY(ring))
    elif gt == QgsWkbTypes.GeometryType.LineGeometry:
        lines = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        for line in lines:
            if len(line) > 1:
                out.append(QgsGeometry.fromPolylineXY(line))
    return out


def linework(geoms, window=None):
    """All lines and polygon edges of ``geoms`` as linestrings, clipped to ``window`` (a QgsRectangle)."""
    out = []
    win = QgsGeometry.fromRect(window) if isinstance(window, QgsRectangle) else window
    for g in geoms:
        for part in _parts(g):
            if win is not None:
                part = part.intersection(win)
                if part.isEmpty():
                    continue
                out.extend(_parts(part))
            else:
                out.append(part)
    return out


def _noded(lines):
    if not lines:
        return []
    u = QgsGeometry.unaryUnion(lines)
    return _parts(u)


def _key(p, scale):
    return (round(p.x() / scale), round(p.y() / scale))


def close_gaps(lines, tolerance):
    """Short connecting segments for the dangling ends of ``lines`` that come within ``tolerance`` of
    another end (preferred) or of another line."""
    if tolerance <= 0 or not lines:
        return []
    parts = _noded(lines)
    scale = max(tolerance * 1e-6, 1e-12)
    degree = {}
    ends = []
    for i, part in enumerate(parts):
        pl = part.asPolyline()
        for p in (pl[0], pl[-1]):
            k = _key(p, scale)
            degree[k] = degree.get(k, 0) + 1
        ends.append((pl[0], pl[-1]))
    dangles = []
    for i, (a, b) in enumerate(ends):
        if a == b:
            continue
        for p in (a, b):
            if degree[_key(p, scale)] == 1:
                dangles.append((i, p))
    connectors = []
    done = set()
    for n, (i, p) in enumerate(dangles):
        best, best_d = None, None
        for m, (j, q) in enumerate(dangles):
            if m == n:
                continue
            if j == i and parts[i].length() <= 3 * tolerance:
                continue
            d = p.distance(q)
            if 0 < d <= tolerance and (best_d is None or d < best_d):
                best, best_d = (q, ("d", min(n, m), max(n, m))), d
        if best is None:
            pg = QgsGeometry.fromPointXY(p)
            for j, part in enumerate(parts):
                if j == i:
                    continue
                d = part.distance(pg)
                if 0 < d <= tolerance and (best_d is None or d < best_d):
                    q = part.nearestPoint(pg).asPoint()
                    best, best_d = (q, ("l", n, j)), d
        if best is None:
            continue
        q, tag = best
        if tag in done:
            continue
        done.add(tag)
        connectors.append(QgsGeometry.fromPolylineXY([QgsPointXY(p), QgsPointXY(q)]))
    return connectors


def faces(geoms, tolerance=0.0, window=None):
    """The polygons enclosed by the linework of ``geoms`` (after closing gaps up to ``tolerance``)."""
    lines = linework(geoms, window)
    if not lines:
        return []
    lines = lines + close_gaps(lines, tolerance)
    u = QgsGeometry.unaryUnion(lines)
    poly = QgsGeometry.polygonize([u])
    if poly.isNull() or poly.isEmpty():
        return []
    return [QgsGeometry(p.clone()) for p in poly.constParts()]


def fill_face(point, geoms, tolerance=0.0, window=None):
    """The face containing ``point`` (QgsPointXY), or None when the point is not enclosed."""
    pg = QgsGeometry.fromPointXY(QgsPointXY(point))
    best = None
    for f in faces(geoms, tolerance, window):
        if f.contains(pg) and (best is None or f.area() < best.area()):
            best = f
    return best


def auto_complete(sketch, polygons, tolerance=0.0):
    """The area enclosed by the sketched line and the edges of ``polygons``, outside of them (None when
    the sketch does not close an area)."""
    polys = [p for p in polygons if p is not None and not p.isEmpty()]
    lines = linework(polys)
    sk = _parts(sketch)
    if not sk:
        return None
    allw = lines + sk
    allw = allw + close_gaps(allw, tolerance)
    u = QgsGeometry.unaryUnion(allw)
    cand = QgsGeometry.polygonize([u])
    if cand.isNull() or cand.isEmpty():
        return None
    covered = QgsGeometry.unaryUnion(polys) if polys else None
    sketch_line = QgsGeometry.unaryUnion(sk)
    eps = max(tolerance, 1e-9)
    keep = []
    for part in cand.constParts():
        f = QgsGeometry(part.clone())
        if f.area() <= eps * eps:
            continue
        inside = f.pointOnSurface()
        if covered is not None and covered.contains(inside):
            continue
        shared = f.buffer(eps * 0.5 if eps > 1e-9 else 1e-7, 2).intersection(sketch_line)
        if shared.isEmpty() or shared.length() <= 2 * eps:
            continue
        keep.append(f)
    if not keep:
        return None
    res = QgsGeometry.unaryUnion(keep)
    if res.isMultipart():
        parts = res.asMultiPolygon()
        if len(parts) == 1:
            res = QgsGeometry.fromPolygonXY(parts[0])
    return res


def gap_window(point, half_size):
    """A square search window around a point."""
    x, y = point.x(), point.y()
    return QgsRectangle(x - half_size, y - half_size, x + half_size, y + half_size)


def polygon_area(points):
    """Shoelace area of a list of (x, y) (any orientation)."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0

