"""Spatial fingerprints: two copies of a layer compared area by area, without sending either of them.

Every feature has a fingerprint: its shape and its values, written the same way whatever the file holds them
(a shapefile turns a polygon into a one-part multipolygon and its rings around; a value of 3.0 and one of 3 are
the same; coordinates are taken to a tenth of a millimetre). The features are put in the squares of a fixed grid
by the centre of their extent, the same grid on every computer, so the same feature falls in the same square in
any copy. A square's fingerprint is made of the fingerprints in it, four squares make the one above them, up to
one fingerprint for the whole layer.

Two copies are the same when the top fingerprints are the same. When they are not, only the squares whose
fingerprints differ are opened, level by level: a few dozen fingerprints compared find the one parcel changed
among a hundred thousand. Then only those squares are carried from one copy to the other.

A certificate is the fingerprints of the smallest squares of each layer, with the top one: whoever receives the
data proves it is what was delivered, and when it is not the certificate shows where it differs.

Beside it a second tree, the reach: every square's fingerprint also holds the box its features reach (a road whose
centre is far away still reaches the squares it crosses). With it, what is in an area can be proved from a few
squares opened and the boxes of the others: nothing hidden, nothing left out (see proof.py).

And a third, the commitments: every value of every feature sealed on its own, with a salt only the full data
gives. A copy with some fields (or features) taken out then proves that all it shows is what the certificate
sealed, and the values taken out cannot be guessed from it (see disclose.py).

And a fourth, the sums: every square's fingerprint also holds how many features it has, how much area (or length)
and the totals of the numeric fields. The totals of an area are then proved with the squares inside it kept closed:
nothing of their features is shown, only their sums (see totals.py).
"""
import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import math
from collections import Counter

from qgis.core import (QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsFeatureRequest, QgsGeometry,
                       QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes)

from ..edit_journal import decode, encode

FORMAT = 1
TOP = 24                 # levels of squares above the smallest: 64 m x 2^24 covers any projected map
NULL_CELL = (1 << 40, 1 << 40)   # where the features without a shape are kept
SKIP = {"fid", "objectid", "shape_area", "shape_length", "shape_leng", "shape_len", "shape__area",
        "shape__length", "st_area(shape)", "st_length(shape)"}   # numbers each program writes again by itself
IDENTITY = ("globalid", "guid", "uuid", "uid", "objectid", "id")
SHORT = 16               # hex digits of a square's fingerprint kept in a certificate
SEALS = ("commit", "sums", "quality")    # roots a certificate may add, each covered by its code when every layer has it
CODE_KEYS = ("name", "root", "reach") + SEALS   # what of a certificate's layer a proof carries to make its code again


class FingerprintError(Exception):
    pass


def grid_for(layer):
    """(square size, coordinate step) in the layer's units."""
    if layer.crs().isGeographic():
        return 2.0 ** -11, 1e-9          # about 54 m and a tenth of a millimetre
    return 64.0, 1e-4


def field_names(layer):
    """The fields that are the data: not the file's own id, not the areas and lengths a program computes."""
    keys = {layer.fields().at(i).name().lower() for i in layer.primaryKeyAttributes()}
    return [f.name().lower() for f in layer.fields() if f.name().lower() not in keys and f.name().lower() not in SKIP]


def common_fields(a, b):
    fb = set(field_names(b))
    return [n for n in field_names(a) if n in fb]


def guess_identity(names):
    for n in IDENTITY:
        if n in names:
            return n
    for n in names:      # PARCEL_ID, PARCELID, plot_id: not the file's own fid
        if n != "fid" and n.endswith("id") and len(n) > 3:
            return n
    return ""


# ---- one feature ------------------------------------------------------------------------------------
def _value(v):
    v = encode(v)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        v = round(v, 6)
        return int(v) if v.is_integer() else v
    if isinstance(v, str):
        return v.rstrip()
    return v


