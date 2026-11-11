"""Read an architectural plan into plain lines, arcs, texts, block inserts and hatches, in metres.

DXF goes through Biruni's own reader (survey/dxf.py: encodings, MTEXT, xarb.shx Arabic keyboard fonts,
R12 polylines, attributes); block contents are read from the BLOCKS section with the same reader and
placed at every insert, so door blocks give their swing arcs. Hatches come from GDAL, as in CAD to GIS.
DWG is converted to DXF with the ODA File Converter when it is installed, else read through GDAL's CAD
driver (AutoCAD 2000 DWG only, arcs approximated).
"""
import glob
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from ..survey import dxf as dxfmod

# $INSUNITS -> metres
INSUNITS = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 14: 0.1}


@dataclass
class Line:
    layer: str
    pts: list                 # [(x, y)]
    closed: bool = False
    block: str = ""           # top-level block name when it comes from an insert
    insert: int = -1          # index of that insert
    entity: str = "LINE"


@dataclass
class Arc:
    layer: str
    cx: float
    cy: float
    r: float
    a1: float                 # degrees, counter-clockwise from a1 to a2
    a2: float
    block: str = ""
    insert: int = -1

    @property
    def sweep(self):
        s = (self.a2 - self.a1) % 360.0
        return s if s > 1e-9 else 360.0

    def end(self, which):
        a = math.radians(self.a1 if which == 0 else self.a2)
        return self.cx + self.r * math.cos(a), self.cy + self.r * math.sin(a)

    def points(self, step_deg=10.0):
        n = max(2, int(math.ceil(self.sweep / step_deg)) + 1)
        return [(self.cx + self.r * math.cos(math.radians(self.a1 + self.sweep * i / (n - 1))),
                 self.cy + self.r * math.sin(math.radians(self.a1 + self.sweep * i / (n - 1)))) for i in range(n)]


@dataclass
class Text:
    layer: str
    x: float
    y: float
    text: str
    block: str = ""
    insert: int = -1
    height: float = 0.0


@dataclass
class Insert:
    layer: str
    block: str
    x: float
    y: float
    rotation: float = 0.0
    sx: float = 1.0
    sy: float = 1.0
    attribs: dict = field(default_factory=dict)
    index: int = -1


@dataclass
class Plan:
    path: str = ""
    lines: list = field(default_factory=list)
    arcs: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    inserts: list = field(default_factory=list)
    hatches: list = field(default_factory=list)     # (layer, QgsGeometry polygon)
    scale: float = 1.0
    encoding: str = ""
    warnings: list = field(default_factory=list)


# ---- affine helpers ------------------------------------------------------------------------------------
class _Affine:
    """p' = R(rot) * S * (p - base) + ins, composable for nested blocks."""

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, e=0.0, f=0.0):
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f   # x' = a x + c y + e ; y' = b x + d y + f

    @classmethod
    def for_insert(cls, ins_x, ins_y, rot_deg, sx, sy, base=(0.0, 0.0)):
        cr, sr = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
        a, b, c, d = cr * sx, sr * sx, -sr * sy, cr * sy
        e = ins_x - (a * base[0] + c * base[1])
        f = ins_y - (b * base[0] + d * base[1])
        return cls(a, b, c, d, e, f)

    def then(self, outer):
        """outer ∘ self."""
        o = outer
        return _Affine(o.a * self.a + o.c * self.b, o.b * self.a + o.d * self.b,
                       o.a * self.c + o.c * self.d, o.b * self.c + o.d * self.d,
                       o.a * self.e + o.c * self.f + o.e, o.b * self.e + o.d * self.f + o.f)

    def __call__(self, x, y):
        return self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f

    @property
    def det(self):
        return self.a * self.d - self.b * self.c


def _arc_through(t, cx, cy, r, a1, a2):
    """Arc (cx, cy, r, a1, a2) mapped by the affine t (uniform scale assumed; mirrors swap the ends)."""
    p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    p2 = (cx + r * math.cos(math.radians(a2)), cy + r * math.sin(math.radians(a2)))
    c = t(cx, cy)
    q1, q2 = t(*p1), t(*p2)
    if t.det < 0:
        q1, q2 = q2, q1
    rr = r * math.sqrt(abs(t.det))
    b1 = math.degrees(math.atan2(q1[1] - c[1], q1[0] - c[0]))
    b2 = math.degrees(math.atan2(q2[1] - c[1], q2[0] - c[0]))
    return c[0], c[1], rr, b1 % 360.0, b2 % 360.0


