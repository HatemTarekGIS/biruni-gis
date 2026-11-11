"""arcpy.management (Data Management tools) on GDAL/OGR."""
import collections
import os
import re
import shutil

from osgeo import gdal, ogr, osr

from . import _ops as O
from . import data as D
from . import geometry as G
from ._base import env, fail, missing_callable, note_output, tool, tool_warning, unsupported


def _names(value):
    if value is None or value == "" or value == "#":
        return []
    if isinstance(value, str):
        return [s.strip().strip("'\"") for s in value.split(";") if s.strip()]
    out = []
    for v in value:
        out.append(str(v[0]) if isinstance(v, (list, tuple)) else str(v))
    return out


def _inputs(value):
    if isinstance(value, (list, tuple)):
        return [v[0] if isinstance(v, (list, tuple)) else v for v in value]
    if isinstance(value, str) and ";" in value:
        return [s.strip().strip("'\"") for s in value.split(";") if s.strip()]
    return [value]


def _out_path(out):
    if isinstance(out, D.Ref):
        return out.path
    return D.full_path(out) if isinstance(out, str) and not D._is_mem(out) else str(out)


# ---------------------------------------------------------------- counting and layers

@tool("management.GetCount")
def GetCount(in_rows):  # noqa: N802
    src = D.Source(in_rows)
    if src.layer is not None and src.layer.selection is not None:
        return str(len(src.layer.selection))
    if src.layer is None:
        ds, lyr = D.open_layer(src.ref)
        return str(lyr.GetFeatureCount())
    return str(sum(1 for _ in src.features()))


def _make_layer(in_features, out_layer, where_clause, is_table):
    src = D.Source(in_features)
    where = where_clause or None
    if src.layer is not None and src.layer.where:
        where = f"({src.layer.where}) AND ({where})" if where else src.layer.where
    name = str(out_layer)
    lyr = D.FeatureLayer(name, src.ref, where, is_table)
    if src.layer is not None and src.layer.selection is not None:
        lyr.selection = set(src.layer.selection)
    if where:
        ds, ol = D.open_layer(src.ref)
        if ol.SetAttributeFilter(D.sql_where(where, ol, src.ref)) != 0:
            fail(358, f"The SQL expression is invalid: {where}")
    D.LAYERS[name] = lyr
    return lyr


@tool("management.MakeFeatureLayer")
def MakeFeatureLayer(in_features, out_layer, where_clause=None, workspace=None, field_info=None):  # noqa: N802
    return _make_layer(in_features, out_layer, where_clause, False)


@tool("management.MakeTableView")
def MakeTableView(in_table, out_view, where_clause=None, workspace=None, field_info=None):  # noqa: N802
    return _make_layer(in_table, out_view, where_clause, True)


def _ensure_layer(value):
    lyr = D.as_layer(value)
    if lyr is not None:
        return lyr
    from . import mp
    if isinstance(value, mp.Layer):
        return value._as_feature_layer()
    src = D.Source(value)
    name = src.ref.base_name
    lyr = D.FeatureLayer(name, src.ref)
    D.LAYERS[name] = lyr
    return lyr


def _scan_fids(lyr, where=None):
    ds, ol = D.open_layer(lyr.ref)
    clauses = [c for c in (lyr.where, where) if c]
    if clauses:
        sql = " AND ".join(f"({D.sql_where(c, ol, lyr.ref)})" for c in clauses)
        if ol.SetAttributeFilter(sql) != 0:
            fail(358, f"The SQL expression is invalid: {sql}")
    return {f.GetFID() for f in ol}


def _combine(lyr, hits, selection_type):
    st = str(selection_type or "NEW_SELECTION").upper()
    cur = lyr.selection if lyr.selection is not None else set()
    if st == "NEW_SELECTION":
        new = hits
    elif st == "ADD_TO_SELECTION":
        new = cur | hits
    elif st == "REMOVE_FROM_SELECTION":
        new = cur - hits
    elif st == "SUBSET_SELECTION":
        new = cur & hits
    elif st == "SWITCH_SELECTION":
        new = _scan_fids(lyr) - cur
    elif st == "CLEAR_SELECTION":
        new = None
    else:
        fail(800, f"The value is not a member of NEW_SELECTION | ADD_TO_SELECTION | REMOVE_FROM_SELECTION | "
                  f"SUBSET_SELECTION | SWITCH_SELECTION | CLEAR_SELECTION: {selection_type}")
    lyr.selection = new
    return lyr


@tool("management.SelectLayerByAttribute")
def SelectLayerByAttribute(in_layer_or_view, selection_type="NEW_SELECTION", where_clause=None,  # noqa: N802
                           invert_where_clause=None):
    lyr = _ensure_layer(in_layer_or_view)
    st = str(selection_type or "NEW_SELECTION").upper()
    hits = set()
    if st not in ("CLEAR_SELECTION", "SWITCH_SELECTION"):
        hits = _scan_fids(lyr, where_clause or None)
        if str(invert_where_clause or "").upper() in ("INVERT", "TRUE") or invert_where_clause is True:
            hits = _scan_fids(lyr) - hits
    _combine(lyr, hits, st)
    return [lyr, str(len(lyr.selection) if lyr.selection is not None else 0)]


def _relation_test(overlap_type, d):
    ot = str(overlap_type or "INTERSECT").upper()

    def dist_ok(g, s):
        return g.Distance(s) <= d

    if ot in ("INTERSECT", "INTERSECT_3D", "INTERSECT_DBMS"):
        return (lambda g, s: g.Intersects(s)) if not d else dist_ok
    if ot.startswith("WITHIN_A_DISTANCE") or ot == "WITHIN_A_DISTANCE_3D":
        return dist_ok
    if ot in ("CONTAINS", "COMPLETELY_CONTAINS", "CONTAINS_CLEMENTINI"):
        return lambda g, s: g.Contains(s)
    if ot in ("WITHIN", "COMPLETELY_WITHIN", "WITHIN_CLEMENTINI"):
        return lambda g, s: g.Within(s) if not d else g.Within(s.Buffer(d))
    if ot == "HAVE_THEIR_CENTER_IN":
        return lambda g, s: (g.Centroid().Within(s) or g.Centroid().Intersects(s)) if not d else \
            g.Centroid().Distance(s) <= d
    if ot == "ARE_IDENTICAL_TO":
        return lambda g, s: g.Equals(s)
    if ot == "BOUNDARY_TOUCHES":
        return lambda g, s: g.Touches(s) or (g.Intersects(s) and not g.Within(s) and not s.Within(g))
    if ot == "SHARE_A_LINE_SEGMENT_WITH":
        return lambda g, s: O.keep_dim(g.Intersection(s), 1) is not None or O.keep_dim(
            g.Boundary().Intersection(s.Boundary()), 1) is not None
    if ot == "CROSSED_BY_THE_OUTLINE_OF":
        return lambda g, s: g.Crosses(s.Boundary()) or g.Intersects(s.Boundary())
    unsupported(f"management.SelectLayerByLocation overlap type {overlap_type}")


