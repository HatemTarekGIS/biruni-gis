"""Recognising what a file, folder or web address holds, by itself: one plain dict ("item") per layer that can be
added. Everything here runs in the background thread (no widgets, no project): the engine's own
``querySublayers`` says what is inside, GDAL reads the cheap facts (coordinate system, size), and CSV / text tables
are sniffed here for their coordinate columns (English and Arabic names) and coordinate system.

An item:
    source      what the user added (path or address)
    path        the file (or folder) the layer comes from
    name        the layer name it gets on the map
    kind        points / lines / areas / shapes / table / spreadsheet / image / elevation / pointcloud / mesh /
                vectortile / scene / cad / gps / web / service / project / archive / unknown
    layer_type  vector / raster / mesh / pointcloud / vectortile / tiledscene / project / service / None
    provider, uri   how the engine opens it
    readable    False: shown greyed, never added
    checked     ticked by default
    count, geometry, crs, size, inside (layers in the file), container ("archive"), xy (CSV columns), note
    page        for services: the stock Data Source Manager page that adds them
"""
import csv
import os
import re

PROJECT_EXT = {".qgz", ".qgs"}
TEXT_TABLE_EXT = {".csv", ".tsv", ".txt", ".tab_csv", ".psv"}
SPREADSHEET_EXT = {".xlsx", ".xls", ".ods"}
CAD_EXT = {".dxf", ".dwg", ".dgn"}
GPS_EXT = {".gpx"}
ELEVATION_EXT = {".hgt", ".dem", ".asc", ".bil", ".dt0", ".dt1", ".dt2", ".ter"}
ARCHIVE_EXT = {".zip", ".7z", ".tar", ".tgz", ".gz", ".rar"}
# files a folder scan looks at (everything else - documents, photos, sidecars - is passed over silently)
FOLDER_EXT = {
    ".shp", ".geojson", ".json", ".kml", ".kmz", ".gml", ".tab", ".mif", ".fgb", ".gpx", ".geojsonl", ".topojson",
    ".osm", ".pbf", ".gpkg", ".sqlite", ".db", ".mbtiles", ".tif", ".tiff", ".jp2", ".img", ".vrt", ".asc", ".dem",
    ".ecw", ".sid", ".nc", ".hgt", ".bil", ".dxf", ".dwg", ".dgn", ".csv", ".tsv", ".txt", ".xlsx", ".xls", ".ods",
    ".las", ".laz", ".copc", ".e57", ".2dm", ".zip", ".qgz", ".qgs", ".dbf", ".pmtiles", ".vtpk", ".gdb",
}
SIDECAR_EXT = {".shx", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qmd", ".ovr", ".tfw", ".tifw", ".wld", ".pgw",
               ".jgw", ".aux", ".xml", ".fix", ".atx", ".ain", ".aih", ".lock", ".qgd", ".gpkg-wal", ".gpkg-shm",
               ".sqlite-journal", ".bak", ".tmp", ".pyc", ".log", ".qml", ".sld"}
FOLDER_MAX = 300
COUNT_MAX_BYTES = 64 * 1024 * 1024   # feature counts only for files smaller than this (they are cheap then)
SNIFF_BYTES = 256 * 1024
SNIFF_ROWS = 500

# draw order on the map: lower first (so points end up on top)
DRAW_ORDER = {"web": 0, "image": 1, "elevation": 2, "mesh": 3, "scene": 3, "pointcloud": 4, "vectortile": 5,
              "areas": 6, "shapes": 7, "cad": 7, "lines": 8, "gps": 9, "points": 10, "table": 11, "spreadsheet": 11}

_gdal_ready = []


def _gdal():
    from osgeo import gdal, ogr, osr

    if not _gdal_ready:
        gdal.UseExceptions()
        _gdal_ready.append(True)
    return gdal, ogr, osr


def is_url(text):
    return bool(re.match(r"^(https?|ftp)://", (text or "").strip(), re.I))


def ext_of(path):
    low = path.lower()
    for double in (".tar.gz", ".copc.laz"):
        if low.endswith(double):
            return double
    return os.path.splitext(low)[1]


