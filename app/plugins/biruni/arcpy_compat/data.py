"""Data access for the arcpy shim, all through GDAL/OGR: catalog paths (folders, shapefiles, file geodatabases with
feature datasets, GeoPackages, memory), feature layers with selections, fields, Describe, List* and Exists."""
import datetime
import fnmatch
import glob
import os
import re
import shutil
import weakref

from osgeo import gdal, ogr, osr

from . import geometry as G
from ._base import ExecuteError, env, fail, temp_path, unsupported

# ---------------------------------------------------------------- constants

GDB_EXT = (".gdb",)
GPKG_EXT = (".gpkg", ".sqlite")
EXCEL_EXT = (".xlsx", ".xls")
VECTOR_FILE_EXT = {".shp": "ESRI Shapefile", ".geojson": "GeoJSON", ".json": "GeoJSON", ".kml": "KML",
                   ".gml": "GML", ".gpx": "GPX", ".fgb": "FlatGeobuf", ".tab": "MapInfo File",
                   ".mif": "MapInfo File", ".dxf": "DXF", ".csv": "CSV", ".txt": "CSV", ".dbf": "ESRI Shapefile"}
TABLE_EXT = {".dbf", ".csv", ".txt"}
RASTER_EXT = {".tif", ".tiff", ".img", ".jpg", ".jpeg", ".png", ".asc", ".bil", ".dem", ".vrt", ".jp2", ".sid",
              ".ecw", ".bmp", ".gif", ".nc", ".hdf", ".dt0", ".dt1", ".dt2", ".flt", ".sdat", ".rst", ".bip",
              ".bsq", ".grd", ".hgt", ".xyz"}
SHP_SIDECARS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fbn", ".fbx", ".ain", ".aih",
                ".atx", ".ixs", ".mxs", ".shp.xml", ".qmd", ".fix")

GEOM_TYPES = {"POINT": ogr.wkbPoint, "MULTIPOINT": ogr.wkbMultiPoint, "POLYLINE": ogr.wkbLineString,
              "LINE": ogr.wkbLineString, "POLYGON": ogr.wkbPolygon, "MULTIPATCH": None}

_MEM = {"ds": None}
_PENDING_FD = {}  # gdb path (lower) -> {dataset name: osr srs}; feature datasets that have no feature class yet


def _mem_ds():
    if _MEM["ds"] is None:
        drv = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
        _MEM["ds"] = drv.CreateDataSource("biruni_memory")
    return _MEM["ds"]


# ---------------------------------------------------------------- catalog paths

class Ref:
    """Where a dataset lives. kind: gdb, gpkg, file, mem, folder, raster, excel, gdbws, gpkgws, fd."""

    def __init__(self, kind, container, name=None, dataset=None):
        self.kind = kind
        self.container = container
        self.name = name
        self.dataset = dataset

    @property
    def path(self):
        if self.kind == "mem":
            return "memory\\" + (self.name or "")
        if self.kind in ("gdb", "gpkg", "excel"):
            parts = [self.container] + ([self.dataset] if self.dataset else []) + ([self.name] if self.name else [])
            return os.sep.join(parts)
        if self.kind == "fd":
            return os.path.join(self.container, self.dataset)
        return self.container

    @property
    def base_name(self):
        if self.kind in ("gdb", "gpkg", "mem", "excel"):
            return self.name or ""
        if self.kind == "fd":
            return self.dataset
        return os.path.splitext(os.path.basename(self.container))[0]

    def __repr__(self):
        return f"<Ref {self.kind} {self.path}>"


def _is_mem(text):
    low = text.replace("/", "\\").lower()
    return low.startswith("memory\\") or low.startswith("in_memory\\")


def full_path(value):
    """Catalog path string, joined to env.workspace when relative."""
    p = os.fspath(value).strip().strip('"') if not isinstance(value, str) else value.strip().strip('"')
    if _is_mem(p):
        return p
    if not os.path.isabs(p) and not re.match(r"^[A-Za-z]:", p) and not p.startswith("\\\\"):
        ws = env.workspace
        p = os.path.join(ws, p) if ws else os.path.abspath(p)
    return os.path.normpath(p)


def gdb_layout(gdb):
    """{'': [root feature classes and tables], dataset: [feature classes]} of a file geodatabase."""
    out = {"": []}
    ds = _open_raw(gdb, False, ["OpenFileGDB"])
    if ds is not None:
        try:
            root = ds.GetRootGroup()
        except Exception:
            root = None
        if root is not None:
            out[""] = list(root.GetVectorLayerNames() or [])
            for g in root.GetGroupNames() or []:
                try:
                    out[g] = list(root.OpenGroup(g).GetVectorLayerNames() or [])
                except Exception:
                    out[g] = []
        else:
            out[""] = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
        ds = None
    for name in _PENDING_FD.get(os.path.normcase(gdb), {}):
        if not any(k.lower() == name.lower() for k in out):
            out[name] = []
    return out


def _ci(name, names):
    for n in names:
        if n.lower() == str(name).lower():
            return n
    return None


def parse(value, want="vector"):
    """A catalog path (or layer) -> Ref. want: 'vector', 'table' or 'raster' (picks the extension for new files)."""
    from . import mp
    if isinstance(value, FeatureLayer):
        return value.ref
    if isinstance(value, mp.Layer):
        return parse(value.dataSource, want)
    if isinstance(value, Ref):
        return value
    p = full_path(value)
    if _is_mem(p):
        name = re.split(r"[\\/]", p, maxsplit=1)[1]
        return Ref("mem", "memory", name)
    parts = re.split(r"[\\/]", p)
    for i, part in enumerate(parts):
        low = part.lower()
        if low.endswith(GDB_EXT + GPKG_EXT + EXCEL_EXT) and i > 0:
            container = os.sep.join(parts[:i + 1])
            rest = [x for x in parts[i + 1:] if x]
            if low.endswith(GDB_EXT):
                if not rest:
                    return Ref("gdbws", container)
                layout = gdb_layout(container) if os.path.isdir(container) else {"": []}
                if len(rest) == 1:
                    fd = _ci(rest[0], [k for k in layout if k])
                    if fd:
                        return Ref("fd", container, None, fd)
                    found = None
                    for k, names in layout.items():
                        hit = _ci(rest[0], names)
                        if hit:
                            found = (k, hit)
                            break
                    if found:
                        return Ref("gdb", container, found[1], found[0] or None)
                    return Ref("gdb", container, rest[0])
                fd = _ci(rest[0], [k for k in layout if k]) or rest[0]
                hit = _ci(rest[1], layout.get(fd, [])) or rest[1]
                return Ref("gdb", container, hit, fd)
            if low.endswith(GPKG_EXT):
                if not rest:
                    return Ref("gpkgws", container)
                return Ref("gpkg", container, rest[-1])
            if not rest:
                return Ref("file", container) if want != "excel" else Ref("excel", container)
            return Ref("excel", container, rest[-1].rstrip("$"))
    ext = os.path.splitext(p)[1].lower()
    if os.path.isdir(p) and ext not in RASTER_EXT:
        return Ref("folder", p)
    if ext in RASTER_EXT:
        return Ref("raster", p)
    if ext in VECTOR_FILE_EXT:
        return Ref("file", p)
    # no extension: an existing dataset first, else what the tool wants to write
    for cand in (".shp", ".dbf", ".tif", ".csv"):
        if os.path.exists(p + cand):
            return Ref("raster" if cand == ".tif" else "file", p + cand)
    if want == "raster":
        return Ref("raster", p + ".tif")
    if want == "table":
        return Ref("file", p + ".dbf")
    if want == "folder":
        return Ref("folder", p)
    return Ref("file", p + ".shp")


