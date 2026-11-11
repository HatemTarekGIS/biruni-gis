"""arcpy.sa basics: Raster objects with map algebra (numpy), Slope / Aspect / Hillshade (GDAL DEM tools),
Con, SetNull, IsNull, math functions, Reclassify, ExtractByMask, CellStatistics."""
import math
import os
import re

import numpy as np
from osgeo import gdal, osr

from . import data as D
from . import geometry as G
from ._base import ExecuteError, env, fail, missing_callable, note_output, temp_path, unsupported

_F_NODATA = -3.4028234663852886e38
_I_NODATA = -2147483648


def raster_path(value):
    """A GDAL-openable path for a Raster object or a catalog path (in-memory rasters are written to temp)."""
    if isinstance(value, Raster):
        return value._ensure_file()
    if hasattr(value, "getOutput") and not isinstance(value, str):
        return raster_path(value.getOutput(0))
    p = D.full_path(value)
    if not os.path.exists(p):
        for ext in (".tif", ".img"):
            if os.path.exists(p + ext):
                return p + ext
        fail(732, f"Input raster: Dataset {value} does not exist or is not supported")
    return p


class Raster:
    """arcpy.Raster: a raster dataset that also supports map algebra (+ - * / > < & | ~ ...)."""

    def __init__(self, inRaster, is_multidimensional=False):  # noqa: N803
        if isinstance(inRaster, Raster):
            self.__dict__.update(inRaster.__dict__)
            return
        if isinstance(inRaster, (list, tuple)):
            unsupported("sa.Raster of a raster list")
        self._path = raster_path(inRaster)
        ds = gdal.Open(self._path)
        if ds is None:
            fail(732, f"Input raster: Dataset {inRaster} does not exist or is not supported")
        self._gt = ds.GetGeoTransform()
        self._wkt = ds.GetProjection()
        self._shape = (ds.RasterYSize, ds.RasterXSize)
        self._bands = ds.RasterCount
        b = ds.GetRasterBand(1)
        self._nodata = b.GetNoDataValue()
        self._dtype = gdal.GetDataTypeName(b.DataType)
        self._arr = None
        self._temp = False

    @classmethod
    def _from_array(cls, arr, gt, wkt):
        r = cls.__new__(cls)
        r._path = None
        r._gt = tuple(gt)
        r._wkt = wkt
        r._arr = np.ma.masked_invalid(arr) if not np.ma.isMaskedArray(arr) else arr
        r._shape = r._arr.shape
        r._bands = 1
        r._nodata = None
        r._dtype = "Float32" if r._arr.dtype.kind == "f" else "Int32"
        r._temp = True
        return r

    # data ------------------------------------------------------------------
    def _read(self):
        if self._arr is None:
            ds = gdal.Open(self._path)
            b = ds.GetRasterBand(1)
            a = b.ReadAsArray()
            mask = np.zeros(a.shape, bool)
            nd = b.GetNoDataValue()
            if nd is not None:
                mask |= (a == nd) if not (isinstance(nd, float) and math.isnan(nd)) else np.isnan(a)
            if a.dtype.kind == "f":
                mask |= np.isnan(a)
            self._arr = np.ma.array(a, mask=mask)
        return self._arr

    def _ensure_file(self):
        if self._path is None:
            self._path = temp_path(".tif")
            self._write(self._path)
        return self._path

    def _write(self, path):
        arr = self._read()
        is_float = arr.dtype.kind == "f"
        if arr.dtype == np.bool_:
            arr = arr.astype(np.int32)
        gdt = gdal.GDT_Float32 if is_float else gdal.GDT_Int32
        nd = _F_NODATA if is_float else _I_NODATA
        ext = os.path.splitext(path)[1].lower()
        drv = {".img": "HFA", ".asc": "AAIGrid"}.get(ext, "GTiff")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if drv == "AAIGrid":
            mem = gdal.GetDriverByName("MEM").Create("", arr.shape[1], arr.shape[0], 1, gdt)
            target = mem
        else:
            target = gdal.GetDriverByName(drv).Create(path, arr.shape[1], arr.shape[0], 1, gdt,
                                                      ["COMPRESS=LZW"] if drv == "GTiff" else [])
        target.SetGeoTransform(self._gt)
        if self._wkt:
            target.SetProjection(self._wkt)
        band = target.GetRasterBand(1)
        band.SetNoDataValue(nd)
        band.WriteArray(np.ma.filled(arr.astype(np.float32 if is_float else np.int32), nd))
        band.FlushCache()
        if drv == "AAIGrid":
            gdal.GetDriverByName("AAIGrid").CreateCopy(path, target)
        target = None

    def save(self, name=None):
        if name in (None, ""):
            if self._path and not self._temp:
                return
            ws = env.workspace or env.scratchFolder
            name = os.path.join(ws if not str(ws).lower().endswith(".gdb") else env.scratchFolder,
                                "raster_out.tif")
        ref = D.check_output(name, "raster")
        if ref.kind != "raster":
            unsupported("sa.Raster.save to a geodatabase", "Save rasters as .tif files in a folder.")
        self._write(ref.container)
        self._path = ref.container
        self._temp = False
        note_output(ref.container)

    # properties --------------------------------------------------------------
    @property
    def catalogPath(self):  # noqa: N802
        return self._ensure_file()

    @property
    def name(self):
        return os.path.basename(self._path) if self._path else "raster"

    @property
    def path(self):
        return os.path.dirname(self._path) if self._path else ""

    @property
    def isTemporary(self):  # noqa: N802
        return bool(self._temp)

    @property
    def width(self):
        return self._shape[1]

    @property
    def height(self):
        return self._shape[0]

    @property
    def bandCount(self):  # noqa: N802
        return self._bands

    @property
    def meanCellWidth(self):  # noqa: N802
        return abs(self._gt[1])

    @property
    def meanCellHeight(self):  # noqa: N802
        return abs(self._gt[5])

    @property
    def extent(self):
        x0, y0 = self._gt[0], self._gt[3]
        x1, y1 = x0 + self._gt[1] * self.width, y0 + self._gt[5] * self.height
        return G.Extent(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), spatial_reference=self.spatialReference)

    @property
    def spatialReference(self):  # noqa: N802
        if not self._wkt:
            return G.SpatialReference()
        s = osr.SpatialReference()
        s.ImportFromWkt(self._wkt)
        return G.SpatialReference._from_osr(s)

    @property
    def noDataValue(self):  # noqa: N802
        if self._temp:
            return _F_NODATA if self._read().dtype.kind == "f" else _I_NODATA
        return self._nodata

    @property
    def pixelType(self):  # noqa: N802
        return {"Byte": "U8", "Int16": "S16", "UInt16": "U16", "Int32": "S32", "UInt32": "U32", "Float32": "F32",
                "Float64": "F64"}.get(self._dtype, self._dtype)

    @property
    def isInteger(self):  # noqa: N802
        return not self._dtype.startswith("Float")

    def _stat(self, fn):
        a = self._read()
        if a.count() == 0:
            return None
        return float(fn(a))

    @property
    def minimum(self):
        return self._stat(np.ma.min)

    @property
    def maximum(self):
        return self._stat(np.ma.max)

    @property
    def mean(self):
        return self._stat(np.ma.mean)

    @property
    def standardDeviation(self):  # noqa: N802
        return self._stat(np.ma.std)

    @property
    def format(self):
        return "TIFF"

    @property
    def hasRAT(self):  # noqa: N802
        return False

    @property
    def uncompressedSize(self):  # noqa: N802
        return self.width * self.height * 4

    def read(self, *a, **k):
        return np.ma.filled(self._read().astype(float), np.nan)

    def __str__(self):
        return self._ensure_file()

    def __fspath__(self):
        return self._ensure_file()

    def __repr__(self):
        return f"<Raster '{self._path or 'in memory'}'>"

    # map algebra ---------------------------------------------------------
    def _aligned(self, other):
        """other (Raster or number) as a masked array on this raster's grid."""
        if isinstance(other, (int, float, np.integer, np.floating, bool)):
            return other
        if not isinstance(other, Raster):
            other = Raster(other)
        if other._shape == self._shape and tuple(other._gt) == tuple(self._gt):
            return other._read()
        src = gdal.Open(other._ensure_file())
        x0, y0 = self._gt[0], self._gt[3] + self._gt[5] * self.height
        x1, y1 = self._gt[0] + self._gt[1] * self.width, self._gt[3]
        ds = gdal.Warp("", src, format="MEM", outputBounds=(x0, y0, x1, y1), width=self.width, height=self.height,
                       dstSRS=self._wkt or None, dstNodata=_F_NODATA, outputType=gdal.GDT_Float64)
        a = ds.GetRasterBand(1).ReadAsArray()
        return np.ma.masked_equal(a, _F_NODATA)

    def _op(self, other, fn, cast_bool=False, reverse=False):
        a = self._read()
        b = self._aligned(other)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            res = fn(b, a) if reverse else fn(a, b)
        res = np.ma.masked_invalid(res) if res.dtype.kind == "f" else res
        if cast_bool:
            res = res.astype(np.int32)
        return Raster._from_array(res, self._gt, self._wkt)

    def __add__(self, o):
        return self._op(o, np.ma.add)

    def __radd__(self, o):
        return self._op(o, np.ma.add, reverse=True)

    def __sub__(self, o):
        return self._op(o, np.ma.subtract)

    def __rsub__(self, o):
        return self._op(o, np.ma.subtract, reverse=True)

    def __mul__(self, o):
        return self._op(o, np.ma.multiply)

    def __rmul__(self, o):
        return self._op(o, np.ma.multiply, reverse=True)

    def __truediv__(self, o):
        return self._op(o, lambda x, y: np.ma.divide(np.ma.asarray(x, dtype=float), y))

    def __rtruediv__(self, o):
        return self._op(o, lambda x, y: np.ma.divide(np.ma.asarray(x, dtype=float), y), reverse=True)

    def __floordiv__(self, o):
        return self._op(o, np.ma.floor_divide)

    def __mod__(self, o):
        return self._op(o, np.ma.mod)

    def __pow__(self, o):
        return self._op(o, lambda x, y: np.ma.power(np.ma.asarray(x, dtype=float), y))

    def __rpow__(self, o):
        return self._op(o, lambda x, y: np.ma.power(np.ma.asarray(x, dtype=float), y), reverse=True)

    def __neg__(self):
        return Raster._from_array(-self._read(), self._gt, self._wkt)

    def __pos__(self):
        return self

    def __abs__(self):
        return Raster._from_array(np.ma.abs(self._read()), self._gt, self._wkt)

    def __gt__(self, o):
        return self._op(o, np.ma.greater, True)

    def __ge__(self, o):
        return self._op(o, np.ma.greater_equal, True)

    def __lt__(self, o):
        return self._op(o, np.ma.less, True)

    def __le__(self, o):
        return self._op(o, np.ma.less_equal, True)

    def __eq__(self, o):
        return self._op(o, np.ma.equal, True)

    def __ne__(self, o):
        return self._op(o, np.ma.not_equal, True)

    def __and__(self, o):
        return self._op(o, lambda x, y: np.ma.logical_and(x != 0, np.ma.asarray(y) != 0), True)

    __rand__ = __and__

    def __or__(self, o):
        return self._op(o, lambda x, y: np.ma.logical_or(x != 0, np.ma.asarray(y) != 0), True)

    __ror__ = __or__

    def __xor__(self, o):
        return self._op(o, lambda x, y: np.ma.logical_xor(x != 0, np.ma.asarray(y) != 0), True)

    def __invert__(self):
        return Raster._from_array((self._read() == 0).astype(np.int32), self._gt, self._wkt)

    __hash__ = object.__hash__