def stem_of(path):
    base = os.path.basename(path.rstrip("/\\"))
    e = ext_of(base)
    return base[: -len(e)] if e and base.lower().endswith(e) else base


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def dataset_size(path):
    try:
        if os.path.isdir(path):
            return sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)
        if path.lower().endswith(".shp"):
            stem = os.path.splitext(path)[0]
            return sum(os.path.getsize(stem + e) for e in (".shp", ".shx", ".dbf", ".prj")
                       if os.path.isfile(stem + e))
        return os.path.getsize(path)
    except OSError:
        return None


def _item(source, path, **kw):
    it = {"source": source, "path": path, "name": stem_of(path) or path, "kind": "unknown", "layer_type": None,
          "provider": "", "uri": "", "readable": False, "checked": False, "count": None, "geometry": "",
          "crs": "", "size": None, "inside": 1, "container": "", "xy": None, "note": "", "page": "", "table": ""}
    it.update(kw)
    return it


def unknown(source, path, note=""):
    return _item(source, path, kind="unknown", note=note, size=dataset_size(path) if os.path.exists(path) else None)


# ---- folders ------------------------------------------------------------------------------------------------------
# data containers: shown as one item that opens onto its layers (never as a folder of raw files)
CONTAINER_EXT = {".gdb", ".gpkg", ".sqlite", ".zip", ".kmz", ".kml", ".gpx", ".dxf", ".dwg"}
SKIP_DIRS = {"__pycache__", ".git", ".svn", "$recycle.bin", "system volume information", "node_modules"}
LIST_MAX = 3000


def gdb_root(path):
    """The File Geodatabase folder a path is in or is (D:\\x\\EG.gdb\\a00000009.gdbtable → D:\\x\\EG.gdb), or ""."""
    if not path or is_url(path):
        return ""
    p = os.path.normpath(path)
    if ".gdb" not in p.lower():
        return ""
    while True:
        if p.lower().endswith(".gdb") and os.path.isdir(p):
            return p
        parent = os.path.dirname(p)
        if parent == p or not parent:
            return ""
        p = parent


def list_dir(folder, max_entries=LIST_MAX):
    """What the browser shows of a folder: {folders, containers, files, truncated} (full paths, sorted by name).
    Sidecar files, documents and pictures without a world file are left out; File Geodatabases, GeoPackages,
    archives, KML, GPX and CAD drawings are containers."""
    out = {"folders": [], "containers": [], "files": [], "truncated": False, "error": ""}
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError as e:
        out["error"] = str(e)
        return out
    lower = {e.name.lower() for e in entries}
    n = 0
    for ent in entries:
        name = ent.name
        low = name.lower()
        if low.startswith(("~$", ".")) or low in SKIP_DIRS:
            continue
        try:
            is_dir = ent.is_dir()
        except OSError:
            continue
        e = ext_of(low)
        if is_dir:
            target = out["containers"] if e == ".gdb" else out["folders"]
        else:
            if low.endswith((".aux.xml", ".shp.xml")) or e in SIDECAR_EXT:
                continue
            if e == ".dbf" and os.path.splitext(low)[0] + ".shp" in lower:
                continue
            if e in (".jpg", ".jpeg", ".png") and not any(os.path.splitext(low)[0] + w in lower
                                                          for w in (".jgw", ".pgw", ".wld", ".jpgw", ".pngw")):
                continue
            if e in CONTAINER_EXT:
                target = out["containers"]
            elif e in FOLDER_EXT or e in (".jpg", ".jpeg", ".png"):
                target = out["files"]
            else:
                continue
        if n >= max_entries:
            out["truncated"] = True
            break
        target.append(ent.path)
        n += 1
    return out


