"""Recognise an indoor map in one floor plan (heuristics, Arabic and English drawings).

1. Layers get a role from their name: wall, door, room, stairs, elevator, ignore (furniture, dimensions…).
   Without any wall-like layer, all model-space linework that is not doors/furniture/annotation is walls.
2. Doors: swing arcs (60-120°, door-sized radius; any layer when ~90°, any sweep on door layers/blocks)
   with the leaf line from the hinge; the closed position of the leaf (the end without a leaf line, or
   the end nearest a wall) spans the wall gap. Two leaves meeting = double door. Door blocks without an
   arc (sliding doors) span the long side of their outline.
3. Wall gaps are closed: by the door lines, by joining wall ends that face each other or reach a wall
   within the gap tolerance (single-line walls), and by joining two short parallel jambs that face each
   other within the tolerance (thick walls) - the last two give plain openings.
4. The closed linework is polygonized; thin faces (wall bodies: no room of the minimum width fits) and
   tiny faces are dropped, and a face that only wraps other spaces (outline, frame) is dropped.
   Closed polylines / hatches on room layers are taken as they are and win over polygonized faces.
5. Texts and block attributes inside a space give its name and number; the category comes from words
   (Arabic/English/Spanish/French/Turkish), then from stair/lift layers, blocks and hatches inside,
   then from shape: long spaces with many doors are corridors, tiny closed ones shafts.
"""
import math
import re
from dataclasses import dataclass, field

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle, QgsSpatialIndex

from .. import arabic_text as at
from ..i18n import tr

UNIT_CATEGORIES = ["room", "office", "classroom", "lab", "corridor", "lobby", "stairs", "elevator", "ramp",
              "restroom", "kitchen", "storage", "parking", "shaft", "other"]
OPENING_CATEGORIES = ["door", "double_door", "sliding_door", "opening", "emergency_exit"]

LAYER_ROLES = [  # first match wins
    ("ignore", ["dim", "dims", "dimension", "anno", "text", "txt", "hatch", "furn", "furniture", "eqpm", "equip",
                "اثاث", "عفش", "فرش", "grid", "axis", "axes", "محاور", "defpoints", "title", "frame", "border",
                "sheet", "vport", "viewport", "fixt", "fixture", "plumb", "sanr", "ceil", "ceiling", "elec",
                "light", "hvac", "mech", "تكييف", "كهرباء", "note", "notes", "symb", "tag", "iden", "leader",
                "break", "mobil", "mobilier", "mueble", "mobilya", "ff e"]),
    ("door", ["door", "doors", "dr", "باب", "ابواب", "بيبان", "puerta", "porte", "porta", "kapi", "kapı"]),
    ("elevator", ["elev", "elevator", "lift", "مصعد", "مصاعد", "اسانسير", "ascensor", "ascenseur", "asansor"]),
    ("stairs", ["stair", "stairs", "str", "سلم", "سلالم", "درج", "escalera", "escalier", "merdiven"]),
    ("room", ["room", "rooms", "space", "spaces", "area", "zone", "غرف", "غرفه", "فراغ", "فراغات", "مساحات",
              "espacio", "espace", "local", "mekan"]),
    ("wall", ["wall", "walls", "mur", "murs", "pared", "parede", "duvar", "wand", "جدار", "جدران", "حائط",
              "حوائط", "حوايط", "حيطان", "column", "columns", "col", "cols", "عمود", "اعمده", "window", "windows",
              "glaz", "win", "شباك", "شبابيك", "نافذه", "نوافذ", "partition", "قواطيع", "structure", "struct",
              "hueco", "ventana", "fenetre", "pencere"]),
]
SLIDING_WORDS = ["slid", "sliding", "جرار", "سحاب", "منزلق", "corred", "coulis", "surgu"]
EXIT_WORDS = ["exit", "emerg", "emergency", "طوارئ", "طواري", "حريق", "fire", "salida", "secours", "acil"]

