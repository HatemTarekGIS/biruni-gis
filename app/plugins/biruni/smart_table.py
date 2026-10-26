"""Smart table import: turn a spreadsheet or CSV into points without asking the
user which column is which.

Understands decimal degrees, degrees-minutes-seconds in all the usual spellings
(30°02'15"N, 30 2 15.5, N 30-02-15, ٣٠°٢'…), Arabic-Indic digits and
separators, map links (Google Maps / WhatsApp locations), "lat, lon" in one
column, WKT, and projected coordinates (UTM and local grids). Pure Python
except the reading of .xls/.ods files (GDAL) - testable headless.
"""
import csv
import math
import os
import re
from dataclasses import dataclass, field

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

LON_NAMES = ("lon", "long", "lng", "longitude", "longtitude", "x", "easting", "east", "e", "x_coord", "xcoord",
             "coord_x", "point_x", "خط الطول", "الطول", "طول", "شرقيات", "شرقي", "الشرقيات", "س", "اكس",
             "الاحداثي السيني", "إحداثي س")
LAT_NAMES = ("lat", "latitude", "y", "northing", "north", "n", "y_coord", "ycoord", "coord_y", "point_y",
             "خط العرض", "العرض", "عرض", "شماليات", "شمالي", "الشماليات", "ص", "واي", "الاحداثي الصادي", "إحداثي ص")
PAIR_NAMES = ("coordinates", "coordinate", "coords", "location", "latlon", "latlng", "lat_lon", "lat/lon", "position",
              "gps", "link", "map", "maps", "url", "الموقع", "الإحداثيات", "الاحداثيات", "اللوكيشن", "رابط", "الرابط")
WKT_RE = re.compile(r"^\s*(MULTI)?(POINT|LINESTRING|POLYGON)\s*(Z|M|ZM)?\s*\(", re.I)
LINK_RE = re.compile(r"(?:[?&](?:q|query|ll|daddr|destination)=|@|/place/)(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")
PAIR_RE = re.compile(r"^\s*\(?\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*\)?\s*$")


def normalise_text(value):
    if value is None:
        return ""
    s = str(value).translate(ARABIC_DIGITS)
    return s.replace("٫", ".").replace("٬", ",").replace("،", ",").replace("−", "-").strip()


def parse_number(value):
    """A float from numbers and number-like text; None when it isn't one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    s = normalise_text(value).replace(" ", "")
    if not s:
        return None
    if s.count(",") == 1 and "." not in s and len(s.split(",")[1]) != 3:
        s = s.replace(",", ".")          # decimal comma
    elif "," in s and "." in s:
        s = s.replace(",", "")           # thousands separators
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


_HEMI = {"n": 1, "s": -1, "e": 1, "w": -1, "ش": 1, "ج": -1, "ق": 1, "غ": -1}
_HEMI_WORDS = {"شمال": "n", "جنوب": "s", "شرق": "e", "غرب": "w"}


def parse_angle(value):
    """Decimal degrees from 30.5, '30°30\\'00"N', '30 30 0', 'N 30-30-00', 'W12d5m'…
    Returns (degrees, hemisphere letter or '') or (None, '')."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value), "") if math.isfinite(value) else (None, "")
    s = normalise_text(value)
    if not s:
        return None, ""
    if re.fullmatch(r"-?[\d.,]+", s):
        plain = parse_number(s)
        return (plain, "") if plain is not None else (None, "")
    low = s.lower()
    had_minutes = re.search(r"\d\s*m\s*\d", low) is not None
    low = re.sub(r"(\d)\s*[dm]\s*(?=\d)", r"\1 ", low)          # 12d30m5s -> 12 30 5s
    if had_minutes:
        low = re.sub(r"(\d)\s*s(?=\s*[nsew]?\s*$)", r"\1 ", low)  # trailing seconds unit
    for word, letter in _HEMI_WORDS.items():
        low = low.replace(word, letter)
    hemi = ""
    m = re.search(r"(?<![a-z])([nsew])(?![a-z])|([شجقغ])", low)
    if m:
        hemi = (m.group(1) or m.group(2))
        low = low[:m.start()] + " " + low[m.end():]
    sign = -1 if re.match(r"^\s*-", low) else 1
    nums = re.findall(r"\d+(?:\.\d+)?", low)
    if not nums or len(nums) > 3:
        return None, ""
    d = float(nums[0])
    mnt = float(nums[1]) if len(nums) > 1 else 0.0
    sec = float(nums[2]) if len(nums) > 2 else 0.0
    if mnt >= 60 or sec >= 60:
        return None, ""
    deg = d + mnt / 60 + sec / 3600
    hemi_key = hemi if hemi in _HEMI else ""
    if hemi_key:
        deg *= _HEMI[hemi_key]
    else:
        deg *= sign
    letter = {"ش": "n", "ج": "s", "ق": "e", "غ": "w"}.get(hemi_key, hemi_key)
    return deg, letter