def list_folder(folder, max_files=FOLDER_MAX):
    """The data files directly in a folder (one level deep; File Geodatabases count as files).
    Returns (paths, truncated). A File Geodatabase folder itself is returned as [folder]."""
    root = gdb_root(folder)
    if root:
        return [root], False
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return [], False
    lower = {n.lower() for n in names}
    out = []
    truncated = False
    for n in names:
        low = n.lower()
        if low.startswith(("~$", ".")) or low.endswith((".aux.xml", ".shp.xml")):
            continue
        full = os.path.join(folder, n)
        e = ext_of(low)
        if os.path.isdir(full):
            if e != ".gdb":
                continue
        elif e not in FOLDER_EXT or e in SIDECAR_EXT:
            continue
        if e == ".dbf" and os.path.splitext(low)[0] + ".shp" in lower:
            continue  # part of a shapefile
        if len(out) >= max_files:
            truncated = True
            break
        out.append(full)
    return out, truncated


# ---- the engine's view of a file ----------------------------------------------------------------------------------
def _layer_type_name(t):
    from qgis.core import Qgis

    return {Qgis.LayerType.Vector: "vector", Qgis.LayerType.Raster: "raster", Qgis.LayerType.Mesh: "mesh",
            Qgis.LayerType.PointCloud: "pointcloud", Qgis.LayerType.VectorTile: "vectortile",
            Qgis.LayerType.TiledScene: "tiledscene"}.get(t)


def geometry_kind(wkb_type):
    """points / lines / areas / table / shapes for a WKB type."""
    from qgis.core import Qgis, QgsWkbTypes

    if wkb_type in (Qgis.WkbType.NoGeometry,):
        return "table"
    g = QgsWkbTypes.geometryType(wkb_type)
    if g == Qgis.GeometryType.Point:
        return "points"
    if g == Qgis.GeometryType.Line:
        return "lines"
    if g == Qgis.GeometryType.Polygon:
        return "areas"
    if g == Qgis.GeometryType.Null:
        return "table"
    return "shapes"


def _ogr_facts(dataset):
    """{layer name: crs name} of a vector dataset (one GDAL open)."""
    gdal, _ogr, _osr = _gdal()
    out = {}
    try:
        ds = gdal.OpenEx(dataset, gdal.OF_VECTOR | gdal.OF_READONLY)
    except RuntimeError:
        return out
    if ds is None:
        return out
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(i)
        if lyr is None:
            continue
        out[lyr.GetName()] = _srs_name(lyr.GetSpatialRef())
    return out


def _srs_name(srs):
    if srs is None:
        return ""
    try:
        name = srs.GetName() or ""
    except Exception:  # noqa: BLE001
        name = ""
    try:
        auth, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
        if auth and code and name:
            return f"{name} ({auth}:{code})"
        if auth and code:
            return f"{auth}:{code}"
    except Exception:  # noqa: BLE001
        pass
    return name


def raster_facts(path):
    """(bands, data type name, crs name, width, height) of a raster, or None."""
    gdal, _ogr, osr = _gdal()
    try:
        ds = gdal.Open(path)
    except RuntimeError:
        return None
    if ds is None:
        return None
    wkt = ds.GetProjection()
    crs = ""
    if wkt:
        srs = osr.SpatialReference()
        try:
            srs.ImportFromWkt(wkt)
            crs = _srs_name(srs)
        except RuntimeError:
            crs = ""
    dtype = ""
    if ds.RasterCount:
        dtype = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType) or ""
    return ds.RasterCount, dtype, crs, ds.RasterXSize, ds.RasterYSize


def raster_kind(path, bands, dtype):
    if ext_of(path) in ELEVATION_EXT:
        return "elevation"
    if bands == 1 and dtype in ("Float32", "Float64", "Int16", "Int32", "Float16"):
        return "elevation"
    return "image"


