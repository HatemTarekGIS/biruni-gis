r"""From floor plan to indoor map (indoor/): two synthetic architectural DXF plans -> indoor.gpkg, IMDF, report.

Run:  engine\bin\python-qgis.bat tests\test_indoor.py

The plans are written here as real DXF text: thick walls drawn as closed polylines with door gaps, door
swing arcs drawn in model space and as DOOR blocks (rotated and mirrored), a double entrance door, a
sliding elevator door block, a plain opening without any door symbol, stair treads, furniture, an area
text, Arabic labels in Windows-1256, an Arabic label in the xarb.shx keyboard font, an MTEXT label and a
room-tag block with attributes.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem, QgsGeometry  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from biruni.indoor import cad, graph, pipeline, recognize as rec, store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- a tiny DXF writer -----------------------------------------------------------------------------------
X0, Y0 = 670000.0, 2730000.0          # UTM 38N, Riyadh
T = 0.2                              # wall thickness


class Dxf:
    def __init__(self, k=1.0, insunits=6):
        self.ents, self.blocks = [], []
        self.k, self.insunits = k, insunits      # k: drawing units per metre (1000 = a plan drawn in mm)

    def _p(self, *pairs):
        self.ents.extend(pairs)

    def line(self, p, q, layer):
        self._p((0, "LINE"), (8, layer), (10, p[0] + X0), (20, p[1] + Y0), (30, 0.0), (11, q[0] + X0),
                (21, q[1] + Y0), (31, 0.0))

    def poly(self, pts, layer, closed=True):
        self._p((0, "POLYLINE"), (8, layer), (66, 1), (10, 0.0), (20, 0.0), (30, 0.0), (70, 1 if closed else 0))
        for x, y in pts:
            self._p((0, "VERTEX"), (8, layer), (10, x + X0), (20, y + Y0), (30, 0.0))
        self._p((0, "SEQEND"), (8, layer))

    def arc(self, c, r, a1, a2, layer):
        self._p((0, "ARC"), (8, layer), (10, c[0] + X0), (20, c[1] + Y0), (30, 0.0), (40, r), (50, a1), (51, a2))

    def text(self, p, s, layer="A-ANNO-TEXT", style="STANDARD", h=0.3):
        self._p((0, "TEXT"), (8, layer), (7, style), (10, p[0] + X0), (20, p[1] + Y0), (30, 0.0), (40, h), (1, s))

    def mtext(self, p, s, layer="A-ANNO-TEXT", h=0.3):
        self._p((0, "MTEXT"), (8, layer), (10, p[0] + X0), (20, p[1] + Y0), (30, 0.0), (40, h), (1, s))

    def insert(self, name, p, layer, rot=0.0, sx=1.0, sy=1.0, attribs=None):
        pairs = [(0, "INSERT"), (8, layer), (2, name), (10, p[0] + X0), (20, p[1] + Y0), (30, 0.0), (41, sx),
                 (42, sy), (50, rot)]
        if attribs:
            pairs.insert(1, (66, 1))
        self._p(*pairs)
        for tag, val in (attribs or {}).items():
            self._p((0, "ATTRIB"), (8, layer), (10, p[0] + X0), (20, p[1] + Y0), (30, 0.0), (40, 0.3), (1, val),
                    (2, tag), (70, 0))
        if attribs:
            self._p((0, "SEQEND"), (8, layer))

    def save(self, path):
        out = [(0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1015"), (9, "$DWGCODEPAGE"), (3, "ANSI_1256"),
               (9, "$INSUNITS"), (70, self.insunits), (0, "ENDSEC"),
               (0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "STYLE"), (70, 2),
               (0, "STYLE"), (2, "STANDARD"), (70, 0), (40, 0.0), (41, 1.0), (3, "arial.ttf"),
               (0, "STYLE"), (2, "XARB"), (70, 0), (40, 0.0), (41, 1.0), (3, "xarb.shx"),
               (0, "ENDTAB"), (0, "ENDSEC"),
               (0, "SECTION"), (2, "BLOCKS")]
        # DOOR: hinge at the origin, closed leaf along +x, open leaf along +y, radius 1 (scaled to the width)
        out += [(0, "BLOCK"), (8, "0"), (2, "DOOR"), (70, 0), (10, 0.0), (20, 0.0), (30, 0.0), (3, "DOOR"),
                (0, "ARC"), (8, "0"), (10, 0.0), (20, 0.0), (30, 0.0), (40, 1.0), (50, 0.0), (51, 90.0),
                (0, "LINE"), (8, "0"), (10, 0.0), (20, 0.0), (30, 0.0), (11, 0.0), (21, 1.0), (31, 0.0),
                (0, "ENDBLK"), (8, "0")]
        # sliding door: two thin panels along +x over one unit
        out += [(0, "BLOCK"), (8, "0"), (2, "SLIDING_DOOR"), (70, 0), (10, 0.0), (20, 0.0), (30, 0.0),
                (3, "SLIDING_DOOR"),
                (0, "LWPOLYLINE"), (8, "0"), (90, 4), (70, 1), (10, 0.0), (20, -0.03), (10, 0.55), (20, -0.03),
                (10, 0.55), (20, 0.0), (10, 0.0), (20, 0.0),
                (0, "LWPOLYLINE"), (8, "0"), (90, 4), (70, 1), (10, 0.45), (20, 0.0), (10, 1.0), (20, 0.0),
                (10, 1.0), (20, 0.03), (10, 0.45), (20, 0.03),
                (0, "ENDBLK"), (8, "0")]
        out += [(0, "BLOCK"), (8, "0"), (2, "ROOMTAG"), (70, 2), (10, 0.0), (20, 0.0), (30, 0.0), (3, "ROOMTAG"),
                (0, "ATTDEF"), (8, "0"), (10, 0.0), (20, 0.0), (30, 0.0), (40, 0.3), (1, ""), (3, "Name"),
                (2, "NAME"), (70, 0), (0, "ENDBLK"), (8, "0")]
        ents = [(c, v * self.k if c in (10, 20, 11, 21, 40, 41, 42) and isinstance(v, float) else v)
                for c, v in self.ents]
        out += [(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")] + ents + [(0, "ENDSEC"), (0, "EOF")]
        lines = []
        for code, value in out:
            if isinstance(value, float):
                value = f"{value:.6f}"
            lines.append(f"{code:>3}\r\n{value}\r\n")
        with open(path, "wb") as f:
            f.write("".join(lines).encode("cp1256"))


def rect(x1, y1, x2, y2):
    return QgsGeometry.fromRect(__import__("qgis.core", fromlist=["QgsRectangle"]).QgsRectangle(x1, y1, x2, y2))


# cells between wall centre lines
SOUTH = {"A": (0, 0, 5, 5), "B": (5, 0, 8, 5), "C": (8, 0, 13, 5), "D": (13, 0, 16, 5), "E": (16, 0, 18.5, 5),
         "F": (18.5, 0, 20, 5)}
CORR = (0, 5, 20, 7)
NORTH = {"G": (0, 7, 12, 12), "H": (12, 7, 20, 12)}


def door(d, kind, wall_y, a, w, side, hinge_left=True, layer="A-DOOR"):
    """A door in a horizontal wall at y=wall_y over x a..a+w, swinging to side (+1 north / -1 south)."""
    hx = a if hinge_left else a + w
    hinge = (hx, wall_y + side * T / 2)
    closed = 0.0 if hinge_left else 180.0
    opened = 90.0 if side > 0 else 270.0
    if kind == "arc":
        a1, a2 = (closed, opened) if (opened - closed) % 360 == 90 else (opened, closed)
        d.arc(hinge, w, a1, a2, layer)
        o = math.radians(opened)
        d.line(hinge, (hinge[0] + w * math.cos(o), hinge[1] + w * math.sin(o)), layer)
    else:
        sy = w if (opened - closed) % 360 == 90 else -w
        d.insert("DOOR", hinge, layer, rot=closed, sx=w, sy=sy)


def make_plan(path, level, k=1.0, insunits=6):
    d = Dxf(k, insunits)
    gaps = []
    # doors: (kind, wall y, x, width, side, hinge left, layer)
    doors = [("arc", 5, 1.0, 0.8, -1, True, "A-DOOR"),            # office: 0.8 m, not accessible
             ("block", 5, 5.8, 0.9, 1, False, "A-DOOR"),           # stairs
             ("block", 5, 10.0, 1.0, -1, True, "A-DOOR"),          # lab
             ("arc", 5, 14.0, 0.9, 1, True, "0"),                  # restroom: arc + leaf on layer 0
             ("block", 7, 2.0, 1.0, 1, True, "A-DOOR"),            # lobby / classroom
             ("arc", 7, 15.0, 0.9, -1, False, "A-DOOR")]           # storage / kitchen
    for kind, wy, a, w, side, left, layer in doors:
        door(d, kind, wy, a, w, side, left, layer)
        gaps.append(rect(a, wy - T / 2, a + w, wy + T / 2))
    # elevator: sliding door block in the middle of the wall
    d.insert("SLIDING_DOOR", (16.6, 5.0), "A-DOOR", sx=1.0, sy=1.0)
    gaps.append(rect(16.6, 5 - T / 2, 17.6, 5 + T / 2))
    # plain opening between lobby/classroom and the corridor (no door symbol)
    gaps.append(rect(7.0, 7 - T / 2, 8.0, 7 + T / 2))
    if level == 0:   # double entrance door in the outer wall, two 0.8 leaves swinging in
        door(d, "arc", 12, 5.0, 0.8, -1, True, "A-DOOR")
        door(d, "arc", 12, 5.8, 0.8, -1, False, "A-DOOR")
        gaps.append(rect(5.0, 12 - T / 2, 6.6, 12 + T / 2))
    building = rect(-T / 2, -T / 2, 20 + T / 2, 12 + T / 2)
    rooms = [rect(x1 + T / 2, y1 + T / 2, x2 - T / 2, y2 - T / 2) for x1, y1, x2, y2 in
             list(SOUTH.values()) + [CORR] + list(NORTH.values())]
    wall = building.difference(QgsGeometry.unaryUnion(rooms + gaps))
    for part in wall.asGeometryCollection() if wall.isMultipart() else [wall]:
        for ring in part.asPolygon():
            d.poly([(p.x(), p.y()) for p in ring[:-1]], "A-WALL")
    # stair treads and a stair arrow, furniture, area text
    for k in range(10):
        d.line((5.3, 0.5 + 0.4 * k), (7.7, 0.5 + 0.4 * k), "A-STAIR")
    d.poly([(1, 1), (3, 1), (3, 2), (1, 2)], "A-FURN")
    d.poly([(9, 1), (12, 1), (12, 2), (9, 2)], "A-FURN")
    d.line((19, 0.2), (19, 4.8), "A-FURN")                       # a stray line in the shaft (furniture layer)
    f = level  # 1 or 2 hundreds
    d.text((2.0, 3.0), f"مكتب {f + 1}01")
    d.text((6.0, 3.5), "سلم")
    d.text((9.5, 3.0), f"Lab {f + 1}02")
    d.text((9.5, 2.4), "24.5 m²")
    d.text((16.8, 2.5), "مصعد")
    d.text((10.0, 6.0), "ممر")
    if level == 0:
        d.text((14.0, 3.0), "WC")
        d.text((5.0, 10.0), "بهو")
        d.text((15.0, 10.0), "lo.k", style="XARB")              # «مخزن» typed with the Arabic keyboard font
    else:
        d.text((14.0, 3.0), "حمام")
        d.insert("ROOMTAG", (5.0, 10.0), "A-ANNO-TAG", attribs={"NAME": "Classroom", "NUMBER": "203"})
        d.mtext((15.0, 10.0), "{\\fArial|b1;مطبخ}")
    d.save(path)


work = tempfile.mkdtemp()
p0, p1 = os.path.join(work, "plan_gf.dxf"), os.path.join(work, "plan_1.dxf")
make_plan(p0, 0)
make_plan(p1, 1)

# ---- words --------------------------------------------------------------------------------------------------
check(rec.layer_role("A-WALL") == "wall" and rec.layer_role("جدران") == "wall" and rec.layer_role("A-DOOR") == "door",
      "layer roles: walls and doors in English and Arabic")
check(rec.layer_role("A-FURN") == "ignore" and rec.layer_role("A-ANNO-TEXT") == "ignore", "furniture/annotation ignored")
check(rec.category_of("مكتب") == "office" and rec.category_of("دورة مياه") == "restroom"
      and rec.category_of("المصعد") == "elevator" and rec.category_of("Lab") == "lab"
      and rec.category_of("Label") is None and rec.category_of("قاعة المحاضرات") == "classroom",
      "categories from Arabic and English words")
check(rec.split_label("مكتب ١٠١") == ("مكتب", "101") and rec.split_label("Lab 102") == ("Lab", "102")
      and rec.split_label("24.5 m²") == ("", "") and rec.split_label("+3.50") == ("", ""),
      "room names and numbers; area and level texts ignored")
check(pipeline.parse_plans("a.dxf=0; b.dxf=1:First floor", "C:/x") ==
      [(os.path.join("C:/x", "a.dxf"), 0, ""), (os.path.join("C:/x", "b.dxf"), 1, "First floor")],
      "plan list 'file=level:name' parsed")

# ---- reading --------------------------------------------------------------------------------------------------
plan = cad.read_plan(p0)
check(plan.scale == 1.0, "drawing units from $INSUNITS (metres)")
block_arcs = [a for a in plan.arcs if a.block == "DOOR"]
check(len(block_arcs) == 3 and all(abs(a.sweep - 90) < 1e-6 for a in block_arcs),
      "door arcs read from the DOOR blocks at every insert (rotated and mirrored)")
check(any(t.text == "مخزن" for t in plan.texts), "xarb.shx keyboard-font text decoded to Arabic")

res0 = rec.recognize(plan)
check(len(res0.units) == 9, f"level 0: 9 spaces ({len(res0.units)})")
pmm = os.path.join(work, "plan_mm.dxf")
make_plan(pmm, 0, k=1000.0, insunits=0)          # drawn in millimetres, units not set in the header
plan_mm = cad.read_plan(pmm)
res_mm = rec.recognize(plan_mm)
check(plan_mm.scale == 0.001 and len(res_mm.units) == 9 and
      abs(max(s.geom.area() for s in res_mm.units) - max(s.geom.area() for s in res0.units)) < 0.01,
      f"a plan in millimetres is recognised as such ({plan_mm.scale}, {len(res_mm.units)} spaces)")
check(res0.stats["slivers_dropped"] >= 1, f"thin wall faces dropped ({res0.stats['slivers_dropped']})")

# ---- the whole pipeline -------------------------------------------------------------------------------------
out = os.path.join(work, "out")
crs = QgsCoordinateReferenceSystem("EPSG:32638")
result = pipeline.run([(p0, 0, ""), (p1, 1, "")], crs, out, min_door_width=0.9)
gpkg = result["gpkg"]
from osgeo import ogr  # noqa: E402

ds = ogr.Open(gpkg)
check(ds is not None and all(ds.GetLayerByName(n) is not None for n in
                             ("levels", "units", "openings", "nav_nodes", "nav_edges")), "indoor.gpkg has the 5 layers")


def rows(name):
    lyr = ds.GetLayerByName(name)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    out_rows = []
    for f in lyr:
        r = {n: f.GetField(n) for n in names}
        r["_geom"] = f.GetGeometryRef().ExportToWkt() if f.GetGeometryRef() else None
        r["_gtype"] = f.GetGeometryRef().GetGeometryName() if f.GetGeometryRef() else None
        out_rows.append(r)
    return names, out_rows


names, units = rows("units")
check(names == ["unit_id", "level_id", "name", "number", "category", "area_m2"], f"units fields {names}")
check(ds.GetLayerByName("units").GetSpatialRef().GetAuthorityCode(None) == "32638", "gpkg in the plan's CRS")
by = {(u["level_id"], u["category"]): u for u in units}
cats0 = sorted(u["category"] for u in units if u["level_id"] == 0)
cats1 = sorted(u["category"] for u in units if u["level_id"] == 1)
print("level 0:", cats0)
print("level 1:", cats1)
check(cats0 == sorted(["office", "stairs", "lab", "restroom", "elevator", "shaft", "corridor", "lobby", "storage"]),
      "level 0 categories")
check(cats1 == sorted(["office", "stairs", "lab", "restroom", "elevator", "shaft", "corridor", "classroom",
                       "kitchen"]), "level 1 categories")
ids = {u["unit_id"] for u in units}
check({"L0-101", "L0-102", "L1-201", "L1-202", "L1-203"} <= ids, f"unit ids from room numbers ({sorted(ids)})")
check(len(ids) == len(units), "unit ids are unique")
check(by[(0, "office")]["name"] == "مكتب" and by[(0, "office")]["number"] == "101", "Arabic name and number")
check(by[(0, "lab")]["name"] == "Lab" and by[(0, "lab")]["number"] == "102", "English name; area text not a number")
check(by[(1, "classroom")]["name"] == "Classroom" and by[(1, "classroom")]["number"] == "203",
      "name and number from block attributes")
check(by[(0, "storage")]["name"] == "مخزن" and by[(1, "kitchen")]["name"] == "مطبخ", "xarb and MTEXT labels")
check(by[(0, "shaft")]["unit_id"].startswith("L0-U"), "a space without a number gets L<level>-U<n>")
check(abs(by[(0, "office")]["area_m2"] - 4.8 * 4.8) < 0.05, f"office area {by[(0, 'office')]['area_m2']}")
check(all(u["_gtype"] == "POLYGON" for u in units), "units are polygons")
check(not any(u["area_m2"] < 1.0 for u in units), "no slivers among the units")
_, levels = rows("levels")
check(sorted((lv["level_id"], lv["name"], lv["elevation_m"]) for lv in levels) == [(0, "Ground", 0.0),
                                                                                (1, "Level 1", 3.5)],
      "levels with names and elevations")
check(abs(QgsGeometry.fromWkt(levels[0]["_geom"]).area() - 20.2 * 12.2) < 0.5, "level outline = building outline")

_, opens = rows("openings")
uid = {(u["level_id"], u["category"]): u["unit_id"] for u in units}


def opening(level, cat_a, cat_b):
    a, b = uid.get((level, cat_a)), uid.get((level, cat_b), "")
    return [o for o in opens if o["level_id"] == level and {o["unit_a"], o["unit_b"]} == {a, b}]


o = opening(0, "office", "corridor")
check(len(o) == 1 and o[0]["category"] == "door" and abs(o[0]["width_m"] - 0.8) < 0.01,
      f"office door 0.8 m between office and corridor {o}")
o = opening(0, "lab", "corridor")
check(len(o) == 1 and abs(o[0]["width_m"] - 1.0) < 0.01, "lab door from a rotated DOOR block, 1.0 m")
o = opening(0, "stairs", "corridor")
check(len(o) == 1 and abs(o[0]["width_m"] - 0.9) < 0.01, "stairs door from a mirrored DOOR block, 0.9 m")
o = opening(0, "elevator", "corridor")
check(len(o) == 1 and o[0]["category"] == "sliding_door" and abs(o[0]["width_m"] - 1.0) < 0.02,
      f"sliding elevator door {o}")
o = opening(0, "restroom", "corridor")
check(len(o) == 1 and abs(o[0]["width_m"] - 0.9) < 0.01, "restroom door: arc and leaf on layer 0")
o = opening(0, "lobby", "")
check(len(o) == 1 and o[0]["category"] == "double_door" and abs(o[0]["width_m"] - 1.6) < 0.01,
      f"double entrance door to the outside {o}")
o = [x for x in opening(0, "lobby", "corridor")]
check(sorted(x["category"] for x in o) == ["door", "opening"], f"lobby: a door and a plain opening {o}")
check(len([x for x in opens if x["level_id"] == 0]) == 9, f"9 openings on level 0 ({len([x for x in opens if x['level_id'] == 0])})")
check(len([x for x in opens if x["level_id"] == 1]) == 8, "8 openings on level 1")
check(all(x["unit_a"] for x in opens), "every opening is attached")

# ---- graph and routes -------------------------------------------------------------------------------------
linked = {e["from_node"] for e in result["edges"]} | {e["to_node"] for e in result["edges"]}
isolated = sorted(result["nodes"][n]["unit_id"] for n in result["nodes"] if n not in linked)
check(len(graph.components(sorted(linked), result["edges"])) == 1 and isolated == ["L0-U007", "L1-U006"],
      f"the navigation graph is connected; only the shafts (no door) stand alone {isolated}")
_, edges = rows("nav_edges")
vert = [e for e in edges if e["level_id"] == -999]
check(sorted(e["kind"] for e in vert) == ["elevator", "stairs"], f"stairs and elevator linked between levels {vert}")
check(all(e["accessible"] == 0 for e in edges if e["kind"] == "stairs") and
      all(e["accessible"] == 1 for e in edges if e["kind"] == "elevator"), "stairs not accessible, elevator is")
_, nodes = rows("nav_nodes")
check({n["kind"] for n in nodes} == {"unit", "door", "junction", "vertical"} or
      {n["kind"] for n in nodes} >= {"unit", "door", "vertical"}, f"node kinds {sorted({n['kind'] for n in nodes})}")

path, length = graph.route(gpkg, "بهو", "L1-202")
kinds = [s["kind"] for s in graph.find_route(gpkg, "بهو", "L1-202")["steps"]]
check(path and path[0] == "N-" + uid[(0, "lobby")] and path[-1] == "N-L1-202" and length > 0,
      f"route lobby -> Lab 202 across levels ({length and round(length, 1)} m)")
check("stairs" in kinds and "elevator" not in kinds, f"the shortest route takes the stairs {kinds}")
r = graph.find_route(gpkg, "بهو", "202", accessible_only=True)
kinds_a = [s["kind"] for s in r["steps"]] if r else []
check(r is not None and "elevator" in kinds_a and "stairs" not in kinds_a,
      f"the accessible route takes the elevator ({r and round(r['length'], 1)} m)")
check(r["length"] >= length, "the accessible route is not shorter")
p_none, l_none = graph.route(gpkg, "L0-101", "L1-202", accessible_only=True)
check(p_none == [] and l_none is None, "no accessible route out of the office with a 0.8 m door")
p_ok, l_ok = graph.route(gpkg, "L0-101", "L0-102")
check(p_ok and 5 < l_ok < 20, f"office 101 -> lab 102 on one level ({l_ok and round(l_ok, 1)} m)")
try:
    graph.route(gpkg, "nowhere", "L0-101")
    check(False, "unknown unit raises")
except KeyError:
    check(True, "unknown unit raises KeyError")

# ---- IMDF -------------------------------------------------------------------------------------------------------
imdf = result["imdf"]
check(imdf and os.path.exists(imdf), "indoor_imdf.zip written")
with zipfile.ZipFile(imdf) as z:
    names_z = set(z.namelist())
    check({"manifest.json", "venue.geojson", "building.geojson", "level.geojson", "unit.geojson",
           "opening.geojson"} <= names_z, f"IMDF files {sorted(names_z)}")
    docs = {n: json.loads(z.read(n).decode("utf-8")) for n in names_z}
check(docs["manifest.json"]["version"] == "1.0.0", "IMDF manifest")
units_j = docs["unit.geojson"]["features"]
check(len(units_j) == 18 and all(f["feature_type"] == "unit" for f in units_j), "18 IMDF units")
lon, lat = units_j[0]["geometry"]["coordinates"][0][0]
check(44 < lon < 48 and 23 < lat < 26, f"IMDF in WGS 84 ({lon:.4f}, {lat:.4f})")
check({f["properties"]["category"] for f in units_j} >= {"office", "laboratory", "walkway", "stairs", "elevator",
                                                        "restroom", "lobby"}, "IMDF unit categories")
lv_ids = {f["id"] for f in docs["level.geojson"]["features"]}
check(len(lv_ids) == 2 and all(f["properties"]["level_id"] in lv_ids for f in units_j), "units point to their level")
check(len(docs["opening.geojson"]["features"]) == 17 and
      all(f["geometry"]["type"] == "LineString" for f in docs["opening.geojson"]["features"]), "IMDF openings")
check(any(f["properties"]["name"] == {"ar": "مكتب 101"} for f in units_j), "IMDF Arabic names tagged 'ar'")
why = store.write_imdf(os.path.join(work, "x.zip"), QgsCoordinateReferenceSystem(), result["levels"], "x")
check(bool(why) and not os.path.exists(os.path.join(work, "x.zip")), f"no IMDF without a CRS: {why}")

# ---- report ---------------------------------------------------------------------------------------------------
rep = result["report"]
print(rep)
check(os.path.exists(result["report_path"]), "indoor_report.txt written")
check("Level 0" in rep and "Level 1" in rep and "corridor: 1" in rep, "report lists the levels and categories")
check("Units without a name or number" in rep and uid[(0, "shaft")] in rep, "report lists the unnamed shaft")
check("Doors to the outside (1)" in rep, "report lists the entrance")
check("narrower than 0.9 m" in rep, "report lists the narrow doors")
check("all connected" in rep, "report: graph connected")

# ---- GDAL fallback reader (as used for DWG) -------------------------------------------------------------------------
gp = cad.scale_plan(cad.read_with_gdal(p0), 1.0)
gres = rec.recognize(gp)
print("GDAL reader:", len(gres.units), "units,", gres.stats)
check(len(gres.units) >= 7, f"GDAL fallback reader still finds the spaces ({len(gres.units)})")

# ---- the Processing algorithms -------------------------------------------------------------------------------------
from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject  # noqa: E402
from biruni.algorithms import algorithm_classes  # noqa: E402

check({"floorplantoindoor", "indoorroute"} <= {c.ID for c in algorithm_classes()},
      "both tools are registered by the Biruni provider")
from biruni.algorithms.indoor_map import FloorPlanToIndoorMap  # noqa: E402
from biruni.algorithms.indoor_route import IndoorRoute  # noqa: E402

ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
alg = FloorPlanToIndoorMap()
alg.initAlgorithm()
out2 = os.path.join(work, "out2")
res = alg.processAlgorithm({"INPUT": p0, "LEVEL": 0, "MORE": f"{os.path.basename(p1)}=1:First floor",
                            "CRS": crs, "UNITS": 0, "DOOR_MIN": 0.9, "GAP": 1.2, "MIN_WIDTH": 0.5,
                            "STOREY": 3.5, "ENCODING": 0, "OUTPUT": out2}, ctx, fb)
check(os.path.exists(res["GPKG"]) and res["UNITS"] == 18, f"Floor plan to indoor map algorithm ({res.get('UNITS')})")
check(len(ctx.layersToLoadOnCompletion()) >= 8, f"layers to load per level ({len(ctx.layersToLoadOnCompletion())})")
ralg = IndoorRoute()
ralg.initAlgorithm()
rres = ralg.processAlgorithm({"GPKG": res["GPKG"], "FROM": "بهو", "TO": "202",
                              "ACCESSIBLE": True, "OUTPUT": "memory:"}, ctx, fb)
rl = ctx.getMapLayer(rres["OUTPUT"]) or QgsProject.instance().mapLayer(rres["OUTPUT"])
check(rres["LENGTH"] > 0 and rl is not None and rl.featureCount() >= 3, f"Route algorithm ({rres.get('LENGTH')})")

# ---- a look at level 0 ---------------------------------------------------------------------------------------------
png = os.environ.get("INDOOR_PNG") or os.path.join(work, "level0.png")
from biruni.indoor import style  # noqa: E402

made = style.render_png(res["GPKG"], 0, png, route=graph.find_route(res["GPKG"], "بهو", "L1-202", True))
check(os.path.exists(made) and os.path.getsize(made) > 5000, f"level 0 rendered to {made}")

# ---- the indoor map in the field: rooms to the phones, codes on the doors (field_photos) -----------------------
from biruni.field_photos import dock as fd  # noqa: E402
from biruni.field_photos.server import indoor_payload  # noqa: E402
from qgis.core import QgsVectorLayer  # noqa: E402

units_lyr = QgsVectorLayer(f"{gpkg}|layername=units", "units", "ogr")
lv_list, rooms = fd.indoor_items(units_lyr)
payload = indoor_payload(lv_list, rooms)
check([x["id"] for x in lv_list] == [0, 1] and len(payload["units"]) == units_lyr.featureCount(),
      f"every room of both levels goes to the phones ({len(payload['units'])})")
r202 = next((u for u in payload["units"] if u["id"] == "L1-202"), None)
check(r202 is not None and r202["lv"] == 1 and r202["num"] == "202" and 0 < r202["r"] < 30,
      "a room keeps its number, level and size: " + str(r202 and {k: r202[k] for k in ("id", "lv", "num", "r")}))
codes = os.path.join(work, "room_codes.pdf")
n_codes = fd.room_labels_pdf(units_lyr, codes)
check(n_codes > 0 and os.path.getsize(codes) > 5000, f"room codes printed for the plan's rooms ({n_codes})")

QgsProject.instance().clear()
if not os.environ.get("INDOOR_KEEP"):
    shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
