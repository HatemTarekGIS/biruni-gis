"""The astrolabe's commands. A command is a list of steps; a step says on what (``t``), what to do (``v``) and how
much or with what (``val`` / ``other`` / ``opt``):

    {"t": "L:roads", "v": "buffer", "val": 15}
    {"t": "L:parcels", "v": "intersect", "other": "prev"}
    {"t": "prev", "v": "measure", "opt": "m2"}

``run`` works the steps out on copies of the geometries - nothing in the project changes - so the same code makes
the preview drawn on the map while the rings turn and the result written when the command is carried out.
Everything is in one working CRS (metres); the source gives the features already in it.
"""
from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.PyQt.QtGui import QTransform

VERBS = ("buffer", "split", "union", "intersect", "erase", "select", "rotate", "scale", "centroid", "hull", "measure")
POINT, LINE, POLYGON = Qgis.GeometryType.Point, Qgis.GeometryType.Line, Qgis.GeometryType.Polygon
_RANK = {POINT: 0, LINE: 1, POLYGON: 2}


class Item:
    """A feature being worked on: where it comes from (layer id, feature id), its geometry in the working CRS and
    its attributes. ``made``: a step made it (it is not in a layer yet); ``new``: a part a split adds to a layer."""
    __slots__ = ("layer", "fid", "geom", "attrs", "made", "new")

    def __init__(self, layer, fid, geom, attrs=None, made=False, new=False):
        self.layer, self.fid, self.geom, self.attrs, self.made, self.new = layer, fid, geom, attrs, made, new

    def copy(self):
        return Item(self.layer, self.fid, QgsGeometry(self.geom), self.attrs, self.made, self.new)


class Out:
    """What one step gives: ``n`` features it works on, ``err`` (a code, "" when fine), ``geoms`` to show,
    ``labels`` [(point, value, kind)], ``sel`` the features it selects, ``pivot`` of a rotation, ``last`` the
    features the next step gets as "prev", ``made`` when it makes new features, and for changes in place
    ``changed`` (features with a new geometry) and ``added`` (parts a split adds)."""

    def __init__(self, n=0):
        self.n = n
        self.err = ""
        self.note = None        # ("select", k, n) | ("split", a, b)
        self.geoms = []
        self.labels = []
        self.sel = None         # list of Items when the step selects
        self.pivot = None
        self.last = []
        self.made = False
        self.changed = []
        self.added = []
        self.target_layers = ()   # a selecting step: the layers it selects in