def _r(value):
    return value if isinstance(value, Raster) else Raster(value)


def _unary(value, fn):
    r = _r(value)
    with np.errstate(divide="ignore", invalid="ignore"):
        res = fn(np.ma.asarray(r._read(), dtype=float))
    return Raster._from_array(np.ma.masked_invalid(res), r._gt, r._wkt)


def Abs(in_raster_or_constant):  # noqa: N802
    return abs(_r(in_raster_or_constant))


def Sqrt(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.sqrt)


def Square(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, lambda a: a * a)


def Exp(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.exp)


def Ln(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.log)


def Log10(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.log10)


def Log2(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.log2)


def Sin(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.sin)


def Cos(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.cos)


def Tan(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.tan)


def Power(in_raster_or_constant1, in_raster_or_constant2):  # noqa: N802
    return _r(in_raster_or_constant1) ** in_raster_or_constant2


def Int(in_raster_or_constant):  # noqa: N802
    r = _r(in_raster_or_constant)
    a = r._read()
    return Raster._from_array(np.ma.array(np.trunc(np.ma.filled(a, 0)).astype(np.int32),
                                          mask=np.ma.getmaskarray(a)), r._gt, r._wkt)


def Float(in_raster_or_constant):  # noqa: N802
    r = _r(in_raster_or_constant)
    return Raster._from_array(r._read().astype(np.float32), r._gt, r._wkt)


