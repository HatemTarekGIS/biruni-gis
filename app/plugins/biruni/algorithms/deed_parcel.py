"""Deeds: draw a parcel from the boundary text of a deed (صك / عقد), and check
a surveyed parcel against its deed.

A deed gives the four sides (north, east, south, west) with their lengths and
neighbours, and usually the area - but no angles. Four lengths leave one
degree of freedom; the stated area fixes it. The text parser and the
geometry are plain Python (no Qt) so they are testable on their own."""
import math
import re
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField,
                       QgsFields, QgsGeometry, QgsLineString, QgsMultiLineString, QgsPointXY,
                       QgsProcessingException, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterBoolean, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterPoint, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType

from .. import bearings as brg
from .. import deed_languages as dl
from ..arabic_text import light_normalise, normalise, number_from_match, parse_length, words_to_number
from ..i18n import is_rtl, tr
from .base import BiruniAlgorithm

DIRS = ("N", "E", "S", "W")
DIR_NAMES = {"N": "North", "E": "East", "S": "South", "W": "West"}
_STEM_DIR = {"شمال": "N", "بحري": "N", "جنوب": "S", "قبلي": "S", "شرق": "E", "غرب": "W"}
_AR = "ء-ي"
_KEY = r"(?:يحد(?:ه|ها)?|حد(?:ه|ها)?|الحد|حدود(?:ه|ها)?|من|جهه|الجهه|ضلع|الضلع|واجهه|الواجهه|طرف|الطرف)"
_MARK = re.compile(rf"(?<![{_AR}])(و?)((?:{_KEY}\s+)*)(?:ال)?(شمال|جنوب|شرق|غرب|بحري|قبلي)(?:يه|ي|ا|ه)?(?![{_AR}])")
_AREA_KEY = re.compile(rf"(?<![{_AR}])(?:وب|و|ب)?(?:ال)?(?:مساحت(?:ه|ها)|مساحه|مسطح(?:ه|ها)?)(?![{_AR}])")
_LEN_KEY = re.compile(rf"(?<![{_AR}])(?:وب|و|ب)?(?:طول(?:ه|ها)?|امتداد(?:ه|ها)?|مسافه|يمتد)(?![{_AR}])")
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?"
_NUM_RE = re.compile(_NUM)
_M_UNIT = rf"(?:امتار|مترا|متر|م|m)(?![{_AR}\w²2])"
_LEN_NUM = re.compile(rf"\(?\s*({_NUM})\s*\)?\s*({_M_UNIT})?")
_MASKS = [
    re.compile(rf"(?:بعرض|عرض(?:ه|ها)?|رقم(?:ه|ها)?)\s*[:(]?\s*\(?\s*(?:{_NUM})[\d/.-]*\s*\)?\s*(?:{_M_UNIT})?"),
    re.compile(rf"(?:بعرض|عرض(?:ه|ها)?)\s+(?:[{_AR}]+\s+){{1,5}}?(?:امتار|مترا|متر)(?![{_AR}])"),
    re.compile(rf"(?<![{_AR}])(?:ال)?(?:قطعه|بلوك|بلك|مخطط|ق)\s*(?:رقم\s*)?[:(]?\s*\(?\s*\d+[\d/.-]*\)?"),
    re.compile(rf"(?:{_NUM})\s*(?:م2|م²|متر مربع|مترا مربعا)"),
]
_FEDDAN = {"فدان": 4200.83, "ف": 4200.83, "قيراط": 175.035, "ط": 175.035, "سهم": 7.293, "س": 7.293}
_FEDDAN_RE = re.compile(rf"({_NUM})\s*(فدان|قيراط|سهم|ف|ط|س)(?![{_AR}])")


@dataclass
class Side:
    direction: str
    lengths: list = field(default_factory=list)
    neighbour: str = ""
    kind: str = "other"          # street / parcel / property / other
    width: float = None
    number: str = ""
    broken: bool = False
    text: str = ""

    @property
    def length(self):
        return sum(self.lengths) if self.lengths else None


@dataclass
class Deed:
    sides: dict = field(default_factory=dict)
    area: float = None
    parcel: str = ""
    warnings: list = field(default_factory=list)

    def complete(self):
        return all(d in self.sides and self.sides[d].length for d in DIRS)


def _mask(s):
    for rx in _MASKS:
        s = rx.sub(lambda m: " " * len(m.group(0)), s)
    return s


def _piece_length(piece):
    """Length in metres in one piece of text, the first number winning."""
    if re.search(r"(?:سنتيمتر|سنتي|سم|cm)", piece):
        v = parse_length(piece)
        if v:
            return v
    m = _NUM_RE.search(piece)
    if m:
        return number_from_match(m.group(0))
    return words_to_number(piece)


def _side_lengths(seg):
    """[(start, metres)] of the lengths written in one side's text."""
    masked = _mask(seg)
    found = []
    keys = list(_LEN_KEY.finditer(masked))
    if keys:
        for i, k in enumerate(keys):
            end = keys[i + 1].start() if i + 1 < len(keys) else len(masked)
            chunk = masked[k.end():end]
            pieces = re.split(r"\+|ثم|ويليه|يليه", chunk)
            offset = k.end()
            for j, piece in enumerate(pieces):
                if j and not re.search(rf"(?:{_NUM})\s*(?:{_M_UNIT})|سم", piece):
                    offset += len(piece) + 1
                    continue
                v = _piece_length(piece)
                if v and v > 0:
                    found.append((k.start(), v))
                offset += len(piece) + 1
    else:
        for m in _LEN_NUM.finditer(masked):
            if m.group(2):
                v = number_from_match(m.group(1))
                if v:
                    found.append((m.start(), v))
    return found