CATEGORY_WORDS = [  # first match wins: specific before general
    ("elevator", ["مصعد", "مصاعد", "اسانسير", "elevator", "elevators", "lift", "lifts", "elev", "ascensor",
                  "ascenseur", "asansor", "asansör"]),
    ("stairs", ["سلم", "سلالم", "درج", "stair", "stairs", "staircase", "stairwell", "escalera", "escalier",
                "merdiven"]),
    ("ramp", ["منحدر", "رامب", "ramp", "rampa", "rampe"]),
    ("restroom", ["حمام", "حمامات", "دوره مياه", "دورات مياه", "دوره المياه", "مياه", "wc", "w c", "toilet",
                  "toilets", "restroom", "restrooms", "lavatory", "bath", "bathroom", "مرحاض", "وضوء", "ميضاه",
                  "aseo", "bano", "baño", "toilette", "sanitaires", "tuvalet"]),
    ("kitchen", ["مطبخ", "مطابخ", "بوفيه", "kitchen", "pantry", "kitchenette", "cocina", "cuisine", "mutfak"]),
    ("storage", ["مخزن", "مخازن", "مستودع", "store", "storage", "stor", "storeroom", "almacen", "depot",
                 "rangement", "depo"]),
    ("parking", ["موقف", "مواقف", "جراج", "كراج", "parking", "garage", "car park", "estacionamiento", "otopark"]),
    ("shaft", ["shaft", "duct", "منور", "شافت", "بير", "بئر", "riser", "void"]),
    ("lab", ["معمل", "معامل", "مختبر", "مختبرات", "lab", "labs", "laboratory", "laboratorio", "laboratoire"]),
    ("classroom", ["فصل", "فصول", "قاعه", "قاعات", "مدرج", "classroom", "class", "lecture", "seminar", "aula",
                   "salle", "sinif", "sınıf"]),
    ("office", ["مكتب", "مكاتب", "اداره", "مدير", "سكرتاريه", "office", "offices", "admin", "oficina", "bureau",
                "ofis", "manager"]),
    ("corridor", ["ممر", "ممرات", "طرقه", "رواق", "corridor", "hallway", "passage", "pasillo", "couloir",
                  "koridor", "circulation"]),
    ("lobby", ["صاله", "بهو", "ردهه", "استقبال", "مدخل", "lobby", "foyer", "reception", "entrance", "vestibule",
               "vestibulo", "hall", "lobi"]),
    ("room", ["غرفه", "غرف", "room", "bedroom", "نوم", "meeting", "اجتماعات", "مجلس", "habitacion", "chambre",
              "oda"]),
]


# ---- words ---------------------------------------------------------------------------------------------
def _norm(text):
    s = at.unshape(str(text or ""))
    s = re.sub(r"[_\-$|]", " ", s)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)          # CamelCase layers
    return at.normalise(s)


def _words_match(norm, words):
    if not norm:
        return None
    tokens = norm.split()
    toks = set(tokens) | {at._strip_article(t) for t in tokens} | {t[1:] for t in tokens if t[:1] in "وب" and len(t) > 3}
    padded = f" {norm} "
    for w in words:
        w = _norm(w)
        if not w:
            continue
        if " " in w:
            if f" {w} " in padded:
                return w
        elif w.isascii() and len(w) >= 4:
            if w in norm:
                return w
        elif w in toks:
            return w
    return None


def layer_role(name):
    n = _norm(name)
    for role, words in LAYER_ROLES:
        if _words_match(n, words):
            return role
    return None


def category_of(text):
    """Category from a room name (also tried reversed, for Arabic stored in visual order)."""
    for t in (text, str(text or "")[::-1]):
        n = _norm(t)
        for cat, words in CATEGORY_WORDS:
            if _words_match(n, words):
                return cat
        if not at.has_arabic(text):
            break
    return None


_AREA = re.compile(r"(m2|m²|㎡|sqm|sq\s*m|sq\.?\s*ft|م2|م٢|م²|متر\s*مربع|\bm\b)", re.I)
_LEVELTXT = re.compile(r"^\s*[+±\-]\s*\d|ffl|f\.f\.l|sfl|منسوب|\bel\.?\s*[+\-]?\d", re.I)
_DECIMAL = re.compile(r"^\s*[+\-]?\d+[.,]\d+\s*$")
_NUMBER = re.compile(r"(?<![\w.])([A-Za-z]{0,3}[\-.]?\d{1,4}[A-Za-z]?(?:[.\-]\d{1,3})?)(?![\w.])")


