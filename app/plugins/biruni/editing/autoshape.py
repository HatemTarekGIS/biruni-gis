"""Shape completion while drawing a polygon - like a phone keyboard that offers the next word.

From the sides already drawn the tool offers the whole shape, faint on the map; Tab takes it:

* one side drawn: the neighbours of the same layer whose shape has a side of that length (5 %) are copied
  onto it - a row of equal plots is drawn one side and one Tab each. The copy goes on the side of the cursor
  and must not cover the existing polygons.
* two sides drawn: a neighbour with two consecutive sides of those lengths and that angle, else - when the
  corner is square (±2°) - the rectangle the two sides make.

Pure geometry on (x, y) tuples in one planar CRS; no Qt.
"""
import math

from qgis.core import QgsGeometry, QgsPointXY

LENGTH_TOL = 0.05       # sides match within 5 % of their length
ANGLE_TOL = 2.0         # degrees: a corner within this of 90° is square; consecutive sides match within it
MAX_OVERLAP = 0.02      # a suggestion may cover at most 2 % of its area with existing polygons
NEAR = 4.0              # neighbours looked at: within this many times the drawn side's length


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _angle(a, b, c):
    """Inner angle at b, degrees."""
    v1, v2 = (a[0] - b[0], a[1] - b[1]), (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n == 0:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))))


def ring_of(g):
    """Outer ring of a single polygon without the closing point and without collinear vertices, or None."""
    if g is None or g.isEmpty():
        return None
    polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
    if len(polys) != 1 or not polys[0]:
        return None
    pts = [(p.x(), p.y()) for p in polys[0][0]][:-1]
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        if abs(180.0 - _angle(a, b, c)) > 0.5:
            out.append(b)
    return out if len(out) >= 3 else None


def _place(ring, i, a, b, reverse=False):
    """``ring`` moved and turned so its side i → i+1 lies on a → b (no scaling: the drawn length wins)."""
    pts = list(reversed(ring)) if reverse else list(ring)
    n = len(pts)
    if reverse:
        i = n - 1 - ((i + 1) % n)
    p, q = pts[i], pts[(i + 1) % n]
    ang = math.atan2(b[1] - a[1], b[0] - a[0]) - math.atan2(q[1] - p[1], q[0] - p[0])
    c, s = math.cos(ang), math.sin(ang)
    k = _dist(a, b) / _dist(p, q)       # the copy keeps its shape; the drawn side's length is kept exactly
    out = []
    for j in range(n):
        x, y = pts[(i + j) % n]
        dx, dy = (x - p[0]) * k, (y - p[1]) * k
        out.append((a[0] + c * dx - s * dy, a[1] + s * dx + c * dy))
    return out                            # starts with a, b, …


def _polygon(ring):
    return QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in ring + ring[:1]]])


def _overlap(ring, others):
    g = _polygon(ring)
    area = g.area()
    if area <= 0:
        return 1.0
    covered = 0.0
    for o in others:
        if o.boundingBox().intersects(g.boundingBox()):
            x = g.intersection(o)
            if not x.isEmpty():
                covered += x.area()
    return covered / area


def _side(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def suggestions(verts, neighbours):
    """[(ring, side, how)] best first: ``ring`` starts with the drawn points; ``side`` = +1 / -1, the side of the
    first drawn side the shape lies on (for one drawn side the cursor picks it); ``how`` = "neighbour" or
    "rectangle". ``neighbours`` = polygon QgsGeometry of the layer near the sketch."""
    if len(verts) < 2 or len(verts) > 3:
        return []
    a, b = verts[0], verts[1]
    ab = _dist(a, b)
    if ab <= 0:
        return []
    rings = [r for r in (ring_of(g) for g in neighbours) if r]
    found = []
    if len(verts) == 2:
        for ring in rings:
            n = len(ring)
            for i in range(n):
                if abs(_dist(ring[i], ring[(i + 1) % n]) - ab) > LENGTH_TOL * ab:
                    continue
                for rev in (False, True):
                    cand = _place(ring, i, a, b, rev)
                    c = cand[2]
                    side = 1 if _side(a, b, c) > 0 else -1
                    err = abs(_dist(ring[i], ring[(i + 1) % n]) - ab) / ab
                    found.append((cand, side, "neighbour", err))
    else:
        c = verts[2]
        bc = _dist(b, c)
        corner = _angle(a, b, c)
        for ring in rings:
            n = len(ring)
            for i in range(n):
                p, q, r = ring[i], ring[(i + 1) % n], ring[(i + 2) % n]
                for rev in (False, True):
                    if rev:
                        p, r = r, p
                    if abs(_dist(p, q) - ab) > LENGTH_TOL * ab or abs(_dist(q, r) - bc) > LENGTH_TOL * bc or \
                            abs(_angle(p, q, r) - corner) > ANGLE_TOL:
                        continue
                    j = (n - 1 - ((i + 2) % n)) if rev else i
                    cand = _place(list(reversed(ring)) if rev else ring, j, a, b)
                    if _dist(cand[2], c) > LENGTH_TOL * bc + 1e-9:
                        continue
                    cand = [a, b, c] + cand[3:]
                    err = (abs(_dist(p, q) - ab) / ab + abs(_dist(q, r) - bc) / bc)
                    found.append((cand, 1 if _side(a, b, c) > 0 else -1, "neighbour", err))
        if abs(corner - 90.0) <= ANGLE_TOL and not found:
            d = (a[0] + c[0] - b[0], a[1] + c[1] - b[1])
            found.append(([a, b, c, d], 1 if _side(a, b, c) > 0 else -1, "rectangle", 0.0))
    others = list(neighbours)
    keep, seen = [], []
    for cand, side, how, err in sorted(found, key=lambda x: x[3]):
        key = tuple((round(x, 3), round(y, 3)) for x, y in cand)
        if key in seen or _overlap(cand, others) > MAX_OVERLAP:
            continue
        seen.append(key)
        keep.append((cand, side, how))
    return keep


def pick(cands, verts, cursor):
    """The suggestion to show: for one drawn side, the best one on the cursor's side of it."""
    if not cands:
        return None
    if len(verts) == 2 and cursor is not None:
        s = 1 if _side(verts[0], verts[1], cursor) > 0 else -1
        for cand in cands:
            if cand[1] == s:
                return cand
        return None
    return cands[0]


def near_rect(verts):
    """(xmin, ymin, xmax, ymax) to look for neighbours in."""
    xs, ys = [p[0] for p in verts], [p[1] for p in verts]
    r = max(_dist(verts[0], verts[1]), 1e-9) * NEAR
    return min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r