# ---------------------------------------------------------------- opening

_LIVE = weakref.WeakSet()  # open edit cursors to flush before any read


def register_live(obj):
    _LIVE.add(obj)


def flush_live():
    for c in list(_LIVE):
        try:
            c._flush()
        except Exception:
            pass


def _open_raw(path, update, drivers=None):
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else gdal.OF_READONLY)
    try:
        if drivers:
            return gdal.OpenEx(path, flags, allowed_drivers=drivers)
        return gdal.OpenEx(path, flags)
    except RuntimeError:
        return None


def open_ds(ref, update=False):
    flush_live()
    if ref.kind == "mem":
        return _mem_ds()
    if ref.kind in ("gdb", "gdbws", "fd"):
        ds = _open_raw(ref.container, update, ["OpenFileGDB"])
        if ds is None and not update and os.path.isdir(ref.container):
            ds = _open_raw(ref.container, True, ["OpenFileGDB"])
        return ds
    if ref.kind in ("gpkg", "gpkgws"):
        return _open_raw(ref.container, update, ["GPKG", "SQLite"])
    if ref.kind == "excel":
        return _open_raw(ref.container, update, ["XLSX", "XLS"])
    return _open_raw(ref.container, update)


def open_layer(ref, update=False):
    ds = open_ds(ref, update)
    if ds is None:
        fail(732, f"Input Dataset: Dataset {ref.path} does not exist or is not supported")
    if ref.name:
        lyr = ds.GetLayerByName(ref.name)
        if lyr is None:
            names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
            hit = _ci(ref.name, names) or _ci(ref.name.rstrip("$"), names)
            lyr = ds.GetLayerByName(hit) if hit else None
    else:
        lyr = ds.GetLayer(0) if ds.GetLayerCount() else None
    if lyr is None:
        fail(732, f"Input Dataset: Dataset {ref.path} does not exist or is not supported")
    return ds, lyr


def exists_ref(ref):
    try:
        if ref.kind == "mem":
            return _mem_ds().GetLayerByName(ref.name) is not None
        if ref.kind == "folder":
            return os.path.isdir(ref.container)
        if ref.kind in ("gdbws",):
            return os.path.isdir(ref.container)
        if ref.kind == "gpkgws":
            return os.path.isfile(ref.container)
        if ref.kind == "fd":
            return os.path.isdir(ref.container) and any(
                k.lower() == ref.dataset.lower() for k in gdb_layout(ref.container) if k)
        if ref.kind == "gdb":
            if not os.path.isdir(ref.container):
                return False
            layout = gdb_layout(ref.container)
            if ref.dataset:
                return _ci(ref.name, layout.get(_ci(ref.dataset, layout) or ref.dataset, [])) is not None
            return any(_ci(ref.name, names) for names in layout.values())
        if ref.kind in ("gpkg", "excel"):
            if not os.path.isfile(ref.container):
                return False
            ds = open_ds(ref)
            if ds is None:
                return False
            names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
            return _ci(ref.name, names) is not None
        if ref.kind == "raster":
            return os.path.exists(ref.container)
        return os.path.isfile(ref.container)
    except ExecuteError:
        return False


def Exists(dataset):  # noqa: N802
    if dataset is None or dataset == "":
        return False
    if isinstance(dataset, FeatureLayer):
        return True
    if isinstance(dataset, str) and dataset in LAYERS:
        return True
    try:
        return exists_ref(parse(dataset))
    except Exception:
        return False


def delete_ref(ref):
    if ref.kind == "mem":
        ds = _mem_ds()
        for i in range(ds.GetLayerCount()):
            if ds.GetLayer(i).GetName().lower() == ref.name.lower():
                ds.DeleteLayer(i)
                return
        return
    if ref.kind in ("folder", "gdbws"):
        _PENDING_FD.pop(os.path.normcase(ref.container), None)
        shutil.rmtree(ref.container, ignore_errors=False)
        return
    if ref.kind == "gpkgws":
        os.remove(ref.container)
        return
    if ref.kind == "fd":
        layout = gdb_layout(ref.container)
        names = layout.get(_ci(ref.dataset, layout) or ref.dataset, [])
        for n in names:
            delete_ref(Ref("gdb", ref.container, n, ref.dataset))
        _PENDING_FD.get(os.path.normcase(ref.container), {}).pop(ref.dataset, None)
        return
    if ref.kind in ("gdb", "gpkg", "excel"):
        ds = open_ds(ref, update=True)
        if ds is None:
            return
        for i in range(ds.GetLayerCount()):
            if ds.GetLayer(i).GetName().lower() == ref.name.lower():
                ds.DeleteLayer(i)
                break
        ds = None
        return
    if ref.kind == "raster":
        try:
            drv = gdal.IdentifyDriver(ref.container)
            if drv is not None:
                drv.Delete(ref.container)
                return
        except RuntimeError:
            pass
        if os.path.isfile(ref.container):
            os.remove(ref.container)
        return
    base, ext = os.path.splitext(ref.container)
    if ext.lower() in (".shp", ".dbf"):
        for side in SHP_SIDECARS:
            for cand in (base + side, base + side.upper()):
                if os.path.isfile(cand):
                    os.remove(cand)
        return
    if os.path.isfile(ref.container):
        os.remove(ref.container)


def check_output(out, want="vector"):
    """Ref for a tool output: refuses an existing one unless overwriteOutput, else deletes it first."""
    ref = parse(out, want)
    if isinstance(out, str) and out in LAYERS:
        LAYERS.pop(out)
    if exists_ref(ref):
        if not env.overwriteOutput:
            fail(725, f"Output Dataset: Dataset {ref.path} already exists.")
        delete_ref(ref)
    return ref


def create_container(ref):
    """Make the gdb / gpkg / folder that will hold a new dataset."""
    if ref.kind in ("gdb", "fd") and not os.path.isdir(ref.container):
        os.makedirs(os.path.dirname(ref.container) or ".", exist_ok=True)
        ogr.GetDriverByName("OpenFileGDB").CreateDataSource(ref.container)
    elif ref.kind in ("file", "raster", "excel"):
        os.makedirs(os.path.dirname(ref.container) or ".", exist_ok=True)


