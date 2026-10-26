"""Coordinate converter: type a coordinate in any common format, see it in all
the others, jump to it, or keep it as a point.

Understood input: decimal degrees ("30.0444, 31.2357"), degrees-minutes-seconds
(30°02'40"N 31°14'08"E), map links, UTM ("36N 330125 3324100" - N/S after the
zone is the hemisphere, other letters are MGRS latitude bands), MGRS
("36RUU3012524100"), and plain X Y in a chosen coordinate system.
"""
import math
import re

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry,
                       QgsPointXY, QgsProject, QgsVectorLayer)

from .smart_table import normalise_text, parse_angle, parse_pair

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
BANDS = "CDEFGHJKLMNPQRSTUVWX"
COL_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"
# labels of formats() rows, listed for the translation extractor
TITLES = ["Decimal degrees", "Degrees, minutes, seconds", "Degrees, decimal minutes", "UTM", "MGRS", "Map link"]


def _xform(src, dst, x, y):
    t = QgsCoordinateTransform(src, dst, QgsProject.instance())
    p = t.transform(QgsPointXY(x, y))
    return p.x(), p.y()


def utm_crs(zone, north):
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if north else 32700) + zone}")


def utm_zone(lon, lat):
    zone = int((lon + 180) // 6) + 1
    return max(1, min(60, zone)), lat >= 0


def to_utm(lon, lat):
    zone, north = utm_zone(lon, lat)
    e, n = _xform(WGS84, utm_crs(zone, north), lon, lat)
    return zone, north, e, n


def band_letter(lat):
    if lat >= 72:
        return "X"
    return BANDS[max(0, min(19, int((lat + 80) // 8)))]


def to_mgrs(lon, lat, digits=5):
    if not -80 <= lat < 84:
        return ""
    zone, _north, e, n = to_utm(lon, lat)
    col = COL_SETS[(zone - 1) % 3][int(e // 100000) - 1]
    row = ROW_LETTERS[(int(n // 100000) + (5 if zone % 2 == 0 else 0)) % 20]
    scale = 10 ** (5 - digits)
    ee = int((e % 100000) // scale)
    nn = int((n % 100000) // scale)
    return f"{zone}{band_letter(lat)}{col}{row}{ee:0{digits}d}{nn:0{digits}d}"


def from_mgrs(text):
    m = re.fullmatch(r"(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)", re.sub(r"\s+", "", text.upper()))
    if not m or len(m.group(5)) % 2:
        return None
    zone, band, colc, rowc, digits = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
    if not 1 <= zone <= 60 or colc not in COL_SETS[(zone - 1) % 3]:
        return None
    k = len(digits) // 2
    scale = 10 ** (5 - k) if k else 100000
    e_in = int(digits[:k]) * scale if k else 0
    n_in = int(digits[k:]) * scale if k else 0
    # centre of the precision square
    e_in += scale / 2
    n_in += scale / 2
    e = (COL_SETS[(zone - 1) % 3].index(colc) + 1) * 100000 + e_in
    n = ((ROW_LETTERS.index(rowc) - (5 if zone % 2 == 0 else 0)) % 20) * 100000 + n_in
    north = band >= "N"
    band_lat = -80 + BANDS.index(band) * 8
    cm = zone * 6 - 183
    _e0, n_min = _xform(WGS84, utm_crs(zone, north), cm, band_lat)
    while n < n_min - 100000:
        n += 2000000
    lon, lat = _xform(utm_crs(zone, north), WGS84, e, n)
    return lon, lat


def dms(value, positive, negative, sec_decimals=2):
    hemi = positive if value >= 0 else negative
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    if round(s, sec_decimals) >= 60:
        s = 0
        m += 1
    if m >= 60:
        m = 0
        d += 1
    return f"{d}°{m:02d}'{s:0{3 + sec_decimals}.{sec_decimals}f}\"{hemi}"


def parse_any(text, plain_crs=None):
    """(lon, lat) in WGS 84 from any supported spelling, or None."""
    s = normalise_text(text)
    if not s:
        return None
    pair = parse_pair(s)
    if pair and not re.search(r"[°'\"]", s):
        return pair[1], pair[0]   # small numbers are degrees whatever CRS is chosen
    # MGRS
    if re.fullmatch(r"\d{1,2}\s*[C-HJ-NP-X]\s*[A-HJ-NP-Z][A-HJ-NP-V]\s*[\d\s]*", s.upper()):
        r = from_mgrs(s)
        if r:
            return r
    # UTM: zone + hemisphere/band + easting + northing
    m = re.fullmatch(r"(\d{1,2})\s*([A-Za-z])?\s*[,\s]\s*(\d+(?:\.\d+)?)\s*m?\s*[eE]?\s*[,\s]\s*(\d+(?:\.\d+)?)\s*m?\s*[nN]?",
                     s)
    if m and 1 <= int(m.group(1)) <= 60:
        letter = (m.group(2) or "N").upper()
        north = letter == "N" or (letter != "S" and letter >= "N")
        return _xform(utm_crs(int(m.group(1)), north), WGS84, float(m.group(3)), float(m.group(4)))
    # two numbers in a projected CRS
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if plain_crs is not None and plain_crs.isValid() and not plain_crs.isGeographic() and len(nums) == 2 \
            and not re.search(r"[°'\"]", s):
        return _xform(plain_crs, WGS84, float(nums[0]), float(nums[1]))
    # DMS pair: split on a hemisphere letter, comma or slash
    parts = re.split(r"(?<=[NSnsشج])\s+|[,;/]\s*", s, maxsplit=1)
    if len(parts) == 2:
        a, ha = parse_angle(parts[0])
        b, hb = parse_angle(parts[1])
        if a is not None and b is not None:
            if ha in ("e", "w") or hb in ("n", "s"):
                a, b = b, a
            if abs(a) <= 90 and abs(b) <= 180:
                return b, a
    return None


def formats(lon, lat, extra_crs=()):
    """Ordered (label, text) rows for a WGS 84 position."""
    zone, north, e, n = to_utm(lon, lat)
    rows = [("Decimal degrees", f"{lat:.6f}, {lon:.6f}"),
            ("Degrees, minutes, seconds", f"{dms(lat, 'N', 'S')} {dms(lon, 'E', 'W')}"),
            ("Degrees, decimal minutes", f"{_ddm(lat, 'N', 'S')} {_ddm(lon, 'E', 'W')}"),
            ("UTM", f"{zone}{'N' if north else 'S'} {e:.2f} {n:.2f}"),
            ("MGRS", to_mgrs(lon, lat))]
    for crs in extra_crs:
        if crs is not None and crs.isValid() and crs.authid() != "EPSG:4326":
            x, y = _xform(WGS84, crs, lon, lat)
            dec = 6 if crs.isGeographic() else 2
            rows.append((crs.userFriendlyIdentifier(), f"{x:.{dec}f}, {y:.{dec}f}"))
    rows.append(("Map link", f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=18/{lat:.6f}/{lon:.6f}"))
    return rows


def _ddm(value, positive, negative):
    hemi = positive if value >= 0 else negative
    v = abs(value)
    d = int(v)
    m = (v - d) * 60
    return f"{d}°{m:07.4f}'{hemi}"


def points_layer(name):
    fields = "&".join(f"field={n}:{t}" for n, t in (("input", "string"), ("lat", "double"), ("lon", "double"),
                                                     ("dms", "string"), ("utm", "string"), ("mgrs", "string")))
    return QgsVectorLayer(f"Point?crs=EPSG:4326&{fields}", name, "memory")


def add_point(lyr, text, lon, lat):
    rows = dict(formats(lon, lat))
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
    f.setAttributes([text, lat, lon, rows["Degrees, minutes, seconds"], rows["UTM"], rows["MGRS"]])
    lyr.dataProvider().addFeatures([f])
    lyr.updateExtents()
    lyr.triggerRepaint()


def haversine_m(lon1, lat1, lon2, lat2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
