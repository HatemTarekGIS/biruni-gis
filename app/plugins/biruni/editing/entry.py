"""Reading the values typed while drawing: lengths, angles, directions and coordinates.

Pure Python (no Qt): every function takes the text as typed and returns a number or raises ValueError.

* digits may be Latin, Arabic-Indic (٠١٢…) or Persian (۰۱۲…); the decimal separator may be ".", "," or
  the Arabic "٫" ("25,4" and "١٢٫٥" are 25.4 and 12.5; "1,234.5" keeps "," as thousands);
* lengths may carry a unit: m, cm, mm, km, ft (international foot), usft (US survey foot), in, yd, mi,
  and the Arabic م / سم / مم / كم / قدم / متر; "12'" is twelve feet;
* angles: 45.5, 45°30'15", 45-30-00, 45d30m, 45 30 15, 50 gr (gon);
* directions: an azimuth (or a polar angle, as the user chose) or a quadrant bearing N45°30'E / S12-30W
  (which is always an azimuth);
* deflections: + is a turn to the right (clockwise), - to the left; "12.5 L"/"12.5 R" and
  يسار / يمين are read too.
"""
import math
import re

from .. import bearings

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# metres per unit (keys are what the user may type, lower case)
_METRES = {
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0, "م": 1.0, "متر": 1.0, "مت": 1.0,
    "cm": 0.01, "سم": 0.01, "mm": 0.001, "مم": 0.001, "km": 1000.0, "كم": 1000.0,
    "ft": 0.3048, "feet": 0.3048, "foot": 0.3048, "'": 0.3048, "قدم": 0.3048,
    "usft": 1200.0 / 3937.0, "us ft": 1200.0 / 3937.0, "ftus": 1200.0 / 3937.0, "sft": 1200.0 / 3937.0,
    "in": 0.0254, '"': 0.0254, "inch": 0.0254, "بوصة": 0.0254,
    "yd": 0.9144, "yard": 0.9144, "mi": 1609.344, "mile": 1609.344,
}


def ascii_text(text):
    """Latin digits, '.' for the Arabic decimal mark, no Arabic thousands mark, trimmed."""
    t = str(text if text is not None else "").translate(_DIGITS)
    t = t.replace("٫", ".").replace("٬", "").replace("٬", "").replace("‏", "").replace("‎", "")
    return t.replace("−", "-").strip()


def parse_number(text):
    """A plain number: "25.4", "25,4", "1,234.5", "1.234,5", "-3", "١٢٫٥"."""
    t = ascii_text(text).replace(" ", "")
    if not t:
        raise ValueError("empty")
    if "," in t and "." in t:
        # the separator written last is the decimal one
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif t.count(",") == 1:
        t = t.replace(",", ".")
    elif t.count(",") > 1:
        t = t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        raise ValueError(f"not a number: {text!r}") from None


_LEN = re.compile(r"^\s*([-+]?[0-9][0-9.,\s]*)\s*([^\d\s.,][^\d]*)?\s*$")


def parse_length(text):
    """(value, metres per unit or None) of a typed length: "25.4" → (25.4, None); "25,4 م" → (25.4, 1.0);
    "12.5ft" → (12.5, 0.3048). None means the map's own units."""
    t = ascii_text(text)
    m = _LEN.match(t)
    if not m:
        raise ValueError(f"not a length: {text!r}")
    value = parse_number(m.group(1))
    unit = (m.group(2) or "").strip().lower().rstrip(".")
    if not unit:
        return value, None
    unit = re.sub(r"\s+", " ", unit)
    if unit not in _METRES:
        unit = unit.replace(" ", "")
    if unit not in _METRES:
        raise ValueError(f"unknown unit: {unit!r}")
    return value, _METRES[unit]


def length_value(text, map_metres_per_unit=1.0):
    """A typed length in map units (``map_metres_per_unit``: how many metres one map unit is)."""
    value, per = parse_length(text)
    if per is None:
        return value
    return value * per / map_metres_per_unit


