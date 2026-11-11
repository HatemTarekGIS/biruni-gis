"""arcpy.analysis (Analysis tools) on GDAL/OGR/GEOS."""
import collections
import os

from osgeo import ogr

from . import _ops as O
from . import data as D
from . import geometry as G
from ._base import fail, missing_callable, tool, tool_warning, unsupported
from .management import _dissolve_table, _inputs, _names


def _fid_name(t):
    base = os.path.splitext(str(t["name"]))[0]
    return f"FID_{base}"


def _unique(name, taken):
    n, k = name, 1
    while n.lower() in taken:
        n = f"{name}_{k}"
        k += 1
    taken.add(n.lower())
    return n


# ---------------------------------------------------------------- selections

@tool("analysis.Select")
def Select(in_features, out_feature_class, where_clause=None):  # noqa: N802
    return D.copy_source(in_features, out_feature_class, where_clause or None)


@tool("analysis.TableSelect")
def TableSelect(in_table, out_table, where_clause=None):  # noqa: N802
    return D.copy_source(in_table, out_table, where_clause or None, want="table")


# ---------------------------------------------------------------- buffer

@tool("analysis.Buffer")
def Buffer(in_features, out_feature_class, buffer_distance_or_field, line_side="FULL",  # noqa: N802
           line_end_type="ROUND", dissolve_option="NONE", dissolve_field=None, method="PLANAR"):
    t = D.read_all(in_features)
    srs = t["srs"]
    names = [fd.GetName() for fd in t["fields"]]
    lower = {n.lower(): i for i, n in enumerate(names)}
    num, unit = G.parse_linear(buffer_distance_or_field)
    field_i = None
    if num is None:
        if unit is None or unit.lower() not in lower:
            fail(728, f"Field {buffer_distance_or_field} does not exist within table")
        field_i = lower[unit.lower()]
    geographic = srs is not None and srs.IsGeographic()
    opts = []
    if str(line_end_type).upper() == "FLAT":
        opts.append("ENDCAP_STYLE=FLAT")
    side = str(line_side or "FULL").upper()
    if side in ("LEFT", "RIGHT"):
        opts.append("SINGLE_SIDED=YES")

    def meters_or_units(value, value_unit):
        """-> (distance, True if it is meters for a geodesic buffer)."""
        f = G.linear_factor(value_unit) if value_unit else None
        if geographic and (f is not None or value_unit is None and method != "PLANAR"):
            return value * (f or 1.0), True
        if geographic and value_unit is None:
            return value, False
        if f is not None and srs is not None:
            return value * f / (srs.GetLinearUnits() or 1.0), False
        return value, False

    rows = []
    for fid, g, vals in t["rows"]:
        if g is None or g.IsEmpty():
            continue
        if field_i is not None:
            v = vals[field_i]
            if v is None:
                continue
            fnum, funit = G.parse_linear(v)
            if fnum is None:
                continue
            d, geo = meters_or_units(fnum, funit)
        else:
            d, geo = meters_or_units(num, unit)
        if side == "RIGHT":
            d = -d
        b = O.geodesic_buffer(g, d, srs, opts) if geo else O.buffer(g, d, opts)
        if side == "OUTSIDE_ONLY" and O.dim_of_type(t["geom_type"]) == 2:
            b = b.Difference(g)
        rows.append((fid, b, vals, d))
    dopt = str(dissolve_option or "NONE").upper()
    if dopt in ("ALL", "LIST"):
        dt = {"fields": t["fields"], "rows": [(fid, b, vals) for fid, b, vals, _d in rows],
              "geom_type": ogr.wkbMultiPolygon, "srs": srs, "name": t["name"]}
        return _dissolve_table(dt, out_feature_class, _names(dissolve_field) if dopt == "LIST" else [], None)
    fields = [D._clone_defn(fd) for fd in t["fields"]]
    taken = {f.GetName().lower() for f in fields}
    fields.append(ogr.FieldDefn(_unique("BUFF_DIST", taken), ogr.OFTReal))
    fields.append(ogr.FieldDefn(_unique("ORIG_FID", taken), ogr.OFTInteger))
    w = D.Writer(out_feature_class, ogr.wkbPolygon, srs, fields)
    for fid, b, vals, d in rows:
        w.add(vals + [abs(d), fid], O.keep_dim(b, 2))
    return w.close()