def split_label(text):
    """(name, number) of one text; ('', '') for area / level / dimension texts."""
    s = at.unshape(str(text or "")).strip()
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    if not s or _AREA.search(s) or _LEVELTXT.search(s) or _DECIMAL.match(s):
        return "", ""
    number = ""
    m = _NUMBER.search(s)
    if m:
        number = m.group(1)
        s = (s[:m.start()] + " " + s[m.end():])
    name = re.sub(r"[()\[\]{}:#،,\-–]+", " ", s)
    name = re.sub(r"\s+", " ", name).strip()
    if not re.search(r"[^\W\d_]", name):
        name = ""
    return name, number


# ---- small geometry helpers ------------------------------------------------------------------------------
def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _line(p, q):
    return QgsGeometry.fromPolylineXY([QgsPointXY(*p), QgsPointXY(*q)])


def _extend(p, q, e):
    L = _dist(p, q) or 1.0
    ux, uy = (q[0] - p[0]) / L, (q[1] - p[1]) / L
    return (p[0] - ux * e, p[1] - uy * e), (q[0] + ux * e, q[1] + uy * e)


class _Index:
    """Spatial index of geometries with prepared engines."""

    def __init__(self, geoms):
        self.geoms = list(geoms)
        self.index = QgsSpatialIndex()
        self.engines = {}
        for i, g in enumerate(self.geoms):
            f = QgsFeature(i)
            f.setGeometry(g)
            self.index.addFeature(f)

    def engine(self, i):
        if i not in self.engines:
            e = QgsGeometry.createGeometryEngine(self.geoms[i].constGet())
            e.prepareGeometry()
            self.engines[i] = e
        return self.engines[i]

    def containing(self, x, y):
        """Indices of the geometries containing the point, smallest first."""
        pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        hits = [i for i in self.index.intersects(QgsRectangle(x - 1e-9, y - 1e-9, x + 1e-9, y + 1e-9))
                if self.engine(i).contains(pt.constGet())]
        return sorted(hits, key=lambda i: self.geoms[i].area())

    def near(self, rect):
        return self.index.intersects(rect)


# ---- results ---------------------------------------------------------------------------------------------
@dataclass
class Door:
    p: tuple                      # opening line from p to q (across the wall gap)
    q: tuple
    width: float
    category: str = "door"
    source: str = "arc"           # arc, block, gap, jamb
    hinge: tuple = None
    unit_a: int = -1              # index into units
    unit_b: int = -1
    access: dict = field(default_factory=dict)   # unit index -> point inside that unit near the door

    @property
    def mid(self):
        return (self.p[0] + self.q[0]) / 2, (self.p[1] + self.q[1]) / 2

    @property
    def geometry(self):
        return _line(self.p, self.q)


@dataclass
class Space:
    geom: QgsGeometry
    source: str = "walls"         # walls, room layer, hatch
    layer: str = ""
    texts: list = field(default_factory=list)      # (text, height, from_attrib_tag)
    name: str = ""
    number: str = ""
    category: str = ""
    hints: set = field(default_factory=set)
    doors: list = field(default_factory=list)
    unit_id: str = ""


@dataclass
class LevelResult:
    units: list = field(default_factory=list)
    doors: list = field(default_factory=list)
    footprint: QgsGeometry = None
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


@dataclass
class Options:
    gap_tolerance: float = 1.2        # wall gaps up to this width are closed (plain openings)
    min_room_width: float = 0.5       # faces where no circle of this diameter fits are wall bodies
    min_area: float = 0.3             # smaller faces are dropped
    door_min: float = 0.5             # door leaf radius range (single doors)
    door_max: float = 1.6
    max_wall: float = 0.6             # thickest wall, for jamb pairs


# ---- 1. doors ---------------------------------------------------------------------------------------------
def _door_ish(plan, arc, roles, block_roles):
    in_door = roles.get(arc.layer) == "door" or block_roles.get(arc.block) == "door"
    if arc.block and not in_door:
        return False, False                      # arcs inside furniture / fixtures blocks
    return in_door, True