def create_layer(ref, geom_type, srs, field_defns=(), alias=None):
    """Create an empty dataset at ref (already checked by check_output). Returns (ds, layer)."""
    create_container(ref)
    opts = []
    if ref.kind == "mem":
        ds = _mem_ds()
    elif ref.kind == "gdb":
        ds = _open_raw(ref.container, True, ["OpenFileGDB"])
        if ds is None:
            ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(ref.container)
        if ref.dataset:
            opts.append(f"FEATURE_DATASET={ref.dataset}")
            pend = _PENDING_FD.get(os.path.normcase(ref.container), {})
            fd_srs = pend.get(_ci(ref.dataset, pend) or ref.dataset)
            if fd_srs is not None and geom_type != ogr.wkbNone:
                srs = fd_srs
        if alias:
            opts.append(f"LAYER_ALIAS={alias}")
    elif ref.kind == "gpkg":
        ds = _open_raw(ref.container, True, ["GPKG"]) if os.path.isfile(ref.container) else None
        if ds is None:
            os.makedirs(os.path.dirname(ref.container) or ".", exist_ok=True)
            ds = ogr.GetDriverByName("GPKG").CreateDataSource(ref.container)
    elif ref.kind == "excel":
        if os.path.isfile(ref.container):
            ds = _open_raw(ref.container, True, ["XLSX"])
        else:
            ds = ogr.GetDriverByName("XLSX").CreateDataSource(ref.container)
        geom_type = ogr.wkbNone
    elif ref.kind == "file":
        ext = os.path.splitext(ref.container)[1].lower()
        drv_name = VECTOR_FILE_EXT.get(ext, "ESRI Shapefile")
        drv = ogr.GetDriverByName(drv_name)
        if ext == ".dbf":
            geom_type = ogr.wkbNone
        if drv_name == "ESRI Shapefile":
            opts.append("ENCODING=UTF-8")
        if drv_name == "CSV":
            geom_type = ogr.wkbNone
        ds = drv.CreateDataSource(ref.container)
    else:
        fail(210, f"Cannot create output {ref.path}")
    if ds is None:
        fail(210, f"Cannot create output {ref.path}")
    name = ref.name if ref.kind not in ("file",) else os.path.splitext(os.path.basename(ref.container))[0]
    if ref.kind == "mem":
        for i in range(ds.GetLayerCount()):
            if ds.GetLayer(i).GetName().lower() == name.lower():
                ds.DeleteLayer(i)
                break
    srs = srs.Clone() if srs is not None else None
    if srs is not None:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    lyr = ds.CreateLayer(name, srs if geom_type != ogr.wkbNone else None, geom_type, opts)
    if lyr is None:
        fail(210, f"Cannot create output {ref.path}")
    for fd in field_defns:
        lyr.CreateField(fd)
    if ref.kind == "gdb" and ref.dataset:
        _PENDING_FD.get(os.path.normcase(ref.container), {}).pop(ref.dataset, None)
    return ds, lyr


# ---------------------------------------------------------------- feature layers and table views

LAYERS = {}  # name -> FeatureLayer


class FeatureLayer:
    """What MakeFeatureLayer / MakeTableView make: a dataset + definition query + selection."""

    def __init__(self, name, ref, where=None, is_table=False):
        self.name = name
        self.ref = ref
        self.where = where or None
        self.selection = None  # None = no selection; else a set of FIDs
        self.is_table = is_table

    @property
    def dataSource(self):  # noqa: N802
        return self.ref.path

    @property
    def definitionQuery(self):  # noqa: N802
        return self.where or ""

    @definitionQuery.setter
    def definitionQuery(self, value):  # noqa: N802
        self.where = value or None

    @property
    def longName(self):  # noqa: N802
        return self.name

    @property
    def isFeatureLayer(self):  # noqa: N802
        return not self.is_table

    def getSelectionSet(self):  # noqa: N802
        return set(self.selection) if self.selection is not None else None

    def setSelectionSet(self, oidList=None, method="NEW"):  # noqa: N802,N803
        ids = set(int(i) for i in (oidList or []))
        m = (method or "NEW").upper()
        cur = self.selection or set()
        self.selection = (ids if m == "NEW" else cur | ids if m == "UNION" else cur - ids
                          if m == "REMOVE" else cur & ids if m == "INTERSECT" else ids) or None

    def __str__(self):
        return self.name

    def __fspath__(self):
        return self.name

    def __repr__(self):
        return f"<FeatureLayer '{self.name}'>"


def as_layer(value):
    """A FeatureLayer when value names one (or a Result holding one), else None."""
    if isinstance(value, FeatureLayer):
        return value
    if hasattr(value, "getOutput") and not isinstance(value, str):
        return as_layer(value.getOutput(0))
    if isinstance(value, str) and value in LAYERS:
        return LAYERS[value]
    return None


def _unwrap(value):
    if hasattr(value, "getOutput") and not isinstance(value, str):
        return _unwrap(value.getOutput(0))
    return value


def oid_name(lyr, ref):
    fid = lyr.GetFIDColumn() if lyr is not None else ""
    if fid:
        return fid
    if ref.kind == "file":
        ext = os.path.splitext(ref.container)[1].lower()
        return "FID" if ext in (".shp", ".dbf") else "ObjectID"
    return "OBJECTID"


def shape_name(lyr, ref):
    if lyr is None or lyr.GetGeomType() == ogr.wkbNone:
        return ""
    g = lyr.GetGeometryColumn()
    if ref.kind == "gdb":
        return "Shape" if not g or g.upper() == "SHAPE" else g
    return g or "Shape"


def sql_where(where, lyr, ref):
    """arcpy SQL -> OGR SQL: [field] delimiters, the OID name for drivers that only know FID."""
    if not where:
        return None
    w = str(where)
    w = re.sub(r"\[([^\]]+)\]", r'"\1"', w)
    drv = None
    try:
        drv = lyr.GetDataset().GetDriver().ShortName if hasattr(lyr, "GetDataset") else None
    except Exception:
        drv = None
    if not lyr.GetFIDColumn():
        oid = oid_name(lyr, ref)
        if oid.upper() != "FID":
            w = re.sub(r'(?<![\w"])"?' + re.escape(oid) + r'"?(?![\w"])', "FID", w, flags=re.I)
    del drv
    return w


