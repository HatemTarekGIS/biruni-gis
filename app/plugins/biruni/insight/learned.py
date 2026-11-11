"""Rules learned from the data («قواعد تتعلّمها من بياناتك»): the implicit rules nobody wrote down, discovered
from the layers themselves, each with its support and the features that break it.

Rule kinds:
- inside: features of A lie inside a feature of B ("99.6% of buildings lie inside a parcel")
- no_overlap: features of a polygon layer do not overlap each other
- typical: a numeric field takes a few typical values (road widths of 8 / 12 / 16 m)
- typical_width: the measured width of long polygons takes a few typical values
- right_angles: the corners of the polygons are right angles (within ±1°)
- setback: features stay at least d m from the edge of the feature they lie in
- depends: the value of one field decides the value of another (land use -> zoning class)
- depends_max: the value of one field sets the maximum of a number (land use -> floors)
- unique: an id field is unique

A rule is reported only when its support is high (MIN_SUPPORT) and it has exceptions to show: a rule that
holds everywhere tells nothing new, and one that holds for 70% is not a rule.

broken_rules() is for the drawing tool: give it a candidate geometry / attributes and it returns the learned
rules the candidate would break, checked against the live layers (no snapshot is kept for that).
"""
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry, QgsProject, QgsRectangle,
                       QgsVectorLayer)

from ..charter.learn import dependency
from ..i18n import tr
from ..quality import topology as topo
from . import common as c

MIN_SUPPORT = 0.90
MIN_FEATURES = 20
RIGHT_TOL = 1.0         # degrees: a corner within this of 90° is a right angle
NEAR_RIGHT = 10.0       # degrees: a corner off by more than RIGHT_TOL but less than this is a "near miss"
STRAIGHT = 10.0         # degrees: a vertex turning less than this is not a corner
INSIDE_SHARE = 0.98     # share of a feature's area that must lie in the container
MAX_EXCEPTIONS = 5000

TEXTS = {
    "inside": "{support} of {layer} features lie inside a feature of {other}",
    "no_overlap": "{support} of {layer} features overlap no other feature of the layer",
    "typical": "{field} of {layer} takes the values {values} in {support} of features",
    "typical_width": "The width of {layer} features is {values} m in {support} of features",
    "right_angles": "{support} of the corners of {layer} are right angles (within ±1°)",
    "setback": "{layer} features are at least {d} m from the edge of {other} in {support} of cases",
    "depends": "In {layer}, {a} decides {b} in {support} of features (e.g. {example})",
    "depends_max": "In {layer}, {a} sets the maximum of {b} in {support} of features (e.g. {example})",
    "unique": "{field} of {layer} is unique in {support} of features",
}
KINDS = {
    "inside": "Lies inside",
    "no_overlap": "No overlaps",
    "typical": "Typical values",
    "typical_width": "Typical widths",
    "right_angles": "Right angles",
    "setback": "Setback",
    "depends": "Field decides field",
    "depends_max": "Field sets a maximum",
    "unique": "Unique",
}
NOTES = {
    "outside": "Outside every feature of {other}",
    "partly": "Only {share} inside a feature of {other}",
    "overlap": "Overlaps feature {other} by {area} m²",
    "value": "Value {value}",
    "width": "Width {value} m",
    "corner": "Corner of {angle}°",
    "distance": "{d} m from the edge",
    "pair": "«{a}» with «{b}»",
    "above": "{b} = {value}, above {cap} for «{a}»",
    "duplicate": "«{value}» is used by {n} features",
}


@dataclass
class Rule:
    kind: str
    layer_id: str
    layer_name: str
    support: float
    total: int
    values: dict = field(default_factory=dict)       # for the sentence (TEXTS)
    params: dict = field(default_factory=dict)       # what broken_rules() needs
    exceptions: list = field(default_factory=list)   # [(fid, note key, {values})]

    def text(self):
        v = dict(self.values, support=f"{self.support * 100:.1f}%", layer=self.layer_name)
        return tr(TEXTS[self.kind]).format(**v)

    def kind_label(self):
        return tr(KINDS[self.kind])

    @property
    def fids(self):
        return [e[0] for e in self.exceptions]

    @staticmethod
    def note(key, values):
        return tr(NOTES[key]).format(**values)


