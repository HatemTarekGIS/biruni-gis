"""GeoPackages in memory (GDAL /vsimem/): one small GeoPackage per layer, opened by the map from memory,
so the decrypted data is never written to a disk. Also: taking protected fields out of a layer and putting
them back, and the differences between two versions of a layer (a user's change set)."""
import uuid

ROOT = "/vsimem/biruni_secure"
SYSTEM_TABLES = {"layer_styles", "gpkg_contents", "gpkg_spatial_ref_sys", "gpkg_geometry_columns",
                 "gpkg_extensions", "gpkg_metadata", "gpkg_metadata_reference", "gpkg_data_columns",
                 "gpkg_data_column_constraints", "gpkg_tile_matrix", "gpkg_tile_matrix_set", "sqlite_sequence",
                 "qgis_projects", "gpkg_ogr_contents", "rtree"}
_held = [0, None]


def _gdal():
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    return gdal, ogr


# ---- SQLite must keep its temporary files in memory too ---------------------------------------------------
def hold_memory_temp():
    """While a locked file is open, every SQLite connection GDAL opens keeps its temporary data (sorts,
    VACUUM, statement journals) in memory, not in the Windows temporary folder."""
    gdal, _ = _gdal()
    if _held[0] == 0:
        _held[1] = gdal.GetConfigOption("OGR_SQLITE_PRAGMA")
        extra = "temp_store=MEMORY"
        gdal.SetConfigOption("OGR_SQLITE_PRAGMA", f"{_held[1]},{extra}" if _held[1] else extra)
    _held[0] += 1


def release_memory_temp():
    gdal, _ = _gdal()
    if _held[0] <= 0:
        return
    _held[0] -= 1
    if _held[0] == 0:
        gdal.SetConfigOption("OGR_SQLITE_PRAGMA", _held[1])


# ---- memory files --------------------------------------------------------------------------------------------
def new_path(folder, name):
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:40] or "layer"
    return f"{ROOT}/{folder}/{safe}_{uuid.uuid4().hex[:6]}.gpkg"


def mem_write(path, data):
    gdal, _ = _gdal()
    gdal.FileFromMemBuffer(path, bytes(data))


def mem_read(path):
    gdal, _ = _gdal()
    data = gdal.VSIGetMemFileBuffer_unsafe(path)
    return bytes(data) if data is not None else b""


def mem_wipe(path):
    """Overwrites a memory file with zeros, then frees it (and its -journal / -wal neighbours)."""
    gdal, _ = _gdal()
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        st = gdal.VSIStatL(p)
        if st is None:
            continue
        try:
            f = gdal.VSIFOpenL(p, "r+b")
            if f is not None:
                left = st.size
                zero = b"\0" * (1 << 20)
                while left > 0:
                    n = min(left, len(zero))
                    gdal.VSIFWriteL(zero[:n], 1, n, f)
                    left -= n
                gdal.VSIFCloseL(f)
        except Exception:  # noqa: BLE001 - freeing still happens
            pass
        gdal.Unlink(p)


def wipe_folder(folder):
    gdal, _ = _gdal()
    base = f"{ROOT}/{folder}"
    for name in gdal.ReadDir(base) or []:
        mem_wipe(f"{base}/{name}")


# ---- reading a GeoPackage -------------------------------------------------------------------------------------
def list_layers(path):
    """[(name, geometry type name, feature count, [field names])] of the data layers of a GeoPackage."""
    _, ogr = _gdal()
    ds = ogr.Open(path)
    out = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        name = lyr.GetName()
        if name.lower() in SYSTEM_TABLES:
            continue
        defn = lyr.GetLayerDefn()
        fields = [defn.GetFieldDefn(j).GetName() for j in range(defn.GetFieldCount())]
        out.append((name, ogr.GeometryTypeToName(lyr.GetGeomType()), lyr.GetFeatureCount(), fields))
    ds = None
    return out


def extract_layer(src, name):
    """One layer of a GeoPackage (with its saved styles) as the bytes of a GeoPackage of its own."""
    gdal, ogr = _gdal()
    tmp = new_path("work", name)
    try:
        gdal.VectorTranslate(tmp, src, options=gdal.VectorTranslateOptions(
            format="GPKG", layers=[name], preserveFID=True, layerName=name))
        ds = ogr.Open(src)
        has_styles = ds.GetLayerByName("layer_styles") is not None
        ds = None
        if has_styles:
            quoted = name.replace("'", "''")
            gdal.VectorTranslate(tmp, src, options=gdal.VectorTranslateOptions(
                format="GPKG", accessMode="update", layers=["layer_styles"], where=f"f_table_name = '{quoted}'"))
        return mem_read(tmp)
    finally:
        mem_wipe(tmp)


