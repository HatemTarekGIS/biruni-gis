"""Reading and writing bearings in the notations deeds and survey plans use.

Pure Python. All bearings are handled as azimuths: degrees clockwise from
north, 0 <= az < 360 (grid north of the coordinate system unless said
otherwise).

Notations:
    azimuth          45°30'15"             (Brazil memorial, Indonesia, Arabic traverse)
    wcb              045°30'15"            (whole-circle bearing: Nigeria, Ghana, Kenya, South Asia)
    azimuth_decimal  45.5042°
    quadrant         N 45°30'15" E         (rumbos: Spain, Latin America; USA)
    quadrant_ph      N. 45 deg. 30' E.     (Philippine technical descriptions)
    gon              50.5602 gr            (gisement / semt açısı: France, Switzerland, Turkey)

Quadrant letters in other languages are read too: O = Oeste/Ouest (West) in
Spanish, Portuguese and French but Ost (East) in German; L = Leste (East);
Turkish K/G/D/B (Kuzey, Güney, Doğu, Batı); Indonesian U/S/T/B (Utara,
Selatan, Timur, Barat). Arabic-Indic and Persian digits are accepted.
"""
import math
import re

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# quadrant letters per language: {letter: axis}  (N/S for the first, E/W for the second)
LETTERS = {
    "en": {"N": "N", "S": "S", "E": "E", "W": "W"},
    "es": {"N": "N", "S": "S", "E": "E", "O": "W", "W": "W"},
    "pt": {"N": "N", "S": "S", "E": "E", "L": "E", "O": "W", "W": "W"},
    "fr": {"N": "N", "S": "S", "E": "E", "O": "W", "W": "W"},
    "de": {"N": "N", "S": "S", "O": "E", "E": "E", "W": "W"},
    "tr": {"K": "N", "G": "S", "D": "E", "B": "W", "N": "N", "S": "S", "E": "E", "W": "W"},
    "id": {"U": "N", "S": "S", "T": "E", "B": "W", "N": "N", "E": "E", "W": "W"},
}
_DEFAULT_LETTERS = {"N": "N", "S": "S", "E": "E", "W": "W", "O": "W", "L": "E"}
OUT_LETTERS = {"en": "NSEW", "es": "NSEO", "pt": "NSEO", "fr": "NSEO", "de": "NSOW", "tr": "KGDB", "id": "USTB"}

NOTATIONS = ("azimuth", "wcb", "azimuth_decimal", "quadrant", "quadrant_ph", "gon")
_COUNTRY = {
    "BR": "azimuth", "PT": "azimuth", "ID": "azimuth", "SA": "azimuth", "EG": "azimuth", "AE": "azimuth",
    "PH": "quadrant_ph",
    "US": "quadrant", "CA": "quadrant", "MX": "quadrant", "ES": "quadrant", "CO": "quadrant", "PE": "quadrant",
    "AR": "quadrant", "CL": "quadrant", "VE": "quadrant", "EC": "quadrant", "BO": "quadrant", "GT": "quadrant",
    "CR": "quadrant", "PA": "quadrant", "DO": "quadrant", "PR": "quadrant", "AU": "quadrant",
    "FR": "gon", "CH": "gon", "BE": "gon", "TR": "gon", "AT": "gon", "DE": "gon",
    "NG": "wcb", "GH": "wcb", "KE": "wcb", "UG": "wcb", "TZ": "wcb", "ZA": "wcb", "ZM": "wcb", "ZW": "wcb",
    "IN": "wcb", "PK": "wcb", "BD": "wcb", "LK": "wcb", "NP": "wcb", "MY": "wcb", "GB": "wcb",
}


def for_country(cc):
    """Notation surveyors of a country write (ISO alpha-2); plain azimuth otherwise."""
    return _COUNTRY.get((cc or "").upper(), "azimuth")


def normalise(text):
    """ASCII digits, '.' as decimal for Arabic/Persian separators, unified minute/second marks."""
    t = str(text).translate(_DIGITS).replace("٫", ".").replace("’", "'").replace("′", "'").replace("″", '"')
    return t.replace("''", '"').replace("´", "'").replace("“", '"').replace("”", '"')


_NUM = r"\d+(?:[.,]\d+)?"
_DMS = re.compile(
    rf"(?P<d>{_NUM})\s*(?:°|º|deg\.?|degrees?|d\b|درجه|درجة|درجے|درجہ|derece|grados?|graus?|-|\s)\s*"
    rf"(?:(?P<m>{_NUM})\s*(?:'|min\.?|m\b|دقیقه|دقيقة|منٹ|dakika|-|\s)\s*)?"
    rf"(?:(?P<s>{_NUM})\s*(?:\"|sec\.?|s\b|ثانیه|ثانية|سیکنڈ|saniye)\s*)?",
    re.IGNORECASE)
_GON = re.compile(rf"^\s*({_NUM})\s*(?:gr|gon|grad|grades|g|ᵍ)\.?\s*$", re.IGNORECASE)


def _f(s):
    return float(s.replace(",", "."))


def parse_angle(text):
    """Degrees from 45°30'15", 45-30-15, 45d30m15s, 45 deg. 30', '45 درجه 30 دقیقه', or a decimal number."""
    t = normalise(text).strip().rstrip(".")
    if re.fullmatch(_NUM, t):
        return _f(t)
    m = _DMS.match(t + " ")
    if not m or not m.group("d"):
        raise ValueError(f"not an angle: {text!r}")
    d = _f(m.group("d"))
    mi = _f(m.group("m")) if m.group("m") else 0.0
    s = _f(m.group("s")) if m.group("s") else 0.0
    if mi >= 60 or s >= 60:
        raise ValueError(f"minutes or seconds out of range: {text!r}")
    return d + mi / 60 + s / 3600