def RoundUp(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.ceil)


def RoundDown(in_raster_or_constant):  # noqa: N802
    return _unary(in_raster_or_constant, np.ma.floor)


def Plus(a, b):  # noqa: N802
    return _r(a) + b


def Minus(a, b):  # noqa: N802
    return _r(a) - b


def Times(a, b):  # noqa: N802
    return _r(a) * b


def Divide(a, b):  # noqa: N802
    return _r(a) / b


def IsNull(in_raster):  # noqa: N802
    r = _r(in_raster)
    a = r._read()
    return Raster._from_array(np.ma.array(np.ma.getmaskarray(a).astype(np.int32)), r._gt, r._wkt)


def _where(raster, where_clause):
    """'VALUE > 5 AND VALUE < 9' evaluated on a raster -> boolean masked array."""
    a = raster._read()
    if not where_clause:
        return a != 0
    expr = str(where_clause)
    expr = re.sub(r'"?\bVALUE\b"?', "__v", expr, flags=re.I)
    expr = re.sub(r"<>", "!=", expr)
    expr = re.sub(r"(?<![<>!=])=(?!=)", "==", expr)
    expr = re.sub(r"\bAND\b", "&", expr, flags=re.I)
    expr = re.sub(r"\bOR\b", "|", expr, flags=re.I)
    expr = re.sub(r"\bNOT\b", "~", expr, flags=re.I)
    expr = re.sub(r"(__v\s*[<>=!]=?\s*[-\d.eE]+)", r"(\1)", expr)
    if re.search(r"[A-Za-z_]\w*", expr.replace("__v", "")):
        unsupported("sa where clause", f"Only VALUE comparisons are supported: {where_clause}")
    return eval(expr, {"__builtins__": {}}, {"__v": a})  # noqa: S307 - a VALUE comparison only


