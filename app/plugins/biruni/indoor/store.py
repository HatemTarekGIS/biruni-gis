"""indoor.gpkg, the Apple IMDF archive and the summary report.

GeoPackage contract (the phone viewer reads these names - do not rename):
  levels    Polygon     level_id int, name text, elevation_m real
  units     Polygon     unit_id text, level_id int, name, number, category text, area_m2 real
  openings  LineString  opening_id text, level_id int, category text, width_m real, unit_a, unit_b text
  nav_nodes Point       node_id text, level_id int, kind text (unit, door, junction, vertical), unit_id text
  nav_edges LineString  from_node, to_node text, level_id int (-999 between levels), length_m real,
                        kind text (walk, door, stairs, elevator, ramp), accessible bool
"""
import datetime
import json
import os
import uuid
import zipfile

from ..i18n import tr
from .graph import VERTICAL, components

IMDF_UNIT = {"room": "room", "office": "office", "classroom": "classroom", "lab": "laboratory",
             "corridor": "walkway", "lobby": "lobby", "stairs": "stairs", "elevator": "elevator", "ramp": "ramp",
             "restroom": "restroom", "kitchen": "kitchen", "storage": "storage", "parking": "parking",
             "shaft": "structure", "other": "unspecified"}
IMDF_DOOR = {"door": ("pedestrian", {"type": "swinging", "automatic": False, "material": None}),
             "double_door": ("pedestrian", {"type": "swinging", "automatic": False, "material": None}),
             "sliding_door": ("pedestrian", {"type": "sliding", "automatic": False, "material": None}),
             "opening": ("pedestrian", None),
             "emergency_exit": ("emergencyexit", {"type": "swinging", "automatic": False, "material": None})}


def level_prefix(level_id):
    return f"L{level_id}"


def default_level_name(level_id):
    if level_id == 0:
        return "Ground"
    return f"Level {level_id}" if level_id > 0 else f"Basement {-level_id}"


def assign_ids(level_id, res):
    """Stable ids: units L<level>-<room number> (L<level>-U017 without a number), openings L<level>-D005;
    numbered top-left to bottom-right."""
    pre = level_prefix(level_id)

    def key(g):
        p = g.pointOnSurface().asPoint()
        return (-round(p.y(), 1), round(p.x(), 1))

    order = sorted(range(len(res.units)), key=lambda i: key(res.units[i].geom))
    used, k = set(), 0
    for i in order:
        s = res.units[i]
        if s.number:
            uid = f"{pre}-{s.number}"
            n = 2
            while uid in used:
                uid = f"{pre}-{s.number}-{n}"
                n += 1
        else:
            k += 1
            uid = f"{pre}-U{k:03d}"
            while uid in used:
                k += 1
                uid = f"{pre}-U{k:03d}"
        used.add(uid)
        s.unit_id = uid
        s.node_id = f"N-{uid}"
    from qgis.core import QgsGeometry, QgsPointXY
    dorder = sorted(range(len(res.doors)), key=lambda i: key(QgsGeometry.fromPointXY(QgsPointXY(*res.doors[i].mid))))
    for n, i in enumerate(dorder, 1):
        d = res.doors[i]
        d.opening_id = f"{pre}-D{n:03d}"
        d.node_id = f"D-{d.opening_id}"


def _largest(g):
    if g.isMultipart():
        parts = g.asGeometryCollection()
        if parts:
            return max(parts, key=lambda p: p.area())
    return g


def _polygon_parts(g):
    return [p for p in g.asGeometryCollection() if p.area() > 0] if g.isMultipart() else [g]