class Source:
    """A readable input: a path, a FeatureLayer, an mp.Layer or a Result."""

    def __init__(self, value):
        value = _unwrap(value)
        from . import mp
        self.layer = as_layer(value)
        if self.layer is None and isinstance(value, mp.Layer):
            self.layer = value._as_feature_layer()
        if self.layer is not None:
            self.ref = self.layer.ref
        else:
            self.ref = parse(value)
            if not exists_ref(self.ref):
                fail(732, f"Input Dataset: Dataset {value} does not exist or is not supported")
        self.name = self.layer.name if self.layer else self.ref.base_name

    def open(self, where=None, update=False):
        ds, lyr = open_layer(self.ref, update)
        clauses = [c for c in ((self.layer.where if self.layer else None), where) if c]
        if clauses:
            sql = " AND ".join(f"({sql_where(c, lyr, self.ref)})" for c in clauses)
            if lyr.SetAttributeFilter(sql) != 0:
                fail(358, f"The SQL expression is invalid: {sql}")
        return ds, lyr

    def features(self, where=None, update=False):
        ds, lyr = self.open(where, update)
        sel = self.layer.selection if self.layer else None
        lyr.ResetReading()
        for f in lyr:
            if sel is not None and f.GetFID() not in sel:
                continue
            yield f
        ds = None  # noqa: F841 - keep the dataset alive while iterating

    def fids(self, where=None):
        return [f.GetFID() for f in self.features(where)]

    def srs(self):
        ds, lyr = open_layer(self.ref)
        s = lyr.GetSpatialRef()
        return s.Clone() if s is not None else None

    def info(self):
        ds, lyr = open_layer(self.ref)
        defn = lyr.GetLayerDefn()
        fields = [_clone_defn(defn.GetFieldDefn(i)) for i in range(defn.GetFieldCount())]
        srs = lyr.GetSpatialRef()
        return {"fields": fields, "geom_type": lyr.GetGeomType(), "srs": srs.Clone() if srs else None,
                "oid": oid_name(lyr, self.ref), "shape": shape_name(lyr, self.ref)}


def _clone_defn(fd):
    n = ogr.FieldDefn(fd.GetName(), fd.GetType())
    n.SetSubType(fd.GetSubType())
    n.SetWidth(fd.GetWidth())
    n.SetPrecision(fd.GetPrecision())
    try:
        if fd.GetAlternativeName():
            n.SetAlternativeName(fd.GetAlternativeName())
    except Exception:
        pass
    return n


# ---------------------------------------------------------------- values

def get_value(f, i, t=None):
    if not f.IsFieldSetAndNotNull(i):
        return None
    t = f.GetFieldType(i) if t is None else t
    if t == ogr.OFTInteger:
        return f.GetFieldAsInteger(i)
    if t == ogr.OFTInteger64:
        return f.GetFieldAsInteger64(i)
    if t == ogr.OFTReal:
        return f.GetFieldAsDouble(i)
    if t == ogr.OFTString:
        return f.GetFieldAsString(i)
    if t in (ogr.OFTDate, ogr.OFTDateTime):
        y, mo, d, h, mi, s, _tz = f.GetFieldAsDateTime(i)
        try:
            return datetime.datetime(y, mo, d, h, mi, int(s), int(round((s % 1) * 1e6)) % 1000000)
        except ValueError:
            return None
    if t == ogr.OFTTime:
        _y, _mo, _d, h, mi, s, _tz = f.GetFieldAsDateTime(i)
        return datetime.time(h, mi, int(s))
    if t == ogr.OFTBinary:
        return memoryview(f.GetFieldAsBinary(i))
    return f.GetField(i)


def set_value(f, i, v):
    if v is None:
        f.SetFieldNull(i)
        return
    t = f.GetFieldType(i)
    if isinstance(v, datetime.datetime):
        f.SetField(i, v.year, v.month, v.day, v.hour, v.minute, v.second + v.microsecond / 1e6, 0)
        return
    if isinstance(v, datetime.date):
        f.SetField(i, v.year, v.month, v.day, 0, 0, 0, 0)
        return
    if isinstance(v, datetime.time):
        f.SetField(i, 1900, 1, 1, v.hour, v.minute, v.second, 0)
        return
    if isinstance(v, (bytes, bytearray, memoryview)):
        f.SetFieldBinaryFromHexString(i, bytes(v).hex())
        return
    if isinstance(v, bool):
        v = int(v)
    if t in (ogr.OFTInteger, ogr.OFTInteger64):
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                f.SetFieldNull(i)
                return
            v = float(v)
        v = int(round(float(v)))
        if t == ogr.OFTInteger64:
            f.SetFieldInteger64(i, v)
        else:
            f.SetField(i, v)
        return
    if t == ogr.OFTReal:
        if isinstance(v, str) and v.strip() == "":
            f.SetFieldNull(i)
            return
        f.SetField(i, float(v))
        return
    if t == ogr.OFTString:
        f.SetField(i, v if isinstance(v, str) else str(v))
        return
    f.SetField(i, v)


def field_index(defn, name):
    i = defn.GetFieldIndex(name)
    if i >= 0:
        return i
    low = str(name).lower()
    for j in range(defn.GetFieldCount()):
        if defn.GetFieldDefn(j).GetName().lower() == low:
            return j
    return -1


def geom_for_layer(g, lyr):
    """Make an OGR geometry fit a layer's geometry type (multi/single, Z)."""
    if g is None:
        return None
    lt = lyr.GetGeomType()
    if lt in (ogr.wkbUnknown, ogr.wkbNone):
        return g
    flat_l, flat_g = ogr.GT_Flatten(lt), ogr.GT_Flatten(g.GetGeometryType())
    if flat_l != flat_g:
        if flat_l == ogr.wkbMultiPolygon:
            g = ogr.ForceToMultiPolygon(g)
        elif flat_l == ogr.wkbMultiLineString:
            g = ogr.ForceToMultiLineString(g)
        elif flat_l == ogr.wkbMultiPoint:
            g = ogr.ForceToMultiPoint(g)
        elif flat_l == ogr.wkbPolygon and flat_g == ogr.wkbMultiPolygon and g.GetGeometryCount() == 1:
            g = g.GetGeometryRef(0).Clone()
        elif flat_l == ogr.wkbLineString and flat_g == ogr.wkbMultiLineString and g.GetGeometryCount() == 1:
            g = g.GetGeometryRef(0).Clone()
        elif flat_l == ogr.wkbPoint and flat_g == ogr.wkbMultiPoint and g.GetGeometryCount() == 1:
            g = g.GetGeometryRef(0).Clone()
    if ogr.GT_HasZ(lt) and not g.Is3D():
        g.Set3D(True)
    elif not ogr.GT_HasZ(lt) and g.Is3D():
        g.FlattenTo2D()
    return g


# ---------------------------------------------------------------- writer used by every tool