PairwiseBuffer = tool("analysis.PairwiseBuffer")(Buffer.__wrapped__)


# ---------------------------------------------------------------- overlay helpers

def _with_index(t):
    return O.Index([(i, g) for i, (_fid, g, _v) in enumerate(t["rows"])])


@tool("analysis.Clip")
def Clip(in_features, clip_features, out_feature_class, cluster_tolerance=None):  # noqa: N802
    t = D.read_all(in_features)
    c = D.read_all(clip_features, target_srs=t["srs"])
    if O.dim_of_type(c["geom_type"]) != 2:
        fail(732, "Clip Features: the clip features must be polygons")
    idx = _with_index(c)
    dim = O.dim_of_type(t["geom_type"])
    w = D.Writer(out_feature_class, t["geom_type"], t["srs"], t["fields"])
    for _fid, g, vals in t["rows"]:
        if g is None:
            continue
        cands = [idx.geom(k) for k in idx.candidates(g)]
        if not cands:
            continue
        mask = O.union_all(cands)
        if g.Within(mask):
            w.add(vals, g)
            continue
        part = O.keep_dim(g.Intersection(mask), dim)
        if part is not None:
            w.add(vals, part)
    return w.close()


PairwiseClip = tool("analysis.PairwiseClip")(Clip.__wrapped__)


@tool("analysis.Erase")
def Erase(in_features, erase_features, out_feature_class, cluster_tolerance=None):  # noqa: N802
    t = D.read_all(in_features)
    e = D.read_all(erase_features, target_srs=t["srs"])
    idx = _with_index(e)
    dim = O.dim_of_type(t["geom_type"])
    w = D.Writer(out_feature_class, t["geom_type"], t["srs"], t["fields"])
    for _fid, g, vals in t["rows"]:
        if g is None:
            continue
        cands = [idx.geom(k) for k in idx.candidates(g)]
        if not cands:
            w.add(vals, g)
            continue
        rest = O.keep_dim(g.Difference(O.union_all(cands)), dim)
        if rest is not None:
            w.add(vals, rest)
    return w.close()


PairwiseErase = tool("analysis.PairwiseErase")(Erase.__wrapped__)


def _start_table(t, with_fid=True):
    fields = []
    taken = set()
    if with_fid:
        fields.append(ogr.FieldDefn(_unique(_fid_name(t), taken), ogr.OFTInteger))
    for fd in t["fields"]:
        c = D._clone_defn(fd)
        c.SetName(_unique(fd.GetName(), taken))
        fields.append(c)
    rows = [(g, ([fid] if with_fid else []) + list(vals)) for fid, g, vals in t["rows"] if g is not None]
    return {"fields": fields, "rows": rows, "taken": taken, "srs": t["srs"], "geom_type": t["geom_type"]}


def _add_b_fields(acc, b, join_attributes):
    ja = str(join_attributes or "ALL").upper()
    taken = acc["taken"]
    new = []
    if ja in ("ALL", "ONLY_FID"):
        new.append(ogr.FieldDefn(_unique(_fid_name(b), taken), ogr.OFTInteger))
    if ja in ("ALL", "NO_FID"):
        for fd in b["fields"]:
            c = D._clone_defn(fd)
            c.SetName(_unique(fd.GetName(), taken))
            new.append(c)
    return new


def _b_values(b_fid, b_vals, join_attributes, n_b):
    ja = str(join_attributes or "ALL").upper()
    out = []
    if ja in ("ALL", "ONLY_FID"):
        out.append(b_fid)
    if ja in ("ALL", "NO_FID"):
        out.extend(b_vals if b_vals is not None else [None] * n_b)
    return out


def _intersect_pair(acc, b, dim, join_attributes):
    idx = _with_index(b)
    new_fields = _add_b_fields(acc, b, join_attributes)
    rows = []
    for g, vals in acc["rows"]:
        for k in idx.candidates(g):
            bg = idx.geom(k)
            if not g.Intersects(bg):
                continue
            part = O.keep_dim(g.Intersection(bg), dim)
            if part is None:
                continue
            b_fid, _bg, b_vals = b["rows"][k]
            rows.append((part, vals + _b_values(b_fid, b_vals, join_attributes, len(b["fields"]))))
    acc["fields"] += new_fields
    acc["rows"] = rows
    return acc


