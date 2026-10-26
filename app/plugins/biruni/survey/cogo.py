"""COGO engine (no Qt): bearings, distances, curves, traverses, misclosure and
compass (Bowditch) adjustment.

Conventions: a bearing is an azimuth in degrees, clockwise from grid north.
A course goes from the current point along `bearing` for `distance`. With a
radius the course is a circular arc whose chord has that bearing; `distance`
is the chord length, or the arc length when `arc_length` is set; the arc
bulges to the right of the direction of travel for side "R", to the left
for side "L".
"""
import math
import re
from dataclasses import dataclass, field

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
# Quadrant letters, Latin and Arabic (شمال / جنوب / شرق / غرب).
_NS = {"N": "N", "S": "S", "ش": "N", "ج": "S"}
_EW = {"E": "E", "W": "W", "ق": "E", "غ": "W"}


def normalize_digits(text):
    """Arabic-Indic / Persian digits and the Arabic decimal mark to ASCII."""
    return str(text).translate(_DIGITS)


def _dms_to_deg(text):
    """'45°30'15"', '45 30 15', '45-30-15', '45d30m15s', '45:30:15', '45.5' -> degrees."""
    t = normalize_digits(text).strip()
    t = t.replace("º", "°").replace("’", "'").replace("′", "'").replace("″", '"').replace("”", '"')
    t = t.replace("''", '"')
    nums = re.findall(r"\d+(?:[.,]\d+)?", t)
    if not nums:
        raise ValueError(text)
    vals = [float(n.replace(",", ".")) for n in nums]
    if len(vals) > 3:
        raise ValueError(text)
    deg = vals[0]
    if len(vals) >= 2:
        if vals[1] >= 60:
            raise ValueError(text)
        deg += vals[1] / 60
    if len(vals) == 3:
        if vals[2] >= 60:
            raise ValueError(text)
        deg += vals[2] / 3600
    return deg


def parse_bearing(text):
    """Azimuth in degrees [0, 360) from a bearing in any common notation:
    decimal azimuth, D-M-S azimuth, or quadrant bearing (N45°30'E, S12d5mW,
    ش45 30 ق). Raises ValueError for text it cannot read."""
    if isinstance(text, (int, float)):
        return float(text) % 360
    t = normalize_digits(text).strip().upper()
    if not t:
        raise ValueError("empty bearing")
    first, last = t[0], t[-1]
    if first in _NS and last in _EW:
        angle = _dms_to_deg(t[1:-1])
        if angle > 90:
            raise ValueError(text)
        ns, ew = _NS[first], _EW[last]
        if ns == "N":
            az = angle if ew == "E" else 360 - angle
        else:
            az = 180 - angle if ew == "E" else 180 + angle
        return az % 360
    if any(c.isalpha() and c not in "DMS" for c in t):
        raise ValueError(text)
    neg = t.startswith("-")
    az = _dms_to_deg(t.lstrip("+-"))
    return (-az if neg else az) % 360


def format_bearing(az, quadrant=False, seconds_decimals=0):
    az %= 360
    if quadrant:
        if az <= 90:
            ns, ew, a = "N", "E", az
        elif az <= 180:
            ns, ew, a = "S", "E", 180 - az
        elif az <= 270:
            ns, ew, a = "S", "W", az - 180
        else:
            ns, ew, a = "N", "W", 360 - az
        return f"{ns}{_fmt_dms(a, seconds_decimals)}{ew}"
    return _fmt_dms(az, seconds_decimals)


