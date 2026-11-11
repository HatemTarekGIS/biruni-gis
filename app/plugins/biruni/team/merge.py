"""Joining two people's changes to the same feature (three-way merge).

A change arrives with the state its author started from (base). When the feature has changed
since then (current, someone else's work), the two changes are joined instead of one
overwriting the other:

* attributes, field by field: a field only one side changed takes that side's value; a field
  both changed to different values is a conflict;
* the geometry, vertex by vertex: each side's edits are found as runs of vertices replaced,
  moved, added or removed on each line or ring; runs on different vertices are all applied,
  so one person can fix the north edge of a parcel while another fixes its south edge. Runs
  on the same vertices, a different number of parts or rings, curves, or a result that is no
  longer a valid shape are a conflict.

A conflict keeps the current value (nothing written by the other person is lost) and is
reported to the author, who sees both versions and chooses (node.py, dock.py).
States are {"g": WKB hex or None, "a": {field: value}}.
"""
from difflib import SequenceMatcher

from qgis.core import QgsGeometry, QgsWkbTypes


def _geom(hexwkb):
    if not hexwkb:
        return None
    g = QgsGeometry()
    g.fromWkb(bytes.fromhex(hexwkb))
    return g


def _hex(g):
    return bytes(g.asWkb()).hex() if g is not None and not g.isNull() else None


def _point(p, z, m):
    t = (p.x(), p.y())
    if z:
        t += (p.z(),)
    if m:
        t += (p.m(),)
    return t


def _parts(g):
    """[[ring or line as a list of coordinate tuples]] and the type, or None for shapes not merged here."""
    a = g.constGet()
    t = a.wkbType()
    if QgsWkbTypes.isCurvedType(t):
        return None
    z, m = QgsWkbTypes.hasZ(t), QgsWkbTypes.hasM(t)
    return t, [[[_point(p, z, m) for p in ring] for ring in part] for part in a.coordinateSequence()]


def _wkt(t, parts):
    def coords(ring):
        return "(" + ", ".join(" ".join(repr(float(v)) for v in p) for p in ring) + ")"

    flat = QgsWkbTypes.flatType(t)
    name = QgsWkbTypes.displayString(t)
    if flat == QgsWkbTypes.Point:
        return f"{name} " + coords(parts[0][0])
    if flat == QgsWkbTypes.LineString:
        return f"{name} " + coords(parts[0][0])
    if flat == QgsWkbTypes.Polygon:
        return f"{name} (" + ", ".join(coords(r) for r in parts[0]) + ")"
    if flat == QgsWkbTypes.MultiPoint:
        return f"{name} (" + ", ".join(coords(p[0]) for p in parts) + ")"
    if flat == QgsWkbTypes.MultiLineString:
        return f"{name} (" + ", ".join(coords(p[0]) for p in parts) + ")"
    if flat == QgsWkbTypes.MultiPolygon:
        return f"{name} (" + ", ".join("(" + ", ".join(coords(r) for r in p) + ")" for p in parts) + ")"
    return None


def _hunks(base, other):
    return [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in
            SequenceMatcher(None, base, other, autojunk=False).get_opcodes() if tag != "equal"]


def _touch(a, b):
    """Two runs of base vertices [i1, i2) (an insertion has i1 == i2) that cannot both be applied."""
    (a1, a2), (b1, b2) = a, b
    if a1 == a2 and b1 == b2:
        return a1 == b1
    if a1 == a2:
        return b1 < a1 < b2
    if b1 == b2:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2


def merge_sequence(base, mine, theirs):
    """The vertices with both sides' edits, or None when they edit the same vertices."""
    hm, ht = _hunks(base, mine), _hunks(base, theirs)
    keep = list(hm)
    for h in ht:
        same = [k for k in hm if (k[0], k[1]) == (h[0], h[1])]
        if same:
            if same[0][2] != h[2]:
                return None
            continue  # both made the same edit
        if any(_touch((h[0], h[1]), (k[0], k[1])) for k in hm):
            return None
        keep.append(h)
    keep.sort(key=lambda h: (h[0], h[1]))
    out, i = [], 0
    for i1, i2, new in keep:
        out.extend(base[i:i1])
        out.extend(new)
        i = i2
    out.extend(base[i:])
    return out


def _ring(seq, closed):
    return seq[:-1] if closed and len(seq) > 1 and seq[0] == seq[-1] else seq


def merge_geometry(base, mine, theirs):
    """(WKB hex, True) merged, or (theirs, False) when the two edits cannot be joined."""
    if mine == base or mine == theirs:
        return theirs, True
    if theirs == base:
        return mine, True
    gb, gm, gt = _geom(base), _geom(mine), _geom(theirs)
    if gb is None or gm is None or gt is None:
        return theirs, False
    pb, pm, pt = _parts(gb), _parts(gm), _parts(gt)
    if pb is None or pm is None or pt is None or not (pb[0] == pm[0] == pt[0]):
        return theirs, False
    t = pb[0]
    shape = [[len(p)] for p in pb[1]]
    if [[len(p)] for p in pm[1]] != shape or [[len(p)] for p in pt[1]] != shape:
        return theirs, False
    closed = QgsWkbTypes.geometryType(t) == QgsWkbTypes.PolygonGeometry
    parts = []
    for b_part, m_part, t_part in zip(pb[1], pm[1], pt[1]):
        rings = []
        for b, m, th in zip(b_part, m_part, t_part):
            merged = merge_sequence(_ring(b, closed), _ring(m, closed), _ring(th, closed))
            if merged is None or (closed and len(merged) < 3):
                return theirs, False
            if closed:
                merged = merged + merged[:1]
            rings.append(merged)
        parts.append(rings)
    wkt = _wkt(t, parts)
    g = QgsGeometry.fromWkt(wkt) if wkt else QgsGeometry()
    if g.isNull():
        return theirs, False
    if closed and gm.isGeosValid() and gt.isGeosValid() and not g.isGeosValid():
        return theirs, False
    return _hex(g), True


def merge_feature(base, mine, theirs, rules=None, mine_rank=99, theirs_rank=99):
    """(merged state, conflicts). conflicts: [{"what": "geometry"|"field", "field", "mine", "theirs"}].
    rules: {field: rule} of the room's signed merge constitution (rules.py); none = keep, today's behaviour."""
    from .rules import decide_field

    rules = rules or {}
    out = {"g": theirs.get("g"), "a": dict(theirs.get("a") or {})}
    conflicts = []
    ba, ma, ta = base.get("a") or {}, mine.get("a") or {}, theirs.get("a") or {}
    for name, value in ma.items():
        if name not in ta:
            continue  # the field is gone on the host
        b = ba.get(name)
        rule = rules.get(name, "keep")
        if value == b or value == ta[name]:
            continue
        if ta[name] == b:
            if rule == "ask":   # the feature changed meanwhile: this field is never joined by itself
                conflicts.append({"what": "field", "field": name, "mine": value, "theirs": ta[name]})
            else:
                out["a"][name] = value
        else:
            v, conflict = decide_field(rule, b, value, ta[name], mine_rank, theirs_rank)
            out["a"][name] = v
            if conflict:
                conflicts.append({"what": "field", "field": name, "mine": value, "theirs": ta[name]})
    g, ok = merge_geometry(base.get("g"), mine.get("g"), theirs.get("g"))
    out["g"] = g
    if not ok:
        conflicts.append({"what": "geometry", "field": "", "mine": mine.get("g"), "theirs": theirs.get("g")})
    return out, conflicts
