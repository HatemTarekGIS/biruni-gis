"""The same features in two sources that share no id: found by their shapes.

Two agencies draw the same parcel each in its own way: another coordinate system, a few centimetres apart, a
datum shift of a metre, another number. Both layers are taken to one metric system; when B is offset as a whole,
the offset is measured (the median of the moves of the nearest neighbours) and taken out; then every pair of
features near each other is scored: polygons by how much they overlap (the shared area over the area of both),
lines by how much of each runs along the other, points by their distance. The best pairs are taken one to one.
What is left is looked at again: one parcel of A drawn as two in B (a split), or two of A as one in B (merged).
"""
import math
import statistics

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject,
                       QgsSpatialIndex, QgsVectorLayer, QgsWkbTypes)

PAIR = 0.6          # least score of a pair
PART = 0.8          # a piece is part of a split when this share of it is inside the whole
POINT_TOL = 2.0     # metres: a point farther than this from the other is not it


class MatchError(Exception):
    pass


def work_crs(a, b):
    """A metric system for both: A's if it has one, else B's, else the UTM zone of A's centre."""
    for lyr in (a, b):
        if not lyr.crs().isGeographic():
            return lyr.crs()
    ext = a.extent()
    if a.crs().authid() != "EPSG:4326":
        ext = QgsCoordinateTransform(a.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                     QgsProject.instance()).transformBoundingBox(ext)
    lon, lat = ext.center().x(), ext.center().y()
    zone = int(math.floor((lon + 180) / 6)) % 60 + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def _load(layer, crs):
    xf = None if layer.crs() == crs else QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance())
    out = {}
    for f in layer.getFeatures():
        if not f.hasGeometry():
            continue
        g = QgsGeometry(f.geometry())
        if g.isNull() or g.isEmpty():
            continue
        if xf is not None:
            g.transform(xf)
        if not g.isGeosValid():
            g = g.makeValid()
        out[f.id()] = g
    return out


class Result:
    def __init__(self):
        self.pairs = []          # (fid A, fid B, score)
        self.splits = []         # (fid A, [fids B], score): one of A drawn as several in B
        self.merges = []         # ([fids A], fid B, score): several of A drawn as one in B
        self.only_a = []
        self.only_b = []
        self.shift = (0.0, 0.0)  # how far B was offset as a whole, in metres (x, y), taken out before scoring
        self.crs = None          # the metric system the scores were made in
        self.kind = ""           # polygon / line / point

    def counts(self):
        return {"pairs": len(self.pairs), "splits": len(self.splits), "merges": len(self.merges),
                "only_a": len(self.only_a), "only_b": len(self.only_b)}


def _kind(layer):
    t = layer.geometryType()
    return {QgsWkbTypes.PolygonGeometry: "polygon", QgsWkbTypes.LineGeometry: "line",
            QgsWkbTypes.PointGeometry: "point"}.get(t, "")


def _size(geoms, kind):
    """A typical size of the features (metres): the median side of a polygon, length of a line."""
    vals = []
    for g in list(geoms.values())[:2000]:
        if kind == "polygon":
            vals.append(math.sqrt(max(g.area(), 0.0)))
        elif kind == "line":
            vals.append(g.length())
    vals = [v for v in vals if v > 0]
    return statistics.median(vals) if vals else 10.0


def _centre(g):
    c = g.pointOnSurface() if g.type() == QgsWkbTypes.PolygonGeometry else g.centroid()
    p = g.boundingBox().center() if c.isNull() or c.isEmpty() else c.asPoint()
    return p.x(), p.y()


def estimate_shift(ga, gb, reach):
    """The offset of B as a whole (dx, dy), or (0, 0): the median move to the nearest feature of B, when most
    features agree on it."""
    idx = QgsSpatialIndex()
    cb = {}
    for fid, g in gb.items():
        x, y = g.boundingBox().center().x(), g.boundingBox().center().y()
        cb[fid] = (x, y)
        idx.addFeature(fid, g.boundingBox())
    dx, dy = [], []
    for fid, g in list(ga.items())[:3000]:
        c = g.boundingBox().center()
        near = idx.nearestNeighbor(c, 1, reach)
        if near:
            x, y = cb[near[0]]
            dx.append(x - c.x())
            dy.append(y - c.y())
    if len(dx) < 3:
        return 0.0, 0.0
    mx, my = statistics.median(dx), statistics.median(dy)
    agree = sum(1 for a, b in zip(dx, dy) if math.hypot(a - mx, b - my) < 0.25 * reach)
    if agree < 0.5 * len(dx):
        return 0.0, 0.0
    return mx, my