def _value(v, ref):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    return ref._aligned(_r(v))


def Con(in_conditional_raster_or_constant, in_true_raster_or_constant, in_false_raster_or_constant=None,  # noqa
        where_clause=None):
    cond_r = _r(in_conditional_raster_or_constant)
    cond = _where(cond_r, where_clause)
    t = _value(in_true_raster_or_constant, cond_r)
    f = _value(in_false_raster_or_constant, cond_r)
    shape = cond_r._shape
    tv = np.ma.asarray(np.broadcast_to(t, shape)) if np.isscalar(t) else t
    fv = (np.ma.masked_all(shape) if f is None else
          np.ma.asarray(np.broadcast_to(f, shape)) if np.isscalar(f) else f)
    c = np.ma.filled(cond, False).astype(bool)
    out = np.ma.where(c, tv, fv)
    out = np.ma.array(out, mask=np.ma.getmaskarray(cond) | np.where(c, np.ma.getmaskarray(tv),
                                                                    np.ma.getmaskarray(fv)))
    if all(isinstance(x, (int, np.integer)) or (isinstance(x, np.ndarray) and x.dtype.kind in "iu")
           for x in (t, f) if x is not None):
        out = out.astype(np.int32)
    return Raster._from_array(out, cond_r._gt, cond_r._wkt)