def layer_name_of(path):
    _, ogr = _gdal()
    ds = ogr.Open(path)
    for i in range(ds.GetLayerCount()):
        n = ds.GetLayerByIndex(i).GetName()
        if n.lower() not in SYSTEM_TABLES:
            return n
    return None


def field_names(path, layer):
    _, ogr = _gdal()
    ds = ogr.Open(path)
    defn = ds.GetLayerByName(layer).GetLayerDefn()
    return [defn.GetFieldDefn(j).GetName() for j in range(defn.GetFieldCount())]


def feature_count(path, layer):
    _, ogr = _gdal()
    ds = ogr.Open(path)
    return ds.GetLayerByName(layer).GetFeatureCount()


# ---- values to JSON and back ----------------------------------------------------------------------------------
def _get(f, i, t, ogr):
    if not f.IsFieldSetAndNotNull(i):
        return None
    if t in (ogr.OFTInteger, ogr.OFTInteger64):
        return f.GetFieldAsInteger64(i)
    if t == ogr.OFTReal:
        return f.GetFieldAsDouble(i)
    if t == ogr.OFTBinary:
        return {"$b": f.GetFieldAsBinary(i).hex()}
    if t in (ogr.OFTString, ogr.OFTDate, ogr.OFTTime, ogr.OFTDateTime):
        return f.GetFieldAsString(i)
    v = f.GetField(i)
    return list(v) if isinstance(v, (list, tuple)) else v


def _set(f, i, v):
    if v is None:
        f.SetFieldNull(i)
    elif isinstance(v, dict) and "$b" in v:
        f.SetFieldBinaryFromHexString(i, v["$b"])
    else:
        f.SetField(i, v)


def _geom(f):
    g = f.GetGeometryRef()
    return g.ExportToIsoWkb() if g is not None else None


def _rows(lyr, names, ogr):
    defn = lyr.GetLayerDefn()
    idx = [(n, defn.GetFieldIndex(n)) for n in names]
    types = {n: defn.GetFieldDefn(i).GetType() for n, i in idx if i >= 0}
    lyr.ResetReading()
    for f in lyr:
        vals = {n: (_get(f, i, types[n], ogr) if i >= 0 else None) for n, i in idx}
        yield f.GetFID(), _geom(f), vals


# ---- protected fields ------------------------------------------------------------------------------------------
def strip_fields(data, layer, names):
    """(the layer's GeoPackage without the fields 'names', {name: {"def": …, "values": {fid: value}}}).
    VACUUM afterwards so the removed values are not left in free pages of the file."""
    gdal, ogr = _gdal()
    tmp = new_path("work", layer)
    mem_write(tmp, data)
    try:
        ds = ogr.Open(tmp, 1)
        ds.ExecuteSQL("PRAGMA temp_store = MEMORY")
        lyr = ds.GetLayerByName(layer)
        defn = lyr.GetLayerDefn()
        out = {}
        present = [n for n in names if defn.GetFieldIndex(n) >= 0]
        for n in present:
            fd = defn.GetFieldDefn(defn.GetFieldIndex(n))
            out[n] = {"def": {"type": fd.GetType(), "subtype": fd.GetSubType(), "width": fd.GetWidth(),
                              "precision": fd.GetPrecision()}, "values": {}}
        if present:
            for fid, _, vals in _rows(lyr, present, ogr):
                for n in present:
                    if vals[n] is not None:
                        out[n]["values"][str(fid)] = vals[n]
            for n in present:
                lyr.DeleteField(lyr.GetLayerDefn().GetFieldIndex(n))
        ds.ExecuteSQL("VACUUM")
        ds = None
        return mem_read(tmp), out
    finally:
        mem_wipe(tmp)


def vacuum(data):
    """The same GeoPackage rewritten without free pages (deleted features leave their bytes there)."""
    gdal, ogr = _gdal()
    tmp = new_path("work", "v")
    mem_write(tmp, data)
    try:
        ds = ogr.Open(tmp, 1)
        ds.ExecuteSQL("PRAGMA temp_store = MEMORY")
        ds.ExecuteSQL("VACUUM")
        ds = None
        return mem_read(tmp)
    finally:
        mem_wipe(tmp)


def join_fields(path, layer, fields):
    """Puts protected fields back into a layer opened from memory: {name: {"def", "values"}}."""
    gdal, ogr = _gdal()
    if not fields:
        return
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayerByName(layer)
    for name, payload in fields.items():
        d = payload["def"]
        fd = ogr.FieldDefn(name, int(d["type"]))
        fd.SetSubType(int(d.get("subtype", 0)))
        fd.SetWidth(int(d.get("width", 0)))
        fd.SetPrecision(int(d.get("precision", 0)))
        if lyr.GetLayerDefn().GetFieldIndex(name) < 0:
            lyr.CreateField(fd)
    defn = lyr.GetLayerDefn()
    lyr.StartTransaction()
    for name, payload in fields.items():
        i = defn.GetFieldIndex(name)
        for fid, v in payload["values"].items():
            f = lyr.GetFeature(int(fid))
            if f is None:
                continue
            _set(f, i, v)
            lyr.SetFeature(f)
    lyr.CommitTransaction()
    ds = None