def _neighbour(desc):
    n = normalise(desc)
    kind, width, number = "other", None, ""
    if re.search(r"شارع|طريق|ممر|سكه|ممشي|زقاق|حاره|مدخل|شارع", n):
        kind = "street"
        m = re.search(rf"عرض\S*\s*({_NUM})", n)
        if m:
            width = number_from_match(m.group(1))
        else:
            m = re.search(r"عرض\S*\s+(.{0,40}?)(?:امتار|مترا|متر)", n)
            if m:
                width = words_to_number(m.group(1))
    elif re.search(r"قطعه|بلك|بلوك", n):
        kind = "parcel"
    elif re.search(r"ملك|ورثه|منزل|عقار|جار|مسكن", n):
        kind = "property"
    m = re.search(rf"(?:رقم|قطعه)\s*\(?\s*(\d+[\d/-]*)", n)
    if m:
        number = m.group(1)
        if kind == "other":
            kind = "parcel"
    return kind, width, number


def _clean(t):
    t = t.strip(" \t\n:：-–—،,.؛;/|()")
    t = re.sub(r"^(?:و?يحد(?:ه|ها|هم)?|و?حد(?:ه|ها)|و)\s+", "", t.strip())
    t = re.sub(r"\s+(?:و|ب|وذلك)$", "", t)
    return re.sub(r"\s+", " ", t).strip(" :-–—،,.")


def parse_area(s):
    """Area in m² from light-normalised text (first area clause)."""
    for m in _AREA_KEY.finditer(s):
        chunk = s[m.end():m.end() + 120]
        stop = _MARK.search(chunk)
        if stop and stop.start() > 0:
            chunk = chunk[:stop.start()]
        fed = [(number_from_match(a), u) for a, u in _FEDDAN_RE.findall(chunk[:60])]
        if fed and any(u in ("فدان", "قيراط", "سهم", "ط") for _, u in fed):
            return sum(v * _FEDDAN[u] for v, u in fed if v is not None)
        n = _NUM_RE.search(chunk[:70])
        if n:
            return number_from_match(n.group(0))
        words = re.split(r"متر|م2|م²", chunk)[0]
        v = words_to_number(words)
        if v:
            return v
    return None


def parse_deed(text):
    """Sides, area and parcel number from the boundary text of a deed."""
    orig = str(text or "")
    s, index = light_normalise(orig)
    deed = Deed()

    def cut(a, b):
        if a >= b:
            return ""
        return orig[index[a]:index[b - 1] + 1]

    marks = []
    for m in _MARK.finditer(s):
        d = _STEM_DIR[m.group(3)]
        before = s[:m.start()].rstrip(" \t")
        has_key = bool(m.group(2).strip())
        separated = not before or before[-1] in "\n.,;:/-(|*•،؛)" or before.endswith((" و", "\nو")) or before == "و"
        prev_has_len = bool(marks) and bool(_side_lengths(s[marks[-1][1]:m.start()]))
        if d in {x[0] for x in marks}:
            continue
        if has_key or separated or prev_has_len:
            marks.append((d, m.end(), m.start()))
    area_pos = [m.start() for m in _AREA_KEY.finditer(s)]
    deed.area = parse_area(s)
    head = s[:marks[0][2]] if marks else s
    m = re.search(r"(?:قطعه|القطعه)\s*(?:(?:ال)?ارض\s*)?(?:رقم\s*)?[:(]?\s*\(?\s*(\d+[\d/-]*)", head)
    if m:
        deed.parcel = m.group(1)
    for i, (d, start, _) in enumerate(marks):
        end = marks[i + 1][2] if i + 1 < len(marks) else len(s)
        for p in area_pos:
            if start <= p < end:
                end = p
                break
        seg = s[start:end]
        lens = _side_lengths(seg)
        side = Side(d, [v for _, v in lens], text=cut(start, end).strip())
        first = min((p for p, _ in lens), default=len(seg))
        desc = _clean(cut(start, start + first)) if first else ""
        if not desc and lens:
            tail = seg[lens[-1][0]:]
            mm = _LEN_NUM.search(_mask(tail))
            after = tail[mm.end():] if mm else ""
            desc = _clean(cut(start + len(seg) - len(after), end)) if after.strip() else ""
        side.neighbour = desc
        side.kind, side.width, side.number = _neighbour(desc)
        side.broken = len(side.lengths) > 1 or bool(re.search(r"شطف|منكسر|ينكسر|انكسار", seg))
        deed.sides[d] = side
    for d in DIRS:
        if d not in deed.sides:
            deed.warnings.append(tr("The {side} side was not found in the text.").format(side=tr(DIR_NAMES[d])))
        elif not deed.sides[d].length:
            deed.warnings.append(tr("No length was found for the {side} side.").format(side=tr(DIR_NAMES[d])))
        elif deed.sides[d].broken:
            deed.warnings.append(tr("The {side} side has several segments (chamfer or broken line): drawn as one "
                                    "straight side of their total length.").format(side=tr(DIR_NAMES[d])))
    return deed