# ---- GeoPackage ------------------------------------------------------------------------------------------
def write_gpkg(path, crs, levels, nodes, edges):
    """levels: [(level_id, name, elevation, LevelResult)]."""
    from osgeo import ogr, osr

    ogr.UseExceptions()
    if os.path.exists(path):
        os.remove(path)
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs.authid() if crs.authid() else crs.toWkt())
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)

    def layer(name, gtype, fields):
        lyr = ds.CreateLayer(name, srs, gtype, ["FID=fid", "SPATIAL_INDEX=YES"])
        for fname, ftype in fields:
            fd = ogr.FieldDefn(fname, {"s": ogr.OFTString, "i": ogr.OFTInteger, "d": ogr.OFTReal,
                                       "b": ogr.OFTInteger}[ftype])
            if ftype == "b":
                fd.SetSubType(ogr.OFSTBoolean)
            lyr.CreateField(fd)
        return lyr

    def add(lyr, wkb, values):
        f = ogr.Feature(lyr.GetLayerDefn())
        for k, v in values.items():
            if v is None:
                f.SetFieldNull(k)
            else:
                f.SetField(k, v)
        f.SetGeometry(ogr.CreateGeometryFromWkb(bytes(wkb)))
        lyr.CreateFeature(f)

    ds.StartTransaction()
    lv = layer("levels", ogr.wkbPolygon, [("level_id", "i"), ("name", "s"), ("elevation_m", "d")])
    un = layer("units", ogr.wkbPolygon, [("unit_id", "s"), ("level_id", "i"), ("name", "s"), ("number", "s"),
                                         ("category", "s"), ("area_m2", "d")])
    op = layer("openings", ogr.wkbLineString, [("opening_id", "s"), ("level_id", "i"), ("category", "s"),
                                               ("width_m", "d"), ("unit_a", "s"), ("unit_b", "s")])
    nn = layer("nav_nodes", ogr.wkbPoint, [("node_id", "s"), ("level_id", "i"), ("kind", "s"), ("unit_id", "s")])
    ne = layer("nav_edges", ogr.wkbLineString, [("from_node", "s"), ("to_node", "s"), ("level_id", "i"),
                                                ("length_m", "d"), ("kind", "s"), ("accessible", "b")])
    from qgis.core import QgsGeometry, QgsPointXY

    for level_id, name, elev, res in levels:
        if res.footprint is not None and not res.footprint.isEmpty():
            for part in _polygon_parts(res.footprint):
                add(lv, part.asWkb(), {"level_id": level_id, "name": name, "elevation_m": float(elev)})
        for s in res.units:
            g = _largest(s.geom)
            add(un, g.asWkb(), {"unit_id": s.unit_id, "level_id": level_id, "name": s.name or None,
                                "number": s.number or None, "category": s.category,
                                "area_m2": round(s.geom.area(), 2)})
        for d in res.doors:
            ua = res.units[d.unit_a].unit_id if d.unit_a >= 0 else ""
            ub = res.units[d.unit_b].unit_id if d.unit_b >= 0 else ""
            add(op, d.geometry.asWkb(), {"opening_id": d.opening_id, "level_id": level_id, "category": d.category,
                                         "width_m": round(d.width, 3), "unit_a": ua, "unit_b": ub})
    for nid, n in nodes.items():
        add(nn, QgsGeometry.fromPointXY(QgsPointXY(n["x"], n["y"])).asWkb(),
            {"node_id": nid, "level_id": n["level_id"], "kind": n["kind"], "unit_id": n["unit_id"] or ""})
    for e in edges:
        pts = e["pts"] if len(e["pts"]) >= 2 else e["pts"] * 2
        add(ne, QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts]).asWkb(),
            {"from_node": e["from_node"], "to_node": e["to_node"], "level_id": e["level_id"],
             "length_m": round(e["length_m"], 3), "kind": e["kind"], "accessible": 1 if e["accessible"] else 0})
    ds.CommitTransaction()
    ds = None
    return path


# ---- IMDF --------------------------------------------------------------------------------------------------
def imdf_problem(crs):
    """Why IMDF cannot be written for this CRS (None when it can)."""
    if crs is None or not crs.isValid():
        return tr("the drawing has no coordinate system, so its place on Earth is unknown")
    if crs.isGeographic():
        return tr("the coordinate system is in degrees; a floor plan must be in a projected system in metres")
    if not crs.authid() and "LOCAL_CS" in crs.toWkt().upper():
        return tr("the drawing is in local (engineering) coordinates; move it to real coordinates first")
    return None