def _score(kind, a, b, tol):
    if kind == "polygon":
        inter = a.intersection(b)
        ia = inter.area() if not inter.isNull() else 0.0
        if ia <= 0:
            return 0.0, 0.0, 0.0
        union = a.area() + b.area() - ia
        return (ia / union if union > 0 else 0.0), (ia / b.area() if b.area() > 0 else 0.0), \
            (ia / a.area() if a.area() > 0 else 0.0)
    if kind == "line":
        la, lb = a.length(), b.length()
        if la <= 0 or lb <= 0:
            return 0.0, 0.0, 0.0
        ina = a.intersection(b.buffer(tol, 4)).length() / la      # share of A along B
        inb = b.intersection(a.buffer(tol, 4)).length() / lb
        return min(ina, inb), inb, ina
    d = a.distance(b)
    s = max(0.0, 1.0 - d / tol)
    return s, s, s


def match(a, b, max_shift=None, tolerance=None, feedback=None):
    """The features of A found in B. ``max_shift``: how far (metres) B may be offset as a whole (None: about half a
    feature); ``tolerance``: how far apart (metres) two lines or points may run and still be the same."""
    if not isinstance(a, QgsVectorLayer) or not isinstance(b, QgsVectorLayer) or not a.isValid() or not b.isValid():
        raise MatchError("not vector layers")
    kind = _kind(a)
    if not kind or kind != _kind(b):
        raise MatchError("kinds")
    r = Result()
    r.kind = kind
    r.crs = work_crs(a, b)
    ga, gb = _load(a, r.crs), _load(b, r.crs)
    size = _size(ga, kind)
    tol = tolerance if tolerance is not None else (POINT_TOL if kind == "point" else max(0.5, 0.05 * size))
    reach = max_shift if max_shift is not None else (5.0 if kind == "point" else 0.5 * size)
    if reach > 0 and ga and gb:
        r.shift = estimate_shift(ga, gb, reach)
    if r.shift != (0.0, 0.0):
        for g in gb.values():
            g.translate(-r.shift[0], -r.shift[1])
    idx = QgsSpatialIndex()
    for fid, g in gb.items():
        idx.addFeature(fid, g.boundingBox())
    cands = []                 # (score, fid A, fid B)
    part_of = {}               # fid B -> [(share of B in A, fid A)]
    a_in = {}                  # fid A -> [(share of A in B, fid B)]
    total = max(1, len(ga))
    for n, (fa, g) in enumerate(ga.items()):
        box = g.boundingBox()
        box.grow(tol)
        for fb in idx.intersects(box):
            s, share_b, share_a = _score(kind, g, gb[fb], tol)
            if s > 0:
                cands.append((s, fa, fb))
            if share_b >= PART:
                part_of.setdefault(fb, []).append((share_b, fa))
            if share_a >= PART:
                a_in.setdefault(fa, []).append((share_a, fb))
        if feedback and n % 500 == 0 and feedback(100.0 * n / total) is False:
            raise MatchError("cancelled")
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_a, used_b = set(), set()
    for s, fa, fb in cands:
        if s < PAIR:
            break
        if fa in used_a or fb in used_b:
            continue
        used_a.add(fa)
        used_b.add(fb)
        r.pairs.append((fa, fb, round(s, 4)))
    if kind != "point":
        # one of A as several of B: the pieces of B left over that lie in it
        # (a pair already taken counts with its piece: a 70/30 split pairs the big piece first)
        paired = {fa: (fb, s) for fa, fb, s in r.pairs}
        pieces = {}
        for fb, lst in part_of.items():
            if fb in used_b:
                continue
            pieces.setdefault(max(lst)[1], []).append(fb)
        for fa, fbs in pieces.items():
            had = paired.get(fa)
            if had is not None:
                fbs = fbs + [had[0]]
            if len(fbs) < 2:
                continue
            whole = QgsGeometry.unaryUnion([gb[f] for f in fbs])
            s = _score(kind, ga[fa], whole, tol)[0]
            if s >= PAIR and (had is None or s > had[1] + 0.05):
                if had is not None:
                    r.pairs.remove((fa, had[0], had[1]))
                used_a.add(fa)
                used_b.update(fbs)
                r.splits.append((fa, sorted(fbs), round(s, 4)))
        # several of A as one of B
        parts = {}
        for fa, lst in a_in.items():
            if fa in used_a:
                continue
            fb = max(lst)[1]
            if fb not in used_b:
                parts.setdefault(fb, []).append(fa)
        for fb, fas in parts.items():
            if len(fas) < 2:
                continue
            whole = QgsGeometry.unaryUnion([ga[f] for f in fas])
            s = _score(kind, whole, gb[fb], tol)[0]
            if s >= PAIR:
                used_b.add(fb)
                used_a.update(fas)
                r.merges.append((sorted(fas), fb, round(s, 4)))
    r.only_a = sorted(set(ga) - used_a)
    r.only_b = sorted(set(gb) - used_b)
    r.pairs.sort()
    return r
