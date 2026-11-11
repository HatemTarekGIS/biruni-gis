"""Navigation graph of an indoor map, and shortest routes.

Inside every space the graph is a small visibility graph: the space's own node (a point surely inside it,
pointOnSurface, so concave rooms are fine), one node per door (on the door line, entered through a point
just inside the space), and "junction" nodes just inside every reflex corner, so walking round an L-shaped
corridor follows the corner instead of cutting through a wall. Every pair of these nodes that sees each
other inside the space is joined by a straight edge with its metric length. Along a corridor this gives
door-to-door edges that follow the corridor (not via its centre), which is the medial-like path the phone
needs without computing a skeleton.

Stairs, elevators and ramps that overlap in plan on consecutive levels are joined by vertical edges
(level_id -999). Lengths of vertical edges: elevator = storey height, stairs = 2 x height, ramp = 12 x height
(1:12), so routes prefer what a person would.
"""
import heapq
import math

VERTICAL = -999
JUNCTION_OFFSET = 0.35
MAX_JUNCTIONS = 40


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _length(pts):
    return sum(_d(a, b) for a, b in zip(pts, pts[1:]))


def _junctions(geom):
    """Points just inside the reflex corners of a polygon (exterior and holes)."""
    from qgis.core import QgsGeometry, QgsPointXY

    eng = QgsGeometry.createGeometryEngine(geom.constGet())
    eng.prepareGeometry()

    def inside(p):
        g = QgsGeometry.fromPointXY(QgsPointXY(*p))   # keep the geometry alive while GEOS reads it
        return eng.contains(g.constGet())

    out = []
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    for poly in polys:
        for ring in poly:
            pts = [(p.x(), p.y()) for p in ring[:-1]]
            n = len(pts)
            for i in range(n):
                v, a, b = pts[i], pts[i - 1], pts[(i + 1) % n]
                la, lb = _d(a, v), _d(b, v)
                if la < 1e-9 or lb < 1e-9:
                    continue
                ux, uy = (a[0] - v[0]) / la + (b[0] - v[0]) / lb, (a[1] - v[1]) / la + (b[1] - v[1]) / lb
                L = math.hypot(ux, uy)
                if L < 1e-6:
                    continue
                ux, uy = ux / L, uy / L
                plus = (v[0] + ux * JUNCTION_OFFSET, v[1] + uy * JUNCTION_OFFSET)
                minus = (v[0] - ux * JUNCTION_OFFSET * 1.4, v[1] - uy * JUNCTION_OFFSET * 1.4)
                if not inside(plus) and inside(minus):      # reflex corner: inside is away from the edges
                    out.append(minus)
                if len(out) >= MAX_JUNCTIONS:
                    return out
    return out


def build(levels, min_door_width=0.9, storey=3.5):
    """levels: [(level_id, elevation, LevelResult)] with unit_id / opening_id set.
    Returns (nodes, edges): nodes {id: dict(level_id, kind, unit_id, x, y)},
    edges [dict(from_node, to_node, level_id, length_m, kind, accessible, pts)]."""
    from qgis.core import QgsGeometry, QgsPointXY

    nodes, edges = {}, []

    def edge(a, b, level, kind, accessible, pts, length=None):
        edges.append({"from_node": a, "to_node": b, "level_id": level, "kind": kind, "accessible": accessible,
                      "pts": pts, "length_m": _length(pts) if length is None else length})

    for level_id, _elev, res in levels:
        for d in res.doors:
            nodes[d.node_id] = {"level_id": level_id, "kind": "door", "unit_id": "", "x": d.mid[0], "y": d.mid[1]}
        for u, s in enumerate(res.units):
            c = s.geom.pointOnSurface().asPoint()
            nid = s.node_id
            vertical = s.category in ("stairs", "elevator", "ramp")
            nodes[nid] = {"level_id": level_id, "kind": "vertical" if vertical else "unit", "unit_id": s.unit_id,
                          "x": c.x(), "y": c.y()}
            local = [(nid, (c.x(), c.y()), None)]          # (node id, point inside, door)
            for k in s.doors:
                d = res.doors[k]
                local.append((d.node_id, d.access.get(u, d.mid), d))
            for j, p in enumerate(_junctions(s.geom)):
                jid = f"J-{s.unit_id}-{j + 1}"
                nodes[jid] = {"level_id": level_id, "kind": "junction", "unit_id": s.unit_id, "x": p[0], "y": p[1]}
                local.append((jid, p, None))
            body = s.geom.buffer(0.02, 2)
            eng = QgsGeometry.createGeometryEngine(body.constGet())
            eng.prepareGeometry()
            linked = set()
            for i in range(len(local)):
                for j in range(i + 1, len(local)):
                    (na, pa, da), (nb, pb, db) = local[i], local[j]
                    if na == nb:
                        continue
                    if _d(pa, pb) > 1e-9:
                        seg = QgsGeometry.fromPolylineXY([QgsPointXY(*pa), QgsPointXY(*pb)])
                        if not eng.contains(seg.constGet()):
                            continue
                    pts = ([da.mid] if da else []) + [pa, pb] + ([db.mid] if db else [])
                    pts = [p for k2, p in enumerate(pts) if k2 == 0 or _d(p, pts[k2 - 1]) > 1e-9]
                    if len(pts) < 2:
                        pts = [pa, pb]
                    doors = [x for x in (da, db) if x]
                    acc = all(x.width + 1e-9 >= min_door_width for x in doors)
                    edge(na, nb, level_id, "door" if doors else "walk", acc, pts)
                    linked.update({na, nb})
            for nid2, p, d in local[1:]:
                if d is not None and nid2 not in linked:      # never leave a door unreachable
                    edge(nid2, nid, level_id, "door", d.width + 1e-9 >= min_door_width, [d.mid, p, (c.x(), c.y())])
    # vertical links
    order = sorted(levels, key=lambda t: t[0])
    for (la, ea, ra), (lb, eb, rb) in zip(order, order[1:]):
        dh = abs(eb - ea) or storey
        for sa in ra.units:
            if sa.category not in ("stairs", "elevator", "ramp"):
                continue
            for sb in rb.units:
                if sb.category != sa.category or not sa.geom.boundingBox().intersects(sb.geom.boundingBox()):
                    continue
                inter = sa.geom.intersection(sb.geom).area()
                if inter < 0.2 * min(sa.geom.area(), sb.geom.area()):
                    continue
                factor = {"stairs": 2.0, "elevator": 1.0, "ramp": 12.0}[sa.category]
                pa, pb = nodes[sa.node_id], nodes[sb.node_id]
                edge(sa.node_id, sb.node_id, VERTICAL, sa.category, sa.category != "stairs",
                     [(pa["x"], pa["y"]), (pb["x"], pb["y"])], length=dh * factor)
    return nodes, edges


