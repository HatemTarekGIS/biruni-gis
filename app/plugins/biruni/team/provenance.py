"""Where a feature came from, vertex by vertex: who placed each point of its shape, when, and from what.

The room's record keeps every state of every feature with its author and time, and - from this version - what
was on the author's screen (the images shown, the scale: "src", signed with the change). Walking the states in
order, each change's new or moved vertices are given to it and the vertices it left alone keep their owner, so
of a parcel edited by three people each corner says whose it is. Each field says who gave it its value. Field
photos near the feature, taken before the change, are the evidence beside it.
"""
import datetime as dt
from difflib import SequenceMatcher

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsRectangle, QgsVectorLayer, QgsWkbTypes

from . import merge

PHOTO_RADIUS_M = 30.0


def _rings(state):
    """[[[(x, y), …] per ring] per part] of a state's shape (the closing point left out), or None."""
    g = merge._geom((state or {}).get("g"))
    if g is None or g.isNull():
        return None
    p = merge._parts(g)
    if p is None:      # curves: their control points
        a = g.constGet()
        seq = [[[(pt.x(), pt.y()) for pt in ring] for ring in part] for part in a.coordinateSequence()]
        closed = g.type() == QgsWkbTypes.PolygonGeometry
    else:
        closed = QgsWkbTypes.geometryType(p[0]) == QgsWkbTypes.PolygonGeometry
        seq = [[[tuple(pt[:2]) for pt in ring] for ring in part] for part in p[1]]
    return [[merge._ring(r, closed) for r in part] for part in seq]


def vertices(entries):
    """The current shape's vertices with the entry that placed each: [(part, ring, index, (x, y), entry)]."""
    rings, owners = None, None
    first = next((e for e in entries if e.get("kind") != "field"), None)
    if first is not None and first.get("prev"):
        rings = _rings(first["prev"])          # what the feature was before the room: nobody's here
        owners = [[[None] * len(r) for r in part] for part in rings] if rings else None
    for e in entries:
        if e.get("kind") == "field":
            continue
        if e.get("kind") == "del" or e.get("state") is None:
            rings, owners = None, None
            continue
        new = _rings(e["state"])
        if new is None:
            rings, owners = None, None
            continue
        out = []
        for pi, part in enumerate(new):
            po = []
            for ri, ring in enumerate(part):
                old = rings[pi][ri] if rings and pi < len(rings) and ri < len(rings[pi]) else None
                if old is None:
                    po.append([e] * len(ring))
                    continue
                prev = owners[pi][ri]
                mine = [e] * len(ring)
                for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, ring, autojunk=False).get_opcodes():
                    if tag == "equal":
                        mine[j1:j2] = prev[i1:i2]
                po.append(mine)
            out.append(po)
        rings, owners = new, out
    if rings is None:
        return []
    return [(pi, ri, i, pt, owners[pi][ri][i]) for pi, part in enumerate(rings) for ri, ring in enumerate(part)
            for i, pt in enumerate(ring)]


def fields(entries):
    """{field: entry that gave it its current value}."""
    out, last = {}, {}
    first = next((e for e in entries if e.get("kind") != "field"), None)
    if first is not None and first.get("prev"):
        last = dict((first["prev"].get("a") or {}))
    for e in entries:
        if e.get("kind") in ("field", "del") or e.get("state") is None:
            if e.get("kind") == "del":
                out, last = {}, {}
            continue
        a = (e["state"] or {}).get("a") or {}
        for k, v in a.items():
            if k not in last or last[k] != v:
                out[k] = e
            last[k] = v
    return out


def authors(entries):
    """[(name, how many of the current vertices)] most first."""
    count = {}
    for *_r, e in vertices(entries):
        name = (e or {}).get("name") or ""      # "": from before the room
        count[name] = count.get(name, 0) + 1
    return sorted(count.items(), key=lambda kv: -kv[1])


def source_text(e):
    """The provenance of one change in words-ready parts: (images, scale)."""
    src = e.get("src") or {}
    return ", ".join(src.get("img") or []), src.get("scale")


def photo_layers():
    """The layers of field photos on the map (points with a photo and the time it was taken)."""
    from qgis.core import QgsProject

    return [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer) and l.isValid()
            and l.geometryType() == QgsWkbTypes.PointGeometry and l.fields().indexOf("photo") >= 0
            and l.fields().indexOf("taken") >= 0]


def _ts(text):
    try:
        t = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return t.timestamp() if t.tzinfo else t.astimezone().timestamp()
    except ValueError:
        return None


def photos_near(geom, layer_crs, before_ts, radius_m=PHOTO_RADIUS_M):
    """Field photos within radius of the shape, taken before the time: [(taken, photo path, layer, fid)] newest first."""
    from qgis.core import QgsCoordinateTransform, QgsDistanceArea, QgsProject

    out = []
    if geom is None or geom.isNull():
        return out
    for pl in photo_layers():
        g = QgsGeometry(geom)
        if pl.crs() != layer_crs:
            g.transform(QgsCoordinateTransform(layer_crs, pl.crs(), QgsProject.instance()))
        d = QgsDistanceArea()
        d.setSourceCrs(pl.crs(), QgsProject.instance().transformContext())
        d.setEllipsoid(pl.crs().ellipsoidAcronym() or "EPSG:7030")
        bb = g.boundingBox()
        c = bb.center()
        step = d.measureLine(QgsPointXY(c.x(), c.y()), QgsPointXY(c.x() + 1e-3 if pl.crs().isGeographic() else c.x() + 1, c.y()))
        unit = (1e-3 if pl.crs().isGeographic() else 1.0) / step if step else 1.0
        r = radius_m * unit
        box = QgsRectangle(bb.xMinimum() - r, bb.yMinimum() - r, bb.xMaximum() + r, bb.yMaximum() + r)
        for f in pl.getFeatures(QgsFeatureRequest().setFilterRect(box)):
            t = _ts(f["taken"])
            if t is None or (before_ts is not None and t > before_ts + 60):
                continue
            if f.geometry().distance(g) > r:
                continue
            out.append((f["taken"], f["photo"], pl, f.id()))
    out.sort(key=lambda x: str(x[0]), reverse=True)
    return out
