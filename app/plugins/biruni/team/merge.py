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
import math
from difflib import SequenceMatcher

from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes

NODE_TOL = 0.001  # default tolerance in metres (1 mm)


def layer_tolerance(crs=None, mm=1.0):
    """Tolerance in layer units for mm (default 1 mm).
    In geographic CRS (degrees like EPSG:4326), 1 mm is approx 1e-3 / 111320.0 deg (~8.98e-9 deg)."""
    m = mm / 1000.0
    if crs is not None and hasattr(crs, "isGeographic") and crs.isGeographic():
        return m / 111320.0
    return m


def _pt_dist(p1, p2):
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


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


def _split_hunk(i1, i2, new):
    """Split 1-to-1 vertex replacements into single-vertex hunks so independent corner moves merge."""
    if i2 - i1 == len(new) and i2 - i1 > 1:
        return [(i1 + k, i1 + k + 1, [new[k]]) for k in range(len(new))]
    return [(i1, i2, new)]


def _hunks(base, other):
    raw = [(i1, i2, other[j1:j2]) for tag, i1, i2, j1, j2 in
           SequenceMatcher(None, base, other, autojunk=False).get_opcodes() if tag != "equal"]
    out = []
    for i1, i2, new in raw:
        out.extend(_split_hunk(i1, i2, new))
    return out


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
    merged, node_conflicts = merge_sequence_nodes(base, mine, theirs)
    if node_conflicts:
        return None
    return merged


def merge_sequence_nodes(base, mine, theirs):
    """(merged_seq or None, [conflicting_nodes: {"base": p, "mine": pm, "theirs": pt}])."""
    hm, ht = _hunks(base, mine), _hunks(base, theirs)
    keep = []
    node_conflicts = []
    handled_m = set()
    for h in ht:
        same = [(idx, k) for idx, k in enumerate(hm) if (k[0], k[1]) == (h[0], h[1])]
        if same:
            midx, km = same[0]
            handled_m.add(midx)
            if km[2] == h[2]:
                keep.append(h)
            elif h[1] - h[0] == 1 and len(h[2]) == 1 and len(km[2]) == 1:
                keep.append(h)
                node_conflicts.append({"base": base[h[0]], "mine": km[2][0], "theirs": h[2][0]})
            else:
                return None, [{"base": None, "mine": km[2], "theirs": h[2]}]
            continue
        overlapping = [km for km in hm if _touch((h[0], h[1]), (km[0], km[1]))]
        if overlapping:
            return None, [{"base": None, "mine": km[2], "theirs": h[2]} for km in overlapping]
        keep.append(h)
    for idx, km in enumerate(hm):
        if idx not in handled_m:
            keep.append(km)
    keep.sort(key=lambda h: (h[0], h[1]))
    out, i = [], 0
    for i1, i2, new in keep:
        out.extend(base[i:i1])
        out.extend(new)
        i = i2
    out.extend(base[i:])
    return out, node_conflicts



def _ring(seq, closed):
    return seq[:-1] if closed and len(seq) > 1 and seq[0] == seq[-1] else seq


def merge_geometry(base, mine, theirs):
    """(WKB hex, True) merged, or (theirs, False) when the two edits cannot be joined."""
    hex_res, ok, _node_conflicts = merge_geometry_nodes(base, mine, theirs)
    return hex_res, ok