@tool("management.SelectLayerByLocation")
def SelectLayerByLocation(in_layer, overlap_type="INTERSECT", select_features=None, search_distance=None,  # noqa: N802
                          selection_type="NEW_SELECTION", invert_spatial_relationship="NOT_INVERT"):
    lyr = _ensure_layer(in_layer)
    st = str(selection_type or "NEW_SELECTION").upper()
    src = D.Source(lyr)
    srs = src.srs()
    hits = set()
    if st != "CLEAR_SELECTION":
        sel = D.read_all(select_features, target_srs=srs) if select_features is not None else {"rows": []}
        d, _f = O.distance_in_units(search_distance, srs) if search_distance not in (None, "", "#") else (0.0, None)
        d = d or 0.0
        test = _relation_test(overlap_type, d)
        idx = O.Index([(i, g) for i, (_fid, g, _v) in enumerate(sel["rows"])])
        ds, ol = D.open_layer(lyr.ref)
        if lyr.where:
            ol.SetAttributeFilter(D.sql_where(lyr.where, ol, lyr.ref))
        everything = set()
        for f in ol:
            fid = f.GetFID()
            everything.add(fid)
            g = f.GetGeometryRef()
            if g is None:
                continue
            for k in idx.candidates(g, d):
                if test(g, idx.geom(k)):
                    hits.add(fid)
                    break
        inv = invert_spatial_relationship
        if inv is True or str(inv or "").upper() == "INVERT":
            hits = everything - hits
    _combine(lyr, hits, st)
    return [lyr, str(len(lyr.selection) if lyr.selection is not None else 0), str(len(hits))]


# ---------------------------------------------------------------- copying and creating

@tool("management.CopyFeatures")
def CopyFeatures(in_features, out_feature_class, config_keyword=None, spatial_grid_1=None,  # noqa: N802
                 spatial_grid_2=None, spatial_grid_3=None):
    return D.copy_source(in_features, out_feature_class)


@tool("management.CopyRows")
def CopyRows(in_rows, out_table, config_keyword=None):  # noqa: N802
    return D.copy_source(in_rows, out_table, want="table")


@tool("management.ExportTable")
def ExportTable(in_table, out_table, where_clause=None, use_field_alias_as_name=None, field_mapping=None,  # noqa: N802
                sort_field=None):
    return D.copy_source(in_table, out_table, where_clause or None, want="table")


def _geom_type(geometry_type, has_z=False):
    gt = D.GEOM_TYPES.get(str(geometry_type or "POLYGON").upper())
    if gt is None:
        unsupported(f"management.CreateFeatureclass geometry type {geometry_type}")
    if has_z:
        gt = ogr.GT_SetZ(gt)
    return gt


@tool("management.CreateFeatureclass")
def CreateFeatureclass(out_path, out_name, geometry_type="POLYGON", template=None, has_m="DISABLED",  # noqa: N802
                       has_z="DISABLED", spatial_reference=None, config_keyword=None, spatial_grid_1=0,
                       spatial_grid_2=0, spatial_grid_3=0, out_alias=None, oid_type=None):
    path = os.path.join(D.full_path(out_path), str(out_name)) if not D._is_mem(str(out_path)) else \
        f"memory\\{out_name}"
    z = str(has_z or "").upper() in ("ENABLED", "SAME_AS_TEMPLATE", "TRUE") or has_z is True
    fields = []
    srs = G.osr_of(spatial_reference)
    for t in _inputs(template) if template not in (None, "", "#") else []:
        info = D.Source(t).info()
        have = {f.GetName().lower() for f in fields}
        fields += [f for f in info["fields"] if f.GetName().lower() not in have]
        if srs is None and spatial_reference in (None, "", "#"):
            srs = info["srs"]
    w = D.Writer(path, _geom_type(geometry_type, z), srs, fields, alias=out_alias)
    return w.close()


@tool("management.CreateTable")
def CreateTable(out_path, out_name, template=None, config_keyword=None, out_alias=None, oid_type=None):  # noqa: N802
    path = os.path.join(D.full_path(out_path), str(out_name)) if not D._is_mem(str(out_path)) else \
        f"memory\\{out_name}"
    fields = []
    for t in _inputs(template) if template not in (None, "", "#") else []:
        fields += D.Source(t).info()["fields"]
    w = D.Writer(path, ogr.wkbNone, None, fields, want="table", alias=out_alias)
    return w.close()


