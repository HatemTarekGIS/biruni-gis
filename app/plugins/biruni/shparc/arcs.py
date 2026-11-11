"""Arcs written as short lines that keep the arc's area exactly and its length to a tenth of a millimetre.

A shapefile has no arcs: every program that writes one turns an arc into points *on* the arc, and the polygon
loses a sliver of area at every arc (and the line a little length). Here the points between the arc's ends are
put a hair outside the arc, on a radius worked out so that the area under the short lines is the area under the
arc - exactly, whatever the number of points. The length cannot be kept exactly too (of all the lines between
two points that close the same area, the arc is the shortest), so the number of points is raised until the
difference is under a tolerance and the length written to the centimetre is the arc's own.

Two parcels that share an arc get the very same points: they are worked out from the arc alone, always from
the same end.
"""
import math

LENGTH_TOLERANCE = 0.0001     # metres: the short lines are at most this much longer than the arc
ROUND_TO = 2                  # … and equal to it when both are written with this many decimals
MAX_STEP = math.radians(5.0)  # an arc is never coarser than this, however short
MAX_SEGMENTS = 20000           # an arc of many kilometres; past it the length tolerance gives way


def circle(p0, pm, p1):
    """The circle through three points -> (cx, cy, r), or None when they are on a line (or the same point)."""
    # worked out from the first point: map coordinates are millions of metres, and their squares would
    # swallow the millimetres the centre is wanted to
    ox, oy = p0[0], p0[1]
    bx, by, cx, cy = pm[0] - ox, pm[1] - oy, p1[0] - ox, p1[1] - oy
    d = 2.0 * (bx * cy - by * cx)
    scale = max(abs(bx), abs(by), abs(cx), abs(cy), 1e-300)
    if abs(d) < 1e-12 * scale * scale:
        return None
    b2, c2 = bx * bx + by * by, cx * cx + cy * cy
    ux = (cy * b2 - by * c2) / d
    uy = (bx * c2 - cx * b2) / d
    return ox + ux, oy + uy, math.hypot(ux, uy)


def arc_of(p0, pm, p1):
    """The arc from p0 through pm to p1 -> (cx, cy, r, a0, sweep) with the sweep signed (+ is anticlockwise),
    or None for three points on a line. p0 == p1 is a full circle through pm."""
    if p0[0] == p1[0] and p0[1] == p1[1]:
        cx, cy = (p0[0] + pm[0]) / 2.0, (p0[1] + pm[1]) / 2.0
        r = math.hypot(p0[0] - cx, p0[1] - cy)
        if r == 0:
            return None
        return cx, cy, r, math.atan2(p0[1] - cy, p0[0] - cx), 2 * math.pi
    c = circle(p0, pm, p1)
    if c is None:
        return None
    cx, cy, r = c
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    am = math.atan2(pm[1] - cy, pm[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    two = 2 * math.pi
    ccw_m, ccw_1 = (am - a0) % two, (a1 - a0) % two
    sweep = ccw_1 if ccw_m <= ccw_1 else ccw_1 - two   # the way round that passes the middle point
    return cx, cy, r, a0, sweep


def outer_radius(r, sweep, n):
    """The radius of the points between the ends, for ``n`` short lines to close the arc's own area."""
    a = abs(sweep) / n
    if n == 2:
        return r * abs(sweep) / (2.0 * math.sin(a))
    s = math.sin(a)
    qa, qb, qc = (n - 2) * s, 2.0 * r * s, -r * r * abs(sweep)
    return (-qb + math.sqrt(qb * qb - 4.0 * qa * qc)) / (2.0 * qa)


def lines_length(r, sweep, n):
    a = abs(sweep) / n
    rho = outer_radius(r, sweep, n)
    end = math.sqrt(max(0.0, r * r + rho * rho - 2.0 * r * rho * math.cos(a)))
    return 2.0 * end + (n - 2) * 2.0 * rho * math.sin(a / 2.0)


def segments_for(r, sweep, tolerance=LENGTH_TOLERANCE, round_to=ROUND_TO):
    """How many short lines an arc gets: the fewest that keep the length within the tolerance and the same
    when written with ``round_to`` decimals."""
    length = r * abs(sweep)
    if length <= 0:
        return 2
    n = max(2, int(math.ceil(abs(sweep) / MAX_STEP)))
    if tolerance > 0:   # the extra length is about length * step² / 24
        step = math.sqrt(24.0 * tolerance / length)
        n = max(n, int(math.ceil(abs(sweep) / step)))
    n = min(n, MAX_SEGMENTS)
    while n < MAX_SEGMENTS:
        got = lines_length(r, sweep, n)
        if got - length <= tolerance and (round_to is None or round(got, round_to) == round(length, round_to)):
            break
        n = min(MAX_SEGMENTS, int(math.ceil(n * 1.4)) + 1)
    return n


def arc_points(p0, pm, p1, tolerance=LENGTH_TOLERANCE, round_to=ROUND_TO):
    """The arc as points: p0, the points between (a hair outside the arc), p1 -> ([(x, y)], info) where info
    has the arc's radius, length, the short lines' length and their number. Three points on a line give the
    two ends. The points are the same whichever end the arc is given from."""
    flip = (p1[0], p1[1]) < (p0[0], p0[1])
    a, b = (p1, p0) if flip else (p0, p1)      # always worked out from the same end
    arc = arc_of(a, pm, b)
    if arc is None:
        return [tuple(p0[:2]), tuple(p1[:2])], {"r": None, "arc": math.hypot(p1[0] - p0[0], p1[1] - p0[1]),
                                              "lines": math.hypot(p1[0] - p0[0], p1[1] - p0[1]), "n": 1}
    cx, cy, r, a0, sweep = arc
    n = segments_for(r, sweep, tolerance, round_to)
    rho = outer_radius(r, sweep, n)
    step = sweep / n
    pts = [(a[0], a[1])]
    for i in range(1, n):
        t = a0 + step * i
        pts.append((cx + rho * math.cos(t), cy + rho * math.sin(t)))
    pts.append((b[0], b[1]))
    if flip:
        pts.reverse()
    return pts, {"r": r, "arc": r * abs(sweep), "lines": lines_length(r, sweep, n), "n": n}