class Writer:
    """Create an output dataset and add features; honours env.outputCoordinateSystem."""

    def __init__(self, out, geom_type, srs, field_defns, want="vector", ref=None, alias=None, honour_ocs=True):
        self.ct = None
        ocs = G.osr_of(env.outputCoordinateSystem) if geom_type != ogr.wkbNone and honour_ocs else None
        if ocs is not None and srs is not None and not ocs.IsSame(srs):
            self.ct = G.transform(srs, ocs)
            srs = ocs
        elif ocs is not None and srs is None:
            srs = ocs
        self.ref = ref or check_output(out, want)
        self.ds, self.lyr = create_layer(self.ref, geom_type, srs, [], alias)
        self.map = []
        if self.ref.kind == "file" and self.ref.container.lower().endswith((".shp", ".dbf")):
            field_defns = [_clone_defn(fd) for fd in field_defns]
            for fd in field_defns:
                if fd.GetType() == ogr.OFTString and fd.GetWidth() > 254:
                    fd.SetWidth(254)
        for fd in field_defns:
            n_before = self.lyr.GetLayerDefn().GetFieldCount()
            if self.lyr.CreateField(fd) == 0 and self.lyr.GetLayerDefn().GetFieldCount() > n_before:
                self.map.append(self.lyr.GetLayerDefn().GetFieldCount() - 1)
            else:
                self.map.append(-1)
        self.defn = self.lyr.GetLayerDefn()
        self.tx = self.ref.kind == "gpkg" and self.ds.StartTransaction() == 0
        self.count = 0

    def add(self, values, geom=None):
        f = ogr.Feature(self.defn)
        for idx, v in zip(self.map, values):
            if idx >= 0:
                try:
                    set_value(f, idx, v)
                except (ValueError, TypeError, OverflowError):
                    f.SetFieldNull(idx)
        if geom is not None and self.defn.GetGeomType() != ogr.wkbNone:
            g = geom.Clone()
            if self.ct is not None:
                g.Transform(self.ct)
            f.SetGeometry(geom_for_layer(g, self.lyr))
        self.lyr.CreateFeature(f)
        self.count += 1
        return f.GetFID()

    def close(self):
        if self.tx:
            self.ds.CommitTransaction()
            self.tx = False
        if self.lyr is not None:
            self.lyr.SyncToDisk()
        self.lyr = None
        self.ds = None
        from ._base import note_output
        note_output(self.ref.path)
        return self.ref.path


def raw_values(f, n):
    return [get_value(f, i) for i in range(n)]


def read_all(value, where=None, target_srs=None):
    """Read a source fully: dict(fields, rows=[(fid, ogr geom, [values])], srs, geom_type, name)."""
    src = Source(value)
    info = src.info()
    ct = G.transform(info["srs"], target_srs) if target_srs is not None and info["srs"] is not None else None
    n = len(info["fields"])
    rows = []
    for f in src.features(where):
        g = f.GetGeometryRef()
        g = g.Clone() if g is not None else None
        if g is not None and ct is not None:
            g.Transform(ct)
        rows.append((f.GetFID(), g, raw_values(f, n)))
    info.update(rows=rows, name=src.name, ref=src.ref, srs=target_srs.Clone() if ct else info["srs"])
    return info


def copy_source(value, out, where=None, want="vector", target_srs=None, alias=None):
    """Copy features (respecting layer selection/definition query) to a new dataset."""
    src = Source(value)
    info = src.info()
    geom_type = info["geom_type"]
    srs = info["srs"]
    ct = None
    if target_srs is not None and srs is not None and not target_srs.IsSame(srs):
        ct = G.transform(srs, target_srs)
        srs = target_srs
    if want == "table":
        geom_type = ogr.wkbNone
    w = Writer(out, geom_type, srs, info["fields"], want, alias=alias)
    n = len(info["fields"])
    for f in src.features(where):
        g = f.GetGeometryRef()
        g = g.Clone() if g is not None else None
        if g is not None and ct is not None:
            g.Transform(ct)
        w.add(raw_values(f, n), g)
    return w.close()


# ---------------------------------------------------------------- fields

_TYPE_NAMES = {ogr.OFTString: "String", ogr.OFTInteger: "Integer", ogr.OFTInteger64: "BigInteger",
               ogr.OFTReal: "Double", ogr.OFTDate: "Date", ogr.OFTDateTime: "Date", ogr.OFTTime: "TimeOnly",
               ogr.OFTBinary: "Blob"}


class Field:
    def __init__(self, name="", type="String", length=0, precision=0, scale=0, aliasName=None,  # noqa: A002,N803
                 isNullable=True, required=False, editable=True, domain="", defaultValue=None):  # noqa: N803
        self.name = name
        self.baseName = name
        self.aliasName = aliasName or name
        self.type = type
        self.length = length
        self.precision = precision
        self.scale = scale
        self.isNullable = isNullable
        self.required = required
        self.editable = editable
        self.domain = domain
        self.defaultValue = defaultValue

    def __repr__(self):
        return f"<Field object '{self.name}' ({self.type})>"


def _field_from_defn(fd, ref):
    t = fd.GetType()
    st = fd.GetSubType()
    name = _TYPE_NAMES.get(t, "String")
    if t == ogr.OFTInteger and st == ogr.OFSTInt16:
        name = "SmallInteger"
    elif t == ogr.OFTReal and st == ogr.OFSTFloat32:
        name = "Single"
    elif t == ogr.OFTString and st == ogr.OFSTUUID:
        name = "GUID"
    elif t == ogr.OFTInteger64 and ref.kind == "file":
        name = "Integer"
    elif t == ogr.OFTDate and ref.kind not in ("file",):
        name = "DateOnly"
    elif t == ogr.OFTInteger and st == ogr.OFSTBoolean:
        name = "SmallInteger"
    length = fd.GetWidth() or {"String": 255, "Integer": 4, "SmallInteger": 2, "Double": 8, "Single": 4,
                               "Date": 8, "BigInteger": 8}.get(name, 0)
    try:
        alias = fd.GetAlternativeName() or fd.GetName()
    except Exception:
        alias = fd.GetName()
    dom = ""
    try:
        dom = fd.GetDomainName() or ""
    except Exception:
        pass
    return Field(fd.GetName(), name, length, fd.GetWidth() if t == ogr.OFTReal else 0,
                 fd.GetPrecision(), alias, bool(fd.IsNullable()), False, True, dom, fd.GetDefault())


def list_fields(value):
    """Field objects like arcpy: OID, Shape, attribute fields (and Shape_Length/Shape_Area in geodatabases)."""
    src = Source(value)
    ds, lyr = open_layer(src.ref)
    out = [Field(oid_name(lyr, src.ref), "OID", 4, 0, 0, None, False, True, False)]
    gt = lyr.GetGeomType()
    if gt != ogr.wkbNone:
        out.append(Field(shape_name(lyr, src.ref), "Geometry", 0, 0, 0, None, True, True, True))
    defn = lyr.GetLayerDefn()
    for i in range(defn.GetFieldCount()):
        out.append(_field_from_defn(defn.GetFieldDefn(i), src.ref))
    if src.ref.kind == "gdb" and gt != ogr.wkbNone:
        names = {f.name.lower() for f in out}
        flat = ogr.GT_Flatten(gt)
        if flat in (ogr.wkbLineString, ogr.wkbMultiLineString, ogr.wkbPolygon, ogr.wkbMultiPolygon) \
                and "shape_length" not in names:
            out.append(Field("Shape_Length", "Double", 8, 0, 0, None, True, True, False))
        if flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon) and "shape_area" not in names:
            out.append(Field("Shape_Area", "Double", 8, 0, 0, None, True, True, False))
    return out


