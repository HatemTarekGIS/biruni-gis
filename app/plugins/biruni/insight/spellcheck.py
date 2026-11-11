"""Spatial spell checker («مدقق إملائي مكاني»): like a word processor's spell checker, but a value is checked
against where its feature lies.

Five checks, each one only run on a field the data itself vouches for:
- inside: the field names the polygon of another layer the feature lies in (district, zone, plan). Found by
  the overlap of the field's values with the names of a polygon layer, kept when most features agree.
- nearest: the field names the nearest line of a line layer (the street of a building).
- sequence: a numbering broken among neighbours (101, 102, ?, 104 along a row).
- neighbours: a value none of the neighbours use while all of them agree on another one (spatial outlier).
- measure: a number that disagrees with the measured area / length (in m², ha, km², dunam, ft² ...).

The guards are what keep this from crying wolf: a pairing is dropped unless at least AGREE of the features
already agree with it, and a check that would flag a large share of a layer is treated as a wrong pairing,
not as a layer full of mistakes. Nothing here edits until accept() is called.
"""
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsConditionalStyle, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsProject,
                       QgsSpatialIndex, QgsVectorLayer)
from qgis.PyQt.QtGui import QColor

from .. import arabic_text as at
from ..i18n import tr
from . import common as c

AGREE = 0.6            # share of features that must already agree with a pairing before it is used
MATCH = 0.5            # share of a field's values that must be names of the other layer
SIMILAR = 0.85         # arabic_text.similarity above which two names are one name written differently
MAX_SHARE = 0.25       # a check flagging more than this share of the features is a wrong pairing
NEAREST_RADIUS = 150.0     # m: how far a street may be from its building
NEAREST_SLACK = 15.0       # m: a corner building may carry the name of either street
MARK = "biruni_spell"      # name of the conditional styles that mark suspicious cells

# reason templates (English; translated when shown)
REASONS = {
    "inside": "Lies inside «{name}» ({layer})",
    "inside_spelling": "Lies inside «{name}» ({layer}) - written differently here",
    "nearest": "The nearest feature of {layer} is «{name}» ({d} m); «{value}» is {dv} m away",
    "nearest_none": "The nearest feature of {layer} is «{name}» ({d} m); no «{value}» within {r} m",
    "nearest_spelling": "The nearest feature of {layer} is «{name}» - written differently here",
    "sequence": "Its neighbours are numbered {a} and {b}, and {k} is missing from the layer",
    "neighbours": "All {n} neighbours have «{name}»; none has «{value}»",
    "area": "The measured area is {m} {unit}; the field says {value}",
    "length": "The measured length is {m} {unit}; the field says {value}",
}
KINDS = {
    "inside": "Lies inside",
    "nearest": "Nearest line",
    "sequence": "Numbering",
    "neighbours": "Neighbours",
    "measure": "Measured geometry",
}
ORDER = ("inside", "nearest", "sequence", "measure", "neighbours")
# unit names, translated when shown: tr("m²"), tr("ha"), tr("km²"), tr("dunam"), tr("ft²"), tr("m"), tr("km"), tr("ft")
AREA_UNITS = ((1.0, "m²"), (1e-4, "ha"), (1e-6, "km²"), (1e-3, "dunam"), (10.7639, "ft²"))
LENGTH_UNITS = ((1.0, "m"), (1e-3, "km"), (3.28084, "ft"))


@dataclass
class Finding:
    layer_id: str
    layer_name: str
    fid: int
    field: str
    value: object
    suggestion: object
    kind: str
    reason: str                     # key of REASONS
    values: dict = field(default_factory=dict)
    score: float = 1.0              # 0..1, how sure

    def text(self):
        v = dict(self.values)
        if "unit" in v:
            v["unit"] = tr(v["unit"])
        return tr(REASONS[self.reason]).format(**v)

    def kind_label(self):
        return tr(KINDS[self.kind])