# ---- shortest paths ------------------------------------------------------------------------------------------
def dijkstra(edges, start, goal, accessible_only=False):
    """edges: iterable of (from, to, length, accessible, payload). Returns (node list, length, payloads)."""
    adj = {}
    for a, b, length, acc, payload in edges:
        if accessible_only and not acc:
            continue
        adj.setdefault(a, []).append((b, length, payload))
        adj.setdefault(b, []).append((a, length, payload))
    if start == goal:
        return [start], 0.0, []
    dist, prev = {start: 0.0}, {}
    heap = [(0.0, start)]
    while heap:
        du, u = heapq.heappop(heap)
        if u == goal:
            break
        if du > dist.get(u, math.inf):
            continue
        for v, w, payload in adj.get(u, ()):
            nd = du + w
            if nd < dist.get(v, math.inf) - 1e-12:
                dist[v] = nd
                prev[v] = (u, payload)
                heapq.heappush(heap, (nd, v))
    if goal not in dist:
        return None, None, None
    path, payloads, n = [goal], [], goal
    while n != start:
        n, payload = prev[n]
        path.append(n)
        payloads.append(payload)
    return path[::-1], dist[goal], payloads[::-1]


def components(node_ids, edges):
    """Connected groups of node ids."""
    parent = {n: n for n in node_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        a, b = e["from_node"], e["to_node"]
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    groups = {}
    for n in node_ids:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def _read(gpkg):
    from osgeo import ogr

    ds = ogr.Open(gpkg)
    if ds is None:
        raise OSError(gpkg)
    units = []
    for f in ds.GetLayerByName("units"):
        units.append({k: f.GetField(k) for k in ("unit_id", "level_id", "name", "number", "category")})
    nodes = {}
    for f in ds.GetLayerByName("nav_nodes"):
        g = f.GetGeometryRef()
        nodes[f.GetField("node_id")] = {"level_id": f.GetField("level_id"), "kind": f.GetField("kind"),
                                        "unit_id": f.GetField("unit_id"), "x": g.GetX(), "y": g.GetY()}
    edges = []
    for f in ds.GetLayerByName("nav_edges"):
        g = f.GetGeometryRef()
        pts = [(g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount())] if g else []
        edges.append((f.GetField("from_node"), f.GetField("to_node"), f.GetField("length_m"),
                      bool(f.GetField("accessible")),
                      {"from": f.GetField("from_node"), "to": f.GetField("to_node"), "kind": f.GetField("kind"),
                       "level_id": f.GetField("level_id"), "length_m": f.GetField("length_m"), "pts": pts}))
    ds = None
    return units, nodes, edges


def resolve_unit(units, ref):
    """unit_id, else room number, else name (case/diacritics-insensitive); raises KeyError."""
    from ..arabic_text import normalise

    ref = str(ref or "").strip()
    for u in units:
        if u["unit_id"] == ref:
            return u["unit_id"]
    for key in ("unit_id", "number", "name"):
        hits = [u["unit_id"] for u in units if u[key] and normalise(u[key]) == normalise(ref)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise KeyError(f"{ref}: {len(hits)} units ({', '.join(hits[:5])})")
    raise KeyError(ref)


def find_route(gpkg, from_unit, to_unit, accessible_only=False):
    """dict(from_unit, to_unit, nodes, length, steps=[edge dicts]) or None when there is no route."""
    units, nodes, edges = _read(gpkg)
    a, b = resolve_unit(units, from_unit), resolve_unit(units, to_unit)
    start = next((n for n, v in nodes.items() if v["unit_id"] == a and v["kind"] in ("unit", "vertical")), None)
    goal = next((n for n, v in nodes.items() if v["unit_id"] == b and v["kind"] in ("unit", "vertical")), None)
    if start is None or goal is None:
        return None
    path, length, steps = dijkstra(edges, start, goal, accessible_only)
    if path is None:
        return None
    oriented = []
    for n, step in zip(path, steps):
        pts = step["pts"]
        if step["from"] != n:
            pts = pts[::-1]
        oriented.append({**step, "pts": pts})
    return {"from_unit": a, "to_unit": b, "nodes": path, "length": length, "steps": oriented,
            "node_info": {n: nodes[n] for n in path}}


def route(gpkg, from_unit, to_unit, accessible_only=False):
    """Shortest route between two units of an indoor.gpkg: (list of node ids, total length in metres);
    ([], None) when there is none."""
    r = find_route(gpkg, from_unit, to_unit, accessible_only)
    if r is None:
        return [], None
    return r["nodes"], r["length"]