_FIELD_TYPE_FILTER = {"all": None, "integer": {"Integer", "BigInteger"}, "smallinteger": {"SmallInteger"},
                      "string": {"String"}, "double": {"Double"}, "single": {"Single"}, "date": {"Date", "DateOnly"},
                      "oid": {"OID"}, "geometry": {"Geometry"}, "guid": {"GUID"}, "blob": {"Blob"},
                      "globalid": {"GlobalID"}, "raster": {"Raster"}, "biginteger": {"BigInteger"},
                      "dateonly": {"DateOnly"}, "timeonly": {"TimeOnly"}}


def ListFields(dataset, wild_card=None, field_type=None):  # noqa: N802
    fields = list_fields(dataset)
    want = _FIELD_TYPE_FILTER.get(str(field_type or "All").lower())
    return [f for f in fields if (not wild_card or fnmatch.fnmatch(f.name.lower(), str(wild_card).lower()))
            and (want is None or f.type in want)]


def ListIndexes(dataset, wild_card=None):  # noqa: N802
    return []


ARC_FIELD_TYPES = {
    "TEXT": (ogr.OFTString, None), "STRING": (ogr.OFTString, None), "FLOAT": (ogr.OFTReal, ogr.OFSTFloat32),
    "SINGLE": (ogr.OFTReal, ogr.OFSTFloat32), "DOUBLE": (ogr.OFTReal, None), "SHORT": (ogr.OFTInteger, ogr.OFSTInt16),
    "SMALLINTEGER": (ogr.OFTInteger, ogr.OFSTInt16), "LONG": (ogr.OFTInteger, None),
    "INTEGER": (ogr.OFTInteger, None), "BIGINTEGER": (ogr.OFTInteger64, None), "DATE": (ogr.OFTDateTime, None),
    "DATEONLY": (ogr.OFTDate, None), "TIMEONLY": (ogr.OFTTime, None), "BLOB": (ogr.OFTBinary, None),
    "GUID": (ogr.OFTString, ogr.OFSTUUID), "RASTER": (ogr.OFTBinary, None),
    "TIMESTAMPOFFSET": (ogr.OFTDateTime, None),
}


def make_defn(name, field_type="TEXT", length=None, precision=None, scale=None, alias=None, ref=None):
    key = str(field_type or "TEXT").upper()
    if key not in ARC_FIELD_TYPES:
        fail(800, f"The value is not a member of TEXT | FLOAT | DOUBLE | SHORT | LONG | BIGINTEGER | DATE | "
                  f"BLOB | GUID: {field_type}")
    t, st = ARC_FIELD_TYPES[key]
    fd = ogr.FieldDefn(str(name), t)
    if st is not None:
        fd.SetSubType(st)
    if t == ogr.OFTString:
        fd.SetWidth(int(length) if length else (254 if ref is not None and ref.kind == "file" else 255))
    elif t == ogr.OFTReal and precision:
        fd.SetWidth(int(precision))
        if scale:
            fd.SetPrecision(int(scale))
    elif t == ogr.OFTInteger and precision:
        fd.SetWidth(int(precision))
    if alias:
        try:
            fd.SetAlternativeName(str(alias))
        except Exception:
            pass
    return fd


# ---------------------------------------------------------------- describe

_SHAPE_TYPE = {ogr.wkbPoint: "Point", ogr.wkbMultiPoint: "Multipoint", ogr.wkbLineString: "Polyline",
               ogr.wkbMultiLineString: "Polyline", ogr.wkbPolygon: "Polygon", ogr.wkbMultiPolygon: "Polygon",
               ogr.wkbCurvePolygon: "Polygon", ogr.wkbMultiSurface: "Polygon", ogr.wkbCompoundCurve: "Polyline",
               ogr.wkbMultiCurve: "Polyline", ogr.wkbCircularString: "Polyline", ogr.wkbTIN: "MultiPatch",
               ogr.wkbPolyhedralSurface: "MultiPatch"}


def shape_type_of(geom_type):
    return _SHAPE_TYPE.get(ogr.GT_Flatten(geom_type), "Polygon" if geom_type != ogr.wkbNone else "")