def _find_leaves(plan, arc, r):
    """End index (0/1) of the arc that a leaf line reaches from the hinge, or None."""
    c = (arc.cx, arc.cy)
    ends = [arc.end(0), arc.end(1)]
    best = None
    for ln in plan.lines:
        if ln.block and ln.insert != arc.insert:
            continue
        pts = ln.pts
        segs = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if ln.closed else [])
        if len(segs) > 8:
            continue
        for a, b in segs:
            for h, o in ((a, b), (b, a)):
                if _dist(h, c) <= 0.08 * r:
                    for k, e in enumerate(ends):
                        if _dist(o, e) <= 0.2 * r:
                            best = k
    return best


def detect_doors(plan, roles, block_roles, opts, walls_geom):
    doors = []
    used_inserts = set()
    for arc in plan.arcs:
        in_door, allowed = _door_ish(plan, arc, roles, block_roles)
        if not allowed:
            continue
        sw = arc.sweep
        if in_door:
            ok = 25 <= sw <= 120 and 0.3 <= arc.r <= opts.door_max * 1.25
        else:
            ok = 75 <= sw <= 105 and opts.door_min <= arc.r <= opts.door_max and roles.get(arc.layer) not in (
                "ignore", "stairs", "elevator")
        if not ok:
            continue
        leaf = _find_leaves(plan, arc, arc.r)
        if leaf is None and not in_door and roles.get(arc.layer) == "wall":
            continue                                   # a 90° arc in a wall layer without a leaf: a curved wall
        c = (arc.cx, arc.cy)
        ends = [arc.end(0), arc.end(1)]
        if leaf is not None:
            closed = ends[1 - leaf]
        elif walls_geom is not None and not walls_geom.isEmpty():
            d0 = walls_geom.distance(QgsGeometry.fromPointXY(QgsPointXY(*ends[0])))
            d1 = walls_geom.distance(QgsGeometry.fromPointXY(QgsPointXY(*ends[1])))
            closed = ends[0] if d0 <= d1 else ends[1]
        else:
            closed = ends[0]
        names = f"{arc.block} {arc.layer}"
        cat = "emergency_exit" if _words_match(_norm(names), EXIT_WORDS) else "door"
        doors.append(Door(c, closed, arc.r, cat, "arc", hinge=c))
        if arc.insert >= 0:
            used_inserts.add(arc.insert)
    # double doors: two leaves whose closed ends meet
    merged, skip = [], set()
    for i, a in enumerate(doors):
        if i in skip:
            continue
        for j in range(i + 1, len(doors)):
            if j in skip:
                continue
            b = doors[j]
            if _dist(a.q, b.q) <= 0.15 * max(a.width, b.width) and _dist(a.p, b.p) > 0.5 * (a.width + b.width):
                cat = "emergency_exit" if "emergency_exit" in (a.category, b.category) else "double_door"
                a = Door(a.p, b.p, _dist(a.p, b.p), cat, "arc", hinge=a.p)
                skip.add(j)
                break
        merged.append(a)
    doors = merged
    # door blocks without a swing arc (sliding doors, simple symbols)
    by_insert = {}
    for ln in plan.lines:
        if ln.insert >= 0:
            by_insert.setdefault(ln.insert, []).extend(ln.pts)
    for ins in plan.inserts:
        if ins.index in used_inserts:
            continue
        if not (block_roles.get(ins.block) == "door" or roles.get(ins.layer) == "door"):
            continue
        pts = by_insert.get(ins.index, [])
        if len(pts) < 2:
            continue
        g = QgsGeometry.fromMultiPointXY([QgsPointXY(*p) for p in pts])
        box, _area, angle, w, h = g.orientedMinimumBoundingBox()
        if box.isNull():
            continue
        long_side = max(w, h)
        if not (opts.door_min * 0.8 <= long_side <= opts.door_max * 2.4):
            continue
        ring = box.asPolygon()[0]
        e = [(ring[k], ring[k + 1]) for k in range(4)]
        e.sort(key=lambda s: -s[0].distance(s[1]))
        (a1, a2), (b1, b2) = e[0], e[1]
        p = ((a1.x() + b2.x()) / 2, (a1.y() + b2.y()) / 2)
        q = ((a2.x() + b1.x()) / 2, (a2.y() + b1.y()) / 2)
        names = f"{ins.block} {ins.layer}"
        n = _norm(names)
        cat = ("emergency_exit" if _words_match(n, EXIT_WORDS) else
               "sliding_door" if _words_match(n, SLIDING_WORDS) else
               "double_door" if long_side > 1.4 else "door")
        doors.append(Door(p, q, _dist(p, q), cat, "block"))
    return doors