def shape_key(g, step):
    """The shape written one way: one part is not a multi-part, polygons turn the same way, coordinates on a grid."""
    if g is None or g.isNull() or g.isEmpty():
        return None
    g = QgsGeometry(g)
    a = g.constGet()
    if QgsWkbTypes.isMultiType(a.wkbType()) and a.numGeometries() == 1:
        g.convertToSingleType()
    if g.type() == QgsWkbTypes.PolygonGeometry:
        g = g.forceRHR()
    a = g.constGet()
    t = a.wkbType()
    z, m = QgsWkbTypes.hasZ(t), QgsWkbTypes.hasM(t)

    def q(v):
        return int(math.floor(v / step + 0.5)) if math.isfinite(v) else None

    seq = [[[[q(p.x()), q(p.y())] + ([q(p.z())] if z else []) + ([q(p.m())] if m else []) for p in ring]
            for ring in part] for part in a.coordinateSequence()]
    return [QgsWkbTypes.displayString(t), seq]


def _j(data):
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _print_of(sk, vals):
    return hashlib.sha256(_j([sk, vals])).hexdigest()[:32]


def feature_print(feat, idx, step):
    """The fingerprint of one feature (32 hex digits) from its shape and the values of the fields at idx."""
    return _print_of(shape_key(feat.geometry(), step), [[n, _value(feat.attribute(i))] for n, i in idx])


def shape_hash(sk):
    return hashlib.sha256(b"shape\n" + _j(sk)).hexdigest()[:32]


def values_hash(vals):
    return hashlib.sha256(b"values\n" + _j(vals)).hexdigest()[:32]


def reach_box(g, sk, step):
    """The box a shape reaches, in steps: [x0, y0, x1, y1], or None. From the coordinates as they are fingerprinted,
    so every copy gets the same box; a curve can bulge past its points, so its box is the curve's own, widened."""
    if sk is None:
        return None
    if QgsWkbTypes.isCurvedType(g.constGet().wkbType()):
        r = g.boundingBox()
        return [int(math.floor(r.xMinimum() / step)) - 1, int(math.floor(r.yMinimum() / step)) - 1,
                int(math.ceil(r.xMaximum() / step)) + 1, int(math.ceil(r.yMaximum() / step)) + 1]
    xs, ys = [], []
    for part in sk[1]:
        for ring in part:
            for p in ring:
                if p[0] is not None and p[1] is not None:
                    xs.append(p[0])
                    ys.append(p[1])
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def box_text(b):
    return "-" if b is None else ",".join(str(v) for v in b)


def box_union(boxes):
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def reach_item(sh, vh, b):
    return f"{sh}:{vh}:{box_text(b)}"


def reach_leaf(items):
    """items: [(sh, vh, box)] -> (fingerprint, box)"""
    return _h(["reach-leaf"] + sorted(reach_item(*i) for i in items)), box_union([i[2] for i in items])


def reach_entry(k, h, b):
    return f"{k[1]},{k[2]}={h}@{box_text(b)}"


def reach_up(entries, top=False):
    """entries: [(key, fingerprint, box)] of the squares under one -> (fingerprint, box)"""
    return (_h(["reach-root" if top else "reach-node"] + sorted(reach_entry(*e) for e in entries)),
            box_union([e[2] for e in entries]))


def field_seed(prints):
    """The salt of the commitments: from the fingerprints of every feature, so only the full data gives it."""
    return hashlib.sha256(("fields-seed\n" + "\n".join(sorted(prints))).encode("ascii")).digest()