class Describe:
    """arcpy.Describe(value): the properties scripts read most."""

    def __init__(self, value, datatype=None):
        self._p = {}
        value = _unwrap(value)
        from . import mp
        if isinstance(value, mp.Layer):
            value = value._as_feature_layer()
        lyr_obj = as_layer(value)
        if lyr_obj is not None:
            self._layer(lyr_obj)
            return
        if isinstance(value, G.Geometry):
            self._p.update(dataType="Geometry", shapeType=value.type.title(), extent=value.extent,
                           spatialReference=value.spatialReference)
            return
        from .sa import Raster
        if isinstance(value, Raster):
            value = value.catalogPath
        ref = parse(value)
        if not exists_ref(ref):
            raise OSError(f'"{value}" does not exist')
        self._ref(ref)

    def _common(self, ref, path):
        self._p["catalogPath"] = path
        self._p["path"] = os.path.dirname(path)
        base = os.path.basename(path)
        self._p["name"] = base
        self._p["file"] = base
        self._p["baseName"] = os.path.splitext(base)[0] if ref.kind in ("file", "raster") else base
        self._p["extension"] = os.path.splitext(base)[1].lstrip(".") if ref.kind in ("file", "raster") else ""
        self._p["children"] = []
        self._p["childrenExpanded"] = False
        self._p["metadataRetrieved"] = False
        self._p["fullPropsRetrieved"] = True

    def _ref(self, ref):
        self._common(ref, ref.path)
        k = ref.kind
        if k in ("folder", "gdbws", "gpkgws"):
            self._p.update(dataType="Folder" if k == "folder" else "Workspace",
                           workspaceType="FileSystem" if k == "folder" else "LocalDatabase",
                           workspaceFactoryProgID={"folder": "", "gdbws": "esriDataSourcesGDB.FileGDBWorkspaceFactory.1",
                                                   "gpkgws": "esriDataSourcesGDB.SqliteWorkspaceFactory.1"}[k],
                           connectionString=f"DATABASE={ref.container}", domains=[], currentRelease=True,
                           release="3,0,0", connectionProperties=None)
            if k != "folder":
                self._p["dataType"] = "Workspace"
            return
        if k == "fd":
            layout = gdb_layout(ref.container)
            names = layout.get(_ci(ref.dataset, layout) or ref.dataset, [])
            srs = None
            if names:
                ds, lyr = open_layer(Ref("gdb", ref.container, names[0], ref.dataset))
                srs = lyr.GetSpatialRef()
            else:
                srs = _PENDING_FD.get(os.path.normcase(ref.container), {}).get(ref.dataset)
            self._p.update(dataType="FeatureDataset", datasetType="FeatureDataset",
                           spatialReference=G.SpatialReference._from_osr(srs) if srs else G.SpatialReference(),
                           children=names)
            return
        if k == "raster":
            self._raster(ref)
            return
        ds, lyr = open_layer(ref)
        gt = lyr.GetGeomType()
        srs = lyr.GetSpatialRef()
        ext = None
        is_fc = gt != ogr.wkbNone
        if is_fc:
            try:
                x0, x1, y0, y1 = lyr.GetExtent(force=1)
                ext = G.Extent(x0, y0, x1, y1, spatial_reference=G.SpatialReference._from_osr(srs) if srs else None)
            except Exception:
                ext = G.Extent()
        fields = list_fields(ref)
        if ref.kind == "file":
            e = os.path.splitext(ref.container)[1].lower()
            dtype = ("ShapeFile" if e == ".shp" else "DbaseTable" if e == ".dbf" else "TextFile"
                     if e in (".csv", ".txt") else "FeatureClass" if is_fc else "Table")
        else:
            dtype = "FeatureClass" if is_fc else "Table"
        self._p.update(
            dataType=dtype, datasetType="FeatureClass" if is_fc else "Table", fields=fields, indexes=[],
            OIDFieldName=oid_name(lyr, ref), hasOID=True, canVersion=False, isVersioned=False,
            aliasName=ref.base_name, relationshipClassNames=[], defaultSubtypeCode=-1, subtypeFieldName="",
            globalIDFieldName="", hasGlobalID=False, editorTrackingEnabled=False, modelName="",
            dateModified="", changeTracked=False, isArchived=False,
        )
        if is_fc:
            flat = ogr.GT_Flatten(gt)
            self._p.update(
                shapeType=shape_type_of(gt), shapeFieldName=shape_name(lyr, ref), featureType="Simple",
                hasM=bool(ogr.GT_HasM(gt)), hasZ=bool(ogr.GT_HasZ(gt)), hasSpatialIndex=True,
                spatialReference=G.SpatialReference._from_osr(srs) if srs else G.SpatialReference(),
                extent=ext, geometryStorage="",
                areaFieldName="Shape_Area" if ref.kind == "gdb" and flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon)
                else "",
                lengthFieldName="Shape_Length" if ref.kind == "gdb" and flat in (
                    ogr.wkbLineString, ogr.wkbMultiLineString, ogr.wkbPolygon, ogr.wkbMultiPolygon) else "",
                featureDataset=ref.dataset or "",
            )
        self._p["_count"] = None

    def _raster(self, ref):
        ds = gdal.Open(ref.container)
        if ds is None:
            fail(732, f"Input raster: Dataset {ref.path} does not exist or is not supported")
        gt = ds.GetGeoTransform()
        srs = ds.GetSpatialRef()
        sr = G.SpatialReference._from_osr(srs) if srs else G.SpatialReference()
        w, h = ds.RasterXSize, ds.RasterYSize
        x0, y0 = gt[0], gt[3]
        x1, y1 = x0 + gt[1] * w + gt[2] * h, y0 + gt[4] * w + gt[5] * h
        b = ds.GetRasterBand(1)
        dtype = gdal.GetDataTypeName(b.DataType)
        pix = {"Byte": "U8", "Int8": "S8", "UInt16": "U16", "Int16": "S16", "UInt32": "U32", "Int32": "S32",
               "Float32": "F32", "Float64": "F64", "Int64": "S64", "UInt64": "U64"}.get(dtype, dtype)
        drv = ds.GetDriver().ShortName
        self._p.update(
            dataType="RasterDataset", datasetType="RasterDataset", bandCount=ds.RasterCount, width=w, height=h,
            meanCellWidth=abs(gt[1]), meanCellHeight=abs(gt[5]), pixelType=pix, noDataValue=b.GetNoDataValue(),
            isInteger=not dtype.startswith("Float"), spatialReference=sr,
            extent=G.Extent(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), spatial_reference=sr),
            format={"GTiff": "TIFF", "HFA": "IMAGINE Image", "AAIGrid": "AAIGrid", "JPEG": "JPEG", "PNG": "PNG",
                    "VRT": "VRT"}.get(drv, drv), compressionType=(ds.GetMetadataItem("COMPRESSION",
                                                                                       "IMAGE_STRUCTURE") or "None"),
            permanent=True, sensorType="", primaryField=1, tableType="", hasRAT=False,
        )

    def _layer(self, lyr):
        inner = Describe(lyr.ref)
        self._p.update(inner._p)
        self._p.update(
            dataType="TableView" if lyr.is_table else "FeatureLayer", name=lyr.name, nameString=lyr.name,
            whereClause=lyr.where or "", featureClass=inner, table=inner, dataElement=inner,
            FIDSet="; ".join(str(i) for i in sorted(lyr.selection)) if lyr.selection else "",
            catalogPath=lyr.ref.path, fieldInfo=None, layer=lyr,
        )

    def __getattr__(self, name):
        p = object.__getattribute__(self, "_p")
        if name in p:
            return p[name]
        for k, v in p.items():
            if k.lower() == name.lower():
                return v
        raise AttributeError(f"DescribeData: Method {name} does not exist")

    def __repr__(self):
        return f"<geoprocessing describe data object '{self._p.get('catalogPath', '')}'>"

    def as_dict(self):
        return {k: v for k, v in self._p.items() if not k.startswith("_")}


# ---------------------------------------------------------------- listing

def _ws(ws=None):
    ws = ws or env.workspace
    if not ws:
        return None
    return parse(ws, "folder")


def _match(name, wild):
    return not wild or fnmatch.fnmatch(name.lower(), str(wild).lower())


def _gpkg_layers(path):
    ds = _open_raw(path, False, ["GPKG", "SQLite"])
    out = []
    if ds is None:
        return out
    for i in range(ds.GetLayerCount()):
        L = ds.GetLayer(i)
        out.append((L.GetName(), L.GetGeomType()))
    return out


def _gdb_types(gdb):
    ds = _open_raw(gdb, False, ["OpenFileGDB"])
    out = {}
    if ds is None:
        return out
    for i in range(ds.GetLayerCount()):
        L = ds.GetLayer(i)
        out[L.GetName()] = L.GetGeomType()
    return out


def _type_ok(geom_type, feature_type):
    ft = str(feature_type or "All").lower()
    if ft in ("all", ""):
        return True
    st = shape_type_of(geom_type).lower()
    if ft in ("line", "polyline", "arc"):
        return st == "polyline"
    return st == ft