# ---- 2. walls and gap closing -------------------------------------------------------------------------------
def _segments(geom):
    out = []
    for part in geom.constParts():
        pts = [(v.x(), v.y()) for v in part.vertices()]
        out.extend(zip(pts, pts[1:]))
    return out


def _key(p):
    return (round(p[0], 4), round(p[1], 4))


def _close_gaps(noded, walls_union, door_lines, opts, skip=()):
    """Bridges for wall gaps: (line geometry list, [(p, q, kind)] plain openings)."""
    tol = opts.gap_tolerance
    if tol <= 0:
        return [], []
    bridges, openings = [], []
    segs = _segments(noded)
    # -- dangling ends (single-line walls)
    deg, direction = {}, {}
    for a, b in segs:
        for p, o in ((a, b), (b, a)):
            k = _key(p)
            deg[k] = deg.get(k, 0) + 1
            L = _dist(p, o) or 1.0
            direction[k] = ((p[0] - o[0]) / L, (p[1] - o[1]) / L, p)
    dangles = [k for k, n in deg.items() if n == 1 and k not in skip]   # not the ends of door lines
    taken = set()
    for k in dangles:
        if k in taken:
            continue
        ux, uy, p = direction[k]
        best = None
        for k2 in dangles:
            if k2 == k or k2 in taken:
                continue
            q = direction[k2][2]
            d = _dist(p, q)
            if d < 1e-9 or d > tol:
                continue
            cos = ((q[0] - p[0]) * ux + (q[1] - p[1]) * uy) / d
            if cos > 0.85 and (best is None or d < best[0]):
                best = (d, k2, q)
        if best:
            d, k2, q = best
            taken.update({k, k2})
            bridges.append(_line(p, q))
            if d >= opts.door_min:
                openings.append((p, q, "gap"))
            continue
        # a ray along the wall to the next wall
        far = (p[0] + ux * tol, p[1] + uy * tol)
        ray = _line((p[0] + ux * 1e-6, p[1] + uy * 1e-6), far)
        hit = ray.intersection(walls_union)
        if hit.isEmpty():
            continue
        pts = [(v.x(), v.y()) for v in hit.vertices()]
        if not pts:
            continue
        q = min(pts, key=lambda t: _dist(p, t))
        d = _dist(p, q)
        if d < 1e-9:
            continue
        taken.add(k)
        bridges.append(_line(p, q))
        if d >= opts.door_min:
            openings.append((p, q, "gap"))
    # -- facing jambs (thick walls drawn with both faces)
    short = [(a, b) for a, b in segs if 0.05 <= _dist(a, b) <= opts.max_wall]
    if short:
        idx = _Index([_line(a, b) for a, b in short])
        door_union = QgsGeometry.unaryUnion(door_lines) if door_lines else None
        done = set()
        for i, (a, b) in enumerate(short):
            L = _dist(a, b)
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            rect = QgsRectangle(m[0] - tol, m[1] - tol, m[0] + tol, m[1] + tol)
            best = None
            for j in idx.near(rect):
                if j == i or (j, i) in done:
                    continue
                c, d = short[j]
                L2 = _dist(c, d)
                vx, vy = (d[0] - c[0]) / L2, (d[1] - c[1]) / L2
                if abs(ux * vy - uy * vx) > 0.09 or abs(L - L2) > 0.25 * max(L, L2):
                    continue
                m2 = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
                wx, wy = m2[0] - m[0], m2[1] - m[1]
                along = abs(wx * ux + wy * uy)
                across = abs(wx * -uy + wy * ux)
                if along > 0.2 * L or not (opts.door_min <= across <= tol):
                    continue
                if best is None or across < best[0]:
                    best = (across, j, m2)
            if best is None:
                continue
            across, j, m2 = best
            mid = _line(m, m2)
            inner = _line(*_extend(m, m2, -0.02))
            if inner.intersects(walls_union):
                continue
            if door_union is not None and mid.buffer(0.3, 2).intersects(door_union):
                continue
            done.add((i, j))
            c, d = short[j]
            if (c[0] - a[0]) * ux + (c[1] - a[1]) * uy > (d[0] - a[0]) * ux + (d[1] - a[1]) * uy:
                c, d = d, c
            bridges.extend([_line(a, c), _line(b, d)])
            openings.append((m, m2, "jamb"))
    return bridges, openings