def field_salt(seed, sh, name):
    return hmac.new(seed, f"{sh}\n{name}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def field_commit(salt, name, value):
    return hashlib.sha256(b"field\n" + _j([salt, name, value])).hexdigest()[:32]


def field_item(sh, commits):
    """One feature's commitments: {field: commitment} -> its seal."""
    return hashlib.sha256(_j(["field-item", sh, sorted(commits.items())])).hexdigest()[:32]


def field_root(items):
    return hashlib.sha256(("field-root\n" + "\n".join(sorted(items))).encode("ascii")).hexdigest()


def _h(parts):
    return hashlib.sha256("\n".join(parts).encode("ascii")).hexdigest()


# ---- the sums ---------------------------------------------------------------------------------------
MEASURE_SCALE = 1000          # amounts kept in thousandths of the layer's unit (mm, or 0.001 m²)
VALUE_SCALE = 1000000         # field totals in millionths: whole numbers, no float drift


def _ellipsoid(crs):
    da = QgsDistanceArea()
    c = QgsCoordinateReferenceSystem(crs)
    da.setSourceCrs(c, QgsProject.instance().transformContext())
    da.setEllipsoid(c.ellipsoidAcronym() or "EPSG:7030")
    return da


def snapped(g, step):
    """The shape with every vertex on the grid it is fingerprinted on: what is measured, so a copy whose
    coordinates moved inside their own step (a GPKG, a shapefile, a marked copy) measures the same."""
    g = QgsGeometry(g)
    a = g.get()
    for i in range(a.nCoordinates()):
        p = g.vertexAt(i)
        g.moveVertex(math.floor(p.x() / step + 0.5) * step, math.floor(p.y() / step + 0.5) * step, i)
    return g


def _shoelace2(ring):
    """Twice the area of a ring of whole-number points, exact."""
    s = 0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        s += x0 * y1 - x1 * y0
    return abs(s)


def measure(g, sk, step, crs="", geographic=False):
    """The amount a feature adds: its area (polygons) or its length (lines), in thousandths of the unit, from the
    coordinates as they are fingerprinted. Points and features without a shape add nothing."""
    if sk is None:
        return 0
    kind = g.type()
    if kind not in (QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry):
        return 0
    if geographic or QgsWkbTypes.isCurvedType(g.constGet().wkbType()):
        sg = snapped(g, step)
        if geographic:
            da = _ellipsoid(crs)
            v = da.measureArea(sg) if kind == QgsWkbTypes.PolygonGeometry else da.measureLength(sg)
        else:
            v = sg.area() if kind == QgsWkbTypes.PolygonGeometry else sg.length()
        return int(round(v * MEASURE_SCALE))
    if kind == QgsWkbTypes.PolygonGeometry:
        twice = 0
        for part in sk[1]:
            rings = [[(p[0], p[1]) for p in r] for r in part]
            if rings:
                twice += _shoelace2(rings[0]) - sum(_shoelace2(r) for r in rings[1:])
        return int(round(twice * step * step * MEASURE_SCALE / 2))
    total = 0.0
    for part in sk[1]:
        for r in part:
            for a, b in zip(r, r[1:]):
                total += math.hypot(b[0] - a[0], b[1] - a[1])
    return int(round(total * step * MEASURE_SCALE))


def fixed(v):
    """A field's value as a whole number of millionths (0 when it is empty or not a number)."""
    if isinstance(v, bool):
        return int(v) * VALUE_SCALE
    if isinstance(v, int):
        return v * VALUE_SCALE
    if isinstance(v, float) and math.isfinite(v):
        return int(round(v * VALUE_SCALE))
    return 0


def _hu(parts):
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def sums_text(s):
    return ":".join(str(v) for v in s)


def sums_add(vectors, width):
    out = [0] * width
    for v in vectors:
        for i, x in enumerate(v):
            out[i] += x
    return out


def sums_hash(s):
    """One feature's sums, sealed: a feature outside an area shows this, not its numbers."""
    return hashlib.sha256(("sums\n" + sums_text(s)).encode("ascii")).hexdigest()[:32]


def sum_item(sh, vh, b, hs):
    return f"{sh}:{vh}:{box_text(b)}#{hs}"


def sum_leaf(items, width):
    """items: [(sh, vh, box, sums)] -> (fingerprint, box, sums)"""
    return (_h(["sum-leaf"] + sorted(sum_item(sh, vh, b, sums_hash(s)) for sh, vh, b, s in items)),
            box_union([i[2] for i in items]), sums_add([i[3] for i in items], width))


def sum_leaf_open(items, sums):
    """items: [(sh, vh, box, sealed sums)], with the square's sums as given -> (fingerprint, box, sums)"""
    return _h(["sum-leaf"] + sorted(sum_item(*i) for i in items)), box_union([i[2] for i in items]), sums


def sum_entry(k, h, b, s):
    return f"{k[1]},{k[2]}={h}@{box_text(b)}#{sums_text(s)}"


def sum_up(entries, width):
    """entries: [(key, fingerprint, box, sums)] of the squares under one -> (fingerprint, box, sums)"""
    return (_h(["sum-node"] + sorted(sum_entry(*e) for e in entries)), box_union([e[2] for e in entries]),
            sums_add([e[3] for e in entries], width))


def sum_root(entries, sum_fields, width):
    """The top of the sums, with what each number is."""
    h = _hu(["sum-root", json.dumps(sum_fields, ensure_ascii=False)] + sorted(sum_entry(*e) for e in entries))
    return h, sums_add([e[3] for e in entries], width)


# ---- the squares ------------------------------------------------------------------------------------
class Tree:
    """The fingerprints of one layer: features by square (level 0), squares up to the top."""

    def __init__(self, cell, step, fields, crs, identity=""):
        self.cell, self.step, self.fields, self.crs, self.identity = cell, step, list(fields), crs, identity
        self.leaf = {}       # (ix, iy) -> [(fingerprint, fid)]
        self.nodes = {}      # (level, ix, iy) -> fingerprint
        self.kids = {}       # (level, ix, iy) -> [(level - 1, ix, iy)]
        self.ident = {}      # fid -> identity value
        self.items = {}      # (ix, iy) -> [(shape hash, values hash, box, fid)]: the reach
        self.reach_nodes = {}   # (level, ix, iy) -> (fingerprint, box)
        self.count = 0
        self.root = ""
        self.reach = ""
        self.commit = ""     # the root of the commitments, when they were made
        self.rows = {}       # fid -> (shape hash, values, feature seal): when they were made
        self.seed = b""
        self.sum_fields = None  # the numeric fields totalled, when the sums were made
        self.sitems = {}     # (ix, iy) -> [(shape hash, values hash, box, sums, fid)]
        self.sum_nodes = {}  # (level, ix, iy) -> (fingerprint, box, sums)
        self.sums = ""       # the root of the sums
        self.totals = []     # [count, amount, field totals...] of the whole layer

    def settings(self):
        return {"cell": self.cell, "step": self.step, "fields": self.fields, "crs": self.crs,
                "identity": self.identity}

    def cell_of(self, g):
        if g is None or g.isNull() or g.isEmpty():
            return NULL_CELL
        c = g.boundingBox().center()
        return int(math.floor(c.x() / self.cell)), int(math.floor(c.y() / self.cell))

    def rect(self, key):
        ix, iy = key
        return QgsRectangle(ix * self.cell, iy * self.cell, (ix + 1) * self.cell, (iy + 1) * self.cell)

    def leaf_print(self, key):
        return self.nodes.get((0,) + tuple(key), "")

    def finish(self):
        self.nodes = {}
        self.kids = {}
        for key, items in self.leaf.items():
            self.nodes[(0,) + key] = _h(sorted(p for p, _f in items))
        level_keys = [k for k in self.nodes]
        for level in range(1, TOP + 1):
            up = {}
            for k in level_keys:
                p = (level, k[1] >> 1, k[2] >> 1)
                up.setdefault(p, []).append(k)
            for p, ks in up.items():
                self.kids[p] = ks
                self.nodes[p] = _h(sorted(f"{k[1]},{k[2]}={self.nodes[k]}" for k in ks))
            level_keys = list(up)
        self.root = _h(sorted(f"{k[1]},{k[2]}={self.nodes[k]}" for k in level_keys)) if level_keys else _h([])
        self.reach_nodes = {}
        for key, items in self.items.items():
            self.reach_nodes[(0,) + key] = reach_leaf([i[:3] for i in items])
        for p in sorted(self.kids, key=lambda k: k[0]):      # the lower squares first
            self.reach_nodes[p] = reach_up([(k,) + self.reach_nodes[k] for k in self.kids[p]])
        self.reach = reach_up([(k,) + self.reach_nodes[k] for k in self.top()], top=True)[0]
        if self.sum_fields is not None:
            w = 2 + len(self.sum_fields)
            self.sum_nodes = {}
            for key, items in self.sitems.items():
                self.sum_nodes[(0,) + key] = sum_leaf([i[:4] for i in items], w)
            for p in sorted(self.kids, key=lambda k: k[0]):
                self.sum_nodes[p] = sum_up([(k,) + self.sum_nodes[k] for k in self.kids[p]], w)
            self.sums, self.totals = sum_root([(k,) + self.sum_nodes[k] for k in self.top()], self.sum_fields, w)
        return self

    def top(self):
        return sorted(k for k in self.nodes if k[0] == TOP)


def build(layer, fields=None, cell=None, step=None, identity="", feedback=None, commit=False, sums=False):
    """The fingerprints of a vector layer as it is now (its unsaved edits included); with ``commit`` its
    commitments too (every value sealed on its own), with ``sums`` the sums of its squares."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        raise FingerprintError("not a vector layer")
    c0, s0 = grid_for(layer)
    fields = list(fields) if fields is not None else field_names(layer)
    t = Tree(cell or c0, step or s0, fields, layer.crs().authid() or layer.crs().toWkt(), identity)
    lower = {f.name().lower(): i for i, f in enumerate(layer.fields())}
    missing = [n for n in fields if n not in lower]
    if missing:
        raise FingerprintError("fields missing: " + ", ".join(missing))
    idx = [(n, lower[n]) for n in fields]
    id_i = lower.get(identity) if identity else None
    geographic = layer.crs().isGeographic()
    if sums:
        t.sum_fields = [n for n, i in idx if layer.fields().at(i).isNumeric()]
        sidx = [lower[n] for n in t.sum_fields]
    total = max(1, layer.featureCount())
    for n, f in enumerate(layer.getFeatures(QgsFeatureRequest())):
        g = f.geometry()
        key = t.cell_of(g)
        sk = shape_key(g, t.step)
        vals = [[nm, _value(f.attribute(i))] for nm, i in idx]
        p = _print_of(sk, vals)
        sh = shape_hash(sk)
        t.leaf.setdefault(key, []).append((p, f.id()))
        vh, box = values_hash(vals), reach_box(g, sk, t.step)
        t.items.setdefault(key, []).append((sh, vh, box, f.id()))
        if sums:
            sv = [1, measure(g, sk, t.step, t.crs, geographic)] + [fixed(_value(f.attribute(i))) for i in sidx]
            t.sitems.setdefault(key, []).append((sh, vh, box, sv, f.id()))
        if commit:
            t.rows[f.id()] = (sh, vals, p)
        if id_i is not None:
            v = _value(f.attribute(id_i))
            if v is not None:
                t.ident[f.id()] = v
        t.count += 1
        if feedback and n % 2000 == 0:
            if feedback(100.0 * n / total) is False:
                raise FingerprintError("cancelled")
    if commit:
        seal(t)
    return t.finish()


def seal(t):
    """The commitments of a tree built with its rows."""
    t.seed = field_seed([p for _sh, _v, p in t.rows.values()])
    items = []
    for fid, (sh, vals, p) in t.rows.items():
        c = {n: field_commit(field_salt(t.seed, sh, n), n, v) for n, v in vals}
        it = field_item(sh, c)
        t.rows[fid] = (sh, vals, it)
        items.append(it)
    t.commit = field_root(items)


def layer_codes(layers):
    """What a certificate's code is made of: the top fingerprints, with the reaches and the commitments when it has
    them."""
    roots = [e["root"] for e in layers]
    if layers and all(e.get("reach") for e in layers):
        roots += [e["reach"] for e in layers]
        for k in SEALS:
            if all(e.get(k) for e in layers):
                roots += [e[k] for e in layers]
    return roots


def code_part(e):
    """The part of a certificate's layer a proof carries: enough to make the code again, not the squares."""
    return {k: e[k] for k in CODE_KEYS if e.get(k)}


def code(roots):
    """A short code for the top fingerprints of a delivery, to read on the phone: XXXX-XXXX-XXXX-XXXX."""
    d = hashlib.sha256("|".join(roots).encode("ascii")).digest()[:10]
    s = base64.b32encode(d).decode("ascii")
    return "-".join(s[i:i + 4] for i in range(0, 16, 4))


# ---- two copies -------------------------------------------------------------------------------------
def compatible(a, b):
    """None, or why the two cannot be compared fingerprint by fingerprint."""
    if a.crs != b.crs:
        return "crs"
    if a.cell != b.cell or a.step != b.step:
        return "grid"
    if a.fields != b.fields:
        return "fields"
    return None


class Diff:
    def __init__(self):
        self.cells = []          # (ix, iy) of the smallest squares that differ
        self.only_a = []         # fids of A not in B (as they are)
        self.only_b = []
        self.changed = []        # (fid in A, fid in B): the same identity, not the same feature
        self.compared = 0        # fingerprints compared to find the squares
        self.same = False

    @property
    def added(self):
        return self.only_a

    @property
    def removed(self):
        return self.only_b


def compare(a, b):
    """Where A differs from B: the squares, then the features in them."""
    why = compatible(a, b)
    if why:
        raise FingerprintError(why)
    d = Diff()
    d.compared = 1
    if a.root == b.root:
        d.same = True
        return d
    frontier = {k for k in a.nodes if k[0] == TOP} | {k for k in b.nodes if k[0] == TOP}
    for level in range(TOP, -1, -1):
        nxt = set()
        for k in frontier:
            d.compared += 1
            if a.nodes.get(k) == b.nodes.get(k):
                continue
            if level == 0:
                d.cells.append((k[1], k[2]))
            else:
                nxt.update(a.kids.get(k, ()))
                nxt.update(b.kids.get(k, ()))
        frontier = nxt
    d.cells.sort()
    for key in d.cells:
        ia, ib = a.leaf.get(key, []), b.leaf.get(key, [])
        ca, cb = Counter(p for p, _f in ia), Counter(p for p, _f in ib)
        oa, ob = ca - cb, cb - ca
        for p, fid in ia:
            if oa.get(p):
                oa[p] -= 1
                d.only_a.append(fid)
        for p, fid in ib:
            if ob.get(p):
                ob[p] -= 1
                d.only_b.append(fid)
    if a.identity and b.identity:
        by_b = {}
        for fid in d.only_b:
            v = b.ident.get(fid)
            if v is not None:
                by_b.setdefault(json.dumps(v, default=str), []).append(fid)
        still_a = []
        for fid in d.only_a:
            v = a.ident.get(fid)
            lst = by_b.get(json.dumps(v, default=str)) if v is not None else None
            if lst:
                d.changed.append((fid, lst.pop(0)))
            else:
                still_a.append(fid)
        taken = {fb for _fa, fb in d.changed}
        d.only_a = still_a
        d.only_b = [f for f in d.only_b if f not in taken]
    return d


def _copy_values(src, dst_layer, dst_feat):
    """Values of src into dst_feat by field name (not the file's own id)."""
    keys = set(dst_layer.primaryKeyAttributes())
    names = {f.name().lower(): i for i, f in enumerate(src.fields())}
    out = {}
    for i, f in enumerate(dst_layer.fields()):
        if i in keys:
            continue
        j = names.get(f.name().lower())
        if j is not None:
            out[i] = src.attribute(j)
    return out


def bring(diff, a_layer, b_layer):
    """Make B like A in the squares that differ, in B's edits (undo and save as with any edit).
    Returns {"added", "removed", "changed"}."""
    if not b_layer.isEditable() and not b_layer.startEditing():
        raise FingerprintError("the layer cannot be edited")
    wanted = set(diff.only_a) | {fa for fa, _fb in diff.changed}
    src = {f.id(): f for f in a_layer.getFeatures(QgsFeatureRequest().setFilterFids(list(wanted)))} if wanted else {}
    b_layer.beginEditCommand("fingerprint: bring the changes")
    try:
        for fa, fb in diff.changed:
            f = src[fa]
            b_layer.changeGeometry(fb, QgsGeometry(f.geometry()))
            for i, v in _copy_values(f, b_layer, None).items():
                b_layer.changeAttributeValue(fb, i, v)
        if diff.only_b:
            b_layer.deleteFeatures(list(diff.only_b))
        new = []
        for fa in diff.only_a:
            f = src[fa]
            nf = QgsFeature(b_layer.fields())
            nf.setGeometry(QgsGeometry(f.geometry()))
            for i, v in _copy_values(f, b_layer, nf).items():
                nf.setAttribute(i, v)
            new.append(nf)
        if new:
            b_layer.addFeatures(new)
    except Exception:
        b_layer.destroyEditCommand()
        raise
    b_layer.endEditCommand()
    return {"added": len(diff.only_a), "removed": len(diff.only_b), "changed": len(diff.changed)}


# ---- the certificate --------------------------------------------------------------------------------
def certificate(entries):
    """entries: [(layer name, tree)]. The fingerprints of a delivery, as a JSON-ready dict."""
    layers = []
    for name, t in entries:
        e = dict(t.settings(), name=name, features=t.count, root=t.root, reach=t.reach,
                 cells={f"{k[0]},{k[1]}": t.leaf_print(k)[:SHORT] for k in sorted(t.leaf)})
        if t.commit:
            e["commit"] = t.commit
        if t.sums:
            e["sums"] = t.sums
        layers.append(e)
    return {"format": FORMAT, "kind": "biruni-fingerprint",
            "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "code": code(layer_codes(layers)), "layers": layers}


def _key(text):
    x, y = text.split(",")
    return int(x), int(y)


def tree_for(layer, entry, feedback=None):
    """The layer's fingerprints made the way the certificate's were."""
    return build(layer, fields=entry["fields"], cell=entry["cell"], step=entry["step"],
                 identity=entry.get("identity", ""), feedback=feedback, commit=bool(entry.get("commit")),
                 sums=bool(entry.get("sums")))


def check(layer, entry, feedback=None):
    """The layer against one layer of a certificate: {"same", "cells": squares that differ, "features": now}."""
    names = set(field_names(layer)) | {f.name().lower() for f in layer.fields()}
    lost = [n for n in entry["fields"] if n not in names]
    if lost:
        return {"same": False, "fields": lost, "cells": [], "features": layer.featureCount(), "tree": None}
    t = tree_for(layer, entry, feedback)
    if (layer.crs().authid() or layer.crs().toWkt()) != entry["crs"]:
        return {"same": False, "crs": True, "cells": [], "features": t.count, "tree": t}
    if t.root == entry["root"]:
        if (entry.get("reach") and entry["reach"] != t.reach) or \
                (entry.get("commit") and entry["commit"] != t.commit) or \
                (entry.get("sums") and entry["sums"] != t.sums):     # the data is right, the seals of it are not
            return {"same": False, "reach": True, "cells": [], "features": t.count, "tree": t}
        return {"same": True, "cells": [], "features": t.count, "tree": t}
    theirs = {_key(k): v for k, v in entry["cells"].items()}
    keys = set(theirs) | set(t.leaf)
    cells = sorted(k for k in keys if theirs.get(k, "") != t.leaf_print(k)[:SHORT])
    return {"same": False, "cells": cells, "features": t.count, "tree": t}


# ---- a package of changes, to carry -----------------------------------------------------------------
def _state(f):
    g = f.geometry()
    return {"g": bytes(g.asWkb()).hex() if g is not None and not g.isNull() else None,
            "a": {fl.name(): encode(f.attribute(i)) for i, fl in enumerate(f.fields())}}


def make_package(layer, entry, feedback=None):
    """The squares where this layer differs from the other copy's certificate, with this layer's features in them.
    None when the two are the same."""
    res = check(layer, entry, feedback)
    if res.get("fields"):
        raise FingerprintError("fields missing: " + ", ".join(res["fields"]))
    if res.get("crs"):
        raise FingerprintError("crs")
    if res.get("reach"):
        raise FingerprintError("reach")
    if res["same"]:
        return None
    t = res["tree"]
    theirs = {_key(k): v for k, v in entry["cells"].items()}
    fids = [fid for k in res["cells"] for _p, fid in t.leaf.get(k, [])]
    feats = {f.id(): f for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(fids))} if fids else {}
    cells = {}
    for k in res["cells"]:
        cells[f"{k[0]},{k[1]}"] = {"base": theirs.get(k, ""),
                                   "features": [dict(_state(feats[fid]), p=p) for p, fid in t.leaf.get(k, [])]}
    return {"format": FORMAT, "kind": "biruni-changes", "layer": entry["name"], "root": t.root,
            "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "settings": {k: entry[k] for k in ("cell", "step", "fields", "crs", "identity") if k in entry},
            "cells": cells}


def write_json(obj, path):
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if path.endswith(".json"):
        with open(path, "wb") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))
    else:
        with gzip.open(path, "wb") as f:
            f.write(data)