@dataclass
class Pair:
    """A field and what it was found to describe."""
    field: str
    kind: str
    other_layer: str = ""
    other_field: str = ""
    agreement: float = 0.0
    checked: int = 0
    flagged: int = 0

    def text(self):
        pct = f"{self.agreement * 100:.0f}%"
        if self.kind == "inside":
            return tr("{field} names the {other} polygon it lies in ({other_field}) - {pct} agree").format(
                field=self.field, other=self.other_layer, other_field=self.other_field, pct=pct)
        if self.kind == "nearest":
            return tr("{field} names the nearest {other} line ({other_field}) - {pct} agree").format(
                field=self.field, other=self.other_layer, other_field=self.other_field, pct=pct)
        if self.kind == "sequence":
            return tr("{field} numbers neighbouring features in order - {pct} follow it").format(
                field=self.field, pct=pct)
        if self.kind == "measure":
            if self.other_field == "area":
                return tr("{field} holds the measured area in {unit} - {pct} agree").format(
                    field=self.field, unit=tr(self.other_layer), pct=pct)
            return tr("{field} holds the measured length in {unit} - {pct} agree").format(
                field=self.field, unit=tr(self.other_layer), pct=pct)
        return tr("{field} is shared by neighbours - {pct} of neighbours agree").format(field=self.field, pct=pct)


@dataclass
class Result:
    layer_id: str
    findings: list
    pairs: list
    seconds: float
    checked: int


# ---- the checks ------------------------------------------------------------------------------------------
def _keys(snap, fld):
    col = snap.column(fld)
    return {fid: c.value_key(v) for fid, v in col.items()}, col


def _name_fields(other, min_distinct=2):
    """{field: {key: original name}} of the text fields of ``other`` that look like names."""
    out = {}
    for fld in c.text_fields(other):
        names = {}
        for v in other.column(fld).values():
            k = c.value_key(v)
            if k and k not in names:
                names[k] = str(v)
        if len(names) >= min_distinct:
            out[fld] = names
    return out


def _best_match(keys, name_fields):
    """(field, names) of the other layer whose names cover the most of ``keys``; None below MATCH."""
    total = len(keys)
    if not total:
        return None
    counts = Counter(keys.values())
    best, share = None, 0.0
    for fld, names in name_fields.items():
        s = sum(n for k, n in counts.items() if k in names) / total
        if s > share:
            best, share = (fld, names), s
    return best if share >= MATCH else None


def _close_name(key, names):
    """The name of ``names`` written like ``key`` (spelling variant), or None."""
    best, score = None, 0.0
    for k, orig in names.items():
        s = at.similarity_keys(key, k)
        if s > score:
            best, score = orig, s
    return best if score >= SIMILAR else None


def _check_inside(snap, other, fld, keys, col, other_fld, names):
    """Findings for features whose value is not the name of the polygon they lie in."""
    ofi = other.fields.index(other_fld)
    by_key = defaultdict(list)
    for ofid, a in other.attrs.items():
        by_key[c.value_key(a[ofi])].append(ofid)
    out, agree, checked = [], 0, 0
    for fid, k in keys.items():
        if fid not in snap.geoms:
            continue
        p = snap.point(fid)
        conts = other.containing(p)
        if not conts:
            continue
        checked += 1
        cont_keys = [c.value_key(other.attrs[o][ofi]) for o in conts]
        if k in cont_keys:
            agree += 1
            continue
        # a feature astride the border of the polygon it names is right
        g = snap.geoms[fid]
        if any(o in by_key.get(k, ()) and other.engine(o).intersects(g.constGet()) for o in other.near(g, 0)):
            agree += 1
            continue
        name = str(other.attrs[conts[0]][ofi])
        spelling = c.same_name(k, cont_keys[0], SIMILAR)
        out.append(Finding(snap.layer_id, snap.name, fid, fld, col[fid], name, "inside",
                           "inside_spelling" if spelling else "inside",
                           {"name": name, "layer": other.name, "value": col[fid]}, 0.9 if spelling else 1.0))
    return out, agree, checked


def _line_index(other):
    idx = getattr(other, "_geom_index", None)
    if idx is None:
        idx = QgsSpatialIndex(QgsSpatialIndex.Flag.FlagStoreFeatureGeometries)
        for fid, g in other.geoms.items():
            f = QgsFeature(fid)
            f.setGeometry(g)
            idx.addFeature(f)
        other._geom_index = idx
    return idx