def _fmt_dms(deg, sd=0):
    total = round(deg * 3600, sd)
    d = int(total // 3600)
    m = int((total - d * 3600) // 60)
    s = total - d * 3600 - m * 60
    s_txt = f"{s:0{3 + sd if sd else 2}.{sd}f}"
    return f"{d}°{m:02d}'{s_txt}\""


_UNITS = {"m": 1.0, "م": 1.0, "متر": 1.0, "ft": 0.3048, "'": 0.3048, "usft": 1200 / 3937, "km": 1000.0,
          "كم": 1000.0}


def parse_distance(text):
    """Distance in metres from '25.30', '25,30', '٢٥٫٣٠ م', '82.5 ft'."""
    if isinstance(text, (int, float)):
        return float(text)
    t = normalize_digits(text).strip().lower().replace(" ", "")
    m = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)([a-z'؀-ۿ]*)", t)
    if not m:
        raise ValueError(text)
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2)
    if unit and unit not in _UNITS:
        raise ValueError(text)
    return num * _UNITS.get(unit, 1.0)


def parse_radius(text):
    """(radius, side) from '25', 'R25', '25 L', 'L=25', '-25' (negative = left),
    'يمين 25' / 'يسار 25'. Empty -> (None, None)."""
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        if text == 0:
            return None, None
        return abs(float(text)), ("L" if text < 0 else "R")
    t = normalize_digits(text).strip()
    if not t or t.upper() in ("NULL", "NONE", "-"):
        return None, None
    side = "R"
    up = t.upper()
    if ("يسار" in t or "LEFT" in up or re.search(r"(^|[^A-Z])L([^A-Z]|$)", up)
            or up.startswith("-")):
        side = "L"
    num = re.search(r"\d+(?:[.,]\d+)?", t)
    if not num:
        raise ValueError(text)
    r = float(num.group(0).replace(",", "."))
    return (r, side) if r > 0 else (None, None)


@dataclass
class Course:
    bearing: float              # azimuth of the chord, degrees
    distance: float             # chord length (or arc length if arc_length)
    radius: float = None
    side: str = "R"
    arc_length: bool = False

    def chord(self):
        if not self.radius or not self.arc_length:
            return self.distance
        r = self.radius
        return 2 * r * math.sin(min(self.distance / (2 * r), math.pi / 2))

    def is_curve(self):
        return bool(self.radius)


def course_from_text(bearing, distance, radius=None, arc_length=False):
    r, side = parse_radius(radius)
    return Course(parse_bearing(bearing), parse_distance(distance), r, side or "R", arc_length)


def _step(x, y, az, d):
    a = math.radians(az)
    return x + d * math.sin(a), y + d * math.cos(a)


def arc_midpoint(p1, p2, radius, side):
    """Middle point of the minor arc of `radius` from p1 to p2, bulging right/left of p1->p2."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    c = math.hypot(dx, dy)
    if c == 0 or not radius:
        return None
    r = max(radius, c / 2)
    sag = r - math.sqrt(max(r * r - c * c / 4, 0.0))
    nx, ny = dy / c, -dx / c            # right-hand normal of the chord
    if side == "L":
        nx, ny = -nx, -ny
    return (p1[0] + dx / 2 + nx * sag, p1[1] + dy / 2 + ny * sag)


def segment_area(chord, radius):
    """Area between a chord and its minor arc."""
    if not radius or chord <= 0:
        return 0.0
    r = max(radius, chord / 2)
    theta = 2 * math.asin(min(chord / (2 * r), 1.0))
    return r * r / 2 * (theta - math.sin(theta))


@dataclass
class Traverse:
    start: tuple
    courses: list
    points: list = field(default_factory=list)      # unadjusted stations (start first)
    adjusted: list = field(default_factory=list)

    def __post_init__(self):
        self.compute()

    def compute(self):
        x, y = self.start
        self.points = [(x, y)]
        for c in self.courses:
            x, y = _step(x, y, c.bearing, c.chord())
            self.points.append((x, y))
        self.adjusted = list(self.points)

    # -- closure -------------------------------------------------------
    @property
    def total_length(self):
        return sum(c.chord() for c in self.courses)

    def misclosure(self, end=None):
        """(dx, dy, linear) error of the end point against `end` (default: the start)."""
        ex, ey = end if end is not None else self.start
        dx = self.points[-1][0] - ex
        dy = self.points[-1][1] - ey
        return dx, dy, math.hypot(dx, dy)

    def precision(self, end=None):
        """N of '1:N' (inf when it closes exactly)."""
        lin = self.misclosure(end)[2]
        return math.inf if lin == 0 else self.total_length / lin

    def adjust(self, end=None):
        """Compass (Bowditch) rule: distribute the misclosure in proportion to
        the distance travelled. Returns the adjusted stations."""
        dx, dy, _ = self.misclosure(end)
        total = self.total_length or 1.0
        run = 0.0
        out = [self.points[0]]
        for c, p in zip(self.courses, self.points[1:]):
            run += c.chord()
            k = run / total
            out.append((p[0] - dx * k, p[1] - dy * k))
        self.adjusted = out
        return out

    # -- geometry ------------------------------------------------------
    def vertices(self, adjusted=True, with_arcs=True):
        """Stations as a list of (x, y, kind) where kind is 'station' or 'arc'
        (an arc middle point, so a circular string can be built)."""
        pts = self.adjusted if adjusted else self.points
        out = [(pts[0][0], pts[0][1], "station")]
        for c, p1, p2 in zip(self.courses, pts, pts[1:]):
            if with_arcs and c.is_curve():
                m = arc_midpoint(p1, p2, c.radius, c.side)
                if m:
                    out.append((m[0], m[1], "arc"))
            out.append((p2[0], p2[1], "station"))
        return out

    def area(self, adjusted=True):
        """Area enclosed by the (closed) traverse, arcs included."""
        pts = self.adjusted if adjusted else self.points
        ring = list(pts)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        s = 0.0
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            s += x1 * y2 - x2 * y1
        s /= 2  # positive when counter-clockwise
        for c, p1, p2 in zip(self.courses, pts, pts[1:]):
            if c.is_curve():
                seg = segment_area(math.hypot(p2[0] - p1[0], p2[1] - p1[1]), c.radius)
                s += seg if c.side == "R" else -seg
        return abs(s)

    def perimeter(self):
        total = 0.0
        pts = self.adjusted
        for c, p1, p2 in zip(self.courses, pts, pts[1:]):
            ch = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            if c.is_curve():
                r = max(c.radius, ch / 2)
                total += 2 * r * math.asin(min(ch / (2 * r), 1.0))
            else:
                total += ch
        return total


def inverse(p1, p2):
    """(azimuth, distance) from p1 to p2."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    return math.degrees(math.atan2(dx, dy)) % 360, math.hypot(dx, dy)
