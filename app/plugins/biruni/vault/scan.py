"""Reading folders and files for the Vault. Plain Python and GDAL only (no widgets, no project), so every function
here can run in the worker thread. Each one returns plain data (dicts and lists)."""
import datetime
import hashlib
import os

from .kinds import DATA_KINDS, IMAGE_EXT, SEAL_SUFFIXES, SIDECAR_EXT, file_kind

SKIP_DIRS = {"__pycache__", ".git", ".svn", "node_modules", "cache", ".cache", "$recycle.bin", "system volume information"}
MAX_FILES = 4000
MAX_DEPTH = 6
_gdal_ready = []


def _gdal():
    from osgeo import gdal, ogr, osr

    if not _gdal_ready:
        gdal.UseExceptions()
        _gdal_ready.append(True)
    return gdal, ogr, osr


def sidecars(path):
    """The files a dataset is made of (a shapefile is several files; a seal is not part of it)."""
    base, ext = os.path.splitext(path)
    folder = os.path.dirname(path) or "."
    if ext.lower() not in (".shp", ".tif", ".tiff", ".jpg", ".png", ".img", ".mif", ".tab"):
        return [path]
    stem = os.path.basename(base).lower()
    name = os.path.basename(path).lower()
    out = []
    try:
        for f in os.listdir(folder):
            low = f.lower()
            if low.endswith(SEAL_SUFFIXES):
                continue
            s, e = os.path.splitext(low)
            if low == name or low.startswith(name + ".") or (s == stem and (e in SIDECAR_EXT or e == ".dbf")):
                out.append(os.path.join(folder, f))
    except OSError:
        return [path]
    return out or [path]


def dataset_size(path):
    try:
        if os.path.isdir(path):
            return sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)
        return sum(os.path.getsize(f) for f in sidecars(path) if os.path.isfile(f))
    except OSError:
        return None


def file_record(path, kind, root=None, st=None):
    try:
        st = st or os.stat(path)
    except OSError:
        return None
    size = dataset_size(path) if kind in ("vector", "raster") else st.st_size
    rec = {"path": os.path.normpath(path), "name": os.path.basename(path), "kind": kind, "size": size,
           "mtime": st.st_mtime, "ctime": getattr(st, "st_ctime", st.st_mtime),
           "sealed": os.path.exists(path + ".seal.json")}
    if root:
        try:
            rec["rel"] = os.path.relpath(os.path.dirname(path), root).replace("\\", "/")
        except ValueError:
            rec["rel"] = ""
        if rec["rel"] == ".":
            rec["rel"] = ""
    return rec