@tool("management.CreateFileGDB")
def CreateFileGDB(out_folder_path, out_name, out_version="CURRENT"):  # noqa: N802
    name = str(out_name)
    if not name.lower().endswith(".gdb"):
        name += ".gdb"
    path = os.path.join(D.full_path(out_folder_path), name)
    if os.path.exists(path):
        if not env.overwriteOutput:
            fail(725, f"Output File GDB: Dataset {path} already exists.")
        shutil.rmtree(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(path)
    if ds is None:
        fail(210, f"Cannot create output {path}")
    ds = None
    note_output(path)
    return path


@tool("management.CreateFeatureDataset")
def CreateFeatureDataset(out_dataset_path, out_name, spatial_reference=None):  # noqa: N802
    gdb = D.full_path(out_dataset_path)
    if not gdb.lower().endswith(".gdb"):
        fail(732, f"Feature datasets need a file geodatabase: {gdb}")
    ref = D.Ref("fd", gdb, None, str(out_name))
    if D.exists_ref(ref):
        if not env.overwriteOutput:
            fail(725, f"Output Dataset: Dataset {ref.path} already exists.")
        D.delete_ref(ref)
    D.create_container(ref)
    srs = G.osr_of(spatial_reference)
    D._PENDING_FD.setdefault(os.path.normcase(gdb), {})[str(out_name)] = srs
    return ref.path


@tool("management.CreateFolder")
def CreateFolder(out_folder_path, out_name):  # noqa: N802
    path = os.path.join(D.full_path(out_folder_path), str(out_name))
    os.makedirs(path, exist_ok=True)
    return path


@tool("management.Delete")
def Delete(in_data, data_type=None):  # noqa: N802
    for item in _inputs(in_data):
        if isinstance(item, D.FeatureLayer) or (isinstance(item, str) and item in D.LAYERS):
            D.LAYERS.pop(str(item), None)
            continue
        ref = D.parse(item)
        if not D.exists_ref(ref):
            fail(732, f"Input Data Element: Dataset {item} does not exist or is not supported")
        D.delete_ref(ref)
    return "true"


@tool("management.Rename")
def Rename(in_data, out_data, data_type=None):  # noqa: N802
    ref = D.parse(in_data)
    if not D.exists_ref(ref):
        fail(732, f"Input Data Element: Dataset {in_data} does not exist or is not supported")
    new_name = re.split(r"[\\/]", str(out_data))[-1]
    if ref.kind in ("gdb", "gpkg", "mem"):
        ds, lyr = D.open_layer(ref, update=True)
        if lyr.Rename(new_name) != 0:
            fail(210, f"Cannot rename {ref.path}")
        return D.parse(os.path.join(os.path.dirname(ref.path), new_name)).path if ref.kind != "mem" else \
            f"memory\\{new_name}"
    if ref.kind == "file" and ref.container.lower().endswith((".shp", ".dbf")):
        base = os.path.splitext(ref.container)[0]
        new_base = os.path.join(os.path.dirname(ref.container), os.path.splitext(new_name)[0])
        for side in D.SHP_SIDECARS:
            if os.path.isfile(base + side):
                os.rename(base + side, new_base + side)
        return new_base + os.path.splitext(ref.container)[1]
    target = os.path.join(os.path.dirname(ref.container), new_name)
    os.rename(ref.container, target)
    return target


@tool("management.Copy")
def Copy(in_data, out_data, data_type=None, associated_data=None):  # noqa: N802
    ref = D.parse(in_data)
    if ref.kind in ("folder", "gdbws"):
        target = D.full_path(out_data)
        if os.path.exists(target):
            if not env.overwriteOutput:
                fail(725, f"Output Data Element: Dataset {target} already exists.")
            shutil.rmtree(target)
        shutil.copytree(ref.container, target)
        return target
    if ref.kind == "raster":
        out = D.check_output(out_data, "raster")
        D.create_container(out)
        gdal.Translate(out.container, ref.container)
        note_output(out.container)
        return out.container
    return D.copy_source(in_data, out_data)


def _delete_selected(in_rows):
    src = D.Source(in_rows)
    fids = src.fids()
    ds, lyr = D.open_layer(src.ref, update=True)
    tx = src.ref.kind == "gpkg" and ds.StartTransaction() == 0
    for fid in fids:
        lyr.DeleteFeature(fid)
    if tx:
        ds.CommitTransaction()
    lyr.SyncToDisk()
    if src.layer is not None:
        src.layer.selection = None
    return src.ref.path if src.layer is None else src.layer


@tool("management.DeleteFeatures")
def DeleteFeatures(in_features):  # noqa: N802
    return _delete_selected(in_features)


@tool("management.DeleteRows")
def DeleteRows(in_rows):  # noqa: N802
    return _delete_selected(in_rows)


@tool("management.TruncateTable")
def TruncateTable(in_table):  # noqa: N802
    src = D.Source(in_table)
    ds, lyr = D.open_layer(src.ref, update=True)
    for fid in [f.GetFID() for f in lyr]:
        lyr.DeleteFeature(fid)
    lyr.SyncToDisk()
    return src.ref.path


# ---------------------------------------------------------------- fields

@tool("management.AddField")
def AddField(in_table, field_name, field_type, field_precision=None, field_scale=None, field_length=None,  # noqa: N802
             field_alias=None, field_is_nullable="NULLABLE", field_is_required="NON_REQUIRED", field_domain=None):
    src = D.Source(in_table)
    ds, lyr = D.open_layer(src.ref, update=True)
    if D.field_index(lyr.GetLayerDefn(), field_name) >= 0:
        tool_warning(f"WARNING 000012: {field_name} already exists")
        return in_table if isinstance(in_table, str) else src.ref.path
    fd = D.make_defn(field_name, field_type, field_length, field_precision, field_scale, field_alias, src.ref)
    if str(field_is_nullable).upper() == "NON_NULLABLE":
        fd.SetNullable(False)
    if lyr.CreateField(fd) != 0:
        fail(210, f"Cannot add field {field_name} to {src.ref.path}")
    lyr.SyncToDisk()
    return in_table if isinstance(in_table, str) else src.ref.path


@tool("management.AddFields")
def AddFields(in_table, field_description, template=None):  # noqa: N802
    for item in field_description or []:
        item = list(item) + [None] * 6
        AddField(in_table, item[0], item[1], None, None, item[3], item[2], None, None, item[5])
    return in_table


@tool("management.DeleteField")
def DeleteField(in_table, drop_field, method="DELETE_FIELDS"):  # noqa: N802
    src = D.Source(in_table)
    ds, lyr = D.open_layer(src.ref, update=True)
    defn = lyr.GetLayerDefn()
    names = [n.lower() for n in _names(drop_field)]
    current = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    if str(method or "").upper() == "KEEP_FIELDS":
        drop = [n for n in current if n.lower() not in names]
    else:
        drop = [n for n in current if n.lower() in names]
        unknown = set(names) - {n.lower() for n in current} - {"shape_area", "shape_length"}
        for u in unknown:
            tool_warning(f"WARNING 000594: Field {u} does not exist")
    for n in drop:
        i = D.field_index(lyr.GetLayerDefn(), n)
        if i >= 0 and lyr.DeleteField(i) != 0:
            fail(210, f"Cannot delete field {n}")
    lyr.SyncToDisk()
    return in_table if isinstance(in_table, str) else src.ref.path


@tool("management.AlterField")
def AlterField(in_table, field, new_field_name=None, new_field_alias=None, field_type=None,  # noqa: N802
               field_length=None, field_is_nullable=None, clear_field_alias=None):
    src = D.Source(in_table)
    ds, lyr = D.open_layer(src.ref, update=True)
    defn = lyr.GetLayerDefn()
    i = D.field_index(defn, field)
    if i < 0:
        fail(728, f"Field {field} does not exist within table")
    old = defn.GetFieldDefn(i)
    nd = D._clone_defn(old)
    flags = 0
    if new_field_name:
        nd.SetName(str(new_field_name))
        flags |= ogr.ALTER_NAME_FLAG
    if new_field_alias or str(clear_field_alias).upper() in ("TRUE", "CLEAR_ALIAS"):
        nd.SetAlternativeName("" if not new_field_alias else str(new_field_alias))
        flags |= ogr.ALTER_ALTERNATIVE_NAME_FLAG
    if field_length:
        nd.SetWidth(int(field_length))
        flags |= ogr.ALTER_WIDTH_PRECISION_FLAG
    if field_type:
        t = D.make_defn("x", field_type)
        nd.SetType(t.GetType())
        nd.SetSubType(t.GetSubType())
        flags |= ogr.ALTER_TYPE_FLAG
    if flags and lyr.AlterFieldDefn(i, nd, flags) != 0:
        fail(210, f"Cannot alter field {field}")
    lyr.SyncToDisk()
    return src.ref.path


def _calc_rows(src, fn, field_index_or_names, ensure=None):
    """Run fn(feature, geometry_getter) -> {index: value} on every selected row."""
    fids = src.fids()
    ds, lyr = D.open_layer(src.ref, update=True)
    srs = lyr.GetSpatialRef()
    sr = G.SpatialReference._from_osr(srs) if srs is not None else None
    tx = src.ref.kind == "gpkg" and ds.StartTransaction() == 0
    try:
        for fid in fids:
            f = lyr.GetFeature(fid)
            if f is None:
                continue
            cache = {}

            def geom(f=f, cache=cache):
                if "g" not in cache:
                    g = f.GetGeometryRef()
                    cache["g"] = G.wrap(g.Clone(), sr) if g is not None and not g.IsEmpty() else None
                return cache["g"]
            for idx, value in fn(f, geom).items():
                D.set_value(f, idx, value)
            lyr.SetFeature(f)
        if tx:
            ds.CommitTransaction()
            tx = False
    finally:
        if tx:
            ds.RollbackTransaction()
    lyr.SyncToDisk()


def _ensure_field(src, name, field_type):
    ds, lyr = D.open_layer(src.ref, update=True)
    i = D.field_index(lyr.GetLayerDefn(), name)
    if i < 0:
        lyr.CreateField(D.make_defn(name, field_type, ref=src.ref))
        lyr.SyncToDisk()
        i = D.field_index(lyr.GetLayerDefn(), name)
        if i < 0:  # shapefile truncated the name
            i = D.field_index(lyr.GetLayerDefn(), str(name)[:10])
    return i


@tool("management.CalculateField")
def CalculateField(in_table, field, expression, expression_type="PYTHON3", code_block="",  # noqa: N802
                   field_type="TEXT", enforce_domains="NO_ENFORCE_DOMAINS"):
    src = D.Source(in_table)
    idx = _ensure_field(src, field, field_type or "TEXT")
    info = src.info()
    names = [fd.GetName() for fd in info["fields"]]
    et = str(expression_type or "PYTHON3").upper()
    expr = str(expression)
    holder = {}
    if et in ("PYTHON3", "PYTHON", "PYTHON_9.3"):
        py = O.python_expression(expr, names, info["shape"])
        ns = O.base_namespace()
        if code_block:
            exec(compile(str(code_block), "<code_block>", "exec"), ns)  # noqa: S102 - user field-calculator code
    elif et == "ARCADE":
        py = O.arcade_expression(expr, names)
        ns = O.arcade_namespace(lambda: holder["geom"]())
    elif et == "VB":
        if code_block:
            unsupported("management.CalculateField with VB code blocks")
        py = O.vb_expression(expr, names)
        ns = O.base_namespace()
    else:
        unsupported(f"management.CalculateField with {et} expressions")
    try:
        code = compile(py, "<expression>", "eval")
    except SyntaxError as e:
        fail(539, f"Invalid expression {expr}: {e}")
    oid_n = info["oid"]

    def fn(f, geom):
        row = {n: D.get_value(f, i) for i, n in enumerate(names)}
        row[oid_n] = f.GetFID()
        holder["geom"] = geom
        ns["__row"] = row
        ns["__geom"] = geom
        ns["__feature"] = row
        ns["__shape"] = lambda chain: O._shape_token(chain, geom())
        try:
            value = eval(code, ns)  # noqa: S307 - the expression of the script being run
        except Exception as e:  # noqa: BLE001
            fail(539, f"Error running expression: {expr}\n{type(e).__name__}: {e}")
        return {idx: value}
    _calc_rows(src, fn, None)
    return in_table if isinstance(in_table, str) else src.ref.path


@tool("management.CalculateFields")
def CalculateFields(in_table, expression_type="PYTHON3", fields=None, code_block="",  # noqa: N802
                    enforce_domains=None):
    for item in fields or []:
        CalculateField(in_table, item[0], item[1], expression_type, code_block)
    return in_table


_GEOM_PROPS = {
    "AREA", "AREA_GEODESIC", "PERIMETER_LENGTH", "PERIMETER_LENGTH_GEODESIC", "LENGTH", "LENGTH_GEODESIC",
    "LENGTH_3D", "CENTROID_X", "CENTROID_Y", "CENTROID_Z", "INSIDE_X", "INSIDE_Y", "POINT_X", "POINT_Y", "POINT_Z",
    "POINT_COUNT", "PART_COUNT", "EXTENT_MIN_X", "EXTENT_MIN_Y", "EXTENT_MAX_X", "EXTENT_MAX_Y", "LINE_START_X",
    "LINE_START_Y", "LINE_END_X", "LINE_END_Y", "LINE_BEARING", "CENTROID_M", "POINT_M",
}


def _geom_prop(g, prop, length_unit, area_unit):
    if g is None:
        return None
    if prop in ("AREA", "AREA_GEODESIC"):
        return g.getArea("GEODESIC" if prop.endswith("GEODESIC") else "PLANAR", area_unit or None)
    if prop in ("PERIMETER_LENGTH", "PERIMETER_LENGTH_GEODESIC", "LENGTH", "LENGTH_GEODESIC", "LENGTH_3D"):
        return g.getLength("GEODESIC" if prop.endswith("GEODESIC") else "PLANAR", length_unit or None)
    if prop in ("CENTROID_X", "POINT_X"):
        return g.trueCentroid.X if prop == "CENTROID_X" else g.firstPoint.X
    if prop in ("CENTROID_Y", "POINT_Y"):
        return g.trueCentroid.Y if prop == "CENTROID_Y" else g.firstPoint.Y
    if prop in ("CENTROID_Z", "POINT_Z"):
        return g.firstPoint.Z
    if prop in ("INSIDE_X", "INSIDE_Y"):
        p = g.labelPoint
        return p.X if prop == "INSIDE_X" else p.Y
    if prop == "POINT_COUNT":
        return g.pointCount
    if prop == "PART_COUNT":
        return g.partCount
    e = g.extent
    if prop.startswith("EXTENT_"):
        return {"EXTENT_MIN_X": e.XMin, "EXTENT_MIN_Y": e.YMin, "EXTENT_MAX_X": e.XMax, "EXTENT_MAX_Y": e.YMax}[prop]
    if prop.startswith("LINE_"):
        a, b = g.firstPoint, g.lastPoint
        if prop == "LINE_BEARING":
            import math
            return (math.degrees(math.atan2(b.X - a.X, b.Y - a.Y)) + 360) % 360
        return {"LINE_START_X": a.X, "LINE_START_Y": a.Y, "LINE_END_X": b.X, "LINE_END_Y": b.Y}[prop]
    return None


def _apply_geom_props(in_features, pairs, length_unit, area_unit, coordinate_system=None):
    src = D.Source(in_features)
    target = G.as_sr(coordinate_system)
    plan = []
    for name, prop in pairs:
        prop = str(prop).upper()
        if prop not in _GEOM_PROPS:
            unsupported(f"geometry property {prop}")
        ftype = "LONG" if prop in ("POINT_COUNT", "PART_COUNT") else "DOUBLE"
        plan.append((_ensure_field(src, name, ftype), prop))

    def fn(f, geom):
        g = geom()
        if g is not None and target is not None and not target._empty:
            g = g.projectAs(target)
        return {i: _geom_prop(g, prop, length_unit, area_unit) for i, prop in plan}
    _calc_rows(src, fn, None)
    return src.ref.path


@tool("management.CalculateGeometryAttributes")
def CalculateGeometryAttributes(in_features, geometry_property, length_unit="", area_unit="",  # noqa: N802
                                coordinate_system=None, coordinate_format="SAME_AS_INPUT"):
    if isinstance(geometry_property, str):
        pairs = [p.strip().split() for p in geometry_property.split(";") if p.strip()]
    else:
        pairs = [list(p) for p in geometry_property]
    return _apply_geom_props(in_features, pairs, length_unit, area_unit, coordinate_system)


@tool("management.AddGeometryAttributes")
def AddGeometryAttributes(Input_Features, Geometry_Properties, Length_Unit="", Area_Unit="",  # noqa: N802,N803
                          Coordinate_System=None):  # noqa: N803
    names = {"AREA": [("POLY_AREA", "AREA")], "AREA_GEODESIC": [("AREA_GEO", "AREA_GEODESIC")],
             "PERIMETER_LENGTH": [("PERIMETER", "PERIMETER_LENGTH")],
             "PERIMETER_LENGTH_GEODESIC": [("PERIM_GEO", "PERIMETER_LENGTH_GEODESIC")],
             "LENGTH": [("LENGTH", "LENGTH")], "LENGTH_GEODESIC": [("LENGTH_GEO", "LENGTH_GEODESIC")],
             "CENTROID": [("CENTROID_X", "CENTROID_X"), ("CENTROID_Y", "CENTROID_Y")],
             "CENTROID_INSIDE": [("INSIDE_X", "INSIDE_X"), ("INSIDE_Y", "INSIDE_Y")],
             "POINT_X_Y_Z_M": [("POINT_X", "POINT_X"), ("POINT_Y", "POINT_Y")],
             "PART_COUNT": [("PART_COUNT", "PART_COUNT")], "POINT_COUNT": [("PNT_COUNT", "POINT_COUNT")],
             "EXTENT": [("EXT_MIN_X", "EXTENT_MIN_X"), ("EXT_MIN_Y", "EXTENT_MIN_Y"), ("EXT_MAX_X", "EXTENT_MAX_X"),
                        ("EXT_MAX_Y", "EXTENT_MAX_Y")],
             "LINE_START_MID_END": [("START_X", "LINE_START_X"), ("START_Y", "LINE_START_Y"),
                                    ("END_X", "LINE_END_X"), ("END_Y", "LINE_END_Y")],
             "LINE_BEARING": [("BEARING", "LINE_BEARING")]}
    pairs = []
    for p in _names(Geometry_Properties):
        if p.upper() not in names:
            unsupported(f"management.AddGeometryAttributes property {p}")
        pairs += names[p.upper()]
    return _apply_geom_props(Input_Features, pairs, Length_Unit, Area_Unit, Coordinate_System)


@tool("management.AddXY")
def AddXY(in_features):  # noqa: N802
    return _apply_geom_props(in_features, [("POINT_X", "POINT_X"), ("POINT_Y", "POINT_Y")], "", "")


@tool("management.JoinField")
def JoinField(in_data, in_field, join_table, join_field, fields=None, fm_option=None, field_mapping=None,  # noqa: N802
              index_join_fields=None):
    target = D.Source(in_data)
    j = D.read_all(join_table)
    jnames = [fd.GetName() for fd in j["fields"]]
    ji = next((i for i, n in enumerate(jnames) if n.lower() == str(join_field).lower()), None)
    if ji is None:
        fail(728, f"Field {join_field} does not exist within table")
    wanted = _names(fields) or [n for n in jnames]
    take = []
    for w in wanted:
        k = next((i for i, n in enumerate(jnames) if n.lower() == w.lower()), None)
        if k is None:
            tool_warning(f"WARNING 000594: Field {w} does not exist in the join table")
            continue
        take.append(k)
    lookup = {}
    for _fid, _g, vals in j["rows"]:
        key = vals[ji]
        if key not in lookup:
            lookup[key] = vals
    ds, lyr = D.open_layer(target.ref, update=True)
    existing = {lyr.GetLayerDefn().GetFieldDefn(i).GetName().lower()
                for i in range(lyr.GetLayerDefn().GetFieldCount())}
    targets = []
    for k in take:
        fd = D._clone_defn(j["fields"][k])
        name = fd.GetName()
        n = 1
        while name.lower() in existing:
            name = f"{fd.GetName()}_{n}"
            n += 1
        fd.SetName(name)
        lyr.CreateField(fd)
        existing.add(name.lower())
        targets.append((D.field_index(lyr.GetLayerDefn(), name), k))
    lyr.SyncToDisk()
    ds = None
    info = target.info()
    ti = next((i for i, fd in enumerate(info["fields"]) if fd.GetName().lower() == str(in_field).lower()), None)
    if ti is None:
        fail(728, f"Field {in_field} does not exist within table")

    def fn(f, geom):
        vals = lookup.get(D.get_value(f, ti))
        return {i: (vals[k] if vals is not None else None) for i, k in targets if i >= 0}
    _calc_rows(target, fn, None)
    return target.ref.path


# ---------------------------------------------------------------- whole-dataset tools

@tool("management.Merge")
def Merge(inputs, output, field_mappings=None, add_source="NO_SOURCE_INFO"):  # noqa: N802
    items = _inputs(inputs)
    if not items:
        fail(735, "Input Datasets: Value is required")
    first = D.Source(items[0]).info()
    srs = first["srs"]
    tables = [D.read_all(it, target_srs=srs) for it in items]
    fields, seen = [], {}
    for t in tables:
        for fd in t["fields"]:
            if fd.GetName().lower() not in seen:
                seen[fd.GetName().lower()] = len(fields)
                fields.append(D._clone_defn(fd))
    src_info = str(add_source).upper() == "ADD_SOURCE_INFO"
    if src_info:
        f = ogr.FieldDefn("MERGE_SRC", ogr.OFTString)
        f.SetWidth(255)
        fields.append(f)
    dims = {O.dim_of_type(t["geom_type"]) for t in tables}
    gt = first["geom_type"]
    if len(dims - {-1}) > 1:
        fail(361, "The input datasets have different geometry types")
    if len(tables) > 1 and gt != ogr.wkbNone:
        gt = O.multi_type(O.dim_of_type(gt)) if any(O.dim_of_type(t["geom_type"]) >= 0 for t in tables) else gt
        if ogr.GT_Flatten(first["geom_type"]) in (ogr.wkbPoint,) and all(
                ogr.GT_Flatten(t["geom_type"]) == ogr.wkbPoint for t in tables):
            gt = first["geom_type"]
    w = D.Writer(output, gt, srs, fields, want="vector" if gt != ogr.wkbNone else "table")
    for it, t in zip(items, tables):
        pos = [seen[fd.GetName().lower()] for fd in t["fields"]]
        for _fid, g, vals in t["rows"]:
            row = [None] * len(fields)
            for p, v in zip(pos, vals):
                row[p] = v
            if src_info:
                row[-1] = str(D.Source(it).ref.path)
            w.add(row, g)
    return w.close()


@tool("management.Append")
def Append(inputs, target, schema_type="TEST", field_mapping=None, subtype=None, expression=None,  # noqa: N802
           match_fields=None, update_geometry=None, enforce_domains=None, feature_service_mode=None):
    tsrc = D.Source(target)
    tinfo = tsrc.info()
    tnames = [fd.GetName().lower() for fd in tinfo["fields"]]
    tables = [D.read_all(it, expression or None, target_srs=tinfo["srs"]) for it in _inputs(inputs)]
    if str(schema_type or "TEST").upper() == "TEST":
        for t in tables:
            missing = [n for n in tnames if n not in {fd.GetName().lower() for fd in t["fields"]}]
            if missing:
                tool_warning(f"WARNING: fields {', '.join(missing)} are missing in the input; they are left empty")
    ds, lyr = D.open_layer(tsrc.ref, update=True)
    defn = lyr.GetLayerDefn()
    tx = tsrc.ref.kind == "gpkg" and ds.StartTransaction() == 0
    for t in tables:
        mapping = [D.field_index(defn, fd.GetName()) for fd in t["fields"]]
        for _fid, g, vals in t["rows"]:
            f = ogr.Feature(defn)
            for idx, v in zip(mapping, vals):
                if idx >= 0:
                    try:
                        D.set_value(f, idx, v)
                    except (ValueError, TypeError, OverflowError):
                        f.SetFieldNull(idx)
            if g is not None and defn.GetGeomType() != ogr.wkbNone:
                f.SetGeometry(D.geom_for_layer(g, lyr))
            lyr.CreateFeature(f)
    if tx:
        ds.CommitTransaction()
    lyr.SyncToDisk()
    return tsrc.ref.path


@tool("management.Dissolve")
def Dissolve(in_features, out_feature_class, dissolve_field=None, statistics_fields=None,  # noqa: N802
             multi_part="MULTI_PART", unsplit_lines="DISSOLVE_LINES", concatenation_separator=None):
    t = D.read_all(in_features)
    return _dissolve_table(t, out_feature_class, _names(dissolve_field), statistics_fields, multi_part,
                           concatenation_separator)


def _dissolve_table(t, out, dfields, statistics_fields, multi_part="MULTI_PART", sep=None):
    names = [fd.GetName() for fd in t["fields"]]
    lower = {n.lower(): i for i, n in enumerate(names)}
    keys = []
    for n in dfields:
        if n.lower() not in lower:
            fail(728, f"Field {n} does not exist within table")
        keys.append(lower[n.lower()])
    stats = O.parse_stats(statistics_fields)
    for n, _s in stats:
        if n.lower() not in lower:
            fail(728, f"Field {n} does not exist within table")
    groups = collections.OrderedDict()
    for _fid, g, vals in t["rows"]:
        key = tuple(vals[k] for k in keys)
        groups.setdefault(key, ([], []))
        groups[key][0].append(g)
        groups[key][1].append(vals)
    dim = O.dim_of_type(t["geom_type"])
    fields = [D._clone_defn(t["fields"][k]) for k in keys]
    fields += [O.stat_defn(s, n, t["fields"][lower[n.lower()]]) for n, s in stats]
    single = str(multi_part).upper() in ("SINGLE_PART", "FALSE")
    gt = ogr.GT_Flatten(t["geom_type"]) if single else O.multi_type(dim)
    if single and gt in (ogr.wkbMultiPolygon, ogr.wkbMultiLineString, ogr.wkbMultiPoint):
        gt = {ogr.wkbMultiPolygon: ogr.wkbPolygon, ogr.wkbMultiLineString: ogr.wkbLineString,
              ogr.wkbMultiPoint: ogr.wkbPoint}[gt]
    w = D.Writer(out, gt, t["srs"], fields)
    for key, (geoms, rows) in groups.items():
        u = O.union_all(geoms)
        if u is not None and dim >= 0:
            u = O.keep_dim(u, dim) or u
        if dim == 1 and u is not None:
            try:
                u = u.LineMerge() if hasattr(u, "LineMerge") else u
            except Exception:
                pass
        row = list(key) + [O.compute_stat(s, [r[lower[n.lower()]] for r in rows], sep or ", ") for n, s in stats]
        if single and u is not None and u.GetGeometryCount() and ogr.GT_Flatten(u.GetGeometryType()) in (
                ogr.wkbMultiPolygon, ogr.wkbMultiLineString, ogr.wkbMultiPoint):
            for i in range(u.GetGeometryCount()):
                w.add(row, u.GetGeometryRef(i).Clone())
        else:
            w.add(row, u)
    return w.close()


@tool("management.Project")
def Project(in_dataset, out_dataset, out_coor_system, transform_method=None, in_coor_system=None,  # noqa: N802
            preserve_shape=None, max_deviation=None, vertical=None):
    ref = D.parse(in_dataset) if D.as_layer(in_dataset) is None else None
    if ref is not None and ref.kind == "raster":
        return _project_raster(in_dataset, out_dataset, out_coor_system)
    target = G.osr_of(out_coor_system)
    if target is None:
        fail(735, "Output Coordinate System: Value is required")
    t = D.read_all(in_dataset)
    srs = t["srs"] or G.osr_of(in_coor_system)
    if srs is None:
        fail(1985, "Invalid extent for output coordinate system: the input has an unknown coordinate system")
    ct = G.transform(srs, target)
    w = D.Writer(out_dataset, t["geom_type"], target, t["fields"], honour_ocs=False)
    for _fid, g, vals in t["rows"]:
        if g is not None and ct is not None:
            g.Transform(ct)
        w.add(vals, g)
    return w.close()


@tool("management.FeatureToPoint")
def FeatureToPoint(in_features, out_feature_class, point_location="CENTROID"):  # noqa: N802
    t = D.read_all(in_features)
    fields = [ogr.FieldDefn("ORIG_FID", ogr.OFTInteger)] + [D._clone_defn(f) for f in t["fields"]]
    w = D.Writer(out_feature_class, ogr.wkbPoint, t["srs"], fields)
    inside = str(point_location).upper() == "INSIDE"
    for fid, g, vals in t["rows"]:
        p = None
        if g is not None and not g.IsEmpty():
            p = g.PointOnSurface() if inside else g.Centroid()
        w.add([fid] + vals, p)
    return w.close()


@tool("management.MultipartToSinglepart")
def MultipartToSinglepart(in_features, out_feature_class):  # noqa: N802
    t = D.read_all(in_features)
    fields = [D._clone_defn(f) for f in t["fields"]] + [ogr.FieldDefn("ORIG_FID", ogr.OFTInteger)]
    flat = ogr.GT_Flatten(t["geom_type"])
    single = {ogr.wkbMultiPolygon: ogr.wkbPolygon, ogr.wkbMultiLineString: ogr.wkbLineString,
              ogr.wkbMultiPoint: ogr.wkbPoint}.get(flat, flat)
    w = D.Writer(out_feature_class, single, t["srs"], fields)
    for fid, g, vals in t["rows"]:
        if g is not None and ogr.GT_Flatten(g.GetGeometryType()) in (ogr.wkbMultiPolygon, ogr.wkbMultiLineString,
                                                                     ogr.wkbMultiPoint, ogr.wkbGeometryCollection):
            for i in range(g.GetGeometryCount()):
                w.add(vals + [fid], g.GetGeometryRef(i).Clone())
        else:
            w.add(vals + [fid], g)
    return w.close()


@tool("management.RepairGeometry")
def RepairGeometry(in_features, delete_null="DELETE_NULL", validation_method="ESRI"):  # noqa: N802
    src = D.Source(in_features)
    fids = src.fids()
    ds, lyr = D.open_layer(src.ref, update=True)
    drop = str(delete_null).upper() in ("DELETE_NULL", "TRUE")
    for fid in fids:
        f = lyr.GetFeature(fid)
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            if drop:
                lyr.DeleteFeature(fid)
            continue
        if not g.IsValid():
            fixed = g.MakeValid()
            dim = O.dim_of_type(lyr.GetGeomType())
            fixed = O.keep_dim(fixed, dim) if dim >= 0 else fixed
            f.SetGeometry(D.geom_for_layer(fixed, lyr) if fixed is not None else None)
            lyr.SetFeature(f)
    lyr.SyncToDisk()
    return src.ref.path


@tool("management.CheckGeometry")
def CheckGeometry(in_features, out_table, validation_method="ESRI"):  # noqa: N802
    fields = [ogr.FieldDefn("CLASS", ogr.OFTString), ogr.FieldDefn("FEATURE_ID", ogr.OFTInteger),
              ogr.FieldDefn("PROBLEM", ogr.OFTString)]
    w = D.Writer(out_table, ogr.wkbNone, None, fields, want="table")
    for item in _inputs(in_features):
        src = D.Source(item)
        for f in src.features():
            g = f.GetGeometryRef()
            problem = None
            if g is None:
                problem = "null geometry"
            elif g.IsEmpty():
                problem = "empty geometry"
            elif not g.IsValid():
                problem = "self intersections"
            if problem:
                w.add([src.ref.path, f.GetFID(), problem])
    return w.close()


@tool("management.PolygonToLine")
def PolygonToLine(in_features, out_feature_class, neighbor_option="IDENTIFY_NEIGHBORS"):  # noqa: N802
    t = D.read_all(in_features)
    if str(neighbor_option).upper() == "IDENTIFY_NEIGHBORS":
        tool_warning("Neighbours are not identified; each polygon outline is written as it is")
    fields = [ogr.FieldDefn("ORIG_FID", ogr.OFTInteger)] + [D._clone_defn(f) for f in t["fields"]]
    w = D.Writer(out_feature_class, ogr.wkbMultiLineString, t["srs"], fields)
    for fid, g, vals in t["rows"]:
        if g is None or g.IsEmpty():
            continue
        w.add([fid] + vals, ogr.ForceToMultiLineString(g.Boundary()))
    return w.close()


@tool("management.FeatureToPolygon")
def FeatureToPolygon(in_features, out_feature_class, cluster_tolerance=None, attributes="ATTRIBUTES",  # noqa: N802
                     label_features=None):
    tables = [D.read_all(it) for it in _inputs(in_features)]
    srs = tables[0]["srs"]
    if len(tables) > 1:
        tables = [tables[0]] + [D.read_all(it, target_srs=srs) for it in _inputs(in_features)[1:]]
    lines = []
    for t in tables:
        for _fid, g, _v in t["rows"]:
            if g is None or g.IsEmpty():
                continue
            lines.append(g.Boundary() if O.dim_of_type(g.GetGeometryType()) == 2 else g)
    noded = O.union_all(lines)
    polys = noded.Polygonize() if noded is not None else None
    src_polys = [(g, vals) for t in tables if O.dim_of_type(t["geom_type"]) == 2 for _f, g, vals in t["rows"] if g]
    fields = []
    if str(attributes).upper() == "ATTRIBUTES" and src_polys:
        fields = [D._clone_defn(f) for f in next(t for t in tables if O.dim_of_type(t["geom_type"]) == 2)["fields"]]
    w = D.Writer(out_feature_class, ogr.wkbPolygon, srs, fields)
    for i in range(polys.GetGeometryCount() if polys is not None else 0):
        p = polys.GetGeometryRef(i).Clone()
        vals = [None] * len(fields)
        if fields:
            inside = p.PointOnSurface()
            for g, v in src_polys:
                if g.Intersects(inside):
                    vals = v[:len(fields)]
                    break
        w.add(vals, p)
    return w.close()


@tool("management.MinimumBoundingGeometry")
def MinimumBoundingGeometry(in_features, out_feature_class, geometry_type="RECTANGLE_BY_AREA",  # noqa: N802
                            group_option="NONE", group_field=None, mbg_fields_option="NO_MBG_FIELDS"):
    t = D.read_all(in_features)
    names = [fd.GetName().lower() for fd in t["fields"]]
    go = str(group_option or "NONE").upper()
    keys = [names.index(n.lower()) for n in _names(group_field)] if go == "LIST" else []
    groups = collections.OrderedDict()
    for fid, g, vals in t["rows"]:
        if g is None:
            continue
        key = fid if go == "NONE" else tuple(vals[k] for k in keys)
        groups.setdefault(key, ([], vals, fid))[0].append(g)
    gtype = str(geometry_type or "RECTANGLE_BY_AREA").upper()
    if go == "NONE":
        fields = [D._clone_defn(f) for f in t["fields"]] + [ogr.FieldDefn("ORIG_FID", ogr.OFTInteger)]
    else:
        fields = [D._clone_defn(t["fields"][k]) for k in keys]
    w = D.Writer(out_feature_class, ogr.wkbPolygon, t["srs"], fields)
    for key, (geoms, vals, fid) in groups.items():
        coll = ogr.Geometry(ogr.wkbGeometryCollection)
        for g in geoms:
            coll.AddGeometry(g)
        if gtype == "ENVELOPE":
            x0, x1, y0, y1 = coll.GetEnvelope()
            out = ogr.CreateGeometryFromWkt(f"POLYGON(({x0} {y0},{x0} {y1},{x1} {y1},{x1} {y0},{x0} {y0}))")
        elif gtype == "CONVEX_HULL":
            out = coll.ConvexHull()
        elif gtype in ("RECTANGLE_BY_AREA", "RECTANGLE_BY_WIDTH", "CIRCLE"):
            from qgis.core import QgsGeometry
            q = QgsGeometry.fromWkt(coll.ExportToWkt())
            if gtype == "CIRCLE":
                res = q.minimalEnclosingCircle()
                q2 = res[0] if isinstance(res, tuple) else res
            else:
                res = q.orientedMinimumBoundingBox()
                q2 = res[0] if isinstance(res, tuple) else res
            out = ogr.CreateGeometryFromWkt(q2.asWkt())
        else:
            unsupported(f"management.MinimumBoundingGeometry geometry type {geometry_type}")
        row = (vals + [fid]) if go == "NONE" else list(key)
        w.add(row, out)
    return w.close()


@tool("management.DefineProjection")
def DefineProjection(in_dataset, coor_system):  # noqa: N802
    ref = D.parse(in_dataset)
    srs = G.osr_of(coor_system)
    if srs is None:
        fail(735, "Coordinate System: Value is required")
    if ref.kind == "raster":
        ds = gdal.Open(ref.container, gdal.GA_Update)
        ds.SetProjection(srs.ExportToWkt())
        ds = None
        return ref.path
    if ref.kind == "file" and ref.container.lower().endswith(".shp"):
        esri = srs.Clone()
        esri.MorphToESRI()
        with open(os.path.splitext(ref.container)[0] + ".prj", "w", encoding="ascii", errors="replace") as fh:
            fh.write(esri.ExportToWkt())
        return ref.path
    unsupported("management.DefineProjection", "Only shapefiles and rasters can be redefined.")


def _noop(name):
    @tool(f"management.{name}")
    def fn(in_table, *args, **kwargs):
        return in_table if isinstance(in_table, str) else str(in_table)
    fn.__name__ = name
    return fn


AddSpatialIndex = _noop("AddSpatialIndex")
AddIndex = _noop("AddIndex")
AddAttributeIndex = _noop("AddAttributeIndex")
RemoveIndex = _noop("RemoveIndex")
RemoveSpatialIndex = _noop("RemoveSpatialIndex")
Compact = _noop("Compact")
CompressFileGeodatabaseData = _noop("CompressFileGeodatabaseData")
ClearWorkspaceCache = _noop("ClearWorkspaceCache")


@tool("management.Sort")
def Sort(in_dataset, out_dataset, sort_field, spatial_sort_method="UR"):  # noqa: N802
    t = D.read_all(in_dataset)
    names = [fd.GetName().lower() for fd in t["fields"]]
    rows = list(t["rows"])
    if isinstance(sort_field, str):
        spec = [s.strip().split() for s in sort_field.split(";") if s.strip()]
    else:
        spec = [list(s) if isinstance(s, (list, tuple)) else [s] for s in sort_field]
    for item in reversed(spec):
        name = str(item[0]).lower()
        desc = len(item) > 1 and str(item[1]).upper().startswith("DESC")
        if name in ("shape", "shape@"):
            rows.sort(key=lambda r: (-(r[1].Centroid().GetY()) if r[1] else 0, r[1].Centroid().GetX() if r[1] else 0))
            continue
        if name not in names:
            fail(728, f"Field {item[0]} does not exist within table")
        i = names.index(name)
        rows.sort(key=lambda r: (r[2][i] is None, r[2][i]), reverse=desc)
    w = D.Writer(out_dataset, t["geom_type"], t["srs"], t["fields"])
    for _fid, g, vals in rows:
        w.add(vals, g)
    return w.close()


@tool("management.DeleteIdentical")
def DeleteIdentical(in_dataset, fields, xy_tolerance=None, z_tolerance=None):  # noqa: N802
    src = D.Source(in_dataset)
    info = src.info()
    names = [fd.GetName().lower() for fd in info["fields"]]
    plan = []
    for n in _names(fields):
        if n.lower() in ("shape", info["shape"].lower()):
            plan.append(None)
        elif n.lower() in names:
            plan.append(names.index(n.lower()))
    seen, dup = set(), []
    for f in src.features():
        key = []
        for p in plan:
            if p is None:
                g = f.GetGeometryRef()
                key.append(g.ExportToWkb() if g is not None else None)
            else:
                key.append(D.get_value(f, p))
        key = tuple(key)
        if key in seen:
            dup.append(f.GetFID())
        else:
            seen.add(key)
    ds, lyr = D.open_layer(src.ref, update=True)
    for fid in dup:
        lyr.DeleteFeature(fid)
    lyr.SyncToDisk()
    return src.ref.path


# ---------------------------------------------------------------- rasters

def _raster_in(value):
    from .sa import raster_path
    return raster_path(value)


def _bounds(rectangle):
    if rectangle in (None, "", "#", "DEFAULT"):
        return None
    if isinstance(rectangle, G.Extent):
        return rectangle.XMin, rectangle.YMin, rectangle.XMax, rectangle.YMax
    vals = [float(v) for v in str(rectangle).replace(",", " ").split()]
    return tuple(vals[:4]) if len(vals) >= 4 else None


def _out_raster(out):
    ref = D.check_output(out, "raster")
    D.create_container(ref)
    return ref.container


def _driver_for(path):
    ext = os.path.splitext(path)[1].lower()
    return {".tif": "GTiff", ".tiff": "GTiff", ".img": "HFA", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
            ".asc": "AAIGrid", ".vrt": "VRT", ".bil": "EHdr", ".flt": "EHdr"}.get(ext, "GTiff")


@tool("management.Clip")
def Clip(in_raster, rectangle=None, out_raster=None, in_template_dataset=None, nodata_value=None,  # noqa: N802
         clipping_geometry="NONE", maintain_clipping_extent="NO_MAINTAIN_EXTENT"):
    src = _raster_in(in_raster)
    out = _out_raster(out_raster)
    kw = {"format": _driver_for(out)}
    if nodata_value not in (None, "", "#"):
        kw["dstNodata"] = float(nodata_value)
    use_geom = str(clipping_geometry).upper() in ("CLIPPINGGEOMETRY", "TRUE") or clipping_geometry is True
    if in_template_dataset not in (None, "", "#"):
        tref = D.parse(in_template_dataset) if D.as_layer(in_template_dataset) is None else None
        if tref is not None and tref.kind == "raster":
            e = D.Describe(in_template_dataset).extent
            kw["outputBounds"] = (e.XMin, e.YMin, e.XMax, e.YMax)
        elif use_geom:
            path, lname = D.materialize(in_template_dataset)
            kw.update(cutlineDSName=path, cropToCutline=True)
            if lname:
                kw["cutlineLayer"] = lname
            if "dstNodata" not in kw:
                kw["dstNodata"] = _default_nodata(src)
        else:
            e = D.Describe(in_template_dataset).extent
            kw["outputBounds"] = (e.XMin, e.YMin, e.XMax, e.YMax)
    if "outputBounds" not in kw and "cutlineDSName" not in kw:
        b = _bounds(rectangle)
        if b is None:
            fail(735, "Rectangle: Value is required")
        kw["outputBounds"] = b
    res = gdal.Warp(out, src, **kw)
    if res is None:
        fail(210, f"Cannot create output {out}")
    res = None
    note_output(out)
    return out


def _default_nodata(path):
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    nd = b.GetNoDataValue()
    if nd is not None:
        return nd
    return 0 if b.DataType == gdal.GDT_Byte else -9999


_PIXEL = {"1_BIT": gdal.GDT_Byte, "2_BIT": gdal.GDT_Byte, "4_BIT": gdal.GDT_Byte, "8_BIT_UNSIGNED": gdal.GDT_Byte,
          "8_BIT_SIGNED": gdal.GDT_Int16, "16_BIT_UNSIGNED": gdal.GDT_UInt16, "16_BIT_SIGNED": gdal.GDT_Int16,
          "32_BIT_UNSIGNED": gdal.GDT_UInt32, "32_BIT_SIGNED": gdal.GDT_Int32, "32_BIT_FLOAT": gdal.GDT_Float32,
          "64_BIT": gdal.GDT_Float64}


@tool("management.CopyRaster")
def CopyRaster(in_raster, out_rasterdataset, config_keyword=None, background_value=None, nodata_value=None,  # noqa
               onebit_to_eightbit=None, colormap_to_RGB=None, pixel_type=None, scale_pixel_value=None,  # noqa: N803
               RGB_to_Colormap=None, format=None, transform=None, process_as_multidimensional=None,  # noqa: A002,N803
               build_multidimensional_transpose=None):
    src = _raster_in(in_raster)
    out = _out_raster(out_rasterdataset)
    kw = {"format": _driver_for(out)}
    if pixel_type and str(pixel_type).upper() in _PIXEL:
        kw["outputType"] = _PIXEL[str(pixel_type).upper()]
    if nodata_value not in (None, "", "#"):
        kw["noData"] = float(nodata_value)
    gdal.Translate(out, src, **kw)
    note_output(out)
    return out


_RESAMPLE = {"NEAREST": "near", "BILINEAR": "bilinear", "CUBIC": "cubic", "MAJORITY": "mode"}


def _cell(cell_size):
    if cell_size in (None, "", "#"):
        return None
    vals = [float(v) for v in str(cell_size).replace(",", " ").split()]
    return (vals[0], vals[-1]) if vals else None


def _project_raster(in_raster, out_raster, out_coor_system, resampling_type="NEAREST", cell_size=None):
    src = _raster_in(in_raster)
    out = _out_raster(out_raster)
    srs = G.osr_of(out_coor_system)
    kw = {"format": _driver_for(out), "dstSRS": srs.ExportToWkt(),
          "resampleAlg": _RESAMPLE.get(str(resampling_type or "NEAREST").upper(), "near")}
    c = _cell(cell_size)
    if c:
        kw.update(xRes=c[0], yRes=c[1])
    gdal.Warp(out, src, **kw)
    note_output(out)
    return out


@tool("management.ProjectRaster")
def ProjectRaster(in_raster, out_raster, out_coor_system, resampling_type="NEAREST", cell_size=None,  # noqa: N802
                  geographic_transform=None, Registration_Point=None, in_coor_system=None,  # noqa: N803
                  vertical=None):
    return _project_raster(in_raster, out_raster, out_coor_system, resampling_type, cell_size)


@tool("management.Resample")
def Resample(in_raster, out_raster, cell_size=None, resampling_type="NEAREST"):  # noqa: N802
    src = _raster_in(in_raster)
    out = _out_raster(out_raster)
    c = _cell(cell_size)
    kw = {"format": _driver_for(out), "resampleAlg": _RESAMPLE.get(str(resampling_type or "NEAREST").upper(),
                                                                     "near")}
    if c:
        kw.update(xRes=c[0], yRes=c[1])
    gdal.Warp(out, src, **kw)
    note_output(out)
    return out


@tool("management.MosaicToNewRaster")
def MosaicToNewRaster(input_rasters, output_location, raster_dataset_name_with_extension,  # noqa: N802
                      coordinate_system_for_the_raster=None, pixel_type="8_BIT_UNSIGNED", cellsize=None,
                      number_of_bands=1, mosaic_method="LAST", mosaic_colormap_mode="FIRST"):
    srcs = [_raster_in(r) for r in _inputs(input_rasters)]
    out = _out_raster(os.path.join(D.full_path(output_location), str(raster_dataset_name_with_extension)))
    kw = {"format": _driver_for(out)}
    if pixel_type and str(pixel_type).upper() in _PIXEL:
        kw["outputType"] = _PIXEL[str(pixel_type).upper()]
    srs = G.osr_of(coordinate_system_for_the_raster)
    if srs is not None:
        kw["dstSRS"] = srs.ExportToWkt()
    c = _cell(cellsize)
    if c:
        kw.update(xRes=c[0], yRes=c[1])
    if str(mosaic_method).upper() == "FIRST":
        srcs = list(reversed(srcs))
    gdal.Warp(out, srcs, **kw)
    note_output(out)
    return out


@tool("management.GetRasterProperties")
def GetRasterProperties(in_raster, property_type="MINIMUM", band_index=None):  # noqa: N802
    src = _raster_in(in_raster)
    ds = gdal.Open(src)
    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)
    p = str(property_type).upper()
    if p in ("MINIMUM", "MAXIMUM", "MEAN", "STD"):
        mn, mx, mean, std = band.ComputeStatistics(False)
        return str({"MINIMUM": mn, "MAXIMUM": mx, "MEAN": mean, "STD": std}[p])
    w, h = ds.RasterXSize, ds.RasterYSize
    vals = {"CELLSIZEX": abs(gt[1]), "CELLSIZEY": abs(gt[5]), "COLUMNCOUNT": w, "ROWCOUNT": h,
            "BANDCOUNT": ds.RasterCount, "LEFT": gt[0], "TOP": gt[3], "RIGHT": gt[0] + gt[1] * w,
            "BOTTOM": gt[3] + gt[5] * h, "VALUETYPE": band.DataType, "ANYNODATA": int(band.GetNoDataValue() is not None)}
    if p not in vals:
        unsupported(f"management.GetRasterProperties property {property_type}")
    return str(vals[p])


@tool("management.CalculateStatistics")
def CalculateStatistics(in_raster_dataset, *args, **kwargs):  # noqa: N802
    ds = gdal.Open(_raster_in(in_raster_dataset))
    for i in range(1, ds.RasterCount + 1):
        ds.GetRasterBand(i).ComputeStatistics(False)
    ds = None
    return str(in_raster_dataset)


@tool("management.BuildPyramids")
def BuildPyramids(in_raster_dataset, *args, **kwargs):  # noqa: N802
    ds = gdal.Open(_raster_in(in_raster_dataset))
    ds.BuildOverviews("NEAREST", [2, 4, 8, 16])
    ds = None
    return str(in_raster_dataset)


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return missing_callable(f"management.{name}")


del osr