def merge_geometry_nodes(base, mine, theirs):
    """(WKB hex, ok, node_conflicts).
    When conflict shrinks to single nodes, returns (theirs_with_other_edits, False, node_conflicts)."""
    if mine == base or mine == theirs:
        return theirs, True, []
    if theirs == base:
        return mine, True, []
    gb, gm, gt = _geom(base), _geom(mine), _geom(theirs)
    if gb is None or gm is None or gt is None:
        return theirs, False, []
    pb, pm, pt = _parts(gb), _parts(gm), _parts(gt)
    if pb is None or pm is None or pt is None or not (pb[0] == pm[0] == pt[0]):
        return theirs, False, []
    t = pb[0]
    shape = [[len(p)] for p in pb[1]]
    if [[len(p)] for p in pm[1]] != shape or [[len(p)] for p in pt[1]] != shape:
        return theirs, False, []
    closed = QgsWkbTypes.geometryType(t) == QgsWkbTypes.PolygonGeometry
    parts = []
    all_node_conflicts = []
    for b_part, m_part, t_part in zip(pb[1], pm[1], pt[1]):
        rings = []
        for b, m, th in zip(b_part, m_part, t_part):
            merged, nc = merge_sequence_nodes(_ring(b, closed), _ring(m, closed), _ring(th, closed))
            if merged is None or (closed and len(merged) < 3):
                return theirs, False, []
            all_node_conflicts.extend(nc)
            if closed:
                merged = merged + merged[:1]
            rings.append(merged)
        parts.append(rings)
    wkt = _wkt(t, parts)
    g = QgsGeometry.fromWkt(wkt) if wkt else QgsGeometry()
    if g.isNull():
        return theirs, False, []
    if closed and gm.isGeosValid() and gt.isGeosValid() and not g.isGeosValid():
        return theirs, False, []
    ok = (len(all_node_conflicts) == 0)
    return _hex(g), ok, all_node_conflicts


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
    g, ok, node_confs = merge_geometry_nodes(base.get("g"), mine.get("g"), theirs.get("g"))
    out["g"] = g
    if not ok:
        conf_entry = {"what": "geometry", "field": "", "mine": mine.get("g"), "theirs": theirs.get("g")}
        if node_confs:
            conf_entry["nodes"] = node_confs
        conflicts.append(conf_entry)
    return out, conflicts

# ---- Shared-node geometry operations (idea I06 second piece) ------------------------------------------------
def _point_on_segment(pt, s1, s2, tol):
    """Distance from pt to segment (s1, s2) <= tol and strictly between endpoints."""
    dx, dy = s2[0] - s1[0], s2[1] - s1[1]
    l2 = dx * dx + dy * dy
    if l2 < tol * tol:
        return False
    t = ((pt[0] - s1[0]) * dx + (pt[1] - s1[1]) * dy) / l2
    if t <= 0.001 or t >= 0.999:
        return False  # too close to endpoints (already a vertex)
    proj_x, proj_y = s1[0] + t * dx, s1[1] + t * dy
    return _pt_dist(pt, (proj_x, proj_y)) <= tol


def node_moves(base_hex, new_hex, tol=NODE_TOL):
    """[(old_xy, new_xy)] for vertices moved from base to new.
    Matches moved vertices even if vertex count changed."""
    if not base_hex or not new_hex or base_hex == new_hex:
        return []
    gb, gn = _geom(base_hex), _geom(new_hex)
    if gb is None or gn is None:
        return []
    pb, pn = _parts(gb), _parts(gn)
    if pb is None or pn is None or pb[0] != pn[0]:
        return []
    shape_b = [[len(r) for r in part] for part in pb[1]]
    shape_n = [[len(r) for r in part] for part in pn[1]]
    moves = []
    seen = set()

    if shape_b == shape_n:
        for p_b, p_n in zip(pb[1], pn[1]):
            for r_b, r_n in zip(p_b, p_n):
                for pt_b, pt_n in zip(r_b, r_n):
                    if _pt_dist(pt_b, pt_n) > tol:
                        key = (round(pt_b[0], 6), round(pt_b[1], 6))
                        if key not in seen:
                            seen.add(key)
                            moves.append(((pt_b[0], pt_b[1]), (pt_n[0], pt_n[1])))
        return moves

    # Vertex count changed (points added or deleted): match unmatched moved vertices
    pts_b = [(v.x(), v.y()) for v in gb.vertices()]
    pts_n = [(v.x(), v.y()) for v in gn.vertices()]
    unmatched_b = []
    unmatched_n = list(pts_n)
    for pb_ in pts_b:
        match = next((pn_ for pn_ in unmatched_n if _pt_dist(pb_, pn_) <= tol), None)
        if match is not None:
            unmatched_n.remove(match)
        else:
            unmatched_b.append(pb_)
    # Find candidate moves from unmatched_b to closest remaining unmatched_n
    for pb_ in unmatched_b:
        key = (round(pb_[0], 6), round(pb_[1], 6))
        if key in seen:
            continue
        cands = sorted([(pn_, _pt_dist(pb_, pn_)) for pn_ in unmatched_n], key=lambda x: x[1])
        if cands and cands[0][1] > tol:
            best_n = cands[0][0]
            unmatched_n.remove(best_n)
            seen.add(key)
            moves.append((pb_, best_n))
    return moves


