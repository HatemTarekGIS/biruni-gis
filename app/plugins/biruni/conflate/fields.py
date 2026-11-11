"""Which field of B is which field of A, told by the values (statistics, no names needed).

Every field is described: numbers or text, how many are empty, how many differ, the spread of the numbers, the
letters of the text (Arabic, Latin, digits) and a sample of its values written one way (hamza, ta marbuta, digits,
"عبد ال…"). Two fields are alike when their descriptions are. When the same features are known in both (from
match.py), the values of each pair are compared: the strongest sign. Numbers may be in other units: the ratio of
the values is found (an area in m² and the same in hectares: 10000) and kept as the conversion.
"""
import math
import statistics

from qgis.core import QgsFeatureRequest, QgsVectorLayer

from ..arabic_text import light_normalise, normalise, similarity

SAMPLE = 5000
SKIP = {"fid", "ogc_fid", "objectid", "objectid_1", "shape_length", "shape_area", "shape_leng", "shape__length",
        "shape__area", "st_area(shape)", "st_length(shape)"}
NICE = (1.0, 10.0, 100.0, 1000.0, 10000.0, 1e6, 0.1, 0.01, 0.001, 0.0001, 1e-6, 0.3048, 1 / 0.3048,
        2.58998811e6, 4046.8564224, 1 / 4046.8564224)   # 1, powers of ten, feet, square miles, acres
LEAST = 0.5         # least confidence of a proposal


def _null(v):
    return v is None or (hasattr(v, "isNull") and v.isNull()) or (isinstance(v, str) and not v.strip())