# ---- geometry ------------------------------------------------------------------------------

def polygon_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i - 1]
        x2, y2 = pts[i]
        a += x1 * y2 - x2 * y1
    return a / 2


def _quad(a, b, c, d, theta):
    """Corners SW, SE, NE, NW of the quadrilateral with south a, east b, north
    c, west d and angle theta (radians) at the south-west corner; None when
    the sides cannot close at that angle."""
    sw, se = (0.0, 0.0), (a, 0.0)
    nw = (d * math.cos(theta), d * math.sin(theta))
    dx, dy = nw[0] - se[0], nw[1] - se[1]
    q = math.hypot(dx, dy)
    if q < 1e-9 or q > b + c or q < abs(b - c):
        return None
    x = (b * b - c * c + q * q) / (2 * q)
    h2 = b * b - x * x
    h = math.sqrt(max(h2, 0.0))
    mx, my = se[0] + x * dx / q, se[1] + x * dy / q
    # the corner on the far side of the diagonal SE-NW from SW keeps the shape convex
    ne = (mx + h * dy / q, my - h * dx / q)
    if (dx * (ne[1] - se[1]) - dy * (ne[0] - se[0])) * (dx * (sw[1] - se[1]) - dy * (sw[0] - se[0])) > 0:
        ne = (mx - h * dy / q, my + h * dx / q)
    pts = [sw, se, ne, nw]
    return pts if polygon_area(pts) > 0 else None


def _angles(pts):
    out = []
    for i in range(len(pts)):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        v1 = (p0[0] - p1[0], p0[1] - p1[1])
        v2 = (p2[0] - p1[0], p2[1] - p1[1])
        n = math.hypot(*v1) * math.hypot(*v2)
        out.append(math.degrees(math.acos(max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / n)))) if n else 0)
    return out


def max_area(a, b, c, d):
    """Brahmagupta: the largest area any quadrilateral with these sides has."""
    s = (a + b + c + d) / 2
    return math.sqrt(max((s - a) * (s - b) * (s - c) * (s - d), 0.0))


def build_quad(a, b, c, d, area=None):
    """(corners SW, SE, NE, NW, method, warnings) for south a, east b, north c,
    west d and the deed area."""
    warnings = []
    if min(a, b, c, d) <= 0 or max(a, b, c, d) >= (a + b + c + d) - max(a, b, c, d):
        raise ValueError(tr("These four lengths cannot make a closed parcel."))
    if abs(a - c) < 0.01 and abs(b - d) < 0.01 and (not area or abs(a * b - area) <= max(0.005 * area, 0.5)):
        return [(0, 0), (a, 0), (a, d), (0, d)], "rectangle", warnings
    samples = []
    steps = 3600
    for i in range(1, steps):
        t = math.pi * i / steps
        pts = _quad(a, b, c, d, t)
        if pts:
            samples.append((t, polygon_area(pts)))
    if not samples:
        raise ValueError(tr("These four lengths cannot make a closed parcel."))
    t_max, a_max = max(samples, key=lambda x: x[1])
    if not area:
        warnings.append(tr("The deed gives no area: the parcel is drawn with the largest area its sides allow."))
        return _quad(a, b, c, d, t_max), "cyclic", warnings
    if area > a_max + 0.01:
        warnings.append(tr("The deed area ({area:.2f} m²) is larger than any parcel with these sides can have "
                           "({max:.2f} m²): drawn with the largest possible area.").format(area=area, max=a_max))
        return _quad(a, b, c, d, t_max), "cyclic", warnings
    roots = []
    for (t0, a0), (t1, a1) in zip(samples, samples[1:]):
        if (a0 - area) * (a1 - area) <= 0 and t1 - t0 < 2 * math.pi / steps:
            lo, hi, flo = t0, t1, a0 - area
            for _ in range(60):
                mid = (lo + hi) / 2
                pts = _quad(a, b, c, d, mid)
                fm = (polygon_area(pts) if pts else 0) - area
                if (fm < 0) == (flo < 0):
                    lo, flo = mid, fm
                else:
                    hi = mid
            roots.append((lo + hi) / 2)
    if not roots:
        t_min, a_min = min(samples, key=lambda x: x[1])
        warnings.append(tr("The deed area is smaller than these sides allow: drawn with the closest area."))
        return _quad(a, b, c, d, t_min), "closest", warnings
    best = min(roots, key=lambda t: sum(abs(x - 90) for x in _angles(_quad(a, b, c, d, t))))
    return _quad(a, b, c, d, best), "area", warnings


def _transform(pts, rotation, anchor, at_corner):
    """Rotate clockwise by `rotation` degrees and move the centre (or the
    south-west corner) to the anchor."""
    if at_corner:
        ox, oy = pts[0]
    else:
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        ox, oy = cx, cy
    r = math.radians(-rotation)
    cos, sin = math.cos(r), math.sin(r)
    out = []
    for x, y in pts:
        x, y = x - ox, y - oy
        out.append((anchor[0] + x * cos - y * sin, anchor[1] + x * sin + y * cos))
    return out


def side_summary(deed):
    parts = []
    for d in DIRS:
        sd = deed.sides.get(d)
        if sd and sd.length:
            parts.append(f"{tr(DIR_NAMES[d])} {sd.length:.2f}")
    return " / ".join(parts)