def _poly_points(verts, closed):
    """Polyline vertices (x, y, bulge) -> points with bulge arcs split every ~10°, plus the bulge arcs."""
    pts, arcs = [], []
    n = len(verts)
    segs = n if closed else n - 1
    for i in range(segs):
        a, b = verts[i], verts[(i + 1) % n]
        pts.append((a[0], a[1]))
        bulge = a[2]
        if abs(bulge) > 1e-9:
            theta = 4 * math.atan(bulge)            # signed sweep
            chord = math.hypot(b[0] - a[0], b[1] - a[1])
            if chord < 1e-12:
                continue
            r = abs(chord / (2 * math.sin(theta / 2)))
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            # centre: from the chord middle, perpendicular, distance r*cos(theta/2)
            h = r * math.cos(theta / 2) * (1 if bulge > 0 else -1)
            ux, uy = (b[0] - a[0]) / chord, (b[1] - a[1]) / chord
            cx, cy = mx - uy * h, my + ux * h
            s = math.degrees(math.atan2(a[1] - cy, a[0] - cx))
            e = math.degrees(math.atan2(b[1] - cy, b[0] - cx))
            arcs.append((cx, cy, r, s, e) if bulge > 0 else (cx, cy, r, e, s))
            steps = max(2, int(abs(math.degrees(theta)) / 10) + 1)
            for k in range(1, steps):
                ang = math.radians(s) + theta * k / steps
                pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    if not closed and n:
        pts.append((verts[-1][0], verts[-1][1]))
    return pts, arcs


# ---- DXF -------------------------------------------------------------------------------------------------
def _decode(raw, encoding):
    if raw.startswith(b"\xef\xbb\xbf"):
        raw, encoding = raw[3:], "utf-8"
    try:
        return raw.decode(encoding, "replace")
    except LookupError:
        return raw.decode("cp1252", "replace")


def _blocks(text, styles):
    """{block name: (base point, [Entity])} from the BLOCKS section."""
    blocks = {}
    in_blocks = expect = False
    cur = target = None
    for code, value in dxfmod._pairs(text):
        if code == 0:
            v = value.strip()
            if v == "SECTION":
                expect = True
                continue
            if v == "ENDSEC":
                if in_blocks:
                    break
                continue
            if not in_blocks:
                continue
            if v == "BLOCK":
                cur = {"vals": [], "recs": []}
                target = cur["vals"]
                continue
            if v == "ENDBLK":
                if cur is not None:
                    g = dict((c, x) for c, x in reversed(cur["vals"]))
                    name = (g.get(2) or g.get(3) or "").strip()
                    if name and not name.upper().startswith(("*MODEL_SPACE", "*PAPER_SPACE")):
                        d = dxfmod.Drawing(styles=styles)
                        dxfmod._entities(cur["recs"], d)
                        base = (dxfmod._float(g.get(10, "0")), dxfmod._float(g.get(20, "0")))
                        blocks[name] = (base, d.entities)
                cur = target = None
                continue
            if cur is not None:
                cur["recs"].append((v, []))
                target = cur["recs"][-1][1]
            continue
        if expect and code == 2:
            in_blocks = value.strip() == "BLOCKS"
            expect = False
            continue
        if in_blocks and target is not None:
            target.append((code, value))
    return blocks