def _check_nearest(snap, other, fld, keys, col, other_fld, names):
    """Findings for features whose value is not the name of a nearby line (the nearest, or one about as near)."""
    ofi = other.fields.index(other_fld)
    radius = c.to_units(NEAREST_RADIUS, snap.crs)
    slack0 = c.to_units(NEAREST_SLACK, snap.crs)
    idx = _line_index(other)
    out, agree, checked = [], 0, 0
    for fid, k in keys.items():
        if fid not in snap.geoms:
            continue
        p = snap.point(fid)
        pg = QgsGeometry.fromPointXY(p)
        ids = idx.nearestNeighbor(p, 12, radius)
        if not ids:
            continue
        dist = sorted((other.geoms[o].distance(pg), o) for o in ids if o in other.geoms)
        d0, o0 = dist[0]
        checked += 1
        slack = max(slack0, 0.5 * d0)
        close = {c.value_key(other.attrs[o][ofi]) for d, o in dist if d <= d0 + slack}
        if k in close:
            agree += 1
            continue
        name = str(other.attrs[o0][ofi])
        same = [d for d, o in dist if c.value_key(other.attrs[o][ofi]) == k]
        if not same:
            # the named line may be further than the 12 nearest: look along every line of that name
            r = c.point_rect(p, radius)
            same = [other.geoms[o].distance(pg) for o in other.index.intersects(r)
                    if c.value_key(other.attrs[o][ofi]) == k]
            same = [d for d in same if d <= radius]
        m = lambda u: c.fmt(c.to_metres(u, snap.crs), 0)  # noqa: E731
        vals = {"layer": other.name, "name": name, "value": col[fid], "d": m(d0)}
        if c.same_name(k, c.value_key(name), SIMILAR):
            reason, score = "nearest_spelling", 0.9
        elif same:
            reason, score = "nearest", 0.8
            vals["dv"] = m(min(same))
        else:
            reason, score = "nearest_none", 0.9
            vals["r"] = c.fmt(NEAREST_RADIUS, 0)
        out.append(Finding(snap.layer_id, snap.name, fid, fld, col[fid], name, "nearest", reason, vals, score))
    return out, agree, checked