# ---- 3. the whole level -------------------------------------------------------------------------------------
def recognize(plan, opts=None):
    opts = opts or Options()
    res = LevelResult()
    layers = {ln.layer for ln in plan.lines} | {a.layer for a in plan.arcs} | {t.layer for t in plan.texts} | {
        i.layer for i in plan.inserts} | {h[0] for h in plan.hatches}
    roles = {name: layer_role(name) for name in layers}
    block_roles = {}
    for ins in plan.inserts:
        if ins.block not in block_roles:
            block_roles[ins.block] = layer_role(ins.block)
    has_walls = any(roles.get(ln.layer) == "wall" for ln in plan.lines)
    if not has_walls:
        res.warnings.append(tr("No wall layer was recognised (WALL, A-WALL, جدار, حوائط…): all lines that are not "
                               "doors, furniture or annotation were used as walls."))

    def wall_line(ln):
        role = roles.get(ln.layer)
        brole = block_roles.get(ln.block) if ln.block else None
        if brole in ("door", "ignore", "stairs", "elevator") or role in ("door", "stairs", "elevator", "room",
                                                                          "ignore"):
            return False
        if has_walls:
            return role == "wall"
        return not ln.block

    # leaves of door arcs are not walls
    wall_lines = []
    for ln in plan.lines:
        if wall_line(ln) and len(ln.pts) >= 2:
            pts = ln.pts + ([ln.pts[0]] if ln.closed else [])
            wall_lines.append(QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts]))
    curved = [a for a in plan.arcs if (roles.get(a.layer) == "wall" if has_walls else not a.block)]
    walls_pre = QgsGeometry.unaryUnion(wall_lines) if wall_lines else QgsGeometry()
    doors = detect_doors(plan, roles, block_roles, opts, walls_pre)
    door_arcs_hinges = [d.hinge for d in doors if d.hinge]
    for a in curved:
        if any(_dist((a.cx, a.cy), h) < 1e-3 for h in door_arcs_hinges):
            continue
        if 75 <= a.sweep <= 105 and opts.door_min <= a.r <= opts.door_max and not has_walls:
            continue                                   # looks like a door that was not taken: not a wall
        wall_lines.append(QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in a.points()]))
    # remove leaf lines (from a hinge, door length) drawn on wall layers
    leaf_free = []
    for g in wall_lines:
        pts = [(v.x(), v.y()) for v in g.vertices()]
        if len(pts) == 2 and any(min(_dist(pts[0], d.p), _dist(pts[1], d.p)) < 0.05 and
                                 abs(_dist(*pts) - d.width) < 0.15 * d.width for d in doors):
            continue
        leaf_free.append(g)
    wall_lines = leaf_free
    if not wall_lines:
        res.warnings.append(tr("No walls were found in this plan."))
    walls_union = QgsGeometry.unaryUnion(wall_lines) if wall_lines else QgsGeometry()

    # door lines close the wall gaps: each end snaps to a wall corner nearby (the jamb), else it is
    # extended a little into the wall so that it crosses the jamb even when the symbol is slightly off
    grid = {}
    for g in wall_lines:
        for v in g.vertices():
            grid.setdefault((int(v.x() // 0.5), int(v.y() // 0.5)), []).append((v.x(), v.y()))

    def snap(p, tol):
        best = None
        cx, cy = int(p[0] // 0.5), int(p[1] // 0.5)
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for v in grid.get((i, j), ()):
                    d = _dist(p, v)
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, v)
        return best[1] if best else None

    door_lines, door_ends = [], set()
    for d in doors:
        ext = max(0.15, 0.1 * d.width)
        tol = max(0.03, 0.06 * d.width)
        sp, sq = snap(d.p, tol), snap(d.q, tol)
        ep, eq = _extend(d.p, d.q, ext)
        p, q = sp or ep, sq or eq
        if sp and sq:
            d.p, d.q = sp, sq
            d.width = _dist(sp, sq)
        door_lines.append(_line(p, q))
        door_ends.update({_key(p), _key(q)})
    noded = QgsGeometry.unaryUnion(wall_lines + door_lines) if wall_lines else QgsGeometry()
    bridges, gaps = ([], []) if noded.isEmpty() else _close_gaps(noded, walls_union, door_lines, opts, door_ends)
    for p, q, kind in gaps:
        doors.append(Door(p, q, _dist(p, q), "opening", kind))
    if bridges:
        noded = QgsGeometry.unaryUnion([noded] + bridges)

    faces, thin = [], []
    if not noded.isEmpty():
        poly = QgsGeometry.polygonize([noded])
        for part in poly.asGeometryCollection():
            if part.isEmpty() or part.area() < 1e-6:
                continue
            inner = part.buffer(-opts.min_room_width / 2, 2)
            if inner.isEmpty() or inner.area() <= 1e-9:
                thin.append(part)
            elif part.area() < opts.min_area:
                thin.append(part)
            else:
                faces.append(part)
    res.stats["slivers_dropped"] = len(thin)

    # room layers / hatches used as they are
    spaces = []
    room_polys = []
    for ln in plan.lines:
        if roles.get(ln.layer) == "room" and ln.closed and len(ln.pts) >= 3:
            g = QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in ln.pts + [ln.pts[0]]]])
            if g.area() >= opts.min_area:
                room_polys.append((g.makeValid(), "room layer", ln.layer))
    for layer, g in plan.hatches:
        if roles.get(layer) == "room" and g.area() >= opts.min_area:
            room_polys.append((g.makeValid(), "hatch", layer))
    for g, src, layer in room_polys:
        if any(abs(g.area() - s.geom.area()) < 1e-6 and g.equals(s.geom) for s in spaces):
            continue
        spaces.append(Space(g, src, layer))
    for f in faces:
        if spaces:
            cover = sum(f.intersection(s.geom).area() for s in spaces if f.boundingBox().intersects(s.geom.boundingBox()))
            if cover > 0.3 * f.area():
                continue
        spaces.append(Space(f, "walls"))

    # texts
    idx = _Index([s.geom for s in spaces])
    for t in plan.texts:
        if not t.text or roles.get(t.layer) == "ignore" and _AREA.search(t.text or ""):
            continue
        hit = idx.containing(t.x, t.y)
        if hit:
            tag = ""
            if t.insert >= 0 and t.insert < len(plan.inserts):
                ins = plan.inserts[t.insert]
                tag = next((k for k, v in ins.attribs.items() if v == t.text), "")
            spaces[hit[0]].texts.append((t.text, t.height, tag))
    # hints from layers, blocks and hatches inside
    for ln in plan.lines:
        role = roles.get(ln.layer) or (block_roles.get(ln.block) if ln.block else None)
        if role in ("stairs", "elevator") and ln.pts:
            x = sum(p[0] for p in ln.pts) / len(ln.pts)
            y = sum(p[1] for p in ln.pts) / len(ln.pts)
            for i in idx.containing(x, y)[:1]:
                spaces[i].hints.add(role)
    for ins in plan.inserts:
        role = block_roles.get(ins.block) or roles.get(ins.layer)
        if role in ("stairs", "elevator"):
            for i in idx.containing(ins.x, ins.y)[:1]:
                spaces[i].hints.add(role)
    for layer, g in plan.hatches:
        if roles.get(layer) in ("stairs", "elevator"):
            c = g.pointOnSurface().asPoint()
            for i in idx.containing(c.x(), c.y())[:1]:
                spaces[i].hints.add(roles.get(layer))

    # envelope: a space that wraps (nearly) all the others - a frame, an outline, a gross-area polygon
    drop = set()
    n_sp = len(spaces)
    for i, s in enumerate(spaces):
        outer = QgsGeometry(s.geom).removeInteriorRings()
        eng = QgsGeometry.createGeometryEngine(outer.constGet())
        eng.prepareGeometry()
        inside = 0
        for j, o in enumerate(spaces):
            if j != i:
                pos = o.geom.pointOnSurface()        # keep the geometry alive while GEOS reads it
                inside += 1 if eng.contains(pos.constGet()) else 0
        if inside >= 0.9 * (n_sp - 1) and (inside >= 3 or (inside >= 2 and not s.texts)):
            drop.add(i)
    if drop:
        res.stats["envelopes_dropped"] = len(drop)
        spaces = [s for i, s in enumerate(spaces) if i not in drop]
        idx = _Index([s.geom for s in spaces])

    # doors -> the spaces on both sides
    for d in doors:
        L = _dist(d.p, d.q)
        if L < 1e-9:
            continue
        nx, ny = -(d.q[1] - d.p[1]) / L, (d.q[0] - d.p[0]) / L
        mx, my = d.mid
        found = []
        for side in (1, -1):
            got = -1
            for off in (0.08, 0.2, 0.35, 0.55, 0.8, 1.1):
                x, y = mx + side * nx * off, my + side * ny * off
                hit = idx.containing(x, y)
                if hit:
                    got = hit[0]
                    d.access.setdefault(got, (x, y))
                    break
            found.append(got)
        a, b = found
        d.same = a == b and a >= 0
        if a == b:
            b = -1
        if a < 0:
            a, b = b, -1
        d.unit_a, d.unit_b = a, b
    # plain openings that join nothing new are not openings
    kept = []
    for d in doors:
        if d.category == "opening" and (d.unit_a < 0 or getattr(d, "same", False)):
            continue
        if getattr(d, "same", False):
            res.warnings.append(tr("A door at ({x}, {y}) has the same space on both sides: the wall gap "
                                   "around it was not closed.").format(x=f"{d.mid[0]:.2f}", y=f"{d.mid[1]:.2f}"))
        kept.append(d)
    doors = kept
    for k, d in enumerate(doors):
        for u in (d.unit_a, d.unit_b):
            if u >= 0:
                spaces[u].doors.append(k)

    # names, numbers, categories
    for s in spaces:
        label_space(s)
        if not s.category:
            s.category = shape_category(s)
    res.units = spaces
    res.doors = doors
    # footprint: spaces and the wall bodies around them, holes filled
    touching = [t for t in thin if any(t.boundingBox().intersects(s.geom.boundingBox()) and t.touches(s.geom)
                                       for s in spaces)] if len(thin) < 4000 else []
    parts = [s.geom for s in spaces] + touching
    if parts:
        u = QgsGeometry.unaryUnion(parts).buffer(0.01, 2).buffer(-0.01, 2)
        res.footprint = u.removeInteriorRings()
    res.stats["doors"] = sum(1 for d in doors if d.category != "opening")
    res.stats["openings"] = sum(1 for d in doors if d.category == "opening")
    return res