def list_feature_classes_in(ws_ref, wild_card=None, feature_type=None, feature_dataset=None):
    out = []
    if ws_ref.kind == "folder":
        for p in sorted(glob.glob(os.path.join(glob.escape(ws_ref.container), "*.shp"))):
            name = os.path.basename(p)
            if not _match(name, wild_card):
                continue
            if feature_type and str(feature_type).lower() != "all":
                ds = _open_raw(p, False)
                if ds is None or not _type_ok(ds.GetLayer(0).GetGeomType(), feature_type):
                    continue
            out.append(name)
    elif ws_ref.kind in ("gdbws", "fd"):
        layout = gdb_layout(ws_ref.container)
        types = _gdb_types(ws_ref.container)
        fd = feature_dataset if ws_ref.kind == "gdbws" else ws_ref.dataset
        key = _ci(fd, [k for k in layout if k]) if fd else ""
        if fd and key is None:
            return []
        for n in layout.get(key, []):
            gt = types.get(n, ogr.wkbNone)
            if gt != ogr.wkbNone and _match(n, wild_card) and _type_ok(gt, feature_type):
                out.append(n)
    elif ws_ref.kind == "gpkgws":
        for n, gt in _gpkg_layers(ws_ref.container):
            if gt != ogr.wkbNone and _match(n, wild_card) and _type_ok(gt, feature_type):
                out.append(n)
    return out


def ListFeatureClasses(wild_card=None, feature_type=None, feature_dataset=None):  # noqa: N802
    ws = _ws()
    if ws is None:
        return None
    return list_feature_classes_in(ws, wild_card, feature_type, feature_dataset)


def list_tables_in(ws_ref, wild_card=None):
    out = []
    if ws_ref.kind == "folder":
        for p in sorted(os.listdir(ws_ref.container)):
            full = os.path.join(ws_ref.container, p)
            base, ext = os.path.splitext(p)
            if ext.lower() == ".dbf" and not os.path.exists(os.path.join(ws_ref.container, base + ".shp")):
                if _match(p, wild_card):
                    out.append(p)
            elif ext.lower() == ".csv" and os.path.isfile(full) and _match(p, wild_card):
                out.append(p)
    elif ws_ref.kind in ("gdbws",):
        types = _gdb_types(ws_ref.container)
        for n in gdb_layout(ws_ref.container).get("", []):
            if types.get(n) == ogr.wkbNone and _match(n, wild_card):
                out.append(n)
    elif ws_ref.kind == "gpkgws":
        out = [n for n, gt in _gpkg_layers(ws_ref.container) if gt == ogr.wkbNone and _match(n, wild_card)]
    return out


def ListTables(wild_card=None, table_type=None):  # noqa: N802
    ws = _ws()
    if ws is None:
        return None
    return list_tables_in(ws, wild_card)


def list_rasters_in(ws_ref, wild_card=None, raster_type=None):
    out = []
    if ws_ref.kind != "folder":
        return out
    rt = str(raster_type or "All").lower()
    for p in sorted(os.listdir(ws_ref.container)):
        ext = os.path.splitext(p)[1].lower()
        if ext not in RASTER_EXT or not os.path.isfile(os.path.join(ws_ref.container, p)):
            continue
        if rt not in ("all", "") and ext.lstrip(".") not in (rt, {"tif": "tiff"}.get(rt, rt)) and not (
                rt == "tif" and ext in (".tif", ".tiff")):
            continue
        if _match(p, wild_card):
            out.append(p)
    return out


def ListRasters(wild_card=None, raster_type=None):  # noqa: N802
    ws = _ws()
    if ws is None:
        return None
    return list_rasters_in(ws, wild_card, raster_type)


def ListDatasets(wild_card=None, feature_type=None):  # noqa: N802
    ws = _ws()
    if ws is None:
        return None
    ft = str(feature_type or "All").lower()
    if ws.kind == "gdbws":
        if ft not in ("all", "feature", ""):
            return []
        return [k for k in gdb_layout(ws.container) if k and _match(k, wild_card)]
    if ws.kind == "folder" and ft in ("all", "raster", ""):
        return list_rasters_in(ws, wild_card)
    return []


def ListWorkspaces(wild_card=None, workspace_type=None):  # noqa: N802
    ws = _ws()
    if ws is None or ws.kind != "folder":
        return []
    wt = str(workspace_type or "All").lower()
    out = []
    for p in sorted(os.listdir(ws.container)):
        full = os.path.join(ws.container, p)
        low = p.lower()
        kind = ("filegdb" if low.endswith(".gdb") and os.path.isdir(full) else "sqlite"
                if low.endswith(GPKG_EXT) and os.path.isfile(full) else "folder" if os.path.isdir(full) else None)
        if kind is None:
            continue
        if wt not in ("all", "") and wt != kind:
            continue
        if _match(p, wild_card):
            out.append(full)
    return out


def ListFiles(wild_card=None):  # noqa: N802
    ws = _ws()
    if ws is None or ws.kind != "folder":
        return []
    return [p for p in sorted(os.listdir(ws.container)) if _match(p, wild_card)]


def ListSpatialReferences(wild_card=None, spatial_reference_type="ALL"):  # noqa: N802
    unsupported("ListSpatialReferences")


def ListDomains(workspace):  # noqa: N802
    return []


def ListVersions(*a, **k):  # noqa: N802
    return []


def ListUsers(*a, **k):  # noqa: N802
    return []


def ParseFieldName(name, workspace=None):  # noqa: N802
    return f", , {name}, "


def ParseTableName(name, workspace=None):  # noqa: N802
    return f", , {name}"


def ValidateTableName(name, workspace=None):  # noqa: N802
    n = re.sub(r"[^\w]", "_", str(name))
    if n and n[0].isdigit():
        n = "T" + n
    return n


def ValidateFieldName(name, workspace=None):  # noqa: N802
    n = re.sub(r"[^\w]", "_", str(name))
    if n and n[0].isdigit():
        n = "F" + n
    ws = parse(workspace, "folder") if workspace else None
    if ws is not None and ws.kind == "folder":
        n = n[:10]
    return n


def CreateUniqueName(base_name, workspace=None):  # noqa: N802
    ws = workspace or env.workspace or env.scratchFolder
    stem, ext = os.path.splitext(base_name)
    i = 0
    while True:
        cand = f"{stem}{i}{ext}" if i else base_name
        if not Exists(os.path.join(ws, cand)):
            return os.path.join(ws, cand)
        i += 1


def CreateScratchName(prefix="xx", suffix="", data_type="", workspace=None):  # noqa: N802
    ws = workspace or env.scratchGDB
    return CreateUniqueName(f"{prefix}{suffix}", ws)


def AddFieldDelimiters(datasource, field):  # noqa: N802
    return f'"{field}"'


def TestSchemaLock(dataset):  # noqa: N802
    return True


def materialize(value):
    """An OGR-openable (path, layer name) for a source; filtered layers / memory data go to a temp GeoPackage."""
    src = Source(value)
    if src.layer is None or (not src.layer.where and src.layer.selection is None):
        if src.ref.kind in ("file",):
            return src.ref.container, None
        if src.ref.kind in ("gdb", "gpkg"):
            return src.ref.container, src.ref.name
    tmp = temp_path(".gpkg")
    path = copy_source(value, Ref("gpkg", tmp, "data"))
    del path
    return tmp, "data"