def detect_with_engine(source, path, query_path=None, container=""):
    """Items for every layer the engine finds in a file (or File Geodatabase / archive)."""
    from qgis.core import Qgis, QgsProviderRegistry

    query_path = query_path or path
    size = dataset_size(path)
    flags = Qgis.SublayerQueryFlag.ResolveGeometryType
    if size is not None and size < COUNT_MAX_BYTES:
        flags |= Qgis.SublayerQueryFlag.CountFeatures
    try:
        subs = QgsProviderRegistry.instance().querySublayers(query_path, flags)
    except Exception:  # noqa: BLE001 - a file the engine chokes on is simply not readable
        subs = []
    subs = [s for s in subs if not (s.flags() & Qgis.SublayerFlag.SystemTable)]
    e = ext_of(path)
    if e in ARCHIVE_EXT:
        container = "archive"
    seen = set()
    uniq = []
    for s in subs:
        key = (s.providerKey(), s.uri())
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    subs = uniq
    if not subs:
        return []
    facts = {}   # dataset -> {layer: crs}
    items = []
    names = [s.name() for s in subs]
    for s in subs:
        lt = _layer_type_name(s.type())
        if lt is None:
            continue
        uri = s.uri()
        provider = s.providerKey()
        count = s.featureCount()
        count = count if isinstance(count, int) and count >= 0 else None
        it = _item(source, path, provider=provider, uri=uri, layer_type=lt, readable=True, checked=True,
                   size=size, inside=len(subs), container=container, table=s.name())
        inner = s.name() or ""
        # the layer name: the file name for a one-layer file, the table name otherwise
        if len(subs) == 1 and not (e in (".gdb", ".gpkg", ".sqlite") and inner):
            it["name"] = stem_of(inner) if container == "archive" and inner else stem_of(path)
        else:
            it["name"] = stem_of(inner) if container == "archive" else (inner or stem_of(path))
        if lt == "vector":
            kind = geometry_kind(s.wkbType())
            it["count"] = count
            if kind in ("points", "lines", "areas", "shapes"):
                from qgis.core import QgsWkbTypes

                it["geometry"] = QgsWkbTypes.displayString(s.wkbType())
            if e in CAD_EXT:
                it["cad_kind"] = kind
                kind = "cad"
            elif e in GPS_EXT:
                it["gps_kind"] = kind
                kind = "gps"
                if inner in ("route_points", "track_points") or count == 0:
                    it["checked"] = False
            elif kind == "table" and (e in SPREADSHEET_EXT or s.driverName() in ("XLSX", "ODS", "XLS")):
                kind = "spreadsheet"
            if names.count(inner) > 1 and it.get("geometry"):  # one table split by geometry (DXF, GeoJSON …)
                it["name"] = f"{it['name']} {_geom_word(it.get('cad_kind') or it.get('gps_kind') or kind)}"
            it["kind"] = kind
            if provider == "ogr" and kind not in ("table", "spreadsheet"):
                dataset = uri.split("|")[0]
                if dataset not in facts:
                    facts[dataset] = _ogr_facts(dataset)
                it["crs"] = facts[dataset].get(inner, "") or (next(iter(facts[dataset].values()), "")
                                                              if len(facts[dataset]) == 1 else "")
        elif lt == "raster":
            rf = raster_facts(uri) if provider == "gdal" else None
            if rf:
                bands, dtype, crs, w, h = rf
                it["kind"] = raster_kind(path, bands, dtype)
                it["crs"] = crs
                it["bands"] = bands
                it["pixels"] = (w, h)
            else:
                it["kind"] = "image"
        elif lt == "mesh":
            it["kind"] = "mesh"
        elif lt == "pointcloud":
            it["kind"] = "pointcloud"
            it["count"] = count
        elif lt == "vectortile":
            it["kind"] = "vectortile"
        elif lt == "tiledscene":
            it["kind"] = "scene"
        items.append(it)
    return items


def _geom_word(kind):
    from .kinds import kind_name

    return f"({kind_name(kind)})" if kind in ("points", "lines", "areas", "table", "shapes") else ""


# ---- CSV and text tables ------------------------------------------------------------------------------------------
_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_NOT_WORD = re.compile(r"[\W_]+", re.UNICODE)


def norm_name(name):
    s = _DIACRITICS.sub("", (name or "").strip().lower())
    s = s.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ؤ": "و", "ئ": "ي"}))
    return _NOT_WORD.sub("", s)


def _norm_set(words):
    return {norm_name(w) for w in words}


# names of coordinate columns (compared without case, spaces, punctuation or diacritics; a leading "ال" is optional)
LON_NAMES = _norm_set(["lon", "long", "lng", "longitude", "longitud", "longitude_dd", "lon_dd", "x_lon",
                       "خط الطول", "خطوط الطول", "طول", "الطول", "طول جغرافي", "خط طول"])