METHOD_NAMES = {"rectangle": "Rectangle", "area": "Four sides and the deed area", "cyclic": "Largest possible area",
                "closest": "Closest possible area", "traverse": "Bearings and distances (traverse)"}
DEED_CODES = [None] + list(dl.LANGUAGES)                  # None: recognise the style by itself
KIND_NAMES = {"street": "Street", "parcel": "Parcel", "property": "Property", "other": "Other"}


class DeedToParcel(BiruniAlgorithm):
    ID = "deedtoparcel"
    NAME = "Parcel from deed text"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Paste the boundaries of a deed as they are written - for example «شمالاً: شارع عرض 15م بطول 20.50م، "
            "جنوباً: قطعة رقم 12 بطول 20.50م … ومساحتها 615 م²» - and get the parcel drawn with its sides, "
            "neighbours and area. Saudi and Egyptian wordings are understood (بحري/قبلي, الحد الشمالي, يحدها, "
            "بطول/وطوله, lengths in words, Arabic digits, feddan/qirat/sahm). A deed gives no angles: the parcel "
            "is a rectangle when opposite sides are equal, otherwise the shape whose area matches the deed area. "
            "Place it at a point (its centre or its south-west corner) and turn it to match the site. "
            "Descriptions by bearings and distances are read too - Brazilian memorial descritivo, Spanish "
            "rumbos and cuadro de construcción, French gisements, Philippine technical descriptions with their "
            "tie line, Nigerian/Kenyan beacon schedules, Turkish, Persian, Urdu, Indonesian and English metes and "
            "bounds - with the misclosure, the precision (1:N) and the compass-rule adjustment.")

    TEXT, ANCHOR, ANCHOR_AT, CRS, ROTATION, OUTPUT, SIDES = (
        "TEXT", "ANCHOR", "ANCHOR_AT", "CRS", "ROTATION", "OUTPUT", "SIDES")
    LANGUAGE, TIE_POINT, ADJUST, PRECISION = "LANGUAGE", "TIE_POINT", "ADJUST", "PRECISION"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterString(self.TEXT, tr("Deed boundaries text"), multiLine=True))
        self.addParameter(QgsProcessingParameterPoint(self.ANCHOR, tr("Place at point"), optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.ANCHOR_AT, tr("The point is the parcel's"), [tr("Centre"), tr("South-west corner")], defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system (projected, in metres)"),
                                                    "ProjectCrs"))
        self.addParameter(QgsProcessingParameterNumber(
            self.ROTATION, tr("Turn clockwise (degrees)"), Qgis.ProcessingNumberParameterType.Double, 0.0,
            minValue=-360.0, maxValue=360.0))
        self.addParameter(QgsProcessingParameterEnum(
            self.LANGUAGE, tr("Language and style of the text"),
            [tr("Recognise automatically")] + list(dl.LANGUAGES.values()), defaultValue=0))
        self.addParameter(QgsProcessingParameterPoint(
            self.TIE_POINT, tr("Tie point of the description (BLLM, control point)"), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ADJUST, tr("Close the traverse with the compass rule"), defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.PRECISION, tr("Warn below a precision of 1:"), Qgis.ProcessingNumberParameterType.Integer, 5000,
            minValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Parcel"),
                                                            Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(self.SIDES, tr("Parcel sides"),
                                                            Qgis.ProcessingSourceType.VectorLine))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        text = self.parameterAsString(parameters, self.TEXT, context)
        code = DEED_CODES[self.parameterAsEnum(parameters, self.LANGUAGE, context)] \
            if parameters.get(self.LANGUAGE) else dl.detect(text)
        if code and code not in dl.SIDE_STYLES:
            try:
                return self._traverse(text, code, parameters, context, feedback)
            except ValueError as e:
                if parameters.get(self.LANGUAGE):
                    raise QgsProcessingException(str(e))
        deed = parse_deed(text)
        if not deed.complete():
            raise QgsProcessingException("\n".join(deed.warnings) or tr("No boundaries were found in the text."))
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        has_anchor = parameters.get(self.ANCHOR) not in (None, "")
        anchor = (0.0, 0.0)
        if crs.isValid() and crs.isGeographic():
            if not has_anchor:
                raise QgsProcessingException(tr("Choose a projected coordinate system in metres (UTM), or a point "
                                                "to place the parcel at."))
            wgs = QgsCoordinateReferenceSystem("EPSG:4326")
            p = self.parameterAsPoint(parameters, self.ANCHOR, context, wgs)
            zone = int((p.x() + 180) // 6) + 1
            crs = QgsCoordinateReferenceSystem(f"EPSG:{(32600 if p.y() >= 0 else 32700) + zone}")
            feedback.pushInfo(tr("Drawn in {crs} (the chosen coordinate system is in degrees).").format(
                crs=crs.authid()))
        if not crs.isValid():
            raise QgsProcessingException(tr("Choose a projected coordinate system in metres (UTM)."))
        if has_anchor:
            p = self.parameterAsPoint(parameters, self.ANCHOR, context, crs)
            anchor = (p.x(), p.y())
        else:
            feedback.pushWarning(tr("No point was given: the parcel is placed at 0,0 - move it into place."))
        sides = deed.sides
        a, b, c, d = (sides["S"].length, sides["E"].length, sides["N"].length, sides["W"].length)
        try:
            corners, method, warnings = build_quad(a, b, c, d, deed.area)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        warnings = deed.warnings + warnings
        for w in warnings:
            feedback.pushWarning(w)
        corners = _transform(corners, self.parameterAsDouble(parameters, self.ROTATION, context), anchor,
                             self.parameterAsEnum(parameters, self.ANCHOR_AT, context) == 1)
        geo_area = polygon_area(corners)

        pf = QgsFields()
        for name, t in (("parcel", QMetaType.Type.QString), ("sides", QMetaType.Type.QString),
                        ("deed_area", QMetaType.Type.Double), ("geometric_area", QMetaType.Type.Double),
                        ("area_diff", QMetaType.Type.Double), ("method", QMetaType.Type.QString),
                        ("warnings", QMetaType.Type.QString)):
            pf.append(QgsField(name, t))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, pf, Qgis.WkbType.Polygon, crs)
        ring = [QgsPointXY(x, y) for x, y in corners]
        f = QgsFeature(pf)
        f.setGeometry(QgsGeometry.fromPolygonXY([ring + [ring[0]]]))
        f.setAttributes([deed.parcel, side_summary(deed), deed.area, round(geo_area, 4),
                         round(geo_area - deed.area, 4) if deed.area else None, tr(METHOD_NAMES[method]),
                         " | ".join(warnings)])
        sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        lf = QgsFields()
        for name, t in (("direction", QMetaType.Type.QString), ("length", QMetaType.Type.Double),
                        ("segments", QMetaType.Type.QString), ("neighbour", QMetaType.Type.QString),
                        ("neighbour_type", QMetaType.Type.QString), ("street_width", QMetaType.Type.Double),
                        ("neighbour_no", QMetaType.Type.QString), ("label", QMetaType.Type.QString)):
            lf.append(QgsField(name, t))
        lsink, ldest = self.parameterAsSink(parameters, self.SIDES, context, lf, Qgis.WkbType.LineString, crs)
        order = [("S", 0, 1), ("E", 1, 2), ("N", 2, 3), ("W", 3, 0)]
        for key, i, j in order:
            sd = sides[key]
            lfeat = QgsFeature(lf)
            lfeat.setGeometry(QgsGeometry.fromPolylineXY([ring[i], ring[j]]))
            label = f"{sd.neighbour} - {sd.length:.2f} {tr('m')}" if sd.neighbour else f"{sd.length:.2f} {tr('m')}"
            lfeat.setAttributes([tr(DIR_NAMES[key]), sd.length, " + ".join(f"{v:.2f}" for v in sd.lengths),
                                 sd.neighbour, tr(KIND_NAMES[sd.kind]), sd.width, sd.number, label])
            lsink.addFeature(lfeat, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("Parcel drawn: {area:.2f} m² ({method}).").format(area=geo_area,
                                                                                 method=tr(METHOD_NAMES[method])))
        return {self.OUTPUT: dest, self.SIDES: ldest, "METHOD": method, "DEED_AREA": deed.area,
                "GEOMETRIC_AREA": geo_area}

    def _traverse(self, text, code, parameters, context, feedback):
        """A description by bearings and distances: coordinates from the courses (ValueError when none)."""
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        has_anchor = parameters.get(self.ANCHOR) not in (None, "")
        has_tie = parameters.get(self.TIE_POINT) not in (None, "")
        if crs.isValid() and crs.isGeographic():
            ref = self.ANCHOR if has_anchor else (self.TIE_POINT if has_tie else None)
            if ref is None:
                raise QgsProcessingException(tr("Choose a projected coordinate system in metres (UTM), or a point "
                                                "to place the parcel at."))
            p = self.parameterAsPoint(parameters, ref, context, QgsCoordinateReferenceSystem("EPSG:4326"))
            zone = int((p.x() + 180) // 6) + 1
            crs = QgsCoordinateReferenceSystem(f"EPSG:{(32600 if p.y() >= 0 else 32700) + zone}")
            feedback.pushInfo(tr("Drawn in {crs} (the chosen coordinate system is in degrees).").format(
                crs=crs.authid()))
        if not crs.isValid():
            raise QgsProcessingException(tr("Choose a projected coordinate system in metres (UTM)."))
        start = tie_xy = None
        if has_anchor:
            p = self.parameterAsPoint(parameters, self.ANCHOR, context, crs)
            start = (p.x(), p.y())
        if has_tie:
            p = self.parameterAsPoint(parameters, self.TIE_POINT, context, crs)
            tie_xy = (p.x(), p.y())
        adjust = self.parameterAsBoolean(parameters, self.ADJUST, context) if self.ADJUST in parameters else True
        trav = dl.parse(text, code, start=start, tie_xy=tie_xy, adjust=adjust)
        rotation = self.parameterAsDouble(parameters, self.ROTATION, context)
        pts = trav.ring()
        if rotation:
            ox, oy = pts[0]
            a = math.radians(-rotation)
            pts = [(ox + (x - ox) * math.cos(a) - (y - oy) * math.sin(a),
                    oy + (x - ox) * math.sin(a) + (y - oy) * math.cos(a)) for x, y in pts]
        required = self.parameterAsInt(parameters, self.PRECISION, context) if self.PRECISION in parameters else 5000
        warnings = [w for w in trav.warnings if not w.startswith("Precision")]
        prec = "∞" if math.isinf(trav.precision) else f"{trav.precision:,.0f}"
        closure = tr("Misclosure {m:.3f} m, precision 1:{p}").format(m=trav.misclosure, p=prec)
        if required and trav.precision < required:
            warnings.append(tr("The precision 1:{p} is below the required 1:{r}.").format(p=prec, r=f"{required:,}"))
        for w in warnings:
            feedback.pushWarning(w)
        feedback.pushInfo(closure)
        geo_area = abs(polygon_area(pts))

        pf = QgsFields()
        for name, t in (("parcel", QMetaType.Type.QString), ("sides", QMetaType.Type.QString),
                        ("deed_area", QMetaType.Type.Double), ("geometric_area", QMetaType.Type.Double),
                        ("area_diff", QMetaType.Type.Double), ("method", QMetaType.Type.QString),
                        ("warnings", QMetaType.Type.QString)):
            pf.append(QgsField(name, t))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, pf, Qgis.WkbType.Polygon, crs)
        ring = [QgsPointXY(x, y) for x, y in pts]
        f = QgsFeature(pf)
        f.setGeometry(QgsGeometry.fromPolygonXY([ring + [ring[0]]]))
        f.setAttributes(["", f"{len(trav.courses)} × {trav.perimeter:.2f}", None, round(geo_area, 4), None,
                         tr(METHOD_NAMES["traverse"]), " | ".join([closure] + warnings)])
        sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        lf = QgsFields()
        for name, t in (("direction", QMetaType.Type.QString), ("length", QMetaType.Type.Double),
                        ("segments", QMetaType.Type.QString), ("neighbour", QMetaType.Type.QString),
                        ("neighbour_type", QMetaType.Type.QString), ("street_width", QMetaType.Type.Double),
                        ("neighbour_no", QMetaType.Type.QString), ("label", QMetaType.Type.QString)):
            lf.append(QgsField(name, t))
        lsink, ldest = self.parameterAsSink(parameters, self.SIDES, context, lf, Qgis.WkbType.LineString, crs)
        notation = dl.default_notation(code)
        for i, c in enumerate(trav.courses):
            bearing = brg.format(c.azimuth, notation, code)
            lfeat = QgsFeature(lf)
            lfeat.setGeometry(QgsGeometry.fromPolylineXY([ring[i], ring[(i + 1) % len(ring)]]))
            lfeat.setAttributes([bearing, c.distance, f"{c.frm}-{c.to}", None, None, None, None,
                                 f"{bearing} - {c.distance:.2f} {tr('m')}"])
            lsink.addFeature(lfeat, QgsFeatureSink.Flag.FastInsert)
        feedback.pushInfo(tr("Parcel drawn: {area:.2f} m² ({method}).").format(
            area=geo_area, method=tr(METHOD_NAMES["traverse"])))
        return {self.OUTPUT: dest, self.SIDES: ldest, "METHOD": "traverse", "DEED_AREA": None,
                "GEOMETRIC_AREA": geo_area, "MISCLOSURE": trav.misclosure, "PRECISION": trav.precision,
                "LANGUAGE": trav.language}


# ---- deed against survey -------------------------------------------------------------------

def _ring(geom):
    """Exterior ring of the largest polygon part, without repeated or straight-through vertices,
    counter-clockwise."""
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    polys = [p for p in polys if p and p[0]]
    if not polys:
        return []
    ring = max(polys, key=lambda p: abs(polygon_area([(q.x(), q.y()) for q in p[0]])))[0]
    pts = [(q.x(), q.y()) for q in ring]
    if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 0.001:
            out.append(p)
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            p0, p1, p2 = out[i - 1], out[i], out[(i + 1) % len(out)]
            a1 = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            a2 = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
            dev = abs((math.degrees(a2 - a1) + 180) % 360 - 180)
            if dev < 1.0:
                del out[i]
                changed = True
                break
    if polygon_area(out) < 0:
        out.reverse()
    return out


def _normal_dir(az):
    az %= 360
    if az >= 315 or az < 45:
        return "N"
    if az < 135:
        return "E"
    if az < 225:
        return "S"
    return "W"


def survey_sides(pts, deed=None, measure=None):
    """{direction: [(p0, p1, length)]} - each edge goes to the side its outside faces;
    a short corner cut (chamfer) goes to whichever of its two sides the deed explains better."""
    measure = measure or (lambda p, q: math.hypot(q[0] - p[0], q[1] - p[1]))
    edges = []
    for i in range(len(pts)):
        p, q = pts[i], pts[(i + 1) % len(pts)]
        az = math.degrees(math.atan2(q[0] - p[0], q[1] - p[1]))      # clockwise from grid north
        normal = (az + 90) % 360                                       # outside of a counter-clockwise ring
        edges.append((p, q, measure(p, q), normal))
    longest = max((e[2] for e in edges), default=0)
    sides = {d: [] for d in DIRS}
    chamfers = []
    for p, q, ln, normal in edges:
        diag = min(abs(((normal - c) + 180) % 360 - 180) for c in (45, 135, 225, 315))
        if diag < 20 and ln < 0.3 * longest:
            chamfers.append((p, q, ln, normal))
        else:
            sides[_normal_dir(normal)].append((p, q, ln))
    for p, q, ln, normal in chamfers:
        options = {_normal_dir(normal - 30), _normal_dir(normal + 30)}
        if deed is not None and all(d in deed.sides and deed.sides[d].length for d in options):
            def gain(d):
                cur = sum(e[2] for e in sides[d])
                return abs(cur + ln - deed.sides[d].length) - abs(cur - deed.sides[d].length)
            sides[min(sorted(options), key=gain)].append((p, q, ln))
        else:
            sides[_normal_dir(normal)].append((p, q, ln))
    return sides


VERDICTS = {"match": "Matches", "within": "Within tolerance", "fail": "Does not match", "unread": "Deed not read"}


def compare(deed, survey, survey_area, abs_tol=0.10, pct_tol=0.5, area_pct=1.0):
    """Per-side rows and the verdict for one parcel. survey = {direction: length}."""
    rows, worst = [], 0.0
    verdict = "match"
    for d in DIRS:
        sd = deed.sides.get(d)
        dl = sd.length if sd else None
        sl = survey.get(d)
        diff = None if dl is None or sl is None else sl - dl
        allowed = max(abs_tol, pct_tol / 100 * dl) if dl else abs_tol
        ok = diff is not None and abs(diff) <= allowed + 1e-9
        if diff is None:
            verdict = "fail"
        else:
            worst = max(worst, abs(diff))
            if not ok:
                verdict = "fail"
            elif abs(diff) > 0.02 and verdict == "match":
                verdict = "within"
        rows.append((d, dl, sl, diff, allowed, ok, sd.neighbour if sd else ""))
    area_diff = survey_area - deed.area if deed.area else None
    if area_diff is not None:
        if abs(area_diff) > area_pct / 100 * deed.area + 1e-9:
            verdict = "fail"
        elif abs(area_diff) > max(0.5, 0.001 * deed.area) and verdict == "match":
            verdict = "within"
    return rows, area_diff, worst, verdict


class DeedVsSurvey(BiruniAlgorithm):
    ID = "deedvssurvey"
    NAME = "Compare deed with survey"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Checks surveyed parcels against their deeds. Each parcel's boundary is split into its north, east, "
            "south and west sides (by the direction each side faces; corner cuts go to the side the deed "
            "explains), and each side's length and the area are compared with the deed. A side is within "
            "tolerance when its difference is at most the larger of the metres and the percentage given; the "
            "area when its difference is at most the area percentage. Verdict: matches (sides within 2 cm, area "
            "within 0.1%), within tolerance, or does not match. The deed text is pasted once, or read for every "
            "parcel from a field of the layer.")

    INPUT, TEXT, TEXT_FIELD, ABS_TOL, PCT_TOL, AREA_TOL, SIDES, OUTPUT, REPORT = (
        "INPUT", "TEXT", "TEXT_FIELD", "ABS_TOL", "PCT_TOL", "AREA_TOL", "SIDES", "OUTPUT", "REPORT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Surveyed parcels"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterString(self.TEXT, tr("Deed boundaries text"), multiLine=True,
                                                       optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.TEXT_FIELD, tr("Or: field with each parcel's deed text"), parentLayerParameterName=self.INPUT,
            type=Qgis.ProcessingFieldParameterDataType.String, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.ABS_TOL, tr("Side tolerance (m)"), Qgis.ProcessingNumberParameterType.Double, 0.10, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.PCT_TOL, tr("Side tolerance (% of the length)"), Qgis.ProcessingNumberParameterType.Double, 0.5,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.AREA_TOL, tr("Area tolerance (%)"), Qgis.ProcessingNumberParameterType.Double, 1.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(self.SIDES, tr("Sides compared"),
                                                            Qgis.ProcessingSourceType.VectorLine))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Parcels compared"),
                                                            Qgis.ProcessingSourceType.VectorPolygon))
        rep = QgsProcessingParameterFileDestination(self.REPORT, tr("Excel report"), tr("Excel files (*.xlsx)"),
                                                    optional=True, createByDefault=False)
        self.addParameter(rep)

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        text = self.parameterAsString(parameters, self.TEXT, context)
        tfield = self.parameterAsString(parameters, self.TEXT_FIELD, context)
        if not text.strip() and not tfield:
            raise QgsProcessingException(tr("Paste the deed text or choose the field that holds it."))
        abs_tol = self.parameterAsDouble(parameters, self.ABS_TOL, context)
        pct_tol = self.parameterAsDouble(parameters, self.PCT_TOL, context)
        area_tol = self.parameterAsDouble(parameters, self.AREA_TOL, context)
        crs = source.sourceCrs()
        da = QgsDistanceArea()
        if crs.isGeographic():
            da.setSourceCrs(crs, context.transformContext())
            da.setEllipsoid(context.ellipsoid() or "EPSG:7030")
            measure = lambda p, q: da.measureLine(QgsPointXY(*p), QgsPointXY(*q))  # noqa: E731
        else:
            measure = None

        sf = QgsFields()
        for name, t in (("parcel_id", QMetaType.Type.LongLong), ("direction", QMetaType.Type.QString),
                        ("deed_length", QMetaType.Type.Double), ("survey_length", QMetaType.Type.Double),
                        ("diff", QMetaType.Type.Double), ("allowed", QMetaType.Type.Double),
                        ("within_tolerance", QMetaType.Type.Bool), ("neighbour", QMetaType.Type.QString)):
            sf.append(QgsField(name, t))
        ssink, sdest = self.parameterAsSink(parameters, self.SIDES, context, sf, Qgis.WkbType.MultiLineString, crs)
        pf = QgsFields()
        for name, t in (("parcel_id", QMetaType.Type.LongLong), ("deed_parcel", QMetaType.Type.QString),
                        ("deed_area", QMetaType.Type.Double), ("survey_area", QMetaType.Type.Double),
                        ("area_diff", QMetaType.Type.Double), ("area_diff_pct", QMetaType.Type.Double),
                        ("sides_ok", QMetaType.Type.QString), ("max_side_diff", QMetaType.Type.Double),
                        ("verdict", QMetaType.Type.QString), ("notes", QMetaType.Type.QString)):
            pf.append(QgsField(name, t))
        psink, pdest = self.parameterAsSink(parameters, self.OUTPUT, context, pf, Qgis.WkbType.MultiPolygon, crs)

        counts = {k: 0 for k in VERDICTS}
        report_rows, report_sides = [], []
        total = source.featureCount() or 1
        fidx = source.fields().lookupField(tfield) if tfield else -1
        for n, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            g = feat.geometry()
            if g.isNull() or g.isEmpty():
                continue
            t = feat.attributes()[fidx] if fidx >= 0 else None
            t = str(t) if t not in (None, "") and str(t).strip() and str(t) != "NULL" else text
            deed = parse_deed(t)
            pts = _ring(g)
            survey_area = da.measureArea(g) if crs.isGeographic() else abs(g.area())
            pg = QgsGeometry(g)
            pg.convertToMultiType()
            out = QgsFeature(pf)
            out.setGeometry(pg)
            if not deed.complete() or len(pts) < 3:
                counts["unread"] += 1
                out.setAttributes([feat.id(), deed.parcel, deed.area, survey_area, None, None, "", None,
                                   tr(VERDICTS["unread"]), " | ".join(deed.warnings)])
                psink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                continue
            sides = survey_sides(pts, deed, measure)
            lengths = {d: sum(e[2] for e in sides[d]) if sides[d] else None for d in DIRS}
            rows, area_diff, worst, verdict = compare(deed, lengths, survey_area, abs_tol, pct_tol, area_tol)
            counts[verdict] += 1
            for d, dl, sl, diff, allowed, ok, neigh in rows:
                lf = QgsFeature(sf)
                parts = QgsMultiLineString()
                for p, q, _ in sides[d]:
                    parts.addGeometry(QgsLineString([QgsPointXY(*p), QgsPointXY(*q)]))
                lf.setGeometry(QgsGeometry(parts))
                lf.setAttributes([feat.id(), tr(DIR_NAMES[d]), dl, sl, None if diff is None else round(diff, 4),
                                  round(allowed, 4), ok, neigh])
                ssink.addFeature(lf, QgsFeatureSink.Flag.FastInsert)
                report_sides.append([feat.id(), deed.parcel, tr(DIR_NAMES[d]), dl, sl,
                                     None if diff is None else round(diff, 3), round(allowed, 3),
                                     tr("Yes") if ok else tr("No"), neigh])
            ok_count = sum(1 for r in rows if r[5])
            pct = round(100 * area_diff / deed.area, 3) if area_diff is not None else None
            attrs = [feat.id(), deed.parcel, deed.area, round(survey_area, 3),
                     None if area_diff is None else round(area_diff, 3), pct, f"{ok_count}/4", round(worst, 3),
                     tr(VERDICTS[verdict]), " | ".join(deed.warnings)]
            out.setAttributes(attrs)
            psink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            report_rows.append(attrs)
            feedback.setProgress(100 * n / total)

        feedback.pushInfo(tr("{m} match, {w} within tolerance, {f} do not match, {u} deeds not read.").format(
            m=counts["match"], w=counts["within"], f=counts["fail"], u=counts["unread"]))
        result = {self.SIDES: sdest, self.OUTPUT: pdest, "MATCH": counts["match"], "WITHIN": counts["within"],
                  "FAIL": counts["fail"], "UNREAD": counts["unread"]}
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)
        if report:
            write_report(report, report_rows, report_sides)
            result[self.REPORT] = report
        return result