def quadrant_to_azimuth(ns, angle, ew):
    if angle > 90 + 1e-9:
        raise ValueError("a quadrant bearing is at most 90°")
    if ns == "N":
        return angle % 360 if ew == "E" else (360 - angle) % 360
    return 180 - angle if ew == "E" else 180 + angle


def parse(text, lang=None):
    """Azimuth (degrees) of a bearing written in any of the supported notations."""
    t = normalise(text).strip().strip(",;").strip()
    g = _GON.match(t)
    if g:
        return (_f(g.group(1)) * 0.9) % 360
    letters = LETTERS.get((lang or "").split("-")[0].lower(), _DEFAULT_LETTERS)
    up = t.upper()
    # quadrant: N 45°30' E / N. 45 deg. 30' E. / S12-30W
    m = re.match(r"^([A-Z])\.?\s*(.+?)\s*([A-Z])\.?$", up)
    if m and m.group(1) in letters and m.group(3) in letters:
        ns, ew = letters[m.group(1)], letters[m.group(3)]
        if ns in "NS" and ew in "EW":
            return quadrant_to_azimuth(ns, parse_angle(m.group(2)), ew)
    # Brazilian rumo: 45°30'15" NE / SO
    m = re.match(r"^(.+?)\s*([A-Z])([A-Z])$", up)
    if m and m.group(2) in letters and m.group(3) in letters:
        ns, ew = letters[m.group(2)], letters[m.group(3)]
        if ns in "NS" and ew in "EW":
            return quadrant_to_azimuth(ns, parse_angle(m.group(1)), ew)
    return parse_angle(t) % 360


def _dms_parts(deg, sec_decimals=0):
    total = round(deg * 3600, sec_decimals)
    d = int(total // 3600)
    rest = total - d * 3600
    mi = int(rest // 60)
    s = rest - mi * 60
    return d, mi, s


def format_dms(deg, sec_decimals=0, pad=False, seconds=True):
    if not seconds:
        total = round(deg * 60)
        d, mi = divmod(int(total), 60)
        return f"{d:03d}°{mi:02d}'" if pad else f"{d}°{mi:02d}'"
    d, mi, s = _dms_parts(deg, sec_decimals)
    sw = 3 + sec_decimals if sec_decimals else 2
    sec = f"{s:0{sw}.{sec_decimals}f}"
    return f"{d:03d}°{mi:02d}'{sec}\"" if pad else f"{d}°{mi:02d}'{sec}\""


def to_quadrant(az):
    """(N|S, angle, E|W) of an azimuth."""
    az %= 360
    if az <= 90:
        return "N", az, "E"
    if az < 180:
        return "S", 180 - az, "E"
    if az < 270:
        return "S", az - 180, "W"
    return "N", 360 - az, "W"


def format(az, notation="azimuth", lang="en", sec_decimals=0, gon_decimals=4, seconds=True):  # noqa: A001
    """A bearing as text in a notation; quadrant letters follow the language (O for West in es/pt/fr …)."""
    az %= 360
    lang2 = (lang or "en").split("-")[0].lower()
    if notation == "gon":
        return f"{az / 0.9:.{gon_decimals}f} gr"
    if notation == "azimuth_decimal":
        return f"{az:.4f}°"
    if notation == "wcb":
        return format_dms(az, sec_decimals, pad=True, seconds=seconds)
    if notation in ("quadrant", "quadrant_ph"):
        ns, ang, ew = to_quadrant(az)
        out = OUT_LETTERS.get(lang2, "NSEW")
        nsl = out[0] if ns == "N" else out[1]
        ewl = out[2] if ew == "E" else out[3]
        if notation == "quadrant_ph":
            d, mi, s = _dms_parts(ang, 0)
            text = f"{nsl}. {d} deg. {mi:02d}'"
            if seconds and s:
                text += f" {int(round(s)):02d}\""
            return f"{text} {ewl}."
        return f"{nsl} {format_dms(ang, sec_decimals, seconds=seconds)} {ewl}"
    return format_dms(az, sec_decimals, seconds=seconds)


def azimuth(p, q):
    """Grid azimuth (degrees) from point p to point q, both (x=east, y=north)."""
    return math.degrees(math.atan2(q[0] - p[0], q[1] - p[1])) % 360


GRID_NOTE = {
    "en": "Bearings are grid bearings of the map projection.",
    "pt": "Os azimutes são referidos ao Norte da quadrícula da projeção.",
    "es": "Los rumbos están referidos al norte de cuadrícula de la proyección.",
    "fr": "Les gisements sont rapportés au nord du quadrillage de la projection.",
    "tr": "Semt açıları projeksiyonun grid kuzeyine göredir.",
    "id": "Azimut mengacu pada utara grid proyeksi peta.",
    "ar": "الاتجاهات منسوبة إلى شمال الشبكة لمسقط الخريطة.",
    "fa": "آزیموت‌ها نسبت به شمال شبکهٔ سیستم تصویر هستند.",
    "ur": "سمتیں نقشے کے گرڈ شمال کے لحاظ سے ہیں۔",
}


def grid_note(lang):
    return GRID_NOTE.get((lang or "en").split("-")[0].lower(), GRID_NOTE["en"])