LAT_NAMES = _norm_set(["lat", "latitude", "latitud", "lat_dd", "latitude_dd", "y_lat",
                       "دائرة العرض", "دوائر العرض", "عرض", "العرض", "عرض جغرافي", "دائرة عرض", "خط العرض", "خط عرض"])
X_NAMES = _norm_set(["x", "xcoord", "x_coord", "x coordinate", "coord_x", "point_x", "pointx", "easting", "east",
                     "e", "utm_x", "utm_e", "utme", "xm", "x_m", "x (m)", "eastings",
                     "شرقيات", "الشرقيات", "شرقي", "الشرقي", "شرقية", "الإحداثي الشرقي", "احداثي شرقي", "اكس", "إكس",
                     "س", "الإحداثي السيني", "احداثي سيني", "سيني"])
Y_NAMES = _norm_set(["y", "ycoord", "y_coord", "y coordinate", "coord_y", "point_y", "pointy", "northing", "north",
                     "n", "utm_y", "utm_n", "utmn", "ym", "y_m", "y (m)", "northings",
                     "شماليات", "الشماليات", "شمالي", "الشمالي", "شمالية", "الإحداثي الشمالي", "احداثي شمالي", "واي",
                     "ص", "الإحداثي الصادي", "احداثي صادي", "صادي"])
WKT_NAMES = _norm_set(["wkt", "geom", "geometry", "the_geom", "shape", "geometrie", "الشكل", "هندسة", "الهندسة"])
_WKT_VALUE = re.compile(r"^\s*(SRID=\d+;)?\s*(MULTI)?(POINT|LINESTRING|POLYGON|GEOMETRYCOLLECTION)\s*[ZM]*\s*\(",
                        re.I)
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")


def _match(name, names):
    n = norm_name(name)
    if not n:
        return False
    if n in names:
        return True
    if n.startswith("ال") and n[2:] in names:
        return True
    return False


def _number(text):
    t = (text or "").strip().translate(_DIGITS)
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _read_text(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(SNIFF_BYTES)
    except OSError:
        return None, ""
    if b"\0" in raw[:4096]:
        return None, ""  # binary
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if len(raw) == SNIFF_BYTES and "\n" in text:  # drop the cut last line
            text = text[: text.rfind("\n")]
        return text, ("UTF-8" if enc == "utf-8-sig" else "windows-1256" if enc == "cp1256" else "ISO-8859-1")
    return None, ""


def sniff_table(path):
    """{delimiter, encoding, header, rows, x, y, wkt, crs_guess, geographic} of a delimited text file, or None when it
    is not a table (fewer than two columns, no data rows, binary)."""
    text, enc = _read_text(path)
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    sample = "\n".join(lines[:50])
    delim = None
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: lines[0].count(d) for d in (",", ";", "\t", "|")}
        best = max(counts, key=counts.get)
        delim = best if counts[best] else None
    if not delim:
        return None
    rows = list(csv.reader(lines[: SNIFF_ROWS + 1], delimiter=delim))
    header = [h.strip() for h in rows[0]]
    data = [r for r in rows[1:] if any(c.strip() for c in r)]
    if len(header) < 2 or not data:
        return None
    same = sum(1 for r in data if len(r) == len(header))
    if same < max(1, len(data) * 0.6):
        return None
    if all(_number(h) is not None for h in header):
        return None  # no header row - numbers only: not something we can name columns for

    def column(i):
        return [r[i] for r in data if i < len(r)]

    def numeric(i):
        vals = [_number(v) for v in column(i)]
        vals = [v for v in vals if v is not None]
        return vals if len(vals) >= max(1, len(column(i)) * 0.8) else None

    found = {"delimiter": delim, "encoding": enc, "header": header, "rows": len(lines) - 1, "x": "", "y": "",
             "wkt": "", "crs_guess": "", "geographic": False}
    lon = next((h for h in header if _match(h, LON_NAMES)), "")
    lat = next((h for h in header if _match(h, LAT_NAMES)), "")
    xs = next((h for h in header if _match(h, X_NAMES)), "")
    ys = next((h for h in header if _match(h, Y_NAMES)), "")
    pairs = [(lon, lat, True), (xs, ys, False)]
    for xname, yname, named_geo in pairs:
        if not (xname and yname) or xname == yname:
            continue
        xv, yv = numeric(header.index(xname)), numeric(header.index(yname))
        if not xv or not yv:
            continue
        in_degrees = all(-180 <= v <= 180 for v in xv) and all(-90 <= v <= 90 for v in yv)
        found["x"], found["y"] = xname, yname
        found["geographic"] = in_degrees
        found["crs_guess"] = "EPSG:4326" if in_degrees else ""
        found["named_geographic"] = named_geo
        break
    if not found["x"]:
        for i, h in enumerate(header):
            vals = [v for v in column(i) if v.strip()]
            if vals and (_match(h, WKT_NAMES) or sum(1 for v in vals if _WKT_VALUE.match(v)) >= len(vals) * 0.8):
                if sum(1 for v in vals if _WKT_VALUE.match(v)) >= len(vals) * 0.8:
                    found["wkt"] = h
                    first = _WKT_VALUE.match(vals[0])
                    found["wkt_type"] = (first.group(3) or "").upper() if first else ""
                    break
    return found