def _num(v):
    """A number, or None: a text counts only when it is a number and nothing else ("12.5", "٢٠٫٥", "1,250")."""
    if _null(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else None
    t = light_normalise(str(v))[0].strip().replace("٫", ".").replace("٬", "").replace(",", "")
    try:
        x = float(t)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _text(v):
    return None if _null(v) else normalise(str(v))


class Profile:
    def __init__(self, name, values):
        self.name = name
        vals = [v for v in values]
        n = max(1, len(vals))
        full = [v for v in vals if not _null(v)]
        self.nulls = 1.0 - len(full) / n
        nums = [x for x in (_num(v) for v in full) if x is not None]
        self.numeric = bool(full) and len(nums) >= 0.9 * len(full)
        self.nums = sorted(nums) if self.numeric else []
        texts = [t for t in (_text(v) for v in full) if t]
        self.distinct = len(set(texts)) / max(1, len(texts))
        self.values = set(texts[:SAMPLE])
        chars = "".join(texts[:500])
        k = max(1, len(chars))
        self.arabic = sum(1 for c in chars if "؀" <= c <= "ۿ") / k
        self.latin = sum(1 for c in chars if c.isascii() and c.isalpha()) / k
        self.digits = sum(1 for c in chars if c.isdigit()) / k
        self.length = statistics.mean(len(t) for t in texts) if texts else 0.0
        self.integer = self.numeric and all(float(x).is_integer() for x in self.nums[:500])

    def quantiles(self):
        if not self.nums:
            return []
        n = len(self.nums)
        return [self.nums[min(n - 1, int(q * (n - 1)))] for q in (0.1, 0.25, 0.5, 0.75, 0.9)]


def field_list(layer):
    keys = set(layer.primaryKeyAttributes())
    return [f.name() for i, f in enumerate(layer.fields()) if i not in keys and f.name().lower() not in SKIP]


def profiles(layer, names=None, fids=None):
    names = names or field_list(layer)
    idx = {n: layer.fields().indexOf(n) for n in names}
    cols = {n: [] for n in names}
    for k, f in enumerate(layer.getFeatures()):
        if k >= SAMPLE:
            break
        for n, i in idx.items():
            cols[n].append(f.attribute(i))
    return {n: Profile(n, v) for n, v in cols.items()}


def nice(r):
    """The conversion ``r`` as the unit ratio it is near (within half a percent), else itself."""
    for v in NICE:
        if abs(r - v) <= 0.005 * v:
            return v
    return r


def _ratio_sim(qa, qb):
    """How alike two spreads are once one is scaled to the other (and the scale)."""
    if not qa or not qb or qb[2] == 0:
        return 0.0, None
    f = nice(qa[2] / qb[2]) if qb[2] else None
    if not f:
        return 0.0, None
    s = []
    for x, y in zip(qa, qb):
        y = y * f
        if x == y:
            s.append(1.0)
        elif x == 0 or y == 0 or (x > 0) != (y > 0):
            s.append(0.0)
        else:
            s.append(min(abs(x), abs(y)) / max(abs(x), abs(y)))
    if f not in NICE:      # alike only once scaled by a ratio that is no unit: numbers of another kind
        return 0.4 * statistics.mean(s), f
    return statistics.mean(s), f


def profile_score(pa, pb):
    """(0..1, conversion) from the descriptions alone."""
    if pa.numeric != pb.numeric:
        return 0.0, None
    near = 1.0 - min(1.0, abs(pa.nulls - pb.nulls) + abs(pa.distinct - pb.distinct))
    if pa.numeric:
        s, f = _ratio_sim(pa.quantiles(), pb.quantiles())
        return 0.7 * s + 0.3 * near, f
    letters = 1.0 - min(1.0, (abs(pa.arabic - pb.arabic) + abs(pa.latin - pb.latin) + abs(pa.digits - pb.digits)) / 2)
    ln = min(pa.length, pb.length) / max(pa.length, pb.length) if max(pa.length, pb.length) else 1.0
    shared = len(pa.values & pb.values) / max(1, min(len(pa.values), len(pb.values)))
    return 0.45 * shared + 0.2 * letters + 0.15 * ln + 0.2 * near, None


def value_score(va, vb, numeric):
    """(share of the pairs whose values agree, conversion) for the values of matched features."""
    both = [(x, y) for x, y in zip(va, vb) if not _null(x) and not _null(y)]
    if len(both) < 3:
        return None, None
    if numeric:
        nn = [(a, b) for a, b in ((_num(x), _num(y)) for x, y in both) if a is not None and b is not None]
        if len(nn) < 3:
            return None, None
        ratios = [a / b for a, b in nn if b != 0]
        if not ratios:
            return 0.0, None
        f = nice(statistics.median(ratios))
        ok = sum(1 for a, b in nn if abs(a - b * f) <= max(0.01 * abs(a), 1e-9))
        return ok / len(nn), f
    ok = 0
    for x, y in both:
        tx, ty = _text(x), _text(y)
        if tx == ty or (tx and ty and similarity(str(x), str(y)) >= 0.9):
            ok += 1
    return ok / len(both), None


class Proposal:
    def __init__(self, a, b, confidence, factor=None, how="profile"):
        self.a, self.b, self.confidence, self.factor, self.how = a, b, round(confidence, 3), factor, how

    def __repr__(self):
        return f"{self.a}<-{self.b} {self.confidence} x{self.factor} ({self.how})"


def crosswalk(a, b, pairs=None):
    """[Proposal]: one field of B for a field of A, one to one, most sure first. ``pairs``: [(fid A, fid B, …)] of
    the same features in both (match.Result.pairs), the strongest sign when given."""
    if not isinstance(a, QgsVectorLayer) or not isinstance(b, QgsVectorLayer):
        return []
    pa, pb = profiles(a), profiles(b)
    vals_a, vals_b = {}, {}
    if pairs:
        pairs = list(pairs)[:SAMPLE]
        fa = {f.id(): f for f in a.getFeatures(QgsFeatureRequest().setFilterFids([p[0] for p in pairs]))}
        fb = {f.id(): f for f in b.getFeatures(QgsFeatureRequest().setFilterFids([p[1] for p in pairs]))}
        pairs = [p for p in pairs if p[0] in fa and p[1] in fb]
        for n in pa:
            i = a.fields().indexOf(n)
            vals_a[n] = [fa[p[0]].attribute(i) for p in pairs]
        for n in pb:
            i = b.fields().indexOf(n)
            vals_b[n] = [fb[p[1]].attribute(i) for p in pairs]
    scored = []
    for na, xa in pa.items():
        for nb, xb in pb.items():
            s, f = profile_score(xa, xb)
            how = "profile"
            conf = 0.6 * s
            if pairs:
                v, fv = value_score(vals_a[na], vals_b[nb], xa.numeric and xb.numeric)
                if v is not None:
                    conf, f, how = 0.85 * v + 0.15 * s, (fv if xa.numeric and xb.numeric else None), "values"
            if xa.numeric and xb.numeric and f is not None and f == 1.0:
                f = None
            if conf >= LEAST:
                scored.append(Proposal(na, nb, conf, f, how))
    scored.sort(key=lambda p: (-p.confidence, p.a, p.b))
    used_a, used_b, out = set(), set(), []
    for p in scored:
        if p.a in used_a or p.b in used_b:
            continue
        used_a.add(p.a)
        used_b.add(p.b)
        out.append(p)
    return out


def convert(v, factor):
    if factor is None or _null(v):
        return v
    x = _num(v)
    return v if x is None else x * factor


def copy_values(a, b, pairs, proposals=(), new=(), label="Values copied from another layer"):
    """B's values into A's edits by the matched ``pairs``: each proposal's field of B into its field of A (converted),
    each name in ``new`` (a field of B) into a new field of A. One step to undo. Returns the values written."""
    from qgis.core import QgsField

    if not a.isEditable() and not a.startEditing():
        raise ValueError("the layer cannot be edited")
    fb = {f.id(): f for f in b.getFeatures(QgsFeatureRequest().setFilterFids([p[1] for p in pairs]))}
    a.beginEditCommand(label)
    try:
        targets = [(a.fields().indexOf(p.a), b.fields().indexOf(p.b), p.factor) for p in proposals]
        names = {f.name().lower() for f in a.fields()}
        for n in new:
            src = b.fields().field(n)
            name, k = n, 2
            while name.lower() in names:
                name, k = f"{n}_{k}", k + 1
            names.add(name.lower())
            fld = QgsField(src)
            fld.setName(name)
            a.addAttribute(fld)
            targets.append((a.fields().indexOf(name), b.fields().indexOf(n), None))
        written = 0
        for p in pairs:
            f = fb.get(p[1])
            if f is None:
                continue
            for ia, ib, factor in targets:
                if ia < 0 or ib < 0:
                    continue
                a.changeAttributeValue(p[0], ia, convert(f.attribute(ib), factor))
                written += 1
    except Exception:
        a.destroyEditCommand()
        raise
    a.endEditCommand()
    return written