# ---- change sets ---------------------------------------------------------------------------------------------
def diff(base_path, cur_path, layer):
    """What changed from base to current: {"adds": [...], "edits": [...], "deletes": [...], "schema": {...}}.
    Both are read in fid order side by side, so even large layers take little memory."""
    _, ogr = _gdal()
    bds, cds = ogr.Open(base_path), ogr.Open(cur_path)
    bl, cl = bds.GetLayerByName(layer), cds.GetLayerByName(layer)
    bnames = [bl.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(bl.GetLayerDefn().GetFieldCount())]
    cnames = [cl.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(cl.GetLayerDefn().GetFieldCount())]
    names = [n for n in bnames if n in cnames]
    out = {"adds": [], "edits": [], "deletes": [], "schema": {}}
    added = [n for n in cnames if n not in bnames]
    removed = [n for n in bnames if n not in cnames]
    if added or removed:
        out["schema"] = {"added": added, "removed": removed}
    bi, ci = _rows(bl, names, ogr), _rows(cl, cnames, ogr)
    b, c = next(bi, None), next(ci, None)
    while b is not None or c is not None:
        if c is None or (b is not None and b[0] < c[0]):
            out["deletes"].append(b[0])
            b = next(bi, None)
        elif b is None or c[0] < b[0]:
            out["adds"].append({"fid": c[0], "geom": c[1].hex() if c[1] is not None else None,
                                "attrs": {n: c[2][n] for n in cnames if c[2][n] is not None}})
            c = next(ci, None)
        else:
            edit = {"fid": c[0]}
            if b[1] != c[1]:
                edit["geom"] = c[1].hex() if c[1] is not None else None
            attrs = {n: c[2][n] for n in names if c[2][n] != b[2][n]}
            if attrs:
                edit["attrs"] = attrs
            if len(edit) > 1:
                out["edits"].append(edit)
            b, c = next(bi, None), next(ci, None)
    bds = cds = None
    return out


def is_empty(changes):
    return not (changes.get("adds") or changes.get("edits") or changes.get("deletes") or changes.get("schema"))


def apply(path, layer, changes, fid_map=None, keep_fids=True):
    """Applies a change set to a layer in memory. fid_map ({user fid: real fid}) is filled for added features
    and used for later edits of them. Returns {"adds": n, "edits": n, "deletes": n, "missing": [fids]}."""
    _, ogr = _gdal()
    fid_map = {} if fid_map is None else fid_map
    ds = ogr.Open(path, 1)
    lyr = ds.GetLayerByName(layer)
    defn = lyr.GetLayerDefn()
    done = {"adds": 0, "edits": 0, "deletes": 0, "missing": []}

    def real(fid):
        return int(fid_map.get(str(fid), fid))

    def put(f, item):
        if "geom" in item:
            g = item["geom"]
            f.SetGeometry(ogr.CreateGeometryFromWkb(bytes.fromhex(g)) if g else None)
        for n, v in (item.get("attrs") or {}).items():
            i = defn.GetFieldIndex(n)
            if i >= 0:
                _set(f, i, v)

    lyr.StartTransaction()
    for item in changes.get("adds", []):
        f = ogr.Feature(defn)
        put(f, item)
        if keep_fids and lyr.GetFeature(int(item["fid"])) is None and str(item["fid"]) not in fid_map:
            f.SetFID(int(item["fid"]))
        lyr.CreateFeature(f)
        fid_map[str(item["fid"])] = f.GetFID()
        done["adds"] += 1
    for item in changes.get("edits", []):
        f = lyr.GetFeature(real(item["fid"]))
        if f is None:
            done["missing"].append(item["fid"])
            continue
        put(f, item)
        lyr.SetFeature(f)
        done["edits"] += 1
    for fid in changes.get("deletes", []):
        if lyr.GetFeature(real(fid)) is None:
            done["missing"].append(fid)
            continue
        lyr.DeleteFeature(real(fid))
        done["deletes"] += 1
    lyr.CommitTransaction()
    ds = None
    return done


def copy_mem(src, folder, name):
    dst = new_path(folder, name)
    mem_write(dst, mem_read(src))
    return dst


def exists(path):
    gdal, _ = _gdal()
    return gdal.VSIStatL(path) is not None


def is_vsimem(path):
    return str(path or "").replace("\\", "/").startswith(ROOT + "/")


def private_name():
    return uuid.uuid4().hex[:10]