def SetNull(in_conditional_raster, in_false_raster_or_constant, where_clause=None):  # noqa: N802
    cond_r = _r(in_conditional_raster)
    cond = np.ma.filled(_where(cond_r, where_clause), True).astype(bool)
    f = _value(in_false_raster_or_constant, cond_r)
    fv = np.ma.asarray(np.broadcast_to(f, cond_r._shape)).copy() if np.isscalar(f) else f.copy()
    out = np.ma.array(fv, mask=cond | np.ma.getmaskarray(fv))
    return Raster._from_array(out, cond_r._gt, cond_r._wkt)


class RemapValue:
    def __init__(self, remapTable=None):  # noqa: N803
        self.remapTable = [list(x) for x in (remapTable or [])]


class RemapRange:
    def __init__(self, remapTable=None):  # noqa: N803
        self.remapTable = [list(x) for x in (remapTable or [])]


def Reclassify(in_raster, reclass_field, remap, missing_values="DATA"):  # noqa: N802
    r = _r(in_raster)
    a = r._read()
    out = np.ma.masked_all(a.shape, dtype=np.int32)
    done = np.zeros(a.shape, bool)
    table = remap.remapTable if hasattr(remap, "remapTable") else [
        [float(x) for x in item.split()] for item in str(remap).split(";") if item.strip()]
    ranged = isinstance(remap, RemapRange) or (table and len(table[0]) == 3)
    data = np.ma.filled(a, np.nan).astype(float)
    for i, row in enumerate(table):
        if ranged:
            lo, hi, new = row
            sel = (data >= float(lo)) & ((data < float(hi)) if i < len(table) - 1 else (data <= float(hi)))
            if i > 0:
                sel |= (data == float(lo)) & ~done
            sel &= ~done
        else:
            old, new = row
            if str(old).upper() == "NODATA":
                sel = np.ma.getmaskarray(a) & ~done
            else:
                sel = (data == float(old)) & ~done
        if str(new).upper() == "NODATA":
            done |= sel
            continue
        out[sel] = int(new)
        done |= sel
    if str(missing_values).upper() == "DATA":
        keep = ~done & ~np.ma.getmaskarray(a)
        out[keep] = np.ma.filled(a, 0)[keep].astype(np.int32)
    return Raster._from_array(out, r._gt, r._wkt)


def _dem(in_raster, mode, **kw):
    path = raster_path(in_raster)
    src = gdal.Open(path)
    srs = src.GetSpatialRef()
    if srs is not None and srs.IsGeographic() and "scale" not in kw and mode != "aspect":
        kw["scale"] = 111120.0
    ds = gdal.DEMProcessing("", src, mode, format="MEM", computeEdges=True, **kw)
    if ds is None:
        fail(999999, f"{mode} failed")
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype(np.float32)
    nd = b.GetNoDataValue()
    mask = (a == nd) if nd is not None else np.zeros(a.shape, bool)
    return Raster._from_array(np.ma.array(a, mask=mask), ds.GetGeoTransform(), ds.GetProjection()), src