@dataclass
class Violation:
    rule: Rule
    note: str           # key of NOTES
    values: dict = field(default_factory=dict)

    def text(self):
        return self.rule.text() + " - " + Rule.note(self.note, self.values)

    def short(self):
        return Rule.note(self.note, self.values)


_CACHE = {}             # layer id -> [Rule] from the last mining


def _report(rule):
    return rule.support >= MIN_SUPPORT and rule.exceptions and rule.total >= MIN_FEATURES


# ---- geometry helpers --------------------------------------------------------------------------------------
def _rings(g):
    if g.isMultipart():
        polys = g.asMultiPolygon()
    else:
        polys = [g.asPolygon()]
    for poly in polys:
        for ring in poly:
            yield ring


def corner_angles(g):
    """Interior angles (degrees, 0..360) of the real corners of a polygon - vertices that turn by at least
    STRAIGHT degrees."""
    out = []
    if g.type() != Qgis.GeometryType.Polygon:
        return out
    for ring in _rings(g):
        pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
        n = len(pts)
        if n < 3:
            continue
        for i in range(n):
            a, b, d = pts[i - 1], pts[i], pts[(i + 1) % n]
            h1 = math.atan2(b.y() - a.y(), b.x() - a.x())
            h2 = math.atan2(d.y() - b.y(), d.x() - b.x())
            turn = math.degrees((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
            if abs(turn) < STRAIGHT:
                continue
            out.append(180.0 - abs(turn))
    return out


def width_of(area, perimeter):
    """Width of the rectangle with this area and perimeter (the short side); the measure of a road polygon."""
    disc = perimeter * perimeter - 16 * area
    if area <= 0 or perimeter <= 0:
        return 0.0
    return (perimeter - math.sqrt(max(disc, 0.0))) / 4


def clusters(values, rel_tol=0.01, abs_tol=1e-9, max_clusters=8):
    """Typical values of {fid: number}: greedy - the most frequent value first, absorbing everything within
    tolerance, while a cluster still holds max(3, 3%) of the values. Returns (centres, covered fids)."""
    n = len(values)
    if not n:
        return [], set()
    counts = Counter(round(v, 6) for v in values.values())
    floor = max(3, 0.03 * n)
    centres, covered = [], set()
    left = dict(values)
    for v, _k in counts.most_common():
        if len(centres) >= max_clusters:
            break
        if any(abs(v - ce) <= max(abs_tol, rel_tol * abs(ce)) for ce in centres):
            continue
        tol = max(abs_tol, rel_tol * abs(v))
        members = [fid for fid, x in left.items() if abs(x - v) <= tol]
        if len(members) < floor:
            break
        centres.append(v)
        covered.update(members)
        for fid in members:
            del left[fid]
    return sorted(centres), covered


def _near_typical(x, centres, rel_tol, abs_tol):
    return any(abs(x - ce) <= max(abs_tol, rel_tol * abs(ce)) for ce in centres)


# ---- mining, one kind at a time ----------------------------------------------------------------------------
def _mine_unique(snap):
    out = []
    for fld in snap.fields:
        kind = snap.kinds.get(fld)
        if kind not in ("text", "int"):
            continue
        col = snap.column(fld)
        n = len(col)
        if n < MIN_FEATURES:
            continue
        keys = {fid: c.value_key(v) for fid, v in col.items()}
        counts = Counter(keys.values())
        if len(counts) < 0.9 * n:
            continue
        if kind == "int":
            nums = sorted(v for v in col.values() if isinstance(v, int))
            spread = (nums[int(0.95 * (len(nums) - 1))] - nums[int(0.05 * (len(nums) - 1))]) / 0.9 if nums else 0
            pairs = sum(m * (m - 1) / 2 for m in counts.values())
            if spread > 3 * n and pairs > 0.25 * n * n / (2 * spread):
                continue    # no more repeats than chance would give: a measure, not a numbering
        dup = {k for k, m in counts.items() if m > 1}
        unique_share = sum(1 for k in keys.values() if k not in dup) / n
        ex = [(fid, "duplicate", {"value": col[fid], "n": counts[k]}) for fid, k in keys.items() if k in dup]
        r = Rule("unique", snap.layer_id, snap.name, unique_share, n, {"field": fld}, {"field": fld},
                 ex[:MAX_EXCEPTIONS])
        if _report(r):
            out.append(r)
    return out


def _mine_typical(snap):
    out = []
    for fld in snap.fields:
        if snap.kinds.get(fld) not in ("int", "real"):
            continue
        col = snap.column(fld)
        nums = {fid: float(v) for fid, v in col.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        n = len(nums)
        if n < MIN_FEATURES:
            continue
        centres, covered = clusters(nums, 0.01)
        if not centres:
            continue
        support = len(covered) / n
        ex = [(fid, "value", {"value": c.fmt(x, 3)}) for fid, x in nums.items() if fid not in covered]
        if len(Counter(round(x, 6) for x in nums.values())) <= len(centres):
            continue
        r = Rule("typical", snap.layer_id, snap.name, support, n,
                 {"field": fld, "values": " / ".join(c.fmt(v, 3) for v in centres)},
                 {"field": fld, "centres": centres, "rel": 0.01, "abs": 1e-9}, ex[:MAX_EXCEPTIONS])
        if _report(r):
            out.append(r)
    return out


def _mine_widths(snap, area_fn, length_fn):
    """Typical widths of long polygons (roads, canals): only when most of the layer is elongated."""
    if snap.gtype != Qgis.GeometryType.Polygon or len(snap.geoms) < MIN_FEATURES:
        return []
    widths, elongated = {}, 0
    for fid, g in snap.geoms.items():
        a = area_fn(g)
        p = length_fn(g)  # the perimeter
        if a <= 0:
            continue
        if p * p / a >= 25:
            elongated += 1
        widths[fid] = width_of(a, p)
    if elongated < 0.8 * len(widths):
        return []
    rounded = {fid: round(w * 2) / 2 for fid, w in widths.items()}
    centres, covered = clusters(rounded, 0.04, 0.5)
    if not centres:
        return []
    covered = {fid for fid, w in widths.items() if _near_typical(w, centres, 0.04, 0.5)}
    ex = [(fid, "width", {"value": c.fmt(w, 1)}) for fid, w in widths.items() if fid not in covered]
    r = Rule("typical_width", snap.layer_id, snap.name, len(covered) / len(widths), len(widths),
             {"values": " / ".join(c.fmt(v, 1) for v in centres)}, {"centres": centres, "rel": 0.04, "abs": 0.5},
             ex[:MAX_EXCEPTIONS])
    return [r] if _report(r) else []


def _mine_right_angles(snap):
    if snap.gtype != Qgis.GeometryType.Polygon or len(snap.geoms) < MIN_FEATURES:
        return []
    right = total = 0
    ex = []
    for fid, g in snap.geoms.items():
        worst = None
        for ang in corner_angles(g):
            total += 1
            off = min(abs(ang - 90), abs(ang - 270))
            if off <= RIGHT_TOL:
                right += 1
            elif off < NEAR_RIGHT and (worst is None or off > worst[0]):
                worst = (off, ang)
        if worst is not None:
            ex.append((fid, "corner", {"angle": c.fmt(worst[1], 1)}))
    if not total:
        return []
    r = Rule("right_angles", snap.layer_id, snap.name, right / total, len(snap.geoms), {}, {},
             ex[:MAX_EXCEPTIONS])
    return [r] if _report(r) else []


def _mine_overlaps(snap, feedback=None):
    if snap.gtype != Qgis.GeometryType.Polygon or len(snap.geoms) < MIN_FEATURES:
        return []
    found = topo.find_overlaps(snap.geoms, snap.index, relative=0.001, feedback=feedback)
    bad = {}
    for a, b, inter in found:
        area = c.fmt(inter.area() * (c.METRES_PER_DEGREE ** 2 if c.is_geographic(snap.crs) else 1), 1)
        bad.setdefault(a, (b, area))
        bad.setdefault(b, (a, area))
    n = len(snap.geoms)
    ex = [(fid, "overlap", {"other": o, "area": area}) for fid, (o, area) in bad.items()]
    r = Rule("no_overlap", snap.layer_id, snap.name, 1 - len(bad) / n, n, {}, {}, ex[:MAX_EXCEPTIONS])
    return [r] if _report(r) else []


def _categorical(snap, fld, max_distinct=30):
    col = snap.column(fld)
    if len(col) < MIN_FEATURES:
        return None
    keys = {fid: c.value_key(v) for fid, v in col.items()}
    k = len(set(keys.values()))
    if not 2 <= k <= max_distinct or k > 0.3 * len(col):
        return None
    return col, keys


def _mine_dependencies(snap):
    out = []
    cats = {}
    for fld in snap.fields:
        if snap.kinds.get(fld) in ("text", "int"):
            r = _categorical(snap, fld)
            if r is not None:
                cats[fld] = r
    for a, (acol, akeys) in cats.items():
        # a -> b (categories)
        for b, (bcol, bkeys) in cats.items():
            if a == b:
                continue
            fids = [fid for fid in akeys if fid in bkeys]
            if len(fids) < MIN_FEATURES:
                continue
            dep = dependency([(akeys[f], bkeys[f]) for f in fids], "a", "b")
            if dep is None or dep["confidence"] < MIN_SUPPORT or dep["gain"] < 0.5 or dep["ratio"] > 0.7:
                continue
            if not dep["exceptions"] or any(len(v) != 1 for v in dep["pairs"].values()):
                continue    # "decides" means one value of b for each value of a
            allowed = dep["pairs"]
            ex = [(f, "pair", {"a": acol[f], "b": bcol[f]}) for f in fids if bkeys[f] not in allowed[akeys[f]]]
            # a readable example: the biggest group
            big = Counter(akeys[f] for f in fids).most_common(1)[0][0]
            fa = next(f for f in fids if akeys[f] == big)
            example = f"{acol[fa]} → {', '.join(str(bcol[f]) for f in _firsts(fids, akeys, bkeys, big, allowed))}"
            orig_a = {akeys[f]: acol[f] for f in fids}
            r = Rule("depends", snap.layer_id, snap.name, 1 - len(ex) / len(fids), len(fids),
                     {"a": a, "b": b, "example": example},
                     {"a": a, "b": b, "pairs": {k: list(v) for k, v in allowed.items()}, "names": orig_a},
                     ex[:MAX_EXCEPTIONS])
            if _report(r):
                out.append(r)
        # a -> maximum of a small whole number (floors)
        for b in snap.fields:
            if b == a or snap.kinds.get(b) not in ("int", "real"):
                continue
            bcol = snap.column(b)
            nums = {fid: float(v) for fid, v in bcol.items() if isinstance(v, (int, float)) and fid in akeys}
            if len(nums) < MIN_FEATURES or any(not x.is_integer() for x in nums.values()) or \
                    len(set(nums.values())) > 30:
                continue
            groups = defaultdict(list)
            for fid, x in nums.items():
                groups[akeys[fid]].append(x)
            caps = {}
            for k, xs in groups.items():
                if len(xs) < 5:
                    continue
                # the maximum is the largest value seen more than by a slip (a single 9 among 1-4 floors)
                cnt = Counter(xs)
                common = [x for x, m in cnt.items() if m >= max(2, 0.01 * len(xs))]
                if common:
                    caps[k] = max(common)
            if len(set(caps.values())) < 2:
                continue    # the same maximum everywhere: a range, not a dependency
            ex = [(fid, "above", {"a": acol[fid], "b": b, "value": c.fmt(x), "cap": c.fmt(caps[akeys[fid]])})
                  for fid, x in nums.items() if akeys[fid] in caps and x > caps[akeys[fid]]]
            checked = sum(len(groups[k]) for k in caps)
            big = max(caps, key=lambda k: len(groups[k]))
            name = next(acol[f] for f in nums if akeys[f] == big)
            r = Rule("depends_max", snap.layer_id, snap.name, 1 - len(ex) / checked, checked,
                     {"a": a, "b": b, "example": f"{name} → ≤ {c.fmt(caps[big])}"},
                     {"a": a, "b": b, "caps": caps}, ex[:MAX_EXCEPTIONS])
            if _report(r):
                out.append(r)
    return out


def _firsts(fids, akeys, bkeys, key, allowed):
    seen, out = set(), []
    for f in fids:
        if akeys[f] == key and bkeys[f] in allowed[key] and bkeys[f] not in seen:
            seen.add(bkeys[f])
            out.append(f)
            if len(out) >= 3:
                break
    return out


def _mine_inside(snap, other, area_fn, feedback=None):
    """Containment of ``snap`` features in ``other`` polygons, and the setback from the container's edge."""
    if len(snap.geoms) < MIN_FEATURES or not other.geoms:
        return []
    poly = snap.gtype == Qgis.GeometryType.Polygon
    inside, ex, dists = 0, [], {}
    boundaries = {}
    for i, (fid, g) in enumerate(snap.geoms.items()):
        if feedback is not None and i % 2000 == 0 and feedback.isCanceled():
            break
        p = snap.point(fid)
        conts = other.containing(p)
        if not conts:
            ex.append((fid, "outside", {"other": other.name}))
            continue
        o = conts[0]
        e = other.engine(o)
        share = 1.0
        if g.type() != Qgis.GeometryType.Point and not e.contains(g.constGet()):
            if poly:
                inter = g.intersection(other.geoms[o])
                a = g.area()
                share = inter.area() / a if a > 0 else 1.0
            else:
                share = g.intersection(other.geoms[o]).length() / max(g.length(), 1e-12)
        if share < INSIDE_SHARE:
            ex.append((fid, "partly", {"other": other.name, "share": f"{share * 100:.0f}%"}))
            continue
        inside += 1
        if g.type() != Qgis.GeometryType.Point:
            b = boundaries.get(o)
            if b is None:
                b = boundaries[o] = QgsGeometry(other.geoms[o].constGet().boundary())
            dists[fid] = c.to_metres(b.distance(g), snap.crs)
    n = len(snap.geoms)
    out = []
    r = Rule("inside", snap.layer_id, snap.name, inside / n, n, {"other": other.name},
             {"other": other.layer_id}, ex[:MAX_EXCEPTIONS])
    if _report(r):
        out.append(r)
    if r.support >= MIN_SUPPORT and len(dists) >= MIN_FEATURES:
        ds = sorted(dists.values())
        d = c.nice_floor(ds[int(0.05 * len(ds))])
        tol = 0.05
        sex = [(fid, "distance", {"d": c.fmt(x, 2)}) for fid, x in dists.items() if x < d - tol]
        # a real setback is a wall people build up against: many features sit just beyond it. Distances
        # spread evenly (parcels in a district) have as many just below as just above - no rule.
        band = sum(1 for x in ds if d - tol <= x <= 1.5 * d + 0.5) / len(ds)
        if d > 0 and band >= max(0.2, 4 * len(sex) / len(ds)):
            s = Rule("setback", snap.layer_id, snap.name, 1 - len(sex) / len(dists), len(dists),
                     {"other": other.name, "d": c.fmt(d)}, {"other": other.layer_id, "d": d}, sex[:MAX_EXCEPTIONS])
            if _report(s):
                out.append(s)
    return out


def _prune(rules):
    """Drop the echoes: "floors decides use" when "use sets the maximum of floors" (the number is the
    effect), and rules of one kind on one layer that list exactly the same exceptions as a stronger one."""
    capped = {(r.layer_id, r.params["a"], r.params["b"]) for r in rules if r.kind == "depends_max"}
    rules = [r for r in rules if not (r.kind == "depends" and (r.layer_id, r.params["b"], r.params["a"]) in capped)]
    rules.sort(key=lambda r: -r.support)
    seen, out = set(), []
    for r in rules:
        key = (r.layer_id, r.kind, r.params.get("b", r.params.get("field")), frozenset(r.fids))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    for lid in {r.layer_id for r in out}:
        if lid in _CACHE:
            _CACHE[lid] = [r for r in out if r.layer_id == lid]
    return out


def _overlapping(a, b):
    try:
        ext = a.extent()
        if a.crs() != b.crs() and a.crs().isValid() and b.crs().isValid():
            ext = QgsCoordinateTransform(a.crs(), b.crs(), QgsProject.instance()).transformBoundingBox(ext)
        return ext.intersects(b.extent())
    except Exception:  # noqa: BLE001
        return False


# ---- the miner ---------------------------------------------------------------------------------------------
def learn(project=None, layers=None, feedback=None, timings=None):
    """Mine the rules of ``layers`` (default: every vector layer of the project). Containment and setbacks
    are looked for against every polygon layer of the project. Returns [Rule], strongest first, and keeps them
    for broken_rules()."""
    project = project or QgsProject.instance()
    every = c.vector_layers(project)
    layers = [lyr for lyr in (layers or every) if isinstance(lyr, QgsVectorLayer) and lyr.isValid()]
    rules = []
    snaps = {}

    def snap_of(lyr, crs=None):
        key = (lyr.id(), crs.authid() if crs is not None else "")
        if key not in snaps:
            snaps[key] = c.Snapshot(lyr, crs=crs, feedback=feedback)
        return snaps[key]

    for i, lyr in enumerate(layers):
        if feedback is not None:
            if feedback.isCanceled():
                break
            feedback.setProgress(100 * i / max(1, len(layers)))
        t0 = time.perf_counter()
        snap = snap_of(lyr)
        area_fn, length_fn = c.measurer(snap.crs)
        found = []
        for step in (_mine_unique, _mine_typical, _mine_right_angles, _mine_dependencies):
            found += step(snap)
        found += _mine_widths(snap, area_fn, length_fn)
        found += _mine_overlaps(snap, feedback)
        for other_layer in every:
            if other_layer.id() == lyr.id() or other_layer.geometryType() != Qgis.GeometryType.Polygon:
                continue
            if not _overlapping(lyr, other_layer):
                continue
            found += _mine_inside(snap, snap_of(other_layer, snap.crs), area_fn, feedback)
        _CACHE[lyr.id()] = found
        rules += found
        if timings is not None:
            timings[lyr.name()] = time.perf_counter() - t0
    rules = _prune(rules)
    rules.sort(key=lambda r: (-r.support, r.layer_name, r.kind))
    if feedback is not None:
        feedback.setProgress(100)
    return rules


def cached_rules(layer):
    return _CACHE.get(layer.id() if hasattr(layer, "id") else layer)


def forget(layer=None):
    if layer is None:
        _CACHE.clear()
    else:
        _CACHE.pop(layer.id() if hasattr(layer, "id") else layer, None)


# ---- while drawing -----------------------------------------------------------------------------------------
def _in_crs(geom, src, dst):
    if geom is None or src == dst or not src.isValid() or not dst.isValid():
        return geom
    g = QgsGeometry(geom)
    g.transform(QgsCoordinateTransform(src, dst, QgsProject.instance()))
    return g


def _features_near(layer, geom, grow=0.0):
    r = QgsRectangle(geom.boundingBox())
    r.grow(grow)
    return layer.getFeatures(QgsFeatureRequest().setFilterRect(r))


def broken_rules(layer, geometry=None, attributes=None, fid=None, rules=None, mine=True, project=None):
    """The learned rules a candidate feature of ``layer`` would break - for the drawing tool.

    geometry: QgsGeometry in the layer's coordinate system (or None to check attributes only).
    attributes: {field name: value} (missing fields are not checked).
    fid: the feature being edited, left out of the overlap / uniqueness checks (None for a new feature).
    rules: the rules to use; by default the ones learned last for this layer - learned now (once, may take a
    few seconds on a big project) when ``mine`` is True and there are none yet.
    Returns [Violation] (empty when nothing is broken); Violation.text() is the sentence to show.
    """
    project = project or QgsProject.instance()
    if rules is None:
        rules = cached_rules(layer)
        if rules is None:
            rules = learn(project, [layer]) if mine else []
    rules = [r for r in rules if r.layer_id == layer.id()]
    attributes = attributes or {}
    geom = geometry if geometry is not None and not geometry.isNull() and not geometry.isEmpty() else None
    out = []
    crs = layer.crs()
    metric_area, metric_len = c.measurer(crs)
    for r in rules:
        p = r.params
        if r.kind == "unique":
            v = attributes.get(p["field"])
            if c.is_empty(v):
                continue
            key = c.value_key(v)
            idx = layer.fields().indexOf(p["field"])
            if idx < 0:
                continue
            n = 0
            for f in layer.getFeatures(QgsFeatureRequest().setFlags(Qgis.FeatureRequestFlag.NoGeometry)
                                       .setSubsetOfAttributes([idx])):
                if f.id() != fid and c.value_key(f.attributes()[idx]) == key:
                    n += 1
            if n:
                out.append(Violation(r, "duplicate", {"value": v, "n": n + 1}))
        elif r.kind == "typical":
            x = c.number(attributes.get(p["field"]))
            if x is not None and not _near_typical(x, p["centres"], p["rel"], p["abs"]):
                out.append(Violation(r, "value", {"value": c.fmt(x, 3)}))
        elif r.kind == "depends":
            a, b = attributes.get(p["a"]), attributes.get(p["b"])
            if c.is_empty(a) or c.is_empty(b):
                continue
            allowed = p["pairs"].get(c.value_key(a))
            if allowed is not None and c.value_key(b) not in allowed:
                out.append(Violation(r, "pair", {"a": a, "b": b}))
        elif r.kind == "depends_max":
            a, x = attributes.get(p["a"]), c.number(attributes.get(p["b"]))
            if c.is_empty(a) or x is None:
                continue
            cap = p["caps"].get(c.value_key(a))
            if cap is not None and x > cap:
                out.append(Violation(r, "above", {"a": a, "b": p["b"], "value": c.fmt(x), "cap": c.fmt(cap)}))
        elif geom is None:
            continue
        elif r.kind == "right_angles":
            offs = [(min(abs(a - 90), abs(a - 270)), a) for a in corner_angles(geom)]
            near = [o for o in offs if RIGHT_TOL < o[0] < NEAR_RIGHT]
            if near:
                out.append(Violation(r, "corner", {"angle": c.fmt(max(near)[1], 1)}))
        elif r.kind == "typical_width":
            if geom.type() == Qgis.GeometryType.Polygon:
                w = width_of(metric_area(geom), metric_len(geom))
                if w > 0 and not _near_typical(w, p["centres"], p["rel"], p["abs"]):
                    out.append(Violation(r, "width", {"value": c.fmt(w, 1)}))
        elif r.kind == "no_overlap":
            if geom.type() != Qgis.GeometryType.Polygon:
                continue
            g = geom if geom.isGeosValid() else geom.makeValid()
            for f in _features_near(layer, g):
                if f.id() == fid or not f.hasGeometry():
                    continue
                og = f.geometry()
                if g.intersects(og) and not g.touches(og):
                    inter = g.intersection(og)
                    if inter.area() > 0.001 * min(g.area(), og.area()):
                        out.append(Violation(r, "overlap", {"other": f.id(), "area": c.fmt(metric_area(inter), 1)}))
                        break
        elif r.kind in ("inside", "setback"):
            other = project.mapLayer(p["other"])
            if not isinstance(other, QgsVectorLayer):
                continue
            g = _in_crs(geom, crs, other.crs())
            pt = QgsGeometry.fromPointXY(c.inner_point(g))
            cont = None
            for f in _features_near(other, pt):
                if f.hasGeometry() and f.geometry().contains(pt):
                    cont = f.geometry()
                    break
            if r.kind == "inside":
                if cont is None:
                    out.append(Violation(r, "outside", {"other": other.name()}))
                elif g.type() == Qgis.GeometryType.Polygon and not cont.contains(g):
                    share = g.intersection(cont).area() / g.area() if g.area() > 0 else 1.0
                    if share < INSIDE_SHARE:
                        out.append(Violation(r, "partly", {"other": other.name(), "share": f"{share * 100:.0f}%"}))
            elif cont is not None and g.type() != Qgis.GeometryType.Point:
                d = c.to_metres(QgsGeometry(cont.constGet().boundary()).distance(g), other.crs())
                if d < p["d"] - 0.05:
                    out.append(Violation(r, "distance", {"d": c.fmt(d, 2)}))
    return out


__all__ = ["learn", "broken_rules", "cached_rules", "forget", "Rule", "Violation", "corner_angles", "clusters"]