def _prep_inputs(in_features):
    items = _inputs(in_features)
    if not items:
        fail(735, "Input Features: Value is required")
    first = D.read_all(items[0])
    tables = [first] + [D.read_all(it, target_srs=first["srs"]) for it in items[1:]]
    return tables


@tool("analysis.Intersect")
def Intersect(in_features, out_feature_class, join_attributes="ALL", cluster_tolerance=None,  # noqa: N802
              output_type="INPUT"):
    tables = _prep_inputs(in_features)
    dims = [O.dim_of_type(t["geom_type"]) for t in tables]
    ot = str(output_type or "INPUT").upper()
    dim = {"INPUT": min(dims), "LINE": 1, "POINT": 0}.get(ot, min(dims))
    ja = str(join_attributes or "ALL").upper()
    acc = _start_table(tables[0], with_fid=ja in ("ALL", "ONLY_FID"))
    if ja == "ONLY_FID":
        acc["fields"] = acc["fields"][:1]
        acc["rows"] = [(g, v[:1]) for g, v in acc["rows"]]
    for b in tables[1:] if len(tables) > 1 else []:
        acc = _intersect_pair(acc, b, dim, ja)
    if len(tables) == 1:
        tool_warning("Intersect with one input: self-overlaps are not computed")
    w = D.Writer(out_feature_class, O.multi_type(dim) if dim != 0 else ogr.wkbMultiPoint, acc["srs"],
                 acc["fields"])
    for g, vals in acc["rows"]:
        w.add(vals, g)
    return w.close()


PairwiseIntersect = tool("analysis.PairwiseIntersect")(Intersect.__wrapped__)


def _union_pair(acc, b, join_attributes):
    idx_b = _with_index(b)
    new_fields = _add_b_fields(acc, b, join_attributes)
    n_a = len(acc["fields"])
    rows = []
    acc_geoms = [(i, g) for i, (g, _v) in enumerate(acc["rows"])]
    idx_a = O.Index(acc_geoms)
    for g, vals in acc["rows"]:
        cands = [k for k in idx_b.candidates(g) if g.Intersects(idx_b.geom(k))]
        for k in cands:
            part = O.keep_dim(g.Intersection(idx_b.geom(k)), 2)
            if part is None:
                continue
            b_fid, _bg, b_vals = b["rows"][k]
            rows.append((part, vals + _b_values(b_fid, b_vals, join_attributes, len(b["fields"]))))
        rest = O.keep_dim(g.Difference(O.union_all([idx_b.geom(k) for k in cands])), 2) if cands else g
        if rest is not None:
            rows.append((rest, vals + _b_values(-1, None, join_attributes, len(b["fields"]))))
    for b_fid, bg, b_vals in b["rows"]:
        if bg is None:
            continue
        cands = [k for k in idx_a.candidates(bg) if bg.Intersects(idx_a.geom(k))]
        rest = O.keep_dim(bg.Difference(O.union_all([idx_a.geom(k) for k in cands])), 2) if cands else bg
        if rest is not None:
            a_vals = [None] * n_a
            if n_a and acc["fields"][0].GetName().upper().startswith("FID_"):
                a_vals[0] = -1
            rows.append((rest, a_vals + _b_values(b_fid, b_vals, join_attributes, len(b["fields"]))))
    acc["fields"] += new_fields
    acc["rows"] = rows
    return acc


@tool("analysis.Union")
def Union(in_features, out_feature_class, join_attributes="ALL", cluster_tolerance=None, gaps="GAPS"):  # noqa: N802
    tables = _prep_inputs(in_features)
    if any(O.dim_of_type(t["geom_type"]) != 2 for t in tables):
        fail(366, "Invalid geometry type: Union needs polygon inputs")
    ja = str(join_attributes or "ALL").upper()
    acc = _start_table(tables[0], with_fid=ja in ("ALL", "ONLY_FID"))
    if ja == "ONLY_FID":
        acc["fields"] = acc["fields"][:1]
        acc["rows"] = [(g, v[:1]) for g, v in acc["rows"]]
    for b in tables[1:]:
        acc = _union_pair(acc, b, ja)
    w = D.Writer(out_feature_class, ogr.wkbMultiPolygon, acc["srs"], acc["fields"])
    for g, vals in acc["rows"]:
        w.add(vals, g)
    return w.close()