def Slope(in_raster, output_measurement="DEGREE", z_factor=1, method="PLANAR", z_unit="METER",  # noqa: N802
          analysis_target_device=None):
    fmt = "percent" if str(output_measurement).upper().startswith("PERCENT") else "degree"
    kw = {"slopeFormat": fmt}
    zf = float(z_factor or 1)
    src = gdal.Open(raster_path(in_raster))
    srs = src.GetSpatialRef()
    if srs is not None and srs.IsGeographic():
        kw["scale"] = 111120.0 / zf
    elif zf != 1:
        kw["scale"] = 1.0 / zf
    return _dem(in_raster, "slope", **kw)[0]


def Aspect(in_raster, method="PLANAR", z_unit="METER", project_geodesic_azimuths=None,  # noqa: N802
           analysis_target_device=None):
    r, src = _dem(in_raster, "aspect", zeroForFlat=False)
    a = r._read()
    src_a = Raster(raster_path(in_raster))._read()
    flat = np.ma.getmaskarray(a) & ~np.ma.getmaskarray(src_a)
    out = np.ma.array(np.ma.filled(a, -1.0), mask=np.ma.getmaskarray(src_a))
    out[flat] = -1.0
    return Raster._from_array(out, r._gt, r._wkt)


def Hillshade(in_raster, azimuth=315, altitude=45, model_shadows="NO_SHADOWS", z_factor=1):  # noqa: N802
    kw = {"azimuth": float(azimuth), "altitude": float(altitude), "zFactor": float(z_factor or 1)}
    src = gdal.Open(raster_path(in_raster))
    srs = src.GetSpatialRef()
    if srs is not None and srs.IsGeographic():
        kw["scale"] = 111120.0
    r = _dem(in_raster, "hillshade", **kw)[0]
    return Int(r)


def ExtractByMask(in_raster, in_mask_data, extraction_area="INSIDE", analysis_extent=None):  # noqa: N802
    r = _r(in_raster)
    mref = D.parse(in_mask_data) if D.as_layer(in_mask_data) is None else None
    if mref is not None and mref.kind == "raster" or isinstance(in_mask_data, Raster):
        m = r._aligned(_r(in_mask_data))
        inside = ~np.ma.getmaskarray(m)
    else:
        path, lname = D.materialize(in_mask_data)
        mem = gdal.GetDriverByName("MEM").Create("", r.width, r.height, 1, gdal.GDT_Byte)
        mem.SetGeoTransform(r._gt)
        if r._wkt:
            mem.SetProjection(r._wkt)
        vds = gdal.OpenEx(path, gdal.OF_VECTOR)
        lyr = vds.GetLayerByName(lname) if lname else vds.GetLayer(0)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
        inside = mem.GetRasterBand(1).ReadAsArray().astype(bool)
    if str(extraction_area).upper() == "OUTSIDE":
        inside = ~inside
    a = r._read()
    out = np.ma.array(a.data.copy(), mask=np.ma.getmaskarray(a) | ~inside)
    return Raster._from_array(out, r._gt, r._wkt)


def ExtractByAttributes(in_raster, where_clause):  # noqa: N802
    r = _r(in_raster)
    cond = np.ma.filled(_where(r, where_clause), False).astype(bool)
    a = r._read()
    return Raster._from_array(np.ma.array(a.data.copy(), mask=np.ma.getmaskarray(a) | ~cond), r._gt, r._wkt)


def CellStatistics(in_rasters_or_constants, statistics_type="MEAN", ignore_nodata="DATA",  # noqa: N802
                   process_as_multiband=None, percentile_value=None, percentile_interpolation_type=None):
    items = list(in_rasters_or_constants)
    ref = next((_r(x) for x in items if not isinstance(x, (int, float))), None)
    if ref is None:
        fail(735, "Input rasters: Value is required")
    stack = np.ma.stack([np.ma.asarray(np.broadcast_to(ref._aligned(x), ref._shape)) if isinstance(x, (int, float))
                         else ref._aligned(_r(x)) for x in items])
    st = str(statistics_type).upper()
    fn = {"MEAN": np.ma.mean, "MAJORITY": None, "MAXIMUM": np.ma.max, "MEDIAN": np.ma.median, "MINIMUM": np.ma.min,
          "MINORITY": None, "RANGE": lambda s, axis: np.ma.max(s, axis=axis) - np.ma.min(s, axis=axis),
          "STD": np.ma.std, "SUM": np.ma.sum, "VARIETY": None}.get(st)
    if fn is None:
        unsupported(f"sa.CellStatistics {statistics_type}")
    if str(ignore_nodata).upper() == "NODATA":
        stack = np.ma.array(stack, mask=np.broadcast_to(np.ma.getmaskarray(stack).any(axis=0), stack.shape))
    return Raster._from_array(fn(stack, axis=0), ref._gt, ref._wkt)