def scan_folder(folder, max_files=MAX_FILES, max_depth=MAX_DEPTH, exclude=()):
    """The files of a folder (and its sub-folders) that the Vault shows: [{path, name, kind, size, mtime, rel, …}].
    exclude: folders not to enter (the field photos folder: its pictures have their own card)."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    excl = {os.path.normcase(os.path.abspath(e)) for e in exclude if e}
    stack = [(os.path.abspath(folder), 0)]
    while stack and len(out) < max_files:
        here, depth = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        names = {e.name.lower() for e in entries}
        for e in sorted(entries, key=lambda x: x.name.lower()):
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir:
                low = e.name.lower()
                if low.endswith(".gdb"):  # a File Geodatabase is one dataset
                    rec = file_record(e.path, "vector", folder)
                    if rec:
                        out.append(rec)
                elif (depth < max_depth and not low.startswith(".") and low not in SKIP_DIRS
                      and os.path.normcase(e.path) not in excl):
                    stack.append((e.path, depth + 1))
                continue
            kind = file_kind(e.path, names)
            if kind is None:
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            rec = file_record(e.path, kind, folder, st)
            if rec:
                if os.path.exists(e.path + ".seal.json"):
                    rec["sealed"] = True
                out.append(rec)
            if len(out) >= max_files:
                break
    return out


# ---- facts of one file (GDAL / OGR) ----------------------------------------------------------------------------
def _srs_facts(srs):
    if srs is None:
        return {"crs": "", "crs_name": "", "projected": None}
    auth = ""
    try:
        name, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
        if name and code:
            auth = f"{name}:{code}"
    except Exception:  # noqa: BLE001
        pass
    try:
        desc = srs.GetName() or ""
    except Exception:  # noqa: BLE001
        desc = ""
    return {"crs": auth or desc or "?", "crs_name": desc, "projected": bool(srs.IsProjected())}


def _geom_name(ogr, gtype):
    try:
        return ogr.GeometryTypeToName(gtype)
    except Exception:  # noqa: BLE001
        return ""


def vector_facts(path, max_count=2_000_000):
    """{layers: [{name, count, geometry, crs, crs_name, projected, fields: [(name, type)], extent}]}"""
    gdal, ogr, _osr = _gdal()
    out = {"layers": [], "encoding": ""}
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
    except RuntimeError as e:
        out["error"] = str(e)
        return out
    if ds is None:
        out["error"] = "cannot open"
        return out
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(i)
        if lyr is None:
            continue
        defn = lyr.GetLayerDefn()
        fields = [(defn.GetFieldDefn(k).GetName(), defn.GetFieldDefn(k).GetTypeName())
                  for k in range(defn.GetFieldCount())]
        try:
            count = lyr.GetFeatureCount(force=1)
        except RuntimeError:
            count = None
        if count is not None and count < 0:
            count = None
        gt = lyr.GetGeomType()
        info = {"name": lyr.GetName(), "count": count, "geometry": _geom_name(ogr, gt) if gt else "",
                "fields": fields, "has_geometry": gt != ogr.wkbNone}
        info.update(_srs_facts(lyr.GetSpatialRef()) if gt != ogr.wkbNone else
                    {"crs": "", "crs_name": "", "projected": None})
        try:
            if gt != ogr.wkbNone and count:
                info["extent"] = lyr.GetExtent()
        except RuntimeError:
            pass
        out["layers"].append(info)
    if path.lower().endswith(".shp"):
        cpg = os.path.splitext(path)[0] + ".cpg"
        if os.path.exists(cpg):
            try:
                with open(cpg, encoding="ascii", errors="ignore") as f:
                    out["encoding"] = f.read().strip()
            except OSError:
                pass
    try:  # raster tables of a GeoPackage
        if path.lower().endswith(".gpkg"):
            rds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
            if rds is not None:
                subs = rds.GetSubDatasets() or ([(path, rds.GetDescription())] if rds.RasterCount else [])
                out["rasters"] = [s[0].split(":")[-1] for s in subs]
    except RuntimeError:
        pass
    return out


def raster_facts(path):
    gdal, _ogr, osr = _gdal()
    try:
        ds = gdal.Open(path)
    except RuntimeError as e:
        return {"error": str(e)}
    if ds is None:
        return {"error": "cannot open"}
    srs = None
    wkt = ds.GetProjection()
    if wkt:
        srs = osr.SpatialReference()
        try:
            srs.ImportFromWkt(wkt)
        except RuntimeError:
            srs = None
    gt = ds.GetGeoTransform(can_return_null=True)
    out = {"width": ds.RasterXSize, "height": ds.RasterYSize, "bands": ds.RasterCount,
           "pixel": abs(gt[1]) if gt else None}
    out.update(_srs_facts(srs))
    if gt:
        out["extent"] = (gt[0], gt[0] + gt[1] * ds.RasterXSize, gt[3] + gt[5] * ds.RasterYSize, gt[3])
    return out


def table_facts(path):
    low = path.lower()
    if low.endswith((".csv", ".tsv")):
        try:
            import csv

            with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
                reader = csv.reader(f, delimiter="\t" if low.endswith(".tsv") else ",")
                header = next(reader, [])
                n = sum(1 for _ in reader)
            return {"layers": [{"name": os.path.basename(path), "count": n, "geometry": "", "fields":
                                [(h, "") for h in header], "has_geometry": False, "crs": "", "crs_name": "",
                                "projected": None}]}
        except OSError as e:
            return {"error": str(e)}
    return vector_facts(path)


def file_facts(path, kind):
    """Facts of a dataset by kind (vector / CAD / database / raster / table); {} for others."""
    if not os.path.exists(path):
        return {"missing": True}
    try:
        if kind in ("vector", "cad", "database"):
            return vector_facts(path)
        if kind == "raster":
            return raster_facts(path)
        if kind == "table":
            return table_facts(path)
    except Exception as e:  # noqa: BLE001 - a broken file is shown as such, never stops the pane
        return {"error": str(e)}
    return {}


# ---- the field photos folder ----------------------------------------------------------------------------------
def photo_folder_facts(folder, strip=6):
    """{count, last (timestamp), today (count), latest: [paths, newest first]} of a field photos folder."""
    out = {"count": 0, "last": None, "today": 0, "latest": [], "folder": folder}
    if not folder or not os.path.isdir(folder):
        return out
    pics = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if os.path.splitext(f)[1].lower() in IMAGE_EXT:
                p = os.path.join(root, f)
                try:
                    pics.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    pics.sort(reverse=True)
    today = datetime.date.today()
    out["count"] = len(pics)
    out["today"] = sum(1 for t, _p in pics if datetime.date.fromtimestamp(t) == today)
    if pics:
        out["last"] = pics[0][0]
    out["latest"] = [p for _t, p in pics[:strip]]
    gpkg = os.path.join(folder, "field_photos.gpkg")
    if os.path.exists(gpkg):
        out["gpkg"] = gpkg
        f = vector_facts(gpkg)
        out["points"] = sum((ly.get("count") or 0) for ly in f.get("layers", []) if ly["name"] == "field_photos")
    return out


# ---- duplicates ---------------------------------------------------------------------------------------------
def file_hash(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def find_duplicates(path, folders, max_files=50000):
    """Other files with the same size and the same content as ``path`` in the folders (and their sub-folders)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    me = os.path.normcase(os.path.abspath(path))
    same_size, seen, n = [], set(), 0
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                n += 1
                if n > max_files:
                    break
                p = os.path.join(root, f)
                key = os.path.normcase(os.path.abspath(p))
                if key == me or key in seen:
                    continue
                seen.add(key)
                try:
                    if os.path.getsize(p) == size:
                        same_size.append(p)
                except OSError:
                    pass
    if not same_size:
        return []
    try:
        mine = file_hash(path)
    except OSError:
        return []
    out = []
    for p in same_size:
        try:
            if file_hash(p) == mine:
                out.append(os.path.normpath(p))
        except OSError:
            pass
    return out


def is_data(kind):
    return kind in DATA_KINDS