def write_report(path, parcels, sides):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F4E79")
    colours = {tr(VERDICTS["match"]): "C6EFCE", tr(VERDICTS["within"]): "FFEB9C", tr(VERDICTS["fail"]): "FFC7CE"}
    sheets = [
        (tr("Parcels"), [tr("Parcel ID"), tr("Deed parcel number"), tr("Deed area (m²)"), tr("Survey area (m²)"),
                         tr("Area difference (m²)"), tr("Area difference (%)"), tr("Sides within tolerance"),
                         tr("Largest side difference (m)"), tr("Verdict"), tr("Notes")], parcels, 8),
        (tr("Sides"), [tr("Parcel ID"), tr("Deed parcel number"), tr("Side"), tr("Deed length (m)"),
                       tr("Survey length (m)"), tr("Difference (m)"), tr("Allowed (m)"), tr("Within tolerance"),
                       tr("Neighbour")], sides, None),
    ]
    for i, (title, header, rows, verdict_col) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title[:31]
        ws.sheet_view.rightToLeft = is_rtl()
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = head, fill
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
            if verdict_col is not None and r[verdict_col] in colours:
                ws.cell(ws.max_row, verdict_col + 1).fill = PatternFill("solid", fgColor=colours[r[verdict_col]])
        for col in ws.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(12, width + 2), 60)
        ws.freeze_panes = "A2"
    wb.save(path)