def write_imdf(path, crs, levels, venue_name, venue_category="businesscampus"):
    """Apple IMDF 1.0 archive (manifest, venue, building, level, unit, opening) in WGS 84.
    Returns None when written, else the reason it was skipped."""
    from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject

    why = imdf_problem(crs)
    if why:
        return why
    ct = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    ns = uuid.uuid5(uuid.NAMESPACE_URL, f"biruni-indoor:{venue_name}")

    def uid(kind, key):
        return str(uuid.uuid5(ns, f"{kind}/{key}"))

    def geo(g):
        g = QgsGeometry(g)
        g.transform(ct)
        if hasattr(g, "forcePolygonCounterClockwise") and g.type() == Qgis.GeometryType.Polygon:
            g = g.forcePolygonCounterClockwise()
        return json.loads(g.asJson(8))

    def point(g):
        p = g.pointOnSurface()
        p.transform(ct)
        q = p.asPoint()
        return {"type": "Point", "coordinates": [round(q.x(), 8), round(q.y(), 8)]}

    def label(text):
        if not text:
            return None
        from ..arabic_text import has_arabic
        return {"ar" if has_arabic(text) else "en": text}

    def fc(feats):
        return {"type": "FeatureCollection", "features": feats}

    feats = {k: [] for k in ("venue", "building", "level", "unit", "opening")}
    footprints = [res.footprint for _l, _n, _e, res in levels if res.footprint is not None and not res.footprint.isEmpty()]
    if not footprints:
        return tr("no level outline was recognised")
    venue_geom = QgsGeometry.unaryUnion(footprints)
    venue_id, building_id = uid("venue", "venue"), uid("building", "building")
    feats["venue"].append({"id": venue_id, "type": "Feature", "feature_type": "venue", "geometry": geo(venue_geom),
                           "properties": {"category": venue_category, "restriction": None,
                                          "name": label(venue_name) or {"en": "Venue"}, "alt_name": None,
                                          "hours": None, "phone": None, "website": None,
                                          "display_point": point(venue_geom), "address_id": None}})
    feats["building"].append({"id": building_id, "type": "Feature", "feature_type": "building", "geometry": None,
                              "properties": {"name": label(venue_name), "alt_name": None,
                                             "category": "unspecified", "restriction": None,
                                             "display_point": point(venue_geom), "address_id": None}})
    for level_id, name, _elev, res in levels:
        if res.footprint is None or res.footprint.isEmpty():
            continue
        lid = uid("level", level_id)
        feats["level"].append({"id": lid, "type": "Feature", "feature_type": "level", "geometry": geo(res.footprint),
                               "properties": {"category": "unspecified", "restriction": None, "outdoor": False,
                                              "ordinal": int(level_id), "name": {"en": name},
                                              "short_name": {"en": str(level_id)},
                                              "display_point": point(res.footprint), "address_id": None,
                                              "building_ids": [building_id]}})
        for s in res.units:
            feats["unit"].append({"id": uid("unit", s.unit_id), "type": "Feature", "feature_type": "unit",
                                  "geometry": geo(_largest(s.geom)),
                                  "properties": {"category": IMDF_UNIT.get(s.category, "unspecified"),
                                                 "restriction": None, "accessibility": None,
                                                 "name": label(" ".join(x for x in (s.name, s.number) if x)),
                                                 "alt_name": label(s.unit_id), "level_id": lid,
                                                 "display_point": point(s.geom)}})
        for d in res.doors:
            cat, door = IMDF_DOOR.get(d.category, ("pedestrian", None))
            feats["opening"].append({"id": uid("opening", d.opening_id), "type": "Feature",
                                     "feature_type": "opening", "geometry": geo(d.geometry),
                                     "properties": {"category": cat, "accessibility": None, "access_control": None,
                                                    "door": door, "name": None, "alt_name": None,
                                                    "display_point": point(d.geometry), "level_id": lid}})
    manifest = {"version": "1.0.0", "created": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "generated_by": "Biruni GIS", "language": "en", "extensions": None}
    if os.path.exists(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for k, v in feats.items():
            z.writestr(f"{k}.geojson", json.dumps(fc(v), ensure_ascii=False))
    return None


# ---- report -----------------------------------------------------------------------------------------------------
def report(levels, nodes, edges, min_door_width, extra=()):
    """Plain-text summary: what was found per level, and what to check by hand."""
    out = [tr("Indoor map report"), "=" * 40]
    from .recognize import UNIT_CATEGORIES

    total_area = 0.0
    for level_id, name, elev, res in levels:
        area = sum(s.geom.area() for s in res.units)
        total_area += area
        out.append("")
        out.append(tr("Level {l} - {n} (elevation {e} m)").format(l=level_id, n=name, e=f"{elev:g}"))
        out.append("  " + tr("{u} units, {a} m², {d} doors, {o} plain openings, {s} thin wall faces dropped").format(
            u=len(res.units), a=f"{area:.1f}", d=res.stats.get("doors", 0), o=res.stats.get("openings", 0),
            s=res.stats.get("slivers_dropped", 0)))
        counts = {}
        for s in res.units:
            counts[s.category] = counts.get(s.category, 0) + 1
        out.append("  " + ", ".join(f"{c}: {counts[c]}" for c in UNIT_CATEGORIES if c in counts))
        noname = [s.unit_id for s in res.units if not s.name and not s.number]
        if noname:
            out.append("  " + tr("Units without a name or number ({n}): {ids}").format(n=len(noname), ids=", ".join(noname)))
        nodoor = [s.unit_id for s in res.units if not s.doors and s.category not in ("shaft",)]
        if nodoor:
            out.append("  " + tr("Units without any door ({n}): {ids}").format(n=len(nodoor), ids=", ".join(nodoor)))
        loose = [d.opening_id for d in res.doors if d.unit_a < 0]
        if loose:
            out.append("  " + tr("Doors not attached to any unit ({n}): {ids}").format(n=len(loose), ids=", ".join(loose)))
        outside = [d.opening_id for d in res.doors if d.unit_a >= 0 and d.unit_b < 0]
        if outside:
            out.append("  " + tr("Doors to the outside ({n}): {ids}").format(n=len(outside), ids=", ".join(outside)))
        narrow = [d.opening_id for d in res.doors if d.width + 1e-9 < min_door_width]
        if narrow:
            out.append("  " + tr("Doors narrower than {w} m (not accessible) ({n}): {ids}").format(
                w=f"{min_door_width:g}", n=len(narrow), ids=", ".join(narrow)))
        for w in res.warnings:
            out.append("  ! " + w)
    out.append("")
    out.append(tr("Total: {u} units, {a} m²").format(u=sum(len(r.units) for *_x, r in levels), a=f"{total_area:.1f}"))
    unit_nodes = [n for n, v in nodes.items() if v["kind"] in ("unit", "vertical")]
    linked = {e["from_node"] for e in edges} | {e["to_node"] for e in edges}
    groups = components([n for n in nodes if n in linked], edges)    # spaces without doors are listed above
    if len(groups) <= 1:
        out.append(tr("Navigation graph: {n} nodes, {e} edges, all connected.").format(n=len(nodes), e=len(edges)))
    else:
        main = max(groups, key=len)
        cut = sorted(nodes[n]["unit_id"] for g in groups if g is not main for n in g if n in unit_nodes)
        out.append(tr("Navigation graph: {n} nodes, {e} edges, {g} separate parts; units not reachable from the "
                      "largest part: {ids}").format(n=len(nodes), e=len(edges), g=len(groups),
                                                     ids=", ".join(cut) or "-"))
    vertical = sum(1 for e in edges if e["level_id"] == VERTICAL)
    if len(levels) > 1:
        out.append(tr("Links between levels (stairs, elevators, ramps): {n}").format(n=vertical))
        if not vertical:
            out.append("  ! " + tr("No stairs or elevator overlap between levels: the levels are not linked. Check "
                          "that the plans are drawn in the same coordinates."))
    for w in extra:
        out.append("! " + w)
    return "\n".join(out)