def read_json(path):
    with open(path, "rb") as f:
        head = f.read(2)
    opener = gzip.open if head == b"\x1f\x8b" else open
    with opener(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def apply_package(layer, pkg, feedback=None):
    """Bring a package into this layer, in its edits. A square that changed here since the certificate the package
    was made against is left as it is (both copies changed it: someone decides). Returns the counts and the
    squares left."""
    s = pkg["settings"]
    t = build(layer, fields=s["fields"], cell=s["cell"], step=s["step"], identity=s.get("identity", ""),
              feedback=feedback)
    if t.crs != s["crs"]:
        raise FingerprintError("crs")
    if not layer.isEditable() and not layer.startEditing():
        raise FingerprintError("the layer cannot be edited")
    added = removed = 0
    left = []
    fields = layer.fields()
    keys = set(layer.primaryKeyAttributes())
    layer.beginEditCommand("fingerprint: apply a package")
    try:
        for ks, c in sorted(pkg["cells"].items()):
            k = _key(ks)
            if t.leaf_print(k)[:SHORT] != c["base"]:
                left.append(k)
                continue
            want = Counter(f["p"] for f in c["features"])
            have = Counter(p for p, _fid in t.leaf.get(k, []))
            gone = have - want
            drop = []
            for p, fid in t.leaf.get(k, []):
                if gone.get(p):
                    gone[p] -= 1
                    drop.append(fid)
            if drop:
                layer.deleteFeatures(drop)
                removed += len(drop)
            more = want - have
            new = []
            for st in c["features"]:
                if not more.get(st["p"]):
                    continue
                more[st["p"]] -= 1
                nf = QgsFeature(fields)
                g = QgsGeometry()
                if st.get("g"):
                    g.fromWkb(bytes.fromhex(st["g"]))
                nf.setGeometry(g)
                low = {n.lower(): v for n, v in (st.get("a") or {}).items()}
                for i, fl in enumerate(fields):
                    if i not in keys and fl.name().lower() in low:
                        nf.setAttribute(i, decode(low[fl.name().lower()]))
                new.append(nf)
            if new:
                layer.addFeatures(new)
                added += len(new)
    except Exception:
        layer.destroyEditCommand()
        raise
    layer.endEditCommand()
    return {"added": added, "removed": removed, "left": left}