def move_nodes(hexwkb, moves, tol=NODE_TOL):
    """(new_hex, moved_count) with vertices in hexwkb matching old_xy in moves moved to new_xy."""
    if not hexwkb or not moves:
        return hexwkb, 0
    g = _geom(hexwkb)
    if g is None:
        return hexwkb, 0
    parsed = _parts(g)
    if parsed is None:
        return hexwkb, 0
    t, parts = parsed
    closed = QgsWkbTypes.geometryType(t) == QgsWkbTypes.PolygonGeometry
    moved_count = 0
    new_parts = []
    for part in parts:
        new_part = []
        for ring in part:
            new_ring = []
            for p in ring:
                cur_pt = (p[0], p[1])
                target = None
                for old_xy, new_xy in moves:
                    if _pt_dist(cur_pt, old_xy) <= tol:
                        target = new_xy
                        break
                if target is not None:
                    new_ring.append(target + p[2:])
                    moved_count += 1
                else:
                    new_ring.append(p)
            if closed and len(new_ring) > 1 and new_ring[0] != new_ring[-1]:
                new_ring[-1] = new_ring[0]
            new_part.append(new_ring)
        new_parts.append(new_part)
    if not moved_count:
        return hexwkb, 0
    wkt = _wkt(t, new_parts)
    new_g = QgsGeometry.fromWkt(wkt) if wkt else None
    if new_g is None or new_g.isNull():
        return hexwkb, 0
    if closed and g.isGeosValid() and not new_g.isGeosValid():
        return hexwkb, 0
    return _hex(new_g), moved_count


def insert_and_move_node_on_edge(hexwkb, old_xy, new_xy, tol=NODE_TOL):
    """(new_hex, True) if old_xy was a T-junction on an edge and was split and moved to new_xy."""
    if not hexwkb:
        return hexwkb, False
    g = _geom(hexwkb)
    if g is None:
        return hexwkb, False
    parsed = _parts(g)
    if parsed is None:
        return hexwkb, False
    t, parts = parsed
    closed = QgsWkbTypes.geometryType(t) == QgsWkbTypes.PolygonGeometry
    split = False
    new_parts = []
    for part in parts:
        new_part = []
        for ring in part:
            new_ring = []
            n_pts = len(ring)
            for i in range(n_pts - 1 if closed else n_pts):
                p1 = ring[i]
                new_ring.append(p1)
                p2 = ring[(i + 1) % n_pts] if closed else (ring[i + 1] if i + 1 < n_pts else None)
                if p2 is not None and not split and _point_on_segment(old_xy, (p1[0], p1[1]), (p2[0], p2[1]), tol):
                    new_ring.append(new_xy + p1[2:])
                    split = True
            if closed:
                new_ring.append(new_ring[0])
            new_part.append(new_ring)
        new_parts.append(new_part)
    if not split:
        return hexwkb, False
    wkt = _wkt(t, new_parts)
    new_g = QgsGeometry.fromWkt(wkt) if wkt else None
    if new_g is None or new_g.isNull():
        return hexwkb, False
    if closed and g.isGeosValid() and not new_g.isGeosValid():
        return hexwkb, False
    return _hex(new_g), True


def pin_nodes(hexwkb, pinned_pts, tol=NODE_TOL):
    """(new_hex, count) restores any vertices near pinned_pts to their original pinned_pts."""
    moves = [((p[0], p[1]), (p[0], p[1])) for p in pinned_pts]
    return move_nodes(hexwkb, moves, tol=tol)