def parse_angle(text):
    """Degrees of an angle: 45.5, 45°30'15", 45-30-00, 45 30 15, 45d30m, -12.5, 50 gr."""
    t = ascii_text(text)
    if not t:
        raise ValueError("empty")
    sign = 1.0
    if t[0] in "+-":
        sign = -1.0 if t[0] == "-" else 1.0
        t = t[1:].strip()
    g = re.fullmatch(r"([0-9][0-9.,]*)\s*(?:gr|gon|grad|g)\.?", t, re.IGNORECASE)
    if g:
        return sign * parse_number(g.group(1)) * 0.9
    if re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", t):
        return sign * parse_number(t)
    # "45 30 15" / "45-30-15" / "45d30m15s"
    t = re.sub(r"(?<=[0-9])\s*[dD]\s*(?=[0-9]|$)", "°", t)
    t = re.sub(r"(?<=[0-9])\s*[mM]\s*(?=[0-9]|$)", "'", t)
    t = re.sub(r"(?<=[0-9])\s*[sS]\s*$", '"', t)
    m = re.fullmatch(r"([0-9]+)(?:\s*[-\s°]\s*([0-9]+(?:[.,][0-9]+)?))?(?:\s*[-\s']\s*([0-9]+(?:[.,][0-9]+)?))?"
                     r"\s*[\"']*", bearings.normalise(t).strip())
    if m:
        d = float(m.group(1))
        mi = parse_number(m.group(2)) if m.group(2) else 0.0
        s = parse_number(m.group(3)) if m.group(3) else 0.0
        if mi >= 60 or s >= 60:
            raise ValueError(f"minutes or seconds out of range: {text!r}")
        return sign * (d + mi / 60.0 + s / 3600.0)
    return sign * bearings.parse_angle(t)


def parse_direction(text, convention="azimuth"):
    """Azimuth (degrees clockwise from north, 0 ≤ az < 360) of a typed direction. A plain angle is read in
    the user's convention ("azimuth": clockwise from north; "polar": counter-clockwise from east); a
    quadrant bearing (N45°30'E) is always a bearing."""
    t = ascii_text(text)
    if re.match(r"^[A-Za-z]\.?\s*[0-9]", t) or re.search(r"[0-9'\"°\s][NSns][EWOLewol]$", t):
        return bearings.parse(t) % 360.0
    a = parse_angle(t)
    if convention == "polar":
        return polar_to_azimuth(a)
    return a % 360.0


def parse_deflection(text):
    """Degrees of a deflection angle: + turns right (clockwise), - left. "12.5L", "12.5 R", "12 يسار"."""
    t = ascii_text(text)
    low = t.lower()
    sign = 1.0
    for word, s in (("يسار", -1.0), ("شمال", -1.0), ("يمين", 1.0), ("left", -1.0), ("right", 1.0),
                    ("lt", -1.0), ("rt", 1.0), ("l", -1.0), ("r", 1.0)):
        if low.endswith(word):
            sign = s
            t = t[: len(t) - len(word)].strip()
            break
    return sign * parse_angle(t)


def parse_coordinate(text):
    """A coordinate value: a number (25,4 / ١٢٫٥) or, for geographic maps, an angle 31°12'30"."""
    t = ascii_text(text)
    neg = False
    m = re.match(r"^(.*?)\s*([NSEWnsew])$", t)
    if m and re.search(r"[0-9]", m.group(1)):
        t, neg = m.group(1), m.group(2).upper() in "SW"
    try:
        v = parse_number(t)
    except ValueError:
        v = parse_angle(t)
    return -abs(v) if neg else v


# ---- directions --------------------------------------------------------------------------------------
def polar_to_azimuth(angle):
    return (90.0 - angle) % 360.0


def azimuth_to_polar(az):
    return (90.0 - az) % 360.0


def azimuth(x1, y1, x2, y2):
    """Azimuth (degrees clockwise from north) from (x1, y1) to (x2, y2)."""
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360.0


def deflection(az_prev, az_next):
    """Turn from one direction to the next, -180 < d ≤ 180 (+ = right)."""
    d = (az_next - az_prev) % 360.0
    return d - 360.0 if d > 180.0 else d


def point_at(x, y, az, dist):
    a = math.radians(az)
    return x + dist * math.sin(a), y + dist * math.cos(a)


# ---- writing values ----------------------------------------------------------------------------------
def format_angle(deg, dms=False, decimals=2):
    if dms:
        neg = deg < 0
        text = bearings.format_dms(abs(deg), 0)
        return ("-" if neg else "") + text
    return f"{deg:.{decimals}f}°"


def format_direction(az, convention="azimuth", dms=False, quadrant=False):
    if quadrant:
        return bearings.format(az, "quadrant")
    value = azimuth_to_polar(az) if convention == "polar" else az % 360.0
    return format_angle(value, dms)


def format_deflection(d, dms=False):
    text = format_angle(abs(d), dms)
    if abs(d) < 1e-9:
        return text
    return text + (" R" if d > 0 else " L")