def _check_sequence(snap, fld):
    """Findings for numbers that break the order of their neighbours. Returns (findings, pair) or None."""
    col = snap.column(fld)
    if len(col) < 20:
        return None
    parts = {fid: c.split_number(v) for fid, v in col.items()}
    parts = {fid: s for fid, s in parts.items() if s is not None and fid in snap.geoms}
    if len(parts) < 0.9 * len(col) or len(set((s[0], s[1]) for s in parts.values())) < 0.8 * len(parts):
        return None     # not an id-like numbering
    nbrs = snap.neighbours()
    follows, with_nbrs = 0, 0
    for fid, (pre, n, _w) in parts.items():
        ns = [parts[o] for o in nbrs.get(fid, ()) if o in parts]
        if not ns:
            continue
        with_nbrs += 1
        if any(q[0] == pre and abs(q[1] - n) == 1 for q in ns):
            follows += 1
    if not with_nbrs or follows / with_nbrs < 0.5:
        return None
    used = {(s[0], s[1]) for s in parts.values()}
    out = []
    for fid, (pre, n, width) in parts.items():
        ns = {}
        for o in nbrs.get(fid, ()):
            q = parts.get(o)
            if q is not None and q[0] == pre:
                ns.setdefault(q[1], o)
        if not ns or n - 1 in ns or n + 1 in ns:
            continue
        p = snap.point(fid)
        for k in sorted(ns):
            if k + 2 not in ns or k + 1 == n or (pre, k + 1) in used:
                continue
            pa, pb = snap.point(ns[k]), snap.point(ns[k + 2])
            gap = pa.distance(pb)
            if gap <= max(pa.distance(p), pb.distance(p)):
                continue        # not between them
            value = col[fid]
            width = parts[ns[k]][2]      # the neighbours' way of writing it (101, 0101 ...)
            text = f"{pre}{str(k + 1).zfill(width)}"
            sug = (k + 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else text
            out.append(Finding(snap.layer_id, snap.name, fid, fld, value, sug, "sequence", "sequence",
                               {"a": f"{pre}{str(k).zfill(width)}", "b": f"{pre}{str(k + 2).zfill(width)}",
                                "k": text, "value": value}, 0.85))
            break
    pair = Pair(fld, "sequence", agreement=follows / with_nbrs, checked=with_nbrs, flagged=len(out))
    return out, pair


def _check_neighbours(snap, fld):
    """Findings for values none of the neighbours use while all of them agree on another one."""
    col = snap.column(fld)
    n = len(col)
    if n < 20:
        return None
    keys = {fid: c.value_key(v) for fid, v in col.items()}
    counts = Counter(keys.values())
    if not 2 <= len(counts) <= 50 or len(counts) > 0.2 * n:
        return None
    nbrs = snap.neighbours()
    same = pairs = 0
    for fid, k in keys.items():
        for o in nbrs.get(fid, ()):
            if o in keys:
                pairs += 1
                same += keys[o] == k
    if pairs < n:
        return None
    observed = same / pairs
    expected = sum((m / n) ** 2 for m in counts.values())
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 0
    if observed < 0.75 or kappa < 0.5:
        return None     # neighbours do not share the field more than chance would
    out = []
    for fid, k in keys.items():
        ks = [keys[o] for o in nbrs.get(fid, ()) if o in keys]
        if len(ks) < 3 or len(set(ks)) != 1 or ks[0] == k:
            continue
        o = next(o for o in nbrs[fid] if o in keys)
        name = col[o]
        out.append(Finding(snap.layer_id, snap.name, fid, fld, col[fid], name, "neighbours", "neighbours",
                           {"n": len(ks), "name": name, "value": col[fid]}, 0.7))
    if len(out) > 0.05 * n:
        return [], Pair(fld, "neighbours", agreement=observed, checked=n, flagged=0)
    return out, Pair(fld, "neighbours", agreement=observed, checked=n, flagged=len(out))


def _check_measure(snap, fld, measures, what):
    """Findings for numbers that disagree with the measured area / length."""
    col = snap.column(fld)
    nums = {fid: c.number(v) for fid, v in col.items()}
    nums = {fid: x for fid, x in nums.items() if x is not None and x > 0 and fid in measures and measures[fid] > 0}
    if len(nums) < 10 or len(nums) < 0.8 * len(col):
        return None
    ratios = sorted(x / measures[fid] for fid, x in nums.items())
    r = ratios[len(ratios) // 2]
    units = AREA_UNITS if what == "area" else LENGTH_UNITS
    unit = next(((f, u) for f, u in units if abs(r / f - 1) <= 0.03), None)
    if unit is None:
        return None
    factor, uname = unit
    agree, out = 0, []
    for fid, x in nums.items():
        m = measures[fid] * factor
        diff = abs(x - m)
        if diff <= max(0.02 * m, 0.5 * factor if what == "area" else 0.05 * factor):
            agree += 1
        elif diff > max(0.05 * m, factor):
            dec = 2 if m < 1000 else 1
            sug = round(m, dec)
            if snap.kinds.get(fld) == "int":
                sug = int(round(m))
            out.append(Finding(snap.layer_id, snap.name, fid, fld, col[fid], sug, "measure", what,
                               {"m": c.fmt(m, dec), "unit": uname, "value": col[fid]},
                               min(1.0, 0.6 + diff / m)))
    share = agree / len(nums)
    if share < 0.7:
        return None
    return out, Pair(fld, "measure", other_layer=uname, other_field="area" if what == "area" else "length",
                     agreement=share, checked=len(nums), flagged=len(out))


def check_layer(layer, project=None, feedback=None, kinds=None, others=None):
    """Spell-check the values of ``layer`` against where its features lie. ``kinds`` limits the checks (keys
    of KINDS), ``others`` the layers it is compared with (default: every other vector layer of the project).
    Returns a Result: findings sorted by field and certainty, and the pairings found."""
    t0 = time.perf_counter()
    project = project or QgsProject.instance()
    kinds = set(kinds or KINDS)
    snap = c.Snapshot(layer, feedback=feedback)
    findings, pairs = [], []
    fields = c.text_fields(snap)
    if others is None:
        others = [lyr for lyr in c.vector_layers(project) if lyr.id() != layer.id()]
    polys = [o for o in others if o.geometryType() == Qgis.GeometryType.Polygon]
    lines = [o for o in others if o.geometryType() == Qgis.GeometryType.Line]

    def progress(v):
        if feedback is not None:
            feedback.setProgress(v)
        return feedback is None or not feedback.isCanceled()

    located = set()     # fields already explained by another layer: no neighbour vote on them
    jobs = []
    if "inside" in kinds:
        jobs += [(o, "inside", _check_inside) for o in polys]
    if "nearest" in kinds:
        jobs += [(o, "nearest", _check_nearest) for o in lines]
    for j, (other_layer, kind, fn) in enumerate(jobs):
        if not progress(10 + 50 * j / max(1, len(jobs))):
            break
        if not other_layer.extent().isEmpty() and snap.geoms:
            other = c.Snapshot(other_layer, crs=snap.crs, feedback=feedback)
        else:
            continue
        name_fields = _name_fields(other)
        if not name_fields:
            continue
        for fld in fields:
            keys, col = _keys(snap, fld)
            best = _best_match(keys, name_fields)
            if best is None:
                continue
            ofld, names = best
            found, agree, checked = fn(snap, other, fld, keys, col, ofld, names)
            if not checked or agree / checked < AGREE or len(found) > MAX_SHARE * checked:
                continue
            located.add(fld)
            findings += found
            pairs.append(Pair(fld, kind, other.name, ofld, agree / checked, checked, len(found)))
    if "measure" in kinds and snap.gtype in (Qgis.GeometryType.Polygon, Qgis.GeometryType.Line):
        progress(65)
        area, length = c.measurer(snap.crs)
        what = "area" if snap.gtype == Qgis.GeometryType.Polygon else "length"
        fn = area if what == "area" else length
        measures = {fid: fn(g) for fid, g in snap.geoms.items()}
        for fld in snap.fields:
            if snap.kinds.get(fld) is None:
                continue
            r = _check_measure(snap, fld, measures, what)
            if r is not None:
                findings += r[0]
                pairs.append(r[1])
    if "sequence" in kinds:
        progress(75)
        for fld in fields:
            r = _check_sequence(snap, fld)
            if r is not None:
                findings += r[0]
                pairs.append(r[1])
    if "neighbours" in kinds:
        progress(90)
        for fld in fields:
            if fld in located or any(p.field == fld and p.kind == "sequence" for p in pairs):
                continue
            r = _check_neighbours(snap, fld)
            if r is not None:
                findings += r[0]
                pairs.append(r[1])
    # one finding per cell: the surest kind wins
    rank = {k: i for i, k in enumerate(ORDER)}
    findings.sort(key=lambda f: (rank[f.kind], -f.score))
    seen, unique = set(), []
    for f in findings:
        if (f.fid, f.field) not in seen:
            seen.add((f.fid, f.field))
            unique.append(f)
    unique.sort(key=lambda f: (snap.fields.index(f.field), rank[f.kind], -f.score, f.fid))
    progress(100)
    return Result(layer.id(), unique, pairs, time.perf_counter() - t0, snap.count)


# ---- acting on the findings ------------------------------------------------------------------------------
def _typed(layer, idx, value):
    fld = layer.fields().at(idx)
    kind = c.field_kind(fld)
    if kind == "int":
        n = c.number(value)
        return int(round(n)) if n is not None else value
    if kind == "real":
        n = c.number(value)
        return n if n is not None else value
    return value if kind != "text" else str(value)


def accept(findings, project=None):
    """Write the suggestions of ``findings`` into their layers, as one undoable edit per layer. The layers are
    left in edit mode for the user to save or undo. Returns the number of values changed."""
    project = project or QgsProject.instance()
    by_layer = defaultdict(list)
    for f in findings:
        by_layer[f.layer_id].append(f)
    changed = 0
    for lid, fs in by_layer.items():
        layer = project.mapLayer(lid)
        if not isinstance(layer, QgsVectorLayer):
            continue
        if not layer.isEditable() and not layer.startEditing():
            continue
        layer.beginEditCommand(tr("Accept spelling suggestions"))
        for f in fs:
            idx = layer.fields().indexOf(f.field)
            if idx >= 0 and layer.changeAttributeValue(f.fid, idx, _typed(layer, idx, f.suggestion)):
                changed += 1
        layer.endEditCommand()
        layer.triggerRepaint()
    return changed


def mark_table(layer, findings, colour="#FFE08A"):
    """Mark the suspicious cells in the attribute table with the engine's own conditional styles (a cell
    background per field), replacing earlier marks of this tool. Returns the number of cells marked."""
    clear_marks(layer)
    by_field = defaultdict(list)
    for f in findings:
        if f.layer_id == layer.id():
            by_field[f.field].append(f.fid)
    styles = layer.conditionalStyles()
    for fld, fids in by_field.items():
        st = QgsConditionalStyle("$id IN ({})".format(",".join(str(i) for i in sorted(set(fids)))))
        st.setName(MARK)
        st.setBackgroundColor(QColor(colour))
        styles.setFieldStyles(fld, styles.fieldStyles(fld) + [st])
    layer.triggerRepaint()
    return sum(len(v) for v in by_field.values())


def clear_marks(layer):
    styles = layer.conditionalStyles()
    for fld in layer.fields().names():
        old = styles.fieldStyles(fld)
        keep = [s for s in old if s.name() != MARK]
        if len(keep) != len(old):
            styles.setFieldStyles(fld, keep)


def features_request(findings):
    return QgsFeatureRequest().setFilterFids(sorted({f.fid for f in findings}))


__all__ = ["check_layer", "accept", "mark_table", "clear_marks", "Finding", "Pair", "Result"]