def delimited_uri(path, info, crs=""):
    """The delimitedtext provider URI of a sniffed table (points from X/Y, shapes from WKT, or a plain table)."""
    from qgis.PyQt.QtCore import QUrl, QUrlQuery

    url = QUrl.fromLocalFile(path)
    q = QUrlQuery()
    q.addQueryItem("type", "csv")
    q.addQueryItem("delimiter", "\\t" if info["delimiter"] == "\t" else info["delimiter"])
    q.addQueryItem("encoding", info.get("encoding") or "UTF-8")
    q.addQueryItem("detectTypes", "yes")
    q.addQueryItem("trimFields", "yes")
    q.addQueryItem("subsetIndex", "no")
    q.addQueryItem("watchFile", "no")
    if info.get("x") and info.get("y"):
        q.addQueryItem("xField", info["x"])
        q.addQueryItem("yField", info["y"])
        q.addQueryItem("spatialIndex", "yes")
        if crs:
            q.addQueryItem("crs", crs)
    elif info.get("wkt"):
        q.addQueryItem("wktField", info["wkt"])
        q.addQueryItem("spatialIndex", "yes")
        if crs:
            q.addQueryItem("crs", crs)
    else:
        q.addQueryItem("geomType", "none")
    url.setQuery(q)
    return bytes(url.toEncoded()).decode("ascii")


def detect_text_table(source, path, project_crs=""):
    info = sniff_table(path)
    if info is None:
        return []
    it = _item(source, path, provider="delimitedtext", layer_type="vector", readable=True, checked=True,
               size=dataset_size(path), table=stem_of(path))
    if (it["size"] or 0) < SNIFF_BYTES:  # the whole file was read: the row count is exact
        it["count"] = info["rows"]
    if info["x"]:
        crs = info["crs_guess"] or project_crs
        it.update(kind="points", geometry="Point", xy=(info["x"], info["y"]), crs=crs,
                  crs_guessed=bool(info["crs_guess"]), crs_from_project=not info["crs_guess"])
    elif info["wkt"]:
        crs = project_crs
        wt = info.get("wkt_type", "")
        kind = {"POINT": "points", "LINESTRING": "lines", "POLYGON": "areas"}.get(wt, "shapes")
        it.update(kind=kind, geometry=wt.title(), wkt=info["wkt"], crs=crs, crs_from_project=True)
    else:
        crs = ""
        it.update(kind="table")
    it["uri"] = delimited_uri(path, info, crs)
    it["columns"] = len(info["header"])
    return [it]