@tool("analysis.Identity")
def Identity(in_features, identity_features, out_feature_class, join_attributes="ALL",  # noqa: N802
             cluster_tolerance=None, relationship="NO_RELATIONSHIPS"):
    a = D.read_all(in_features)
    b = D.read_all(identity_features, target_srs=a["srs"])
    ja = str(join_attributes or "ALL").upper()
    dim = O.dim_of_type(a["geom_type"])
    acc = _start_table(a, with_fid=ja in ("ALL", "ONLY_FID"))
    idx = _with_index(b)
    new_fields = _add_b_fields(acc, b, ja)
    rows = []
    for g, vals in acc["rows"]:
        cands = [k for k in idx.candidates(g) if g.Intersects(idx.geom(k))]
        for k in cands:
            part = O.keep_dim(g.Intersection(idx.geom(k)), dim)
            if part is not None:
                b_fid, _bg, b_vals = b["rows"][k]
                rows.append((part, vals + _b_values(b_fid, b_vals, ja, len(b["fields"]))))
        rest = O.keep_dim(g.Difference(O.union_all([idx.geom(k) for k in cands])), dim) if cands else g
        if rest is not None:
            rows.append((rest, vals + _b_values(-1, None, ja, len(b["fields"]))))
    w = D.Writer(out_feature_class, O.multi_type(dim), a["srs"], acc["fields"] + new_fields)
    for g, vals in rows:
        w.add(vals, g)
    return w.close()


# ---------------------------------------------------------------- spatial join

def _match_test(match_option, d):
    mo = str(match_option or "INTERSECT").upper()

    def near(g, s):
        return g.Distance(s) <= d

    if mo in ("INTERSECT", "INTERSECT_3D"):
        return near if d else (lambda g, s: g.Intersects(s))
    if mo.startswith("WITHIN_A_DISTANCE"):
        return near
    if mo in ("CONTAINS", "COMPLETELY_CONTAINS", "CONTAINS_CLEMENTINI"):
        return lambda g, s: g.Contains(s)
    if mo in ("WITHIN", "COMPLETELY_WITHIN", "WITHIN_CLEMENTINI"):
        return lambda g, s: g.Within(s)
    if mo == "HAVE_THEIR_CENTER_IN":
        return lambda g, s: g.Centroid().Intersects(s)
    if mo == "ARE_IDENTICAL_TO":
        return lambda g, s: g.Equals(s)
    if mo == "BOUNDARY_TOUCHES":
        return lambda g, s: g.Touches(s)
    if mo == "SHARE_A_LINE_SEGMENT_WITH":
        return lambda g, s: O.keep_dim(g.Intersection(s), 1) is not None
    if mo in ("CLOSEST", "CLOSEST_GEODESIC"):
        return None
    unsupported(f"analysis.SpatialJoin match option {match_option}")