def RasterToNumPyArray(in_raster, lower_left_corner=None, ncols=None, nrows=None,  # noqa: N802
                       nodata_to_value=None):
    """arcpy.RasterToNumPyArray: band 1 as 2-D array (all bands as 3-D when there are several)."""
    ds = gdal.Open(raster_path(in_raster))
    gt = ds.GetGeoTransform()
    x0 = y0 = 0
    if lower_left_corner is not None:
        px = lower_left_corner.X if hasattr(lower_left_corner, "X") else lower_left_corner[0]
        py = lower_left_corner.Y if hasattr(lower_left_corner, "Y") else lower_left_corner[1]
        x0 = max(0, int(round((px - gt[0]) / gt[1])))
        bottom = int(round((py - gt[3]) / gt[5]))
        rows = int(nrows or bottom)
        y0 = max(0, bottom - rows)
    w = int(ncols or ds.RasterXSize - x0)
    h = int(nrows or ds.RasterYSize - y0)
    w, h = min(w, ds.RasterXSize - x0), min(h, ds.RasterYSize - y0)
    arrays = []
    for i in range(1, ds.RasterCount + 1):
        b = ds.GetRasterBand(i)
        a = b.ReadAsArray(x0, y0, w, h)
        nd = b.GetNoDataValue()
        if nodata_to_value is not None and nd is not None:
            if a.dtype.kind != "f" and isinstance(nodata_to_value, float) and not float(nodata_to_value).is_integer():
                a = a.astype(float)
            a = np.where(a == nd, nodata_to_value, a)
        arrays.append(a)
    return arrays[0] if len(arrays) == 1 else np.stack(arrays)


def NumPyArrayToRaster(in_array, lower_left_corner=None, x_cell_size=None, y_cell_size=None,  # noqa: N802
                       value_to_nodata=None, nodata_to_value=None):
    a = np.asarray(in_array)
    if a.ndim == 3:
        a = a[0]
    xc = float(x_cell_size or 1.0)
    yc = float(y_cell_size or xc)
    if lower_left_corner is None:
        llx, lly = 0.0, 0.0
    else:
        llx = lower_left_corner.X if hasattr(lower_left_corner, "X") else lower_left_corner[0]
        lly = lower_left_corner.Y if hasattr(lower_left_corner, "Y") else lower_left_corner[1]
    gt = (llx, xc, 0.0, lly + a.shape[0] * yc, 0.0, -yc)
    mask = (a == value_to_nodata) if value_to_nodata is not None else np.zeros(a.shape, bool)
    if a.dtype.kind == "f":
        mask |= np.isnan(a)
    sr = G.osr_of(env.outputCoordinateSystem)
    return Raster._from_array(np.ma.array(a, mask=mask), gt, sr.ExportToWkt() if sr is not None else "")


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return missing_callable(f"sa.{name}")


__all__ = ["Raster", "Slope", "Aspect", "Hillshade", "Con", "SetNull", "IsNull", "Abs", "Sqrt", "Square", "Exp",
           "Ln", "Log10", "Log2", "Sin", "Cos", "Tan", "Power", "Int", "Float", "RoundUp", "RoundDown", "Plus",
           "Minus", "Times", "Divide", "Reclassify", "RemapValue", "RemapRange", "ExtractByMask",
           "ExtractByAttributes", "CellStatistics"]

del ExecuteError
