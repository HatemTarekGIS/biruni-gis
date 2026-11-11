"""Learn the charter from the data: read a layer and propose a design.

* fields that look like code lists (few distinct values)           -> value lists, with counts
* spellings of the same value (ة/ه, ى/ي, أ/إ/ا, spaces, «ال», case) -> merge suggestions
* numbers with outliers (median ± k·MAD)                            -> a range, suspicious values flagged
* fields that are never empty                                       -> "required?"
* fields with a different value on every feature                    -> "unique id?"
* fields whose values decide another field's values                 -> kinds and linked choices, with confidence

Every proposal can be ticked or not; ``accept`` turns the ticked ones into a charter (and, if asked, fixes the
spellings in the data as one Undo step).
"""
import math
import re
from collections import Counter, defaultdict

from qgis.core import QgsFeatureRequest
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from . import model

MIN_ROWS = 5            # fewer non-empty values: nothing is proposed for the field
MAX_LIST = 30           # at most this many distinct values for a code list
LIST_SHARE = 0.5        # ... and at most this share of the non-empty values
MAD_K = 4.0             # outlier: further than k robust standard deviations from the median
DEP_MIN_ROWS = 10       # rows needed to propose a dependency
DEP_MIN_GAIN = 0.25     # the parent must cut the child's choices by at least a quarter on average
DEP_MIN_CONF = 0.9      # share of the rows that follow the proposed rule
KIND_MAX = 12           # at most this many kinds
ID_WORDS = re.compile(r"(^|_)(id|uid|guid|uuid|code|no|num|number|key|serial|ref|رقم|كود|الرقم|الكود)($|_)", re.I)
DEP_MAX_RATIO = 0.6     # the parent leaves at most this share of the choices chance alone would leave


class Proposal:
    """One suggestion. kind: list | merge | range | required | unique | kind | link."""

    def __init__(self, kind, field, title, detail, confidence, data, checked=True):
        self.kind, self.field, self.title, self.detail = kind, field, title, detail
        self.confidence, self.data, self.checked = confidence, data, checked
        self.fids = data.get("fids", []) if isinstance(data, dict) else []

    def __repr__(self):
        return f"<Proposal {self.kind} {self.field} {self.confidence:.2f}>"


def _empty(v):
    return v is None or (hasattr(v, "isNull") and v.isNull()) or (isinstance(v, str) and v.strip() == "") or \
        str(v) == "NULL"


def _norm_value(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def read_values(layer, limit=200000):
    """{field: [(fid, value)]} of the non-empty values (at most `limit` features)."""
    names = [f.name() for f in layer.fields()]
    out = {n: [] for n in names}
    req = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry)
    total = 0
    for feat in layer.getFeatures(req):
        total += 1
        attrs = feat.attributes()
        for n, v in zip(names, attrs):
            if not _empty(v):
                out[n].append((feat.id(), v))
        if total >= limit:
            break
    return out, total


def spelling_groups(counts):
    """[(canonical, [(value, count, kind)])] for groups with more than one spelling (from the Spelling variants
    tool: normalised Arabic letters, spaces, «ال», case, rare one-letter typos)."""
    from ..algorithms.spell_values import group_spellings

    out = []
    for suggested, _ref, members in group_spellings(counts, threshold=0.9):
        if len(members) < 2:
            continue
        sure = [(v, c, k) for v, c, s, k in members if k in ("suggested", "normalised", "typo")]
        if len(sure) >= 2:
            out.append((suggested, sure))
    return out


def merged_counts(counts, groups):
    to = {v: canon for canon, members in groups for v, _c, _k in members}
    merged = Counter()
    for v, c in counts.items():
        merged[to.get(v, v)] += c
    return merged, to