@tool("analysis.SpatialJoin")
def SpatialJoin(target_features, join_features, out_feature_class, join_operation="JOIN_ONE_TO_ONE",  # noqa: N802
                join_type="KEEP_ALL", field_mapping=None, match_option="INTERSECT", search_radius=None,
                distance_field_name=None, match_fields=None):
    t = D.read_all(target_features)
    j = D.read_all(join_features, target_srs=t["srs"])
    d, _f = O.distance_in_units(search_radius, t["srs"]) if search_radius not in (None, "", "#") else (0.0, None)
    d = d or 0.0
    test = _match_test(match_option, d)
    closest = test is None
    one_to_many = str(join_operation or "").upper() == "JOIN_ONE_TO_MANY"
    keep_all = str(join_type or "KEEP_ALL").upper() in ("KEEP_ALL", "TRUE")
    taken = set()
    fields = [ogr.FieldDefn(_unique("Join_Count", taken), ogr.OFTInteger),
              ogr.FieldDefn(_unique("TARGET_FID", taken), ogr.OFTInteger)]
    if one_to_many:
        fields.append(ogr.FieldDefn(_unique("JOIN_FID", taken), ogr.OFTInteger))
    for fd in t["fields"]:
        c = D._clone_defn(fd)
        c.SetName(_unique(fd.GetName(), taken))
        fields.append(c)
    for fd in j["fields"]:
        c = D._clone_defn(fd)
        c.SetName(_unique(fd.GetName(), taken))
        fields.append(c)
    if distance_field_name:
        fields.append(ogr.FieldDefn(_unique(str(distance_field_name), taken), ogr.OFTReal))
    idx = _with_index(j)
    nj = len(j["fields"])
    w = D.Writer(out_feature_class, t["geom_type"], t["srs"], fields)
    for fid, g, vals in t["rows"]:
        matches = []
        if g is not None:
            if closest:
                best = None
                pool = idx.candidates(g, d) if d else list(idx.items)
                for k in pool:
                    dist = g.Distance(idx.geom(k))
                    if (not d or dist <= d) and (best is None or dist < best[1]):
                        best = (k, dist)
                matches = [best] if best else []
            else:
                matches = [(k, g.Distance(idx.geom(k)) if distance_field_name else 0.0)
                           for k in sorted(idx.candidates(g, d)) if test(g, idx.geom(k))]
        if not matches:
            if keep_all:
                row = [0, fid] + ([-1] if one_to_many else []) + vals + [None] * nj
                if distance_field_name:
                    row.append(None)
                w.add(row, g)
            continue
        if one_to_many:
            for k, dist in matches:
                jfid, _jg, jvals = j["rows"][k]
                row = [1, fid, jfid] + vals + jvals + ([dist] if distance_field_name else [])
                w.add(row, g)
        else:
            k, dist = matches[0]
            _jfid, _jg, jvals = j["rows"][k]
            row = [len(matches), fid] + vals + jvals + ([dist] if distance_field_name else [])
            w.add(row, g)
    return w.close()


# ---------------------------------------------------------------- statistics

@tool("analysis.Statistics")
def Statistics(in_table, out_table, statistics_fields=None, case_field=None,  # noqa: N802
               concatenation_separator=None):
    t = D.read_all(in_table)
    names = [fd.GetName() for fd in t["fields"]]
    lower = {n.lower(): i for i, n in enumerate(names)}
    cases = []
    for n in _names(case_field):
        if n.lower() not in lower:
            fail(728, f"Field {n} does not exist within table")
        cases.append(lower[n.lower()])
    stats = O.parse_stats(statistics_fields)
    for n, _s in stats:
        if n.lower() not in lower:
            fail(728, f"Field {n} does not exist within table")
    groups = collections.OrderedDict()
    for _fid, _g, vals in t["rows"]:
        groups.setdefault(tuple(vals[c] for c in cases), []).append(vals)
    if not cases:
        groups = collections.OrderedDict([((), [v for _f, _g, v in t["rows"]])])
    fields = [D._clone_defn(t["fields"][c]) for c in cases] + [ogr.FieldDefn("FREQUENCY", ogr.OFTInteger)]
    fields += [O.stat_defn(s, n, t["fields"][lower[n.lower()]]) for n, s in stats]
    w = D.Writer(out_table, ogr.wkbNone, None, fields, want="table")
    for key in sorted(groups, key=lambda k: tuple((v is None, str(v) if not isinstance(v, (int, float)) else v)
                                                  for v in k)) if cases else groups:
        rows = groups[key]
        out = list(key) + [len(rows)]
        out += [O.compute_stat(s, [r[lower[n.lower()]] for r in rows], concatenation_separator or ", ")
                for n, s in stats]
        w.add(out)
    return w.close()


@tool("analysis.Frequency")
def Frequency(in_table, out_table, frequency_fields, summary_fields=None):  # noqa: N802
    t = D.read_all(in_table)
    names = [fd.GetName() for fd in t["fields"]]
    lower = {n.lower(): i for i, n in enumerate(names)}
    keys = [lower[n.lower()] for n in _names(frequency_fields) if n.lower() in lower]
    sums = [lower[n.lower()] for n in _names(summary_fields) if n.lower() in lower]
    groups = collections.OrderedDict()
    for _fid, _g, vals in t["rows"]:
        groups.setdefault(tuple(vals[k] for k in keys), []).append(vals)
    fields = [ogr.FieldDefn("FREQUENCY", ogr.OFTInteger)] + [D._clone_defn(t["fields"][k]) for k in keys]
    fields += [ogr.FieldDefn(names[s], ogr.OFTReal) for s in sums]
    w = D.Writer(out_table, ogr.wkbNone, None, fields, want="table")
    for key, rows in groups.items():
        w.add([len(rows)] + list(key) + [O.compute_stat("SUM", [r[s] for r in rows]) for s in sums])
    return w.close()