def label_space(s):
    names, numbers = [], []
    for text, height, tag in sorted(s.texts, key=lambda t: -(t[1] or 0)):
        tagn = (tag or "").upper()
        if tag and re.search(r"NUM|NO\b|NBR|رقم", tagn):
            n = at.to_latin_digits(text.strip()) if hasattr(at, "to_latin_digits") else text.strip()
            if n:
                numbers.append(n)
            continue
        name, number = split_label(text)
        if number:
            numbers.append(number)
        if name and name not in names:
            names.append(name)
    s.name = " ".join(names)[:120]
    s.number = numbers[0] if numbers else ""
    cat = category_of(s.name) if s.name else None
    if not cat and s.layer:
        cat = category_of(s.layer)
    if not cat:
        for h in ("elevator", "stairs"):
            if h in s.hints:
                cat = h
                break
    if not cat and s.name:
        cat = "room"
    s.category = cat or ""


def shape_category(s):
    """Category of an unnamed space from its shape and doors."""
    g = s.geom
    area = g.area()
    box, _a, _ang, w, h = g.orientedMinimumBoundingBox()
    lo, hi = min(w, h) or 1e-9, max(w, h)
    if len(s.doors) >= 3 or (hi / lo >= 4 and area >= 6 and len(s.doors) >= 2):
        return "corridor"
    if not s.doors and (area < 3.0 or (lo < 2.0 and area < 12.0)):     # a closed narrow space: duct, shaft
        return "shaft"
    return "other"