# ---- web addresses ------------------------------------------------------------------------------------------------
def detect_url(url):
    from qgis.core import QgsDataSourceUri

    u = re.sub("%7d", "}", re.sub("%7b", "{", url.strip(), flags=re.I), flags=re.I)  # tile templates, encoded
    low = u.lower()
    name = re.sub(r"^https?://", "", u, flags=re.I).split("/")[0] or u
    if "{z}" in low and "{x}" in low and ("{y}" in low or "{-y}" in low):
        uri = QgsDataSourceUri()
        uri.setParam("type", "xyz")
        uri.setParam("url", u)
        uri.setParam("zmin", "0")
        uri.setParam("zmax", "19")
        return [_item(url, u, name=name, kind="web", layer_type="raster", provider="wms",
                      uri=bytes(uri.encodedUri()).decode(), readable=True, checked=True)]
    m = re.search(r"/(MapServer|ImageServer)/?$", u, re.I)
    if m:
        lt, prov = "raster", "arcgismapserver"
        return [_item(url, u, name=_rest_name(u), kind="web", layer_type=lt, provider=prov, uri=f"url={u}",
                      readable=True, checked=True)]
    if re.search(r"/FeatureServer/\d+/?$", u, re.I):
        return [_item(url, u, name=_rest_name(u), kind="web", layer_type="vector", provider="arcgisfeatureserver",
                      uri=f"url={u.rstrip('/')}", readable=True, checked=True)]
    if re.search(r"/(FeatureServer|rest/services)/?", u, re.I):
        return [_item(url, u, name=name, kind="service", layer_type="service", page="arcgisfeatureserver")]
    if "wmts" in low or "service=wms" in low or re.search(r"[/=]wms\b", low):
        return [_item(url, u, name=name, kind="service", layer_type="service", page="wms")]
    if "service=wfs" in low or re.search(r"[/=]wfs\b", low) or "/collections" in low:
        return [_item(url, u, name=name, kind="service", layer_type="service", page="WFS")]
    if "service=wcs" in low or re.search(r"[/=]wcs\b", low):
        return [_item(url, u, name=name, kind="service", layer_type="service", page="wcs")]
    path_part = u.split("?")[0]
    e = ext_of(path_part)
    if e in FOLDER_EXT:
        items = detect_with_engine(url, path_part, query_path="/vsicurl/" + u)
        for it in items:
            it["size"] = None
            if len(items) == 1:
                it["name"] = stem_of(path_part)
        if items:
            return items
    return [_item(url, u, name=name, kind="service", layer_type="service", page="wms",
                  note="unknown address")]


def _rest_name(url):
    parts = [p for p in re.split(r"/+", url.rstrip("/")) if p]
    try:
        i = next(k for k, p in enumerate(parts) if p.lower() in ("mapserver", "imageserver", "featureserver"))
        return parts[i - 1]
    except StopIteration:
        return parts[-1] if parts else url


# ---- the whole thing ----------------------------------------------------------------------------------------------
def detect(source, project_crs=""):
    """Items for one dropped / chosen / pasted thing (a file, a File Geodatabase, a web address). Folders that are
    not File Geodatabases are expanded with list_folder() first (by the caller)."""
    src = (source or "").strip().strip('"').strip("'")
    if is_url(src):
        return detect_url(src)
    path = src[len("file:///"):] if src.lower().startswith("file:///") else src
    path = gdb_root(path) or path  # a raw .gdbtable (or anything inside a .gdb) means the whole geodatabase
    if not os.path.exists(path):
        return [unknown(source, path, note="missing")]
    e = ext_of(path)
    if os.path.isdir(path):
        if e == ".gdb":
            return detect_with_engine(source, path) or [unknown(source, path)]
        return [unknown(source, path, note="folder")]
    if e in PROJECT_EXT:
        return [_item(source, path, kind="project", layer_type="project", readable=True, checked=False,
                      size=dataset_size(path))]
    if e in TEXT_TABLE_EXT:
        items = detect_text_table(source, path, project_crs)
        if items:
            return items
        if e in (".csv", ".tsv"):
            return detect_with_engine(source, path) or [unknown(source, path)]
        return [unknown(source, path)]
    items = detect_with_engine(source, path)
    if items:
        return items
    if e in ARCHIVE_EXT:
        return [_item(source, path, kind="archive", size=dataset_size(path), note="empty archive")]
    return [unknown(source, path)]