def only(geom, kind):
    """``geom`` kept to the parts of one kind (an intersection also gives the lines and points where shapes only
    touch); None when nothing of that kind is left."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return None
    if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.Type.GeometryCollection:
        parts = [p for p in geom.asGeometryCollection() if p.type() == kind and not p.isEmpty()]
        return QgsGeometry.collectGeometry(parts) if parts else None
    return geom if geom.type() == kind else None


def split_equal(geom, n):
    """A polygon cut into ``n`` parts of equal area, across its long side -> [QgsGeometry]."""
    if not geom.isGeosValid():
        geom = geom.makeValid()
    box = geom.orientedMinimumBoundingBox()[0]
    ring = box.asPolygon()[0] if not box.isEmpty() else []
    if len(ring) < 4 or n < 2:
        return [geom]
    o, p1, p2 = ring[0], ring[1], ring[2]
    e1, e2 = (p1.x() - o.x(), p1.y() - o.y()), (p2.x() - p1.x(), p2.y() - p1.y())
    l1, l2 = (e1[0] ** 2 + e1[1] ** 2) ** .5, (e2[0] ** 2 + e2[1] ** 2) ** .5
    if not l1 or not l2:
        return [geom]
    (u, lu), (v, lv) = (((e1[0] / l1, e1[1] / l1), l1), ((e2[0] / l2, e2[1] / l2), l2))
    if l2 > l1:
        (u, lu), (v, lv) = (v, lv), (u, lu)
    big = (l1 + l2) * 2 + 1
    # along the long side: from -span to +span around the box's first corner
    proj = [(p.x() - o.x()) * u[0] + (p.y() - o.y()) * u[1] for p in ring]
    lo, hi = min(proj), max(proj)

    def slab(a, b):
        pts = [QgsPointXY(o.x() + u[0] * s + v[0] * w, o.y() + u[1] * s + v[1] * w)
               for s, w in ((a, -big), (b, -big), (b, big), (a, big))]
        return QgsGeometry.fromPolygonXY([pts + [pts[0]]])

    total = geom.area()
    cuts = [lo - 1]
    for k in range(1, n):
        a, b = lo, hi
        for _ in range(34):
            m = (a + b) / 2
            if geom.intersection(slab(lo - 1, m)).area() < total * k / n:
                a = m
            else:
                b = m
        cuts.append((a + b) / 2)
    cuts.append(hi + 1)
    parts = [only(geom.intersection(slab(cuts[i], cuts[i + 1])), POLYGON) for i in range(n)]
    return [p for p in parts if p is not None]


class _State:
    """What the steps so far changed, on top of what the source gives."""

    def __init__(self, source):
        self.source = source
        self.changed = {}   # (layer, fid) -> geometry
        self.added = {}     # layer -> [Item]
        self.selected = None

    def resolve(self, key, last):
        if key == "prev":
            return list(last or [])
        if key == "sel" and self.selected is not None:
            return list(self.selected)
        items = [i.copy() for i in self.source.items(key)]
        for i in items:
            g = self.changed.get((i.layer, i.fid))
            if g is not None:
                i.geom = QgsGeometry(g)
        lid = self.source.layer_id(key) if key not in ("sel", "view") else None
        if lid is not None:
            items += self.added.get(lid, [])
        return items


def _centre(items):
    box = None
    for i in items:
        b = i.geom.boundingBox()
        if box is None:
            box = b
        else:
            box.combineExtentWith(b)
    return box.center()


def _apply(st, state, last):
    verb = st["v"]
    target = state.resolve(st["t"], last)
    o = Out(state.source.count(st["t"]) if st["t"] not in ("prev",) and not (st["t"] == "sel" and state.selected is not None)
            else len(target))
    o.last = target
    if not target:
        o.n = 0
        o.err = "no_selection" if st["t"] == "sel" else "no_target"
        return o

    def make(items):
        o.made = True
        o.last = items
        o.geoms = [i.geom for i in items]
        if not items:
            o.err = "empty"

    def others():
        if st.get("other") == st["t"] and verb != "select":
            o.err = "same_layer"
            return None
        other = state.resolve(st.get("other"), last)
        if verb != "select":   # a feature is not cut by itself
            mine = {(i.layer, i.fid) for i in target if not i.made}
            other = [x for x in other if x.made or (x.layer, x.fid) not in mine]
        if not other:
            o.err = "no_selection" if st.get("other") == "sel" else "other_empty"
            return None
        union = QgsGeometry.unaryUnion([x.geom for x in other])
        engine = QgsGeometry.createGeometryEngine(union.constGet())
        engine.prepareGeometry()
        return union, engine, max(_RANK.get(x.geom.type(), 0) for x in other)

    try:
        if verb == "buffer":
            make([Item(i.layer, i.fid, i.geom.buffer(float(st["val"]), 12), i.attrs, made=True) for i in target])
        elif verb == "union":
            make([Item(None, None, QgsGeometry.unaryUnion([i.geom for i in target]), None, made=True)])
        elif verb in ("intersect", "erase"):
            got = others()
            if got is not None:
                union, engine, rank = got
                out = []
                for i in target:
                    touches = engine.intersects(i.geom.constGet())
                    if verb == "intersect":
                        if not touches:
                            continue
                        want = min(_RANK.get(i.geom.type(), 0), rank)
                        g = only(i.geom.intersection(union), {0: POINT, 1: LINE, 2: POLYGON}[want])
                    else:
                        g = only(i.geom.difference(union), i.geom.type()) if touches else i.geom
                    if g is not None:
                        out.append(Item(i.layer, i.fid, g, i.attrs, made=True))
                make(out)
        elif verb == "select":
            if any(i.made or i.fid is None for i in target):
                o.err = "layer_features_only"
            else:
                got = others()
                if got is not None:
                    engine = got[1]
                    found = [i for i in target if engine.intersects(i.geom.constGet())]
                    state.selected = found
                    o.target_layers = {i.layer for i in target}
                    o.sel = found
                    o.last = found
                    o.note = ("select", len(found), len(target))
        elif verb in ("rotate", "scale"):
            c = _centre(target)
            o.pivot = QgsPointXY(c)
            for i in target:
                g = QgsGeometry(i.geom)
                if verb == "rotate":
                    g.rotate(float(st["val"]), QgsPointXY(c))
                else:
                    k = float(st["val"]) / 100.0
                    t = QTransform()
                    t.translate(c.x(), c.y())
                    t.scale(k, k)
                    t.translate(-c.x(), -c.y())
                    g.transform(t)
                i.geom = g
                if not i.made and not i.new:
                    state.changed[(i.layer, i.fid)] = g
                    o.changed.append(i)
            o.geoms = [QgsGeometry(i.geom) for i in target]
        elif verb == "split":
            polys = [i for i in target if i.geom.type() == POLYGON]
            if not polys:
                o.err = "polygons_only"
            else:
                parts_all = []
                for i in polys:
                    parts = split_equal(i.geom, int(st["val"]))
                    if not parts:
                        continue
                    i.geom = parts[0]
                    parts_all.append(i)
                    if not i.made and not i.new:
                        state.changed[(i.layer, i.fid)] = parts[0]
                        o.changed.append(i)
                    for g in parts[1:]:
                        part = Item(i.layer, None, g, i.attrs, made=i.made, new=not i.made)
                        parts_all.append(part)
                        if not i.made:
                            state.added.setdefault(i.layer, []).append(part)
                            o.added.append(part)
                o.last = parts_all
                o.geoms = [QgsGeometry(i.geom) for i in parts_all]
                o.note = ("split", len(polys), len(parts_all))
                if len(parts_all) <= 60:
                    o.labels = [(i.geom.pointOnSurface().asPoint(), state.source.area(i.geom), "area") for i in parts_all]
        elif verb == "centroid":
            make([Item(i.layer, i.fid, i.geom.centroid(), i.attrs, made=True) for i in target])
        elif verb == "hull":
            if st.get("opt") == "each":
                make([Item(i.layer, i.fid, i.geom.convexHull(), i.attrs, made=True) for i in target])
            else:
                make([Item(None, None, QgsGeometry.collectGeometry([i.geom for i in target]).convexHull(), None,
                           made=True)])
        else:  # measure
            for i in target:
                kind = i.geom.type()
                at = i.geom.pointOnSurface() if kind == POLYGON else i.geom.centroid() if kind == POINT \
                    else i.geom.interpolate(i.geom.length() / 2)
                if at is None or at.isNull() or at.isEmpty():
                    continue
                if kind == POLYGON:
                    o.labels.append((at.asPoint(), state.source.area(i.geom), "area"))
                elif kind == LINE:
                    o.labels.append((at.asPoint(), state.source.length(i.geom), "length"))
                else:
                    o.labels.append((at.asPoint(), None, "point"))
    except Exception:  # noqa: BLE001 - a broken shape must not take the preview down
        o.err = "failed"
    return o


def run(steps, source):
    """Work the steps out -> [Out]. ``source`` gives ``items(key)``, ``count(key)``, ``layer_id(key)``,
    ``area(geom)`` and ``length(geom)``."""
    state = _State(source)
    outs, last = [], None
    for st in steps:
        o = _apply(st, state, last)
        outs.append(o)
        last = [] if o.err else o.last
    return outs


def stepping_stone(steps, outs, i):
    """Step ``i`` made features only for the next step to use: they are not kept as a layer of their own."""
    if i + 1 >= len(steps) or not outs[i].made:
        return False
    nxt = steps[i + 1]
    return (nxt.get("t") == "prev" or nxt.get("other") == "prev") and (outs[i + 1].made or nxt["v"] == "select")