def _add_entity(plan, e, t, layer_of_insert, block, insert_idx, blocks, depth):
    layer = e.layer
    if block and layer == "0" and layer_of_insert:
        layer = layer_of_insert          # CAD rule: layer 0 inside a block takes the insert's layer
    if e.kind in ("poly", "line"):
        verts = e.verts
        closed = e.closed
        if e.kind == "poly":
            verts, closed = dxfmod.normalize_polyline(verts, closed, 1e-6)
        if len(verts) < 2:
            return
        pts, arcs = _poly_points(verts, closed) if e.kind == "poly" else ([(v[0], v[1]) for v in verts], [])
        pts = [t(x, y) for x, y in pts]
        plan.lines.append(Line(layer, pts, closed, block, insert_idx, e.dxf_type))
        for cx, cy, r, a1, a2 in arcs:
            plan.arcs.append(Arc(layer, *_arc_through(t, cx, cy, r, a1, a2), block=block, insert=insert_idx))
        return
    if e.kind == "arc":
        cx, cy = e.center
        a1, a2 = e.angles
        if e.mirrored:                       # extrusion (0, 0, -1): x mirrored
            cx, a1, a2 = -cx, 180.0 - a2, 180.0 - a1
        plan.arcs.append(Arc(layer, *_arc_through(t, cx, cy, e.radius, a1, a2), block=block, insert=insert_idx))
        return
    if e.kind == "circle":
        cx, cy = e.center
        if e.mirrored:
            cx = -cx
        c = t(cx, cy)
        r = e.radius * math.sqrt(abs(t.det))
        pts = [(c[0] + r * math.cos(math.radians(a)), c[1] + r * math.sin(math.radians(a))) for a in range(0, 360, 10)]
        plan.lines.append(Line(layer, pts, True, block, insert_idx, "CIRCLE"))
        return
    if e.kind == "text":
        x, y = t(e.x, e.y)
        plan.texts.append(Text(layer, x, y, e.text, block, insert_idx, e.height * math.sqrt(abs(t.det))))
        return
    if e.kind == "insert":
        x, y = t(e.x, e.y)
        if depth == 0:
            ins = Insert(layer, e.block, x, y, e.rotation, e.scale[0], e.scale[1], dict(e.attribs),
                         len(plan.inserts))
            plan.inserts.append(ins)
            block, insert_idx = e.block, ins.index
            for tag, val in e.attribs.items():
                if val:
                    plan.texts.append(Text(layer, x, y, val, e.block, ins.index))
        if depth > 6 or e.block not in blocks:
            return
        base, ents = blocks[e.block]
        inner = _Affine.for_insert(e.x, e.y, e.rotation, e.scale[0], e.scale[1], base).then(t)
        for sub in ents:
            _add_entity(plan, sub, inner, layer, block, insert_idx, blocks, depth + 1)


def read_dxf(path, encoding="", feedback=None):
    with open(path, "rb") as f:
        raw = f.read()
    if dxfmod.is_binary(raw):
        raise ValueError("binary DXF")
    d = dxfmod.parse_bytes(raw, encoding)
    plan = Plan(path=path, encoding=d.encoding)
    blocks = _blocks(_decode(raw, d.encoding), d.styles)
    ident = _Affine()
    for e in d.entities:
        _add_entity(plan, e, ident, "", "", -1, blocks, 0)
    plan.insunits = d.insunits
    # hatches (and other GDAL-only entities) as polygons, like CAD to GIS
    if d.other_handles:
        try:
            from ..algorithms.cad_to_gis import CadToGis
            from qgis.core import Qgis, QgsProcessingFeedback, QgsWkbTypes

            fb = feedback or QgsProcessingFeedback()
            for layer, _h, g, sub in CadToGis._gdal_others(None, path, d.other_handles, fb):
                if QgsWkbTypes.geometryType(g.wkbType()) == Qgis.GeometryType.Polygon:
                    if g.constGet().hasCurvedSegments():
                        from qgis.core import QgsGeometry
                        g = QgsGeometry(g.constGet().segmentize())
                    plan.hatches.append((layer, g))
        except Exception as ex:  # hatches are a bonus: never fail the plan for them
            plan.warnings.append(f"hatches: {ex}")
    return plan


# ---- DWG -----------------------------------------------------------------------------------------------
def find_oda_converter():
    exe = shutil.which("ODAFileConverter")
    if exe:
        return exe
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if root:
            hits = sorted(glob.glob(os.path.join(root, "ODA", "*", "ODAFileConverter.exe")))
            if hits:
                return hits[-1]
    return None


def dwg_to_dxf(path, exe):
    """Converts one DWG with the ODA File Converter; returns the DXF path (in a temporary folder)."""
    src = tempfile.mkdtemp(prefix="biruni_dwg_")
    out = tempfile.mkdtemp(prefix="biruni_dxf_")
    shutil.copy2(path, src)
    name = os.path.basename(path)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([exe, src, out, "ACAD2018", "DXF", "0", "1", name], timeout=300, creationflags=flags,
                   capture_output=True)
    dxf = os.path.join(out, os.path.splitext(name)[0] + ".dxf")
    if not os.path.exists(dxf):
        raise OSError("the ODA File Converter made no DXF")
    return dxf