def robust_range(values):
    """(lo, hi, outliers, median, mad): the range of the values that are not outliers (median ± k·MAD)."""
    xs = sorted(values)
    n = len(xs)
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    dev = sorted(abs(x - med) for x in xs)
    mad = dev[n // 2] if n % 2 else (dev[n // 2 - 1] + dev[n // 2]) / 2
    scale = 1.4826 * mad
    if scale == 0:
        mean_dev = sum(dev) / n
        scale = 1.2533 * mean_dev
    if scale == 0:
        return xs[0], xs[-1], [], med, 0
    keep = [x for x in xs if abs(x - med) <= MAD_K * scale]
    out = [x for x in xs if abs(x - med) > MAD_K * scale]
    return keep[0], keep[-1], out, med, mad


def nice_bounds(lo, hi, integer=False):
    """Round a range outwards to tidy numbers (0.1, 1, 5, 10 … of its size)."""
    span = hi - lo
    if span <= 0:
        span = abs(hi) or 1
    step = 10 ** math.floor(math.log10(span)) / 10
    lo2, hi2 = math.floor(lo / step) * step, math.ceil(hi / step) * step
    if lo >= 0 > lo2:
        lo2 = 0
    if integer:
        lo2, hi2 = math.floor(lo2), math.ceil(hi2)
    return float(f"{lo2:.10g}"), float(f"{hi2:.10g}")


def _is_code_like(distinct, n):
    return MIN_ROWS <= n and 2 <= distinct <= MAX_LIST and distinct <= max(2, LIST_SHARE * n)


def dependency(rows, parent, child, rare=0.05):
    """How much the parent field decides the child field. rows: [(parent value, child value)].
    Returns {"pairs": {p: [c…]}, "gain", "confidence", "exact", "exceptions": [(p, c, count)]} or None."""
    by_parent = defaultdict(Counter)
    for p, c in rows:
        by_parent[p][c] += 1
    child_all = Counter(c for _p, c in rows)
    dom = len(child_all)
    if dom < 2 or len(by_parent) < 2:
        return None
    pairs, kept, exceptions = {}, 0, []
    for p, cnt in by_parent.items():
        total = sum(cnt.values())
        allowed = [c for c, k in cnt.most_common() if not (total >= 10 and k == 1 and k / total < rare)]
        pairs[p] = allowed
        kept += sum(cnt[c] for c in allowed)
        exceptions += [(p, c, k) for c, k in cnt.items() if c not in allowed]
    gain = 1 - sum(len(v) for v in pairs.values()) / (len(pairs) * dom)
    # how many child values each parent group would show by chance alone (independent fields)
    n = len(rows)
    share = {c: k / n for c, k in child_all.items()}
    expected = sum(sum(1 - (1 - p) ** sum(cnt.values()) for p in share.values()) for cnt in by_parent.values())
    ratio = sum(len(v) for v in pairs.values()) / expected if expected else 1
    return {"pairs": pairs, "gain": gain, "confidence": kept / len(rows), "ratio": ratio,
            "exact": all(len(v) == 1 for v in pairs.values()), "exceptions": exceptions}


def infer(layer, lists=None):
    """The proposals for a layer, best first within each kind."""
    lists = model.project_lists() if lists is None else lists
    values, total = read_values(layer)
    fields = layer.fields()
    pk = {fields.at(i).name() for i in layer.primaryKeyAttributes()}
    stored = model.stored_charter(layer) or model.new_charter()
    specs = {f["name"]: f for f in stored["fields"]}
    out = []
    canon_of = {}       # field -> {raw value: canonical value}
    code_fields = {}    # field -> Counter of canonical values
    for fld in fields:
        name = fld.name()
        if name in pk or name.lower() in ("fid", "objectid", "ogc_fid"):
            continue
        vals = values.get(name, [])
        spec = specs.get(name, {})
        numeric = fld.isNumeric()
        is_float = fld.type() == QMetaType.Type.Double
        n = len(vals)
        # never empty -> required?
        if total >= MIN_ROWS and n == total and not spec.get("required"):
            out.append(Proposal("required", name, tr("{f}: make it required").format(f=name),
                                tr("filled in all {n} features").format(n=total),
                                1.0 if total >= 20 else 0.7, {}, checked=total >= 20))
        if n < MIN_ROWS:
            continue
        raw_counts = Counter(_norm_value(v) for _fid, v in vals)
        groups = [] if numeric else spelling_groups(raw_counts)
        merged, to = merged_counts(raw_counts, groups)
        canon_of[name] = to
        # spelling merges
        for canon, members in groups:
            changes = [(v, c) for v, c, _k in members if v != canon]
            if not changes:
                continue
            fids = [fid for fid, v in vals if _norm_value(v) in dict(changes)]
            out.append(Proposal(
                "merge", name, tr("{f}: write «{v}» one way").format(f=name, v=canon),
                " · ".join(f"«{v}» ×{c}" for v, c in changes) + f"  {model.arrow()}  «{canon}»",
                0.95, {"canonical": canon, "values": [v for v, _c in changes], "fids": fids}))
        distinct = len(merged)
        # unique id?
        if distinct == n and n >= MIN_ROWS and not spec.get("unique") and (
                not numeric or not is_float and all(float(str(v)).is_integer() for _f, v in vals
                                                    if _is_number(v))):
            idish = bool(ID_WORDS.search(name))
            if idish or not numeric:
                out.append(Proposal("unique", name, tr("{f}: a unique id?").format(f=name),
                                    tr("{n} values, none repeated").format(n=n), 0.9 if idish else 0.6, {},
                                    checked=idish))
            continue
        # code list
        if _is_code_like(distinct, n) and not (is_float and distinct > 6):
            items = merged.most_common()
            code_fields[name] = merged
            if not spec.get("list"):
                conf = min(1.0, 0.6 + 0.4 * (1 - distinct / max(1, n)))
                out.append(Proposal(
                    "list", name, tr("{f}: a value list of {n} values").format(f=name, n=distinct),
                    " · ".join(f"{v} ({c})" for v, c in items[:12]) + (" …" if len(items) > 12 else ""),
                    conf, {"items": items, "numeric": numeric}))
            continue
        # numeric range with outliers
        if numeric and n >= 8:
            nums = [float(v) for _fid, v in vals if _is_number(v)]
            lo, hi, outliers, med, _mad = robust_range(nums)
            integer = not is_float and all(x.is_integer() for x in nums)
            nlo, nhi = nice_bounds(lo, hi, integer)
            bad = {x for x in outliers if x < nlo or x > nhi}
            fids = [fid for fid, v in vals if _is_number(v) and float(v) in bad]
            if not spec.get("list"):
                detail = tr("{a} to {b}, median {m}").format(a=f"{nlo:g}", b=f"{nhi:g}", m=f"{med:g}")
                if bad:
                    detail += " · " + tr("suspicious: {v}").format(v=", ".join(f"{x:g}" for x in sorted(bad)[:8]))
                out.append(Proposal("range", name, tr("{f}: a range").format(f=name), detail,
                                    0.9 if bad else 0.7, {"min": nlo, "max": nhi, "fids": fids,
                                                          "outliers": sorted(bad)}, checked=True))
    # dependencies between code-like fields -> kinds and linked choices
    deps = []
    names = list(code_fields)
    by_fid = {name: {fid: canon_of.get(name, {}).get(_norm_value(v), _norm_value(v))
                     for fid, v in values[name]} for name in names}
    for a in names:
        if len(code_fields[a]) > KIND_MAX:
            continue
        for b in names:
            if a == b:
                continue
            rows = [(pa, by_fid[b][fid]) for fid, pa in by_fid[a].items() if fid in by_fid[b]]
            if len(rows) < DEP_MIN_ROWS:
                continue
            d = dependency(rows, a, b)
            if d and d["gain"] >= DEP_MIN_GAIN and d["confidence"] >= DEP_MIN_CONF and d["ratio"] <= DEP_MAX_RATIO:
                deps.append((a, b, d))
    # one-to-one pairs (a code and its label): keep one direction
    seen = set()
    deps.sort(key=lambda t: (-t[2]["confidence"], -t[2]["gain"]))
    kept = []
    for a, b, d in deps:
        if (b, a) in seen and d["exact"]:
            continue
        seen.add((a, b))
        kept.append((a, b, d))
    if kept:
        decides = Counter(a for a, _b, _d in kept)
        kind_field = stored["kind_field"] or max(decides, key=lambda a: (decides[a], -len(code_fields[a])))
        have_links = {(tuple(ln.get("parents", [])), ln.get("child")) for ln in stored["links"]}
        for a, b, d in kept:
            if a == stored["kind_field"] and any(b in k["lists"] or b in k["defaults"] for k in stored["kinds"]):
                continue  # already in the charter
            if ((a,), b) in have_links:
                continue
            conf = round(d["confidence"] * (1 - 0.5 * d["ratio"]), 3)
            sample = "; ".join(f"{p} {model.arrow()} {', '.join(cs[:4])}" for p, cs in list(d["pairs"].items())[:4])
            exc = d["exceptions"]
            extra = (" · " + tr("{n} exceptions").format(n=sum(k for _p, _c, k in exc))) if exc else ""
            if a == kind_field:
                title = (tr("{a} decides {b}: a default per kind") if d["exact"] else
                         tr("{a} decides {b}: allowed values per kind")).format(a=a, b=b)
                out.append(Proposal("kind", b, title, sample + extra, conf,
                                    {"kind_field": a, "child": b, "pairs": d["pairs"], "exact": d["exact"]}))
            else:
                out.append(Proposal("link", b, tr("{a} limits the choices of {b}").format(a=a, b=b),
                                    sample + extra, conf, {"parents": [a], "child": b, "pairs": d["pairs"]},
                                    checked=conf >= 0.8))
    order = {"merge": 0, "list": 1, "kind": 2, "link": 3, "range": 4, "required": 5, "unique": 6}
    out.sort(key=lambda p: (order.get(p.kind, 9), -p.confidence))
    return out


def _is_number(v):
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False


def accept(layer, proposals, charter=None, lists=None, fix_data=True):
    """Turns the ticked proposals into (charter, lists). With fix_data the spelling merges are written to the
    layer in its edit session as ONE undo step (not saved). Returns (charter, lists, changed features)."""
    lang = model.ui_lang()
    charter = model.clean_charter(charter or model.stored_charter(layer) or model.read_charter(layer)[0])
    if not charter["fields"]:
        charter = model.read_charter(layer)[0]
    lists = dict(model.project_lists() if lists is None else lists)
    ticked = [p for p in proposals if p.checked]
    specs = {f["name"]: f for f in charter["fields"]}
    merges = defaultdict(dict)
    for p in ticked:
        if p.kind == "merge":
            for v in p.data["values"]:
                merges[p.field][v] = p.data["canonical"]

    def ensure_list(field, items, numeric=False):
        spec = specs.get(field)
        if spec is None:
            return None
        if spec.get("list") and spec["list"] in lists:
            return lists[spec["list"]]
        lid = model.unique_list_id(model.slug(f"{layer.name()}_{field}"), lists)
        alias = spec.get("alias_ar") or spec.get("alias_en") or field
        lst = model.new_list(alias if lang == "ar" else spec.get("alias_ar") or "",
                             alias if lang == "en" else spec.get("alias_en") or "", "codes", lid=lid)
        for v in items:
            v = str(v)
            item = {"code": v, "ar": "", "en": "", "color": ""}
            if re.search("[؀-ۿ]", v):
                item["ar"] = v
            else:
                item["en"] = v
            lst["items"].append(item)
        lists[lid] = lst
        spec["list"] = lid
        return lst

    for p in ticked:
        spec = specs.get(p.field)
        if spec is None:
            continue
        if p.kind == "required":
            spec["required"] = True
        elif p.kind == "unique":
            spec["unique"] = True
        elif p.kind == "list":
            ensure_list(p.field, [v for v, _c in p.data["items"]], p.data.get("numeric"))
        elif p.kind == "range":
            lid = model.unique_list_id(model.slug(f"{layer.name()}_{p.field}"), lists)
            lists[lid] = model.new_list(spec.get("alias_ar") or p.field, spec.get("alias_en") or p.field, "range",
                                        lid=lid, min=p.data["min"], max=p.data["max"])
            spec["list"] = lid
    for p in ticked:
        if p.kind == "kind":
            kf = p.data["kind_field"]
            if charter["kind_field"] and charter["kind_field"] != kf:
                continue
            charter["kind_field"] = kf
            for code in p.data["pairs"]:
                if model.find_kind(charter, code) is None:
                    item = {"code": str(code), "ar": "", "en": "", "color": "", "defaults": {}, "lists": {},
                            "required": []}
                    item["ar" if re.search("[؀-ۿ]", str(code)) else "en"] = str(code)
                    charter["kinds"].append(item)
            child = p.data["child"]
            all_codes = []
            for cs in p.data["pairs"].values():
                all_codes += [c for c in cs if c not in all_codes]
            ensure_list(child, all_codes)
            for code, cs in p.data["pairs"].items():
                k = model.find_kind(charter, code)
                if p.data["exact"]:
                    k["defaults"][child] = cs[0]
                else:
                    k["lists"][child] = list(cs)
        elif p.kind == "link":
            child = p.data["child"]
            all_codes = []
            for cs in p.data["pairs"].values():
                all_codes += [c for c in cs if c not in all_codes]
            ensure_list(child, all_codes)
            charter["links"] = [ln for ln in charter["links"] if ln.get("child") != child]
            charter["links"].append({"id": model.new_id("K"), "parents": list(p.data["parents"]), "child": child,
                                     "pairs": {str(k): list(v) for k, v in p.data["pairs"].items()}})
    if charter["kinds"]:
        palette = ("#0E7C74", "#D97706", "#2563EB", "#DC2626", "#7C3AED", "#059669", "#DB2777", "#65A30D",
                   "#0891B2", "#9333EA", "#EA580C", "#4B5563")
        for n, k in enumerate(charter["kinds"]):
            if not k.get("color"):
                k["color"] = palette[n % len(palette)]
    changed = fix_spellings(layer, merges) if fix_data and merges else 0
    return charter, lists, changed


def fix_spellings(layer, merges):
    """Writes {field: {old value: new value}} into the layer as one undo step (edit session left open).
    Returns the number of features changed."""
    if not merges:
        return 0
    if not layer.isEditable():
        layer.startEditing()
    layer.beginEditCommand(tr("Charter: one spelling for each value"))
    changed = set()
    try:
        idx = {f: layer.fields().indexOf(f) for f in merges}
        req = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry)
        for feat in layer.getFeatures(req):
            for f, mapping in merges.items():
                i = idx[f]
                if i < 0:
                    continue
                v = feat.attribute(i)
                if not _empty(v) and _norm_value(v) in mapping:
                    layer.changeAttributeValue(feat.id(), i, mapping[_norm_value(v)], v)
                    changed.add(feat.id())
    except Exception:
        layer.destroyEditCommand()
        raise
    layer.endEditCommand()
    return len(changed)
