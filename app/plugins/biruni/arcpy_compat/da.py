"""arcpy.da: SearchCursor, UpdateCursor, InsertCursor, Walk, Describe, Editor - over GDAL/OGR."""
import os
import re

from osgeo import ogr

from . import data as D
from . import geometry as G
from ._base import ExecuteError, missing_callable, unsupported

_GEOM_TOKENS = {"SHAPE@", "SHAPE@XY", "SHAPE@TRUECENTROID", "SHAPE@X", "SHAPE@Y", "SHAPE@Z", "SHAPE@M",
                "SHAPE@JSON", "SHAPE@WKB", "SHAPE@WKT", "SHAPE@AREA", "SHAPE@LENGTH"}


def _field_list(field_names, info, oid, shape):
    if isinstance(field_names, str):
        if field_names.strip() == "*":
            names = [oid] + ([shape] if shape else []) + [fd.GetName() for fd in info["fields"]]
            return names
        field_names = [s.strip() for s in re.split(r"[;,]", field_names) if s.strip()]
    out = []
    for n in field_names:
        if n == "*":
            out.extend([oid] + ([shape] if shape else []) + [fd.GetName() for fd in info["fields"]])
        else:
            out.append(n)
    return out


class _Base:
    def __init__(self, in_table, field_names, spatial_reference=None):
        self._src = D.Source(in_table)
        ds, lyr = D.open_layer(self._src.ref)
        defn = lyr.GetLayerDefn()
        self._info = {"fields": [D._clone_defn(defn.GetFieldDefn(i)) for i in range(defn.GetFieldCount())]}
        self._oid = D.oid_name(lyr, self._src.ref)
        self._shape = D.shape_name(lyr, self._src.ref)
        srs = lyr.GetSpatialRef()
        self._layer_srs = srs.Clone() if srs is not None else None
        self._is_gdb = self._src.ref.kind == "gdb"
        ds = None
        self.fields = tuple(_field_list(field_names, self._info, self._oid, self._shape))
        out_sr = G.as_sr(spatial_reference)
        self._sr = out_sr if out_sr is not None and not out_sr._empty else (
            G.SpatialReference._from_osr(self._layer_srs) if self._layer_srs is not None else None)
        self._ct_out = G.transform(self._layer_srs, out_sr._srs) if out_sr is not None and not out_sr._empty \
            and self._layer_srs is not None else None
        self._ct_in = G.transform(out_sr._srs, self._layer_srs) if self._ct_out is not None else None
        self._plan = [self._token(n) for n in self.fields]

    def _token(self, name):
        up = str(name).upper()
        if up == "OID@" or up == self._oid.upper():
            return ("oid", None)
        if up in _GEOM_TOKENS:
            return ("geom", up)
        if up.startswith("SHAPE@"):
            unsupported(f"da cursor token {name}")
        if self._shape and up == self._shape.upper():
            return ("geom", "SHAPE@XY")
        if up in ("SHAPE_AREA", "SHAPE.STAREA()", "SHAPE.AREA"):
            idx = self._index(name, required=False)
            return ("field", idx) if idx >= 0 else ("geom", "SHAPE@AREA")
        if up in ("SHAPE_LENGTH", "SHAPE.STLENGTH()", "SHAPE.LEN", "SHAPE_LEN"):
            idx = self._index(name, required=False)
            return ("field", idx) if idx >= 0 else ("geom", "SHAPE@LENGTH")
        return ("field", self._index(name))

    def _index(self, name, required=True):
        for i, fd in enumerate(self._info["fields"]):
            if fd.GetName().lower() == str(name).lower():
                return i
        if required:
            raise RuntimeError(f"Cannot find field '{name}'")
        return -1

    def _geom_value(self, f, token):
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            return None if token not in ("SHAPE@AREA", "SHAPE@LENGTH") else None
        g = g.Clone()
        if self._ct_out is not None:
            g.Transform(self._ct_out)
        if token == "SHAPE@":
            return G.wrap(g, self._sr)
        if token == "SHAPE@WKT":
            return G.wrap(g, self._sr).WKT
        if token == "SHAPE@WKB":
            return bytearray(g.ExportToIsoWkb())
        if token == "SHAPE@JSON":
            return G.wrap(g, self._sr).JSON
        if token == "SHAPE@AREA":
            return G.wrap(g, self._sr).area
        if token == "SHAPE@LENGTH":
            return G.wrap(g, self._sr).length
        flat = ogr.GT_Flatten(g.GetGeometryType())
        if flat == ogr.wkbPoint:
            x, y = g.GetX(), g.GetY()
            z = g.GetZ() if g.Is3D() else None
        else:
            if token == "SHAPE@Z":
                pts = G._points_of(g)
                return pts[0][2] if pts else None
            c = G.wrap(g, self._sr)
            p = c.trueCentroid if token == "SHAPE@TRUECENTROID" else c.centroid
            x, y, z = p.X, p.Y, None
        if token in ("SHAPE@XY", "SHAPE@TRUECENTROID"):
            return (x, y)
        if token == "SHAPE@X":
            return x
        if token == "SHAPE@Y":
            return y
        if token == "SHAPE@Z":
            return z
        if token == "SHAPE@M":
            return g.GetM() if g.IsMeasured() else None
        return None

    def _sort_key(self, f, row):
        """Values of the ORDER BY fields (cursor fields or any table field) for one feature."""
        key = []
        for name, _desc in getattr(self, "_order", []):
            idx = next((i for i, n in enumerate(self.fields) if n.lower() == name.lower()), None)
            if idx is not None:
                key.append(row[idx])
                continue
            if name.lower() == self._oid.lower():
                key.append(f.GetFID())
                continue
            j = self._index(name, required=False)
            key.append(D.get_value(f, j) if j >= 0 else None)
        return tuple(key)

    def _row(self, f):
        out = []
        for kind, arg in self._plan:
            if kind == "oid":
                out.append(f.GetFID())
            elif kind == "geom":
                out.append(self._geom_value(f, arg))
            else:
                out.append(D.get_value(f, arg))
        return out

    def _set_geom(self, f, token, value, lyr):
        if token in ("SHAPE@AREA", "SHAPE@LENGTH", "SHAPE@JSON") and not isinstance(value, str):
            return
        if value is None:
            f.SetGeometry(None)
            return
        if token in ("SHAPE@XY", "SHAPE@TRUECENTROID"):
            x, y = value
            flat = ogr.GT_Flatten(lyr.GetGeomType())
            if flat in (ogr.wkbPoint, ogr.wkbUnknown):
                g = ogr.Geometry(ogr.wkbPoint)
                g.AddPoint_2D(float(x), float(y))
            else:
                old = f.GetGeometryRef()
                if old is None:
                    return
                cur = self._geom_value(f, "SHAPE@XY")
                if cur is None:
                    return
                dx, dy = float(x) - cur[0], float(y) - cur[1]
                g = old.Clone()
                _translate(g, dx, dy)
                f.SetGeometry(g)
                return
        elif token in ("SHAPE@X", "SHAPE@Y", "SHAPE@Z"):
            old = f.GetGeometryRef()
            g = old.Clone() if old is not None else ogr.Geometry(ogr.wkbPoint)
            if g.GetPointCount() == 0:
                g.AddPoint_2D(0.0, 0.0)
            x, y = g.GetX(), g.GetY()
            z = g.GetZ() if g.Is3D() else 0.0
            if token == "SHAPE@X":
                x = float(value)
            elif token == "SHAPE@Y":
                y = float(value)
            else:
                z = float(value)
            g = ogr.Geometry(ogr.wkbPoint25D if token == "SHAPE@Z" or g.Is3D() else ogr.wkbPoint)
            if g.Is3D():
                g.AddPoint(x, y, z)
            else:
                g.AddPoint_2D(x, y)
            f.SetGeometry(D.geom_for_layer(g, lyr))
            return
        else:
            sr_srs = self._sr._srs if self._sr is not None and not self._sr._empty else None
            g = G.to_ogr(value, sr_srs)
        if g is not None and self._ct_in is not None:
            g.Transform(self._ct_in)
        f.SetGeometry(D.geom_for_layer(g, lyr))

    def _apply(self, f, row, lyr):
        if len(row) != len(self._plan):
            raise RuntimeError(f"Expected {len(self._plan)} values, got {len(row)}")
        for (kind, arg), v in zip(self._plan, row):
            if kind == "oid":
                continue
            if kind == "geom":
                self._set_geom(f, arg, v, lyr)
            else:
                try:
                    D.set_value(f, arg, v)
                except (ValueError, TypeError, OverflowError) as e:
                    raise RuntimeError(f"The value type is incompatible with the field type. [{self.fields}] {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._close(commit=exc_type is None)
        return False

    def __del__(self):
        try:
            self._close(commit=True)
        except Exception:
            pass

    def _close(self, commit=True):
        pass

    def _flush(self):
        pass


def _translate(g, dx, dy):
    n = g.GetGeometryCount()
    if n:
        for i in range(n):
            _translate(g.GetGeometryRef(i), dx, dy)
        return
    for i in range(g.GetPointCount()):
        p = g.GetPoint(i)
        if g.Is3D():
            g.SetPoint(i, p[0] + dx, p[1] + dy, p[2])
        else:
            g.SetPoint_2D(i, p[0] + dx, p[1] + dy)


def _sql_clause(sql_clause):
    prefix, postfix = (sql_clause or (None, None)) if isinstance(sql_clause, (tuple, list)) else (None, sql_clause)
    distinct, top, order = False, None, []
    if prefix:
        p = str(prefix).upper()
        distinct = "DISTINCT" in p
        m = re.search(r"TOP\s+(\d+)", p)
        top = int(m.group(1)) if m else None
    if postfix:
        m = re.search(r"ORDER\s+BY\s+(.+)$", str(postfix), re.I)
        if m:
            for part in m.group(1).split(","):
                bits = part.strip().split()
                if bits:
                    order.append((bits[0].strip('"[]'), len(bits) > 1 and bits[1].upper() == "DESC"))
    return distinct, top, order


class SearchCursor(_Base):
    def __init__(self, in_table, field_names, where_clause=None, spatial_reference=None, explode_to_points=False,
                 sql_clause=(None, None), datum_transformation=None, spatial_filter=None,
                 spatial_relationship="INTERSECTS", search_order="ATTRIBUTEFIRST"):
        super().__init__(in_table, field_names, spatial_reference)
        self._where = where_clause
        self._explode = explode_to_points
        self._distinct, self._top, self._order = _sql_clause(sql_clause)
        self._spatial = spatial_filter
        self._relation = str(spatial_relationship or "INTERSECTS").upper()
        self._it = None
        self._buffer = None

    def _rows(self):
        filt = None
        if self._spatial is not None:
            filt = G.to_ogr(self._spatial, self._layer_srs)
        materialize = self._order or self._distinct
        rows = []
        seen = set()
        count = 0
        for f in self._src.features(self._where):
            if filt is not None:
                g = f.GetGeometryRef()
                if g is None or not _relate(g, filt, self._relation):
                    continue
            if self._explode:
                for row in self._exploded(f):
                    yield tuple(row)
                continue
            row = tuple(self._row(f))
            if self._distinct:
                key = tuple(map(_hashable, row))
                if key in seen:
                    continue
                seen.add(key)
            if materialize:
                rows.append((self._sort_key(f, row), row))
                continue
            yield row
            count += 1
            if self._top and count >= self._top:
                return
        if materialize:
            for k in reversed(range(len(self._order))):
                desc = self._order[k][1]
                rows.sort(key=lambda r: (r[0][k] is None, r[0][k]) if not desc else
                          (r[0][k] is not None, r[0][k]), reverse=desc)
            for n, (_f, row) in enumerate(rows):
                if self._top and n >= self._top:
                    return
                yield row

    def _exploded(self, f):
        g = f.GetGeometryRef()
        pts = G._points_of(g) if g is not None else [None]
        for p in pts:
            row = []
            for kind, arg in self._plan:
                if kind == "oid":
                    row.append(f.GetFID())
                elif kind == "geom":
                    if p is None:
                        row.append(None)
                    elif arg in ("SHAPE@XY", "SHAPE@TRUECENTROID"):
                        row.append((p[0], p[1]))
                    elif arg == "SHAPE@X":
                        row.append(p[0])
                    elif arg == "SHAPE@Y":
                        row.append(p[1])
                    elif arg == "SHAPE@Z":
                        row.append(p[2])
                    else:
                        row.append(G.PointGeometry(G.Point(p[0], p[1], p[2]), self._sr))
                else:
                    row.append(D.get_value(f, arg))
            yield row

    def __iter__(self):
        if self._it is None:
            self._it = self._rows()
        return self

    def __next__(self):
        if self._it is None:
            self._it = self._rows()
        return next(self._it)

    def next(self):
        return self.__next__()

    def reset(self):
        self._it = None

    def _close(self, commit=True):
        self._it = None

    def _as_narray(self):
        unsupported("da.SearchCursor._as_narray")


def _hashable(v):
    try:
        hash(v)
        return v
    except TypeError:
        return repr(v)


def _relate(g, other, relation):
    r = relation.upper()
    if r in ("INTERSECTS", "INTERSECT"):
        return g.Intersects(other)
    if r == "CONTAINS":
        return g.Contains(other)
    if r == "WITHIN":
        return g.Within(other)
    if r == "TOUCHES":
        return g.Touches(other)
    if r == "CROSSES":
        return g.Crosses(other)
    if r == "OVERLAPS":
        return g.Overlaps(other)
    if r == "EQUALS":
        return g.Equals(other)
    if r == "DISJOINT":
        return g.Disjoint(other)
    return g.Intersects(other)


class UpdateCursor(_Base):
    def __init__(self, in_table, field_names, where_clause=None, spatial_reference=None, explode_to_points=False,
                 sql_clause=(None, None), datum_transformation=None, explicit=False, spatial_filter=None,
                 spatial_relationship="INTERSECTS", search_order="ATTRIBUTEFIRST"):
        super().__init__(in_table, field_names, spatial_reference)
        _distinct, _top, order = _sql_clause(sql_clause)
        self._order = order
        filt = G.to_ogr(spatial_filter, self._layer_srs) if spatial_filter is not None else None
        fids = []
        for f in self._src.features(where_clause):
            if filt is not None:
                g = f.GetGeometryRef()
                if g is None or not _relate(g, filt, str(spatial_relationship or "INTERSECTS")):
                    continue
            fids.append((f.GetFID(), self._sort_key(f, self._row(f)) if order else None))
        if order:
            for k in reversed(range(len(order))):
                desc = order[k][1]
                fids.sort(key=lambda r: (r[1][k] is None, r[1][k]) if not desc else
                          (r[1][k] is not None, r[1][k]), reverse=desc)
        self._fids = [fid for fid, _r in fids]
        self._pos = 0
        self._current = None
        self._ds, self._lyr = D.open_layer(self._src.ref, update=True)
        self._tx = self._src.ref.kind == "gpkg" and self._ds.StartTransaction() == 0
        self._closed = False
        D.register_live(self)

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        while self._pos < len(self._fids):
            fid = self._fids[self._pos]
            self._pos += 1
            f = self._lyr.GetFeature(fid)
            if f is None:
                continue
            self._current = f
            return list(self._row(f))
        self._current = None
        self._close()
        raise StopIteration

    def next(self):
        return self.__next__()

    def reset(self):
        self._pos = 0

    def updateRow(self, row):  # noqa: N802
        if self._current is None:
            raise RuntimeError("updateRow called without a current row")
        self._apply(self._current, list(row), self._lyr)
        if self._lyr.SetFeature(self._current) != 0:
            raise RuntimeError("Unable to update the row")

    def deleteRow(self):  # noqa: N802
        if self._current is None:
            raise RuntimeError("deleteRow called without a current row")
        self._lyr.DeleteFeature(self._current.GetFID())
        self._current = None

    def _flush(self):
        if self._closed:
            return
        if self._tx:
            self._ds.CommitTransaction()
            self._tx = self._ds.StartTransaction() == 0
        self._lyr.SyncToDisk()

    def _close(self, commit=True):
        if getattr(self, "_closed", True):
            return
        self._closed = True
        if self._tx:
            if commit:
                self._ds.CommitTransaction()
            else:
                self._ds.RollbackTransaction()
        if self._lyr is not None:
            self._lyr.SyncToDisk()
        self._lyr = None
        self._ds = None
        self._current = None


class InsertCursor(_Base):
    def __init__(self, in_table, field_names, datum_transformation=None, explicit=False):
        super().__init__(in_table, field_names)
        self._ds, self._lyr = D.open_layer(self._src.ref, update=True)
        self._defn = self._lyr.GetLayerDefn()
        self._tx = self._src.ref.kind == "gpkg" and self._ds.StartTransaction() == 0
        self._closed = False
        self._n = 0
        D.register_live(self)

    def insertRow(self, row):  # noqa: N802
        if self._closed:
            raise RuntimeError("Cursor is closed")
        f = ogr.Feature(self._defn)
        self._apply(f, list(row), self._lyr)
        if self._lyr.CreateFeature(f) != 0:
            raise RuntimeError("Unable to insert the row")
        self._n += 1
        if self._tx and self._n % 20000 == 0:
            self._flush()
        return f.GetFID()

    def _flush(self):
        if self._closed:
            return
        if self._tx:
            self._ds.CommitTransaction()
            self._tx = self._ds.StartTransaction() == 0
        self._lyr.SyncToDisk()

    def _close(self, commit=True):
        if getattr(self, "_closed", True):
            return
        self._closed = True
        if self._tx:
            self._ds.CommitTransaction()
        if self._lyr is not None:
            self._lyr.SyncToDisk()
        self._lyr = None
        self._ds = None


# ---------------------------------------------------------------- Walk

def _walk_items(path, datatype, ftype):
    """(dirnames, filenames) of one catalog folder, gdb, gpkg or feature dataset."""
    kinds = {str(t).lower() for t in (datatype if isinstance(datatype, (list, tuple)) else [datatype])
             if t} or {"any"}
    want_fc = bool(kinds & {"any", "featureclass"})
    want_tb = bool(kinds & {"any", "table"})
    want_rs = bool(kinds & {"any", "rasterdataset"})
    ref = D.parse(path, "folder")
    dirs, files = [], []
    if ref.kind == "folder":
        try:
            entries = sorted(os.listdir(ref.container))
        except OSError:
            return dirs, files
        for e in entries:
            full = os.path.join(ref.container, e)
            low = e.lower()
            if os.path.isdir(full):
                dirs.append(e)
            elif low.endswith(D.GPKG_EXT):
                dirs.append(e)
        if want_fc:
            files += D.list_feature_classes_in(ref, None, ftype)
        if want_tb:
            files += D.list_tables_in(ref)
        if want_rs:
            files += D.list_rasters_in(ref)
    elif ref.kind == "gdbws":
        layout = D.gdb_layout(ref.container)
        dirs = [k for k in layout if k] if want_fc or kinds & {"featuredataset"} else []
        if want_fc:
            files += D.list_feature_classes_in(ref, None, ftype)
        if want_tb:
            files += D.list_tables_in(ref)
    elif ref.kind == "fd":
        if want_fc:
            files += D.list_feature_classes_in(ref, None, ftype)
    elif ref.kind == "gpkgws":
        if want_fc:
            files += D.list_feature_classes_in(ref, None, ftype)
        if want_tb:
            files += D.list_tables_in(ref)
    return dirs, files


def Walk(top, topdown=True, onerror=None, followlinks=False, datatype=None, type=None):  # noqa: N802,A002
    top = D.full_path(top)

    def _walk(path):
        try:
            dirs, files = _walk_items(path, datatype, type)
        except Exception as e:  # noqa: BLE001
            if onerror is not None:
                onerror(e)
            return
        if topdown:
            yield path, dirs, files
            for d in list(dirs):
                yield from _walk(os.path.join(path, d))
        else:
            for d in list(dirs):
                yield from _walk(os.path.join(path, d))
            yield path, dirs, files

    yield from _walk(top)


def Describe(value, datatype=None):  # noqa: N802
    d = D.Describe(value, datatype).as_dict()
    if "fields" in d:
        d["fields"] = list(d["fields"])
    return d


class Editor:
    """Edit sessions are not needed here: every edit is saved as it is made."""

    def __init__(self, workspace, multiuser_mode=False):
        self.workspace = workspace
        self.isEditing = False

    def startEditing(self, with_undo=True, multiuser_mode=True):  # noqa: N802
        self.isEditing = True

    def stopEditing(self, save_changes=True):  # noqa: N802
        D.flush_live()
        self.isEditing = False

    def startOperation(self):  # noqa: N802
        pass

    def stopOperation(self):  # noqa: N802
        D.flush_live()

    def abortOperation(self):  # noqa: N802
        pass

    def undoOperation(self):  # noqa: N802
        unsupported("da.Editor.undoOperation")

    def redoOperation(self):  # noqa: N802
        unsupported("da.Editor.redoOperation")

    def __enter__(self):
        self.startEditing()
        return self

    def __exit__(self, *exc):
        self.stopEditing(exc[0] is None)
        return False


def ListDomains(workspace=None):  # noqa: N802
    return []


def ListSubtypes(table):  # noqa: N802
    return {0: {"Name": "", "Default": True, "SubtypeField": "", "FieldValues": {}}}


def ListReplicas(*a, **k):  # noqa: N802
    return []


def _np_dtype(values):
    vals = [v for v in values if v is not None]
    if vals and all(isinstance(v, bool) for v in vals):
        return "?"
    if vals and all(isinstance(v, int) for v in vals):
        return "<i8"
    if vals and all(isinstance(v, (int, float)) for v in vals):
        return "<f8"
    if vals and all(isinstance(v, tuple) for v in vals):
        return ("<f8", (2,))
    width = max([len(str(v)) for v in vals] or [1])
    return f"<U{max(1, width)}"


def FeatureClassToNumPyArray(in_table, field_names, where_clause=None, spatial_reference=None,  # noqa: N802
                             explode_to_points=False, skip_nulls=False, null_value=None):
    import numpy as np
    with SearchCursor(in_table, field_names, where_clause, spatial_reference, explode_to_points) as cur:
        names = list(cur.fields)
        rows = []
        for row in cur:
            if skip_nulls and any(v is None for v in row):
                continue
            if null_value is not None:
                row = tuple((null_value.get(n) if isinstance(null_value, dict) else null_value) if v is None else v
                            for n, v in zip(names, row))
            rows.append(tuple(row))
    cols = list(zip(*rows)) if rows else [[] for _ in names]
    dtype = [(n, _np_dtype(c)) for n, c in zip(names, cols)]
    fixed = [tuple((0 if v is None and d[1] in ("<i8", "<f8", "?") else "" if v is None and
                    str(d[1]).startswith("<U") else v) for v, d in zip(r, dtype)) for r in rows]
    return np.array(fixed, dtype=dtype)


TableToNumPyArray = FeatureClassToNumPyArray
NumPyArrayToTable = missing_callable("da.NumPyArrayToTable")
NumPyArrayToFeatureClass = missing_callable("da.NumPyArrayToFeatureClass")
ExtendTable = missing_callable("da.ExtendTable")


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return missing_callable(f"da.{name}")


del ExecuteError