@tool("analysis.Near")
def Near(in_features, near_features, search_radius=None, location="NO_LOCATION", angle="NO_ANGLE",  # noqa: N802
         method="PLANAR", field_names=None, distance_unit=None):
    import math
    src = D.Source(in_features)
    srs = src.srs()
    near = []
    for item in _inputs(near_features):
        t = D.read_all(item, target_srs=srs)
        near += [(fid, g) for fid, g, _v in t["rows"] if g is not None]
    idx = O.Index([(i, g) for i, (_fid, g) in enumerate(near)])
    d, _f = O.distance_in_units(search_radius, srs) if search_radius not in (None, "", "#") else (None, None)
    names = dict(_names_map(field_names))
    from .management import _calc_rows, _ensure_field
    i_fid = _ensure_field(src, names.get("NEAR_FID", "NEAR_FID"), "LONG")
    i_dist = _ensure_field(src, names.get("NEAR_DIST", "NEAR_DIST"), "DOUBLE")
    want_loc = str(location).upper() == "LOCATION"
    want_ang = str(angle).upper() == "ANGLE"
    i_x = _ensure_field(src, names.get("NEAR_X", "NEAR_X"), "DOUBLE") if want_loc else None
    i_y = _ensure_field(src, names.get("NEAR_Y", "NEAR_Y"), "DOUBLE") if want_loc else None
    i_a = _ensure_field(src, names.get("NEAR_ANGLE", "NEAR_ANGLE"), "DOUBLE") if want_ang else None
    same = D.Source(in_features).ref.path.lower() in [D.Source(n).ref.path.lower() for n in _inputs(near_features)]

    def fn(f, geom):
        g = f.GetGeometryRef()
        best = None
        if g is not None:
            pool = idx.candidates(g, d) if d else list(idx.items)
            for k in pool:
                if same and near[k][0] == f.GetFID():
                    continue
                dist = g.Distance(idx.geom(k))
                if (d is None or dist <= d) and (best is None or dist < best[1]):
                    best = (k, dist)
        out = {i_fid: near[best[0]][0] if best else -1, i_dist: best[1] if best else -1}
        if best and (want_loc or want_ang):
            other = idx.geom(best[0])
            p = _nearest_point(g, other)
            if want_loc:
                out[i_x], out[i_y] = p
            if want_ang:
                c = g.Centroid()
                out[i_a] = math.degrees(math.atan2(p[1] - c.GetY(), p[0] - c.GetX()))
        elif want_loc:
            out[i_x], out[i_y] = -1, -1
        return out
    _calc_rows(src, fn, None)
    return src.ref.path


def _names_map(field_names):
    if not field_names:
        return []
    if isinstance(field_names, str):
        return [tuple(p.split()) for p in field_names.split(";") if len(p.split()) == 2]
    return [tuple(p) for p in field_names]


def _nearest_point(g, other):
    try:
        from qgis.core import QgsGeometry
        a = QgsGeometry.fromWkt(g.ExportToWkt())
        b = QgsGeometry.fromWkt(other.ExportToWkt())
        p = b.nearestPoint(a).asPoint()
        return p.x(), p.y()
    except Exception:
        c = other.Centroid()
        return c.GetX(), c.GetY()


@tool("analysis.PairwiseDissolve")
def PairwiseDissolve(in_features, out_feature_class, dissolve_field=None, statistics_fields=None,  # noqa: N802
                     multi_part="MULTI_PART", concatenation_separator=None):
    return _dissolve_table(D.read_all(in_features), out_feature_class, _names(dissolve_field), statistics_fields,
                           multi_part, concatenation_separator)


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return missing_callable(f"analysis.{name}")