def parse_pair(value):
    """(lat, lon) from a map link, 'lat, lon' text or None."""
    s = normalise_text(value)
    if not s:
        return None
    m = LINK_RE.search(s)
    if m is None:
        m = PAIR_RE.match(s)
    if m is None:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if abs(a) <= 90 and abs(b) <= 180:
        return a, b
    if abs(b) <= 90 and abs(a) <= 180:
        return b, a
    return None


# ---- reading -------------------------------------------------------------
def list_sheets(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    if ext in (".xls", ".ods"):
        from osgeo import ogr

        ds = ogr.Open(path)
        return [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())] if ds else []
    return [""]


def _read_csv(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = max(",;\t|", key=sample.count)
    return [row for row in csv.reader(text.splitlines(), delimiter=delim)]


def read_rows(path, sheet=None, limit=None):
    """All rows (lists of raw values) of a sheet / CSV, header row included."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet else wb.worksheets[0]
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(list(r))
                if limit and len(rows) >= limit:
                    break
            return rows
        finally:
            wb.close()
    if ext in (".xls", ".ods"):
        from osgeo import gdal, ogr

        gdal.SetConfigOption("OGR_XLS_HEADERS", "DISABLE")
        gdal.SetConfigOption("OGR_ODS_HEADERS", "DISABLE")
        ds = ogr.Open(path)
        lyr = ds.GetLayerByName(sheet) if sheet else ds.GetLayer(0)
        rows = []
        for f in lyr:
            rows.append([f.GetField(i) for i in range(f.GetFieldCount())])
            if limit and len(rows) >= limit:
                break
        return rows
    rows = _read_csv(path)
    return rows[:limit] if limit else rows


def split_header(rows):
    """(headers, data rows): the header is the first non-empty row that is mostly text."""
    rows = [r for r in rows if any(normalise_text(c) for c in r)]
    if not rows:
        return [], []
    width = max(len(r) for r in rows)
    rows = [list(r) + [None] * (width - len(r)) for r in rows]
    first = rows[0]
    texty = sum(1 for c in first if normalise_text(c) and parse_number(c) is None)
    if texty >= max(1, len([c for c in first if normalise_text(c)]) // 2):
        headers = [normalise_text(c) or f"column_{i + 1}" for i, c in enumerate(first)]
        data = rows[1:]
    else:
        headers = [f"column_{i + 1}" for i in range(width)]
        data = rows
    seen = {}
    unique = []
    for h in headers:
        k = seen.get(h.lower(), 0)
        seen[h.lower()] = k + 1
        unique.append(h if k == 0 else f"{h}_{k + 1}")
    return unique, data


# ---- detection ------------------------------------------------------------
GEOGRAPHIC, PROJECTED, PAIR, WKT = "geographic", "projected", "pair", "wkt"


@dataclass
class Detection:
    mode: str = ""            # geographic | projected | pair | wkt | "" (nothing found)
    x: int = -1               # column index of lon / easting (or the pair / WKT column)
    y: int = -1               # column index of lat / northing
    swapped: bool = False     # the columns were named the other way round
    dms: bool = False         # values are written as degrees-minutes-seconds
    notes: list = field(default_factory=list)


def _norm_header(h):
    return re.sub(r"[\s_\-\.()\[\]]+", " ", normalise_text(h).lower()).strip()


def _name_score(header, names):
    h = _norm_header(header)
    if h in names:
        return 3
    words = set(h.split())
    if words & {n for n in names if " " not in n and len(n) > 1}:
        return 2
    return 1 if any(len(n) > 2 and n in h for n in names) else 0


def _column(data, i, n=200):
    return [r[i] for r in data[:n] if i < len(r) and normalise_text(r[i])]


def _angles(values):
    out = [parse_angle(v) for v in values]
    return [a for a, _ in out if a is not None], [h for _, h in out if h]


def _is_dms(values):
    return any(re.search(r"[°'\"dmsº′″]|\d\s+\d", normalise_text(v)) for v in values[:20] if isinstance(v, str))


def detect(headers, data):
    det = Detection()
    if not headers or not data:
        return det
    # 1. a single column holding links, "lat, lon" pairs or WKT
    for i, h in sorted(enumerate(headers), key=lambda t: -_name_score(t[1], PAIR_NAMES)):
        vals = _column(data, i, 60)
        if not vals:
            continue
        if sum(1 for v in vals if WKT_RE.match(normalise_text(v))) >= 0.8 * len(vals):
            det.mode, det.x = WKT, i
            return det
        if sum(1 for v in vals if parse_pair(v)) >= 0.8 * len(vals) and not all(
                parse_number(v) is not None for v in vals):
            det.mode, det.x = PAIR, i
            return det

    # 2. two columns, by header names then by values
    lon_c = max(range(len(headers)), key=lambda i: _name_score(headers[i], LON_NAMES))
    lat_c = max(range(len(headers)), key=lambda i: _name_score(headers[i], LAT_NAMES))
    named = _name_score(headers[lon_c], LON_NAMES) > 0 and _name_score(headers[lat_c], LAT_NAMES) > 0 \
        and lon_c != lat_c
    if not named:
        numeric = [i for i in range(len(headers))
                   if len(_angles(_column(data, i))[0]) >= max(1, 0.8 * len(_column(data, i)))
                   and _column(data, i)]
        pair = _pick_by_values(data, numeric)
        if pair is None:
            return det
        lon_c, lat_c = pair
    xs, hx = _angles(_column(data, lon_c))
    ys, hy = _angles(_column(data, lat_c))
    if not xs or not ys:
        return det
    det.dms = _is_dms(_column(data, lon_c)) or _is_dms(_column(data, lat_c))
    geo = max(map(abs, xs)) <= 180 and max(map(abs, ys)) <= 180
    if geo:
        det.mode = GEOGRAPHIC
        # names say lat/lon but the values say otherwise -> swap
        if (max(map(abs, ys)) > 90 >= max(map(abs, xs))) or ({"e", "w"} & set(hy) and {"n", "s"} & set(hx)):
            lon_c, lat_c = lat_c, lon_c
            det.swapped = True
            det.notes.append("swapped")
    else:
        det.mode = PROJECTED
        if _looks_northing(xs) and _looks_easting(ys) and not (_looks_northing(ys) and _looks_easting(xs)):
            lon_c, lat_c = lat_c, lon_c
            det.swapped = True
            det.notes.append("swapped")
    det.x, det.y = lon_c, lat_c
    return det


def _looks_easting(vals):
    return all(100000 <= v <= 900000 for v in vals)


def _looks_northing(vals):
    return all(1000000 < v <= 10000000 for v in vals)


def _pick_by_values(data, numeric):
    """Two numeric columns that look like a coordinate pair: geographic first,
    then easting/northing. Preference goes to adjacent columns."""
    best = None
    for a in numeric:
        for b in numeric:
            if a == b:
                continue
            va, _ = _angles(_column(data, a))
            vb, _ = _angles(_column(data, b))
            if not va or not vb or len(set(va)) < 2 and len(va) > 3:
                continue
            score = 0
            if max(map(abs, va)) <= 180 and max(map(abs, vb)) <= 90 and any(v != int(v) for v in va + vb):
                score = 3
            elif _looks_easting(va) and _looks_northing(vb):
                score = 2
            if score:
                score += 0.5 if abs(a - b) == 1 else 0
                score += 0.25 if a < b else 0   # x usually comes first
                if best is None or score > best[0]:
                    best = (score, a, b)
    return (best[1], best[2]) if best else None


def utm_epsg(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + max(1, min(60, zone))


# ---- building points ------------------------------------------------------------
@dataclass
class Located:
    rows: list            # (row index, x, y, wkt or None)
    failed: list          # row indexes without a usable location


def locate(data, det):
    ok, bad = [], []
    if not det.mode:
        return Located([], list(range(len(data))))
    for k, r in enumerate(data):
        try:
            if det.mode == WKT:
                s = normalise_text(r[det.x])
                if WKT_RE.match(s):
                    ok.append((k, None, None, s))
                else:
                    bad.append(k)
                continue
            if det.mode == PAIR:
                p = parse_pair(r[det.x])
                if p is None:
                    bad.append(k)
                else:
                    ok.append((k, p[1], p[0], None))
                continue
            x, _ = parse_angle(r[det.x])
            y, _ = parse_angle(r[det.y])
            if x is None or y is None or (det.mode == GEOGRAPHIC and (abs(y) > 90 or abs(x) > 180)) \
                    or (x == 0 and y == 0):
                bad.append(k)
            else:
                ok.append((k, x, y, None))
        except (IndexError, TypeError):
            bad.append(k)
    return Located(ok, bad)


def column_type(values):
    """'int' | 'double' | 'date' | 'string' for a column's raw values."""
    import datetime as dt

    vals = [v for v in values if normalise_text(v)]
    if not vals:
        return "string"
    if all(isinstance(v, (dt.date, dt.datetime)) for v in vals):
        return "date"
    nums = [parse_number(v) for v in vals]
    if all(n is not None for n in nums) and not any(isinstance(v, str) and re.match(r"^0\d", v.strip())
                                                    for v in vals):
        return "int" if all(float(n).is_integer() and abs(n) < 2 ** 53 for n in nums) else "double"
    return "string"


def build_layer(headers, data, det, crs, name):
    """Memory point (or WKT geometry) layer with every column, typed.
    Returns (layer, Located)."""
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsVectorLayer, QgsWkbTypes
    from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType

    located = locate(data, det)
    geom_type = "Point"
    if det.mode == WKT and located.rows:
        g = QgsGeometry.fromWkt(located.rows[0][3])
        geom_type = QgsWkbTypes.displayString(QgsWkbTypes.multiType(g.wkbType())) if not g.isNull() else "Point"
    layer = QgsVectorLayer(f"{geom_type}?crs={crs.authid() or 'EPSG:4326'}", name, "memory")
    if not crs.authid():
        layer.setCrs(crs)
    types = [column_type([r[i] for r in data if i < len(r)]) for i in range(len(headers))]
    qtypes = {"int": QMetaType.Type.LongLong, "double": QMetaType.Type.Double, "date": QMetaType.Type.QDateTime,
              "string": QMetaType.Type.QString}
    layer.dataProvider().addAttributes([QgsField(h, qtypes[t]) for h, t in zip(headers, types)])
    layer.updateFields()
    feats = []
    for k, x, y, wkt in located.rows:
        r = data[k]
        f = QgsFeature(layer.fields())
        if wkt:
            g = QgsGeometry.fromWkt(wkt)
            if g.isNull():
                located.failed.append(k)
                continue
            if QgsWkbTypes.isMultiType(layer.wkbType()):
                g.convertToMultiType()
            f.setGeometry(g)
        else:
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        attrs = []
        for i, t in enumerate(types):
            v = r[i] if i < len(r) else None
            if not normalise_text(v):
                attrs.append(None)
            elif t == "int":
                attrs.append(int(parse_number(v)))
            elif t == "double":
                attrs.append(parse_number(v))
            elif t == "date":
                attrs.append(QDateTime(v) if hasattr(v, "hour") else QDateTime(QDate(v.year, v.month, v.day)))
            else:
                attrs.append(str(v).strip() if not isinstance(v, float) or not v.is_integer() else str(int(v)))
        f.setAttributes(attrs)
        feats.append(f)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer, located