def _fit_circle(pts):
    """(cx, cy, r) through the first, middle and last point when all points lie on it, else None."""
    if len(pts) < 4:
        return None
    (x1, y1), (x2, y2), (x3, y3) = pts[0], pts[len(pts) // 2], pts[-1]
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
    uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    if r <= 0 or any(abs(math.hypot(x - ux, y - uy) - r) > 0.02 * r for x, y in pts):
        return None
    return ux, uy, r


def read_with_gdal(path):
    """Fallback reader through GDAL (DWG via the CAD driver): lines, texts, arcs recognised from their
    points, block names; no block attributes."""
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    plan = Plan(path=path, encoding="gdal")
    old = gdal.GetConfigOption("DXF_INLINE_BLOCKS")
    gdal.SetConfigOption("DXF_INLINE_BLOCKS", "TRUE")
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    finally:
        gdal.SetConfigOption("DXF_INLINE_BLOCKS", old)

    def get(f, *names):
        for n in names:
            i = f.GetFieldIndex(n)
            if i >= 0 and f.IsFieldSet(i):
                return f.GetField(i)
        return None

    for li in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(li)
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None:
                continue
            layer = str(get(f, "Layer", "layer", "CADLayer") or lyr.GetName() or "0")
            block = str(get(f, "BlockName", "blockname") or "")
            text = get(f, "Text", "text")
            g = g.GetLinearGeometry() if g.HasCurveGeometry() else g
            gt = ogr.GT_Flatten(g.GetGeometryType())
            if gt == ogr.wkbPoint:
                if text:
                    plan.texts.append(Text(layer, g.GetX(), g.GetY(), dxfmod.clean_mtext(str(text)), block))
                continue
            parts = [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())] if gt in (
                ogr.wkbMultiLineString, ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbGeometryCollection) else [g]
            for p in parts:
                if p is None:
                    continue
                if ogr.GT_Flatten(p.GetGeometryType()) == ogr.wkbPolygon:
                    p = p.GetGeometryRef(0)
                if p is None or p.GetPointCount() < 2:
                    continue
                pts = [(p.GetX(i), p.GetY(i)) for i in range(p.GetPointCount())]
                circ = _fit_circle(pts)
                if circ and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) > 1e-6:
                    cx, cy, r = circ
                    a1 = math.degrees(math.atan2(pts[0][1] - cy, pts[0][0] - cx))
                    a2 = math.degrees(math.atan2(pts[-1][1] - cy, pts[-1][0] - cx))
                    am = math.degrees(math.atan2(pts[len(pts) // 2][1] - cy, pts[len(pts) // 2][0] - cx))
                    if (am - a1) % 360 > (a2 - a1) % 360:   # clockwise
                        a1, a2 = a2, a1
                    plan.arcs.append(Arc(layer, cx, cy, r, a1 % 360, a2 % 360, block))
                    continue
                closed = math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-9
                plan.lines.append(Line(layer, pts[:-1] if closed else pts, closed, block, -1, "GDAL"))
    plan.insunits = 0
    return plan


# ---- entry point ---------------------------------------------------------------------------------------
def guess_scale(plan):
    """Drawing units -> metres: $INSUNITS when set, else from the size of door arcs and of the drawing."""
    u = INSUNITS.get(getattr(plan, "insunits", 0))
    if u:
        return u
    radii = sorted(a.r for a in plan.arcs if 60 <= a.sweep <= 120)
    if radii:
        r = radii[len(radii) // 2]
        if r > 300:
            return 0.001
        if r > 30:
            return 0.01
        return 1.0
    xs = [x for ln in plan.lines for x, _ in ln.pts]
    ys = [y for ln in plan.lines for _, y in ln.pts]
    if xs:
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span > 5000:
            return 0.001
    return 1.0


def scale_plan(plan, s):
    if s == 1.0:
        return plan
    for ln in plan.lines:
        ln.pts = [(x * s, y * s) for x, y in ln.pts]
    for a in plan.arcs:
        a.cx, a.cy, a.r = a.cx * s, a.cy * s, a.r * s
    for t in plan.texts:
        t.x, t.y, t.height = t.x * s, t.y * s, t.height * s
    for i in plan.inserts:
        i.x, i.y = i.x * s, i.y * s
    if plan.hatches:
        from qgis.PyQt.QtGui import QTransform
        for _l, g in plan.hatches:
            g.transform(QTransform.fromScale(s, s))
    plan.scale = s
    return plan


def read_plan(path, units=0.0, encoding="", feedback=None):
    """A Plan in metres. units: metres per drawing unit, 0 = automatic."""
    low = path.lower()
    if low.endswith(".dwg"):
        exe = find_oda_converter()
        if exe:
            plan = read_dxf(dwg_to_dxf(path, exe), encoding, feedback)
            plan.path = path
        else:
            plan = read_with_gdal(path)
            plan.warnings.append("DWG read through GDAL (AutoCAD 2000 format only; no block attributes). "
                                 "For best results save the drawing as DXF.")
    else:
        plan = read_dxf(path, encoding, feedback)
    s = units if units and units > 0 else guess_scale(plan)
    return scale_plan(plan, s)
