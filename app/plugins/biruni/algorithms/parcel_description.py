"""Boundaries text of a parcel (حدود وأطوال) - the reverse of «Parcel from deed text».

From a parcel polygon: each side (north, south, east, west) with its length and
its neighbour - the adjacent parcel by its number, a street by its name and
width, or vacant land - the area in figures and in Arabic words, and the
corner coordinates. The wording is the one deeds and survey reports use, so
«Parcel from deed text» reads it back to the same lengths and area.

The text building is plain Python; the neighbour search uses qgis.core only."""
import math
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterPoint, QgsProcessingParameterString, QgsProject, QgsSpatialIndex)
from qgis.PyQt.QtCore import QMetaType

from .. import bearings as brg
from .. import deed_languages as dl
from .. import units as un
from ..i18n import is_rtl, tr
from .base import BiruniAlgorithm
from .deed_parcel import _normal_dir, polygon_area

DIRS_ORDER = ("N", "S", "E", "W")                    # the order deeds list the sides in
STYLES = {
    "saudi": {"N": "شمالاً", "S": "جنوباً", "E": "شرقاً", "W": "غرباً"},
    "egypt": {"N": "بحري", "S": "قبلي", "E": "شرقي", "W": "غربي"},
}
DIR_EN = {"N": "North", "S": "South", "E": "East", "W": "West"}
DEFAULTS = [("أرض فضاء", "vacant land"), ("ملك الغير", "property of others"), ("جار", "neighbour")]

NUMBER_FIELDS = ("parcel_no", "parcelno", "parcel_num", "plot_no", "plotno", "parcel", "plot", "p_no", "pno",
                 "land_no", "رقم_القطعة", "رقم القطعة", "القطعة", "قطعة", "رقم", "number", "no", "num")
STREET_NAME_FIELDS = ("street_name", "st_name", "name_ar", "arabic_name", "name", "street", "اسم_الشارع", "الاسم",
                      "اسم")
STREET_WIDTH_FIELDS = ("width", "street_w", "st_width", "street_width", "road_width", "w", "العرض", "عرض",
                       "عرض_الشارع")


# ---- number words ----------------------------------------------------------------------------
_ONES = ["", "واحد", "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة"]
_TENS = ["", "عشرة", "عشرون", "ثلاثون", "أربعون", "خمسون", "ستون", "سبعون", "ثمانون", "تسعون"]
_HUNDREDS = ["", "مائة", "مائتان", "ثلاثمائة", "أربعمائة", "خمسمائة", "ستمائة", "سبعمائة", "ثمانمائة", "تسعمائة"]


def _below_1000(n):
    h, r = divmod(n, 100)
    parts = [_HUNDREDS[h]] if h else []
    if r:
        if r < 10:
            parts.append(_ONES[r])
        elif r == 10:
            parts.append("عشرة")
        elif r == 11:
            parts.append("أحد عشر")
        elif r == 12:
            parts.append("اثنا عشر")
        elif r < 20:
            parts.append(f"{_ONES[r - 10]} عشر")
        else:
            t, u = divmod(r, 10)
            parts.append(f"{_ONES[u]} و{_TENS[t]}" if u else _TENS[t])
    return " و".join(parts)


def _scale(k, one, two, plural, accusative):
    if k == 1:
        return one
    if k == 2:
        return two
    if 3 <= k <= 10:
        return f"{_below_1000(k)} {plural}"
    if 11 <= k % 100 <= 99:
        return f"{_below_1000(k)} {accusative}"
    return f"{_below_1000(k)} {one}"


def number_words(n):
    """Arabic words of a whole number: 615 -> «ستمائة وخمسة عشر»."""
    n = int(n)
    if n == 0:
        return "صفر"
    millions, rest = divmod(n, 1_000_000)
    thousands, units = divmod(rest, 1000)
    parts = []
    if millions:
        parts.append(_scale(millions, "مليون", "مليونان", "ملايين", "مليوناً"))
    if thousands:
        parts.append(_scale(thousands, "ألف", "ألفان", "آلاف", "ألفاً"))
    if units:
        parts.append(_below_1000(units))
    return " و".join(parts)


def _square_metres(n):
    """The counted noun after a whole number of square metres."""
    r = n % 100
    if 3 <= r <= 10:
        return "أمتار مربعة"
    if 11 <= r <= 99:
        return "متراً مربعاً"
    return "متر مربع"


def area_words(area):
    """«فقط ستمائة وخمسة عشر متراً مربعاً لا غير» (hundredths read after «فاصلة»)."""
    cents = int(round(float(area) * 100))
    whole, hundredths = divmod(cents, 100)
    if not hundredths:
        if whole == 1:
            text = "متر مربع واحد"
        elif whole == 2:
            text = "متران مربعان"
        else:
            text = f"{number_words(whole)} {_square_metres(whole)}"
    else:
        frac = f"صفر {_ONES[hundredths]}" if hundredths < 10 else number_words(hundredths)
        text = f"{number_words(whole)} فاصلة {frac} متراً مربعاً"
    return f"فقط {text} لا غير"


_EN_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _en_below_1000(n):
    h, r = divmod(n, 100)
    parts = [f"{_EN_ONES[h]} hundred"] if h else []
    if r:
        if r < 20:
            parts.append(_EN_ONES[r])
        else:
            t, u = divmod(r, 10)
            parts.append(f"{_EN_TENS[t]}-{_EN_ONES[u]}" if u else _EN_TENS[t])
    return " ".join(parts)


def number_words_en(n):
    n = int(n)
    if n == 0:
        return "zero"
    parts = []
    for size, name in ((1_000_000, "million"), (1000, "thousand")):
        k, n = divmod(n, size)
        if k:
            parts.append(f"{_en_below_1000(k)} {name}")
    if n:
        parts.append(_en_below_1000(n))
    return " ".join(parts)


def area_words_en(area):
    cents = int(round(float(area) * 100))
    whole, hundredths = divmod(cents, 100)
    text = number_words_en(whole)
    if hundredths:
        text += f" point {number_words_en(hundredths) if hundredths >= 10 else 'zero ' + _EN_ONES[hundredths]}"
    return f"{text} square metres"


def fmt_length(v):
    return f"{v:.2f}"


def fmt_width(w):
    return f"{w:g}" if abs(w - round(w)) > 1e-9 else str(int(round(w)))


# ---- sides -----------------------------------------------------------------------------------

@dataclass
class Edge:
    p: tuple
    q: tuple
    length: float
    chamfer: bool = False
    neighbours: list = field(default_factory=list)   # [(arabic, english)]


@dataclass
class Description:
    sides: dict                                       # {direction: [Edge]}
    area: float
    corners: list                                     # [(no, x, y)]
    style: str = "saudi"
    default: tuple = DEFAULTS[0]

    def side_neighbours(self, d):
        seen, out = set(), []
        for e in self.sides.get(d, []):
            for n in e.neighbours:
                if n[0] not in seen:
                    seen.add(n[0])
                    out.append(n)
        return out or [self.default]

    def side_length(self, d):
        return sum(e.length for e in self.sides.get(d, []))

    def side_ar(self, d):
        edges = self.sides.get(d, [])
        if not edges:
            return ""
        neigh = " + ".join(a for a, _ in self.side_neighbours(d))
        regular = [e.length for e in edges if not e.chamfer]
        chamfers = [e.length for e in edges if e.chamfer]
        if not regular:
            regular, chamfers = chamfers, []
        text = f"{STYLES[self.style][d]}: {neigh} بطول " + " ثم ".join(f"{fmt_length(v)}م" for v in regular)
        if chamfers:
            text += " وشطفة بطول " + " ثم ".join(f"{fmt_length(v)}م" for v in chamfers)
        if len(regular) > 1:
            text += " (منكسر)"
        return text

    def side_en(self, d):
        edges = self.sides.get(d, [])
        if not edges:
            return ""
        neigh = " + ".join(e for _, e in self.side_neighbours(d))
        regular = [e.length for e in edges if not e.chamfer] or [e.length for e in edges]
        chamfers = [e.length for e in edges if e.chamfer] if any(not e.chamfer for e in edges) else []
        text = f"{DIR_EN[d]}: {neigh}, length " + " then ".join(f"{fmt_length(v)} m" for v in regular)
        if chamfers:
            text += ", corner cut " + " then ".join(f"{fmt_length(v)} m" for v in chamfers)
        if len(regular) > 1:
            text += " (broken line)"
        return text

    def area_ar(self):
        return f"ومساحتها: {self.area:,.2f} م² ({area_words(self.area)})"

    def area_en(self):
        return f"Area: {self.area:,.2f} m² ({area_words_en(self.area)})"

    def text_ar(self):
        return "\n".join([t for t in (self.side_ar(d) for d in DIRS_ORDER) if t] + [self.area_ar()])

    def text_en(self):
        return "\n".join([t for t in (self.side_en(d) for d in DIRS_ORDER) if t] + [self.area_en()])

    def corners_text(self, decimals=3):
        return "; ".join(f"{n}: {x:.{decimals}f}, {y:.{decimals}f}" for n, x, y in self.corners)


def merge_straight(pts, tol_deg=1.0):
    """Ring without points on a straight line (the sides of a deed); counter-clockwise."""
    out = [p for i, p in enumerate(pts) if i == 0 or math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) > 1e-3]
    if len(out) > 1 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= 1e-3:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            p0, p1, p2 = out[i - 1], out[i], out[(i + 1) % len(out)]
            a1 = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            a2 = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
            if abs((math.degrees(a2 - a1) + 180) % 360 - 180) < tol_deg:
                del out[i]
                changed = True
                break
    if polygon_area(out) < 0:
        out.reverse()
    return out


def split_sides(pts):
    """{direction: [Edge]} for a counter-clockwise ring: each edge goes to the side its outside faces; a short
    edge facing a diagonal is a corner cut (شطفة) and joins the side it leans to."""
    edges = []
    for i in range(len(pts)):
        p, q = pts[i], pts[(i + 1) % len(pts)]
        az = math.degrees(math.atan2(q[0] - p[0], q[1] - p[1]))
        edges.append((p, q, math.hypot(q[0] - p[0], q[1] - p[1]), (az + 90) % 360))
    longest = max((e[2] for e in edges), default=0)
    sides = {d: [] for d in DIRS_ORDER}
    for p, q, ln, normal in edges:
        diag = min(abs(((normal - c) + 180) % 360 - 180) for c in (45, 135, 225, 315))
        chamfer = diag < 20 and ln < 0.3 * longest and len(edges) > 4
        sides[_normal_dir(normal)].append(Edge(p, q, ln, chamfer))
    return sides


def corner_table(pts):
    """Corners numbered from the north-west one, clockwise: [(no, x, y)]."""
    ring = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if polygon_area(ring) > 0:
        ring.reverse()                                 # clockwise
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    w, h = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    start = min(range(len(ring)), key=lambda i: (ring[i][0] - min(xs)) / w + (max(ys) - ring[i][1]) / h)
    ring = ring[start:] + ring[:start]
    return [(i + 1, x, y) for i, (x, y) in enumerate(ring)]


# ---- neighbours ----------------------------------------------------------------------------

class NeighbourText(tuple):
    """(arabic, english) text of a neighbour that also keeps what it is (kind, number, name, width), so the
    boundary description can be written in other languages too (deed_languages.neighbour_text)."""

    def __new__(cls, ar, en, kind, number="", name="", width=None):
        obj = super().__new__(cls, (ar, en))
        obj.kind, obj.number, obj.name, obj.width, obj.default, obj.text = kind, number, name, width, 0, ""
        return obj


def guess_field(fields, names):
    lower = {f.name().lower(): f.name() for f in fields}
    for n in names:
        if n in lower:
            return lower[n]
    return ""


class NeighbourFinder:
    """What lies beyond each edge: another parcel (by number), a street (name and width), or nothing.
    Geometries are in one projected working system; offsets and distances are in its metres."""

    def __init__(self, parcels, streets, offset=0.5, search=20.0):
        self.parcels = parcels                         # {fid: (geometry, number)}
        self.pindex = QgsSpatialIndex()
        for fid, (g, _) in parcels.items():
            self.pindex.addFeature(fid, g.boundingBox())
        self.streets = streets                          # [(geometry, name, width)]
        self.sindex = QgsSpatialIndex()
        for i, (g, _, _) in enumerate(streets):
            self.sindex.addFeature(i, g.boundingBox())
        self.offset, self.search = offset, search

    def _parcel_text(self, number):
        if number in (None, ""):
            return NeighbourText("قطعة مجاورة", "adjacent parcel", "parcel")
        return NeighbourText(f"قطعة رقم {number}", f"parcel no. {number}", "parcel", number=str(number))

    @staticmethod
    def _street_text(name, width):
        ar, en = "شارع", "street"
        if name:
            nm = str(name).strip()
            if nm.startswith(("شارع", "طريق", "ممر")):
                ar = nm
            else:
                ar = f"شارع {nm}"
            en = f"street {nm}"
        if width:
            ar += f" عرض {fmt_width(width)}م"
            en += f" {fmt_width(width)} m wide"
        return NeighbourText(ar, en, "street", name=str(name).strip() if name else "", width=width)

    def edge(self, p, q, own):
        dx, dy = q[0] - p[0], q[1] - p[1]
        ln = math.hypot(dx, dy)
        if ln < 1e-9:
            return []
        nx, ny = dy / ln, -dx / ln                     # outside of a counter-clockwise ring
        found, seen = [], set()

        def add(item):
            if item[0] not in seen:
                seen.add(item[0])
                found.append(item)

        for t in (0.1, 0.3, 0.5, 0.7, 0.9):
            x, y = p[0] + dx * t + nx * self.offset, p[1] + dy * t + ny * self.offset
            pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            rect = pt.boundingBox().buffered(1e-6)
            hit = False
            for fid in self.pindex.intersects(rect):
                if fid != own and self.parcels[fid][0].contains(pt):
                    add(self._parcel_text(self.parcels[fid][1]))
                    hit = True
                    break
            if hit:
                continue
            for i in self.sindex.intersects(rect):
                g, name, width = self.streets[i]
                if g.type() == Qgis.GeometryType.Polygon and g.contains(pt):
                    add(self._street_text(name, width))
                    hit = True
                    break
            if hit:
                continue
            line = self._street_line(p, q, nx, ny, t)
            if line is not None:
                add(self._street_text(*line))
        return found

    def _street_line(self, p, q, nx, ny, t):
        """A centreline parallel to the edge, on its outside, within the search distance."""
        mx, my = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
        mid = QgsGeometry.fromPointXY(QgsPointXY(mx, my))
        rect = mid.boundingBox().buffered(self.search)
        edge_az = math.atan2(q[1] - p[1], q[0] - p[0])
        best = None
        for i in self.sindex.intersects(rect):
            g, name, width = self.streets[i]
            if g.type() != Qgis.GeometryType.Line:
                continue
            dist, closest, after, _ = g.closestSegmentWithContext(QgsPointXY(mx, my))
            dist = math.sqrt(dist)
            if dist > self.search or (width and dist > width / 2 + self.search):
                continue
            if (closest.x() - mx) * nx + (closest.y() - my) * ny <= 0:
                continue                                 # inside or behind the parcel
            v0 = g.vertexAt(after - 1)
            v1 = g.vertexAt(after)
            seg_az = math.atan2(v1.y() - v0.y(), v1.x() - v0.x())
            diff = abs((math.degrees(seg_az - edge_az) + 90) % 180 - 90)
            if diff > 25:
                continue
            if best is None or dist < best[0]:
                best = (dist, name, width)
        return None if best is None else best[1:]


def working_crs(crs, extent_centre_wgs):
    """The system lengths and areas are measured in: the layer's own when projected, else its UTM zone."""
    if crs.isValid() and not crs.isGeographic():
        return crs
    lon, lat = extent_centre_wgs
    zone = int((lon + 180) // 6) + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def centre_wgs(source_crs, extent, context):
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    tr_ = QgsCoordinateTransform(source_crs, wgs, context.transformContext() if context else QgsProject.instance())
    c = tr_.transform(extent.center())
    return c.x(), c.y()


def outer_ring(geom):
    """Exterior ring of the largest polygon part as [(x, y)]."""
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    polys = [p for p in polys if p and p[0]]
    if not polys:
        return []
    ring = max(polys, key=lambda p: abs(polygon_area([(q.x(), q.y()) for q in p[0]])))[0]
    return [(q.x(), q.y()) for q in ring]


def describe_geometry(geom, finder=None, own=None, style="saudi", default=DEFAULTS[0], corner_geom=None):
    """Description of one parcel geometry (projected, metres). corner_geom: the same parcel in the system
    the corner coordinates are reported in (default: geom)."""
    ring = outer_ring(geom)
    pts = merge_straight(ring)
    sides = split_sides(pts)
    if finder is not None:
        for edges in sides.values():
            for e in edges:
                e.neighbours = finder.edge(e.p, e.q, own)
    corners = corner_table(outer_ring(corner_geom) if corner_geom is not None else ring)
    return Description(sides, abs(geom.area()), corners, style, default)


# ---- shared parameters -------------------------------------------------------------------------

def add_neighbour_parameters(alg):
    alg.addParameter(QgsProcessingParameterFeatureSource(
        alg.STREETS, tr("Streets (polygons or centre lines)"),
        [Qgis.ProcessingSourceType.VectorPolygon, Qgis.ProcessingSourceType.VectorLine], optional=True))
    alg.addParameter(QgsProcessingParameterField(
        alg.STREET_NAME, tr("Street name field"), parentLayerParameterName=alg.STREETS, optional=True))
    alg.addParameter(QgsProcessingParameterField(
        alg.STREET_WIDTH, tr("Street width field (m)"), parentLayerParameterName=alg.STREETS,
        type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
    alg.addParameter(QgsProcessingParameterEnum(
        alg.DEFAULT, tr("Where nothing is next to a side, write"),
        [tr("Vacant land (أرض فضاء)"), tr("Property of others (ملك الغير)"), tr("Neighbour (جار)")], defaultValue=0))
    alg.addParameter(QgsProcessingParameterEnum(
        alg.STYLE, tr("Wording"),
        [tr("Saudi (شمالاً، جنوباً، شرقاً، غرباً)"), tr("Egyptian (بحري، قبلي، شرقي، غربي)")], defaultValue=0))
    alg.addParameter(QgsProcessingParameterNumber(
        alg.TOLERANCE, tr("Look for neighbours this far outside each side (m)"),
        Qgis.ProcessingNumberParameterType.Double, 0.5, minValue=0.01))
    alg.addParameter(QgsProcessingParameterNumber(
        alg.SEARCH, tr("Street centre lines up to (m) from a side"), Qgis.ProcessingNumberParameterType.Double,
        20.0, minValue=0.0))


LANGUAGE_CODES = [None] + dl.TRAVERSE_LANGUAGES          # None: Arabic sides in the wording above
NOTATION_LABELS = {
    "azimuth": "Azimuth (45°30'15\")", "wcb": "Whole-circle bearing (045°30'15\")",
    "azimuth_decimal": "Azimuth in decimal degrees (45.5042°)", "quadrant": "Quadrant bearing (N 45°30'15\" E)",
    "quadrant_ph": "Quadrant, Philippine style (N. 45 deg. 30' E.)", "gon": "Grads (50.5602 gr)",
}


def add_language_parameters(alg):
    alg.addParameter(QgsProcessingParameterEnum(
        alg.LANGUAGE, tr("Also write the boundaries as bearings and distances in"),
        [tr("No - Arabic sides only")] + [dl.LANGUAGES[c] for c in dl.TRAVERSE_LANGUAGES], defaultValue=0))
    alg.addParameter(QgsProcessingParameterEnum(
        alg.NOTATION, tr("Bearings written as"),
        [tr("As usual for that language")] + [tr(NOTATION_LABELS[n]) for n in brg.NOTATIONS], defaultValue=0))
    alg.addParameter(QgsProcessingParameterString(
        alg.AREA_UNITS, tr("Area also in these units (e.g. kanal,marla - feddan,qirat,sahm - rai,ngan,sqwa)"),
        optional=True))
    alg.addParameter(QgsProcessingParameterPoint(
        alg.TIE_POINT, tr("Tie point the description starts from (BLLM, control point)"), optional=True))
    alg.addParameter(QgsProcessingParameterString(alg.TIE_NAME, tr("Name of the tie point"), optional=True))


def area_unit_list(text):
    """Unit ids from 'kanal, marla' - unknown names are returned separately."""
    ids = [u.strip().lower() for u in (text or "").replace("،", ",").split(",") if u.strip()]
    return [u for u in ids if u in un.UNITS and un.UNITS[u].kind == "area"], [u for u in ids if u not in un.UNITS]


def local_description(wg, finder, own, lang, default_index=0, notation=None, area_units=None, crs_note="",
                      tie=None, title=""):
    """(traverse, text) of a parcel as bearings and distances in another language, vertices clockwise from the
    north-west corner (the numbering of the corner table)."""
    pts = [(x, y) for _, x, y in corner_table(merge_straight(outer_ring(wg)))]
    neighbours = []
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        found = finder.edge(q, p, own) if finder is not None else []   # clockwise: the outside is on the left
        if found:
            neighbours.append(dl.Neighbour("text", text=" + ".join(dl.neighbour_text(n, lang) for n in found)))
        else:
            neighbours.append(dl.Neighbour("default", default=default_index))
    trav = dl.from_ring(pts, neighbours=neighbours, name_format=dl.name_format_for(lang), tie=tie)
    text = dl.write(trav, lang, title=title, notation=notation, area_units=area_units, crs_note=crs_note)
    return trav, text


def build_finder(alg, parameters, context, source, number_field, work_crs, layer=None):
    """Neighbour finder over all parcels of the layer (not only the selected ones) and the streets."""
    parcels = {}
    feats = layer.getFeatures() if layer is not None else source.getFeatures()
    src_crs = layer.crs() if layer is not None else source.sourceCrs()
    xf = QgsCoordinateTransform(src_crs, work_crs, context.transformContext())
    for f in feats:
        g = QgsGeometry(f.geometry())
        if g.isNull() or g.isEmpty():
            continue
        g.transform(xf)
        num = f[number_field] if number_field and f.fields().lookupField(number_field) >= 0 else None
        parcels[f.id()] = (g, "" if num is None or str(num) == "NULL" else _clean_number(num))
    streets = []
    ssrc = alg.parameterAsSource(parameters, alg.STREETS, context)
    if ssrc is not None:
        nfield = alg.parameterAsString(parameters, alg.STREET_NAME, context) or guess_field(ssrc.fields(),
                                                                                             STREET_NAME_FIELDS)
        wfield = alg.parameterAsString(parameters, alg.STREET_WIDTH, context) or guess_field(ssrc.fields(),
                                                                                              STREET_WIDTH_FIELDS)
        sxf = QgsCoordinateTransform(ssrc.sourceCrs(), work_crs, context.transformContext())
        for f in ssrc.getFeatures():
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            g.transform(sxf)
            name = f[nfield] if nfield else None
            width = f[wfield] if wfield else None
            try:
                width = float(width) if width not in (None, "") and str(width) != "NULL" else None
            except (TypeError, ValueError):
                width = None
            name = None if name in (None, "") or str(name) == "NULL" else str(name)
            streets.append((g, name, width))
    finder = NeighbourFinder(parcels, streets, alg.parameterAsDouble(parameters, alg.TOLERANCE, context),
                             alg.parameterAsDouble(parameters, alg.SEARCH, context))
    return finder, parcels


def whole_layer(alg, parameters, key, context):
    """The parcels layer itself - all of its parcels are neighbours, even when only some are selected."""
    from qgis.core import QgsProcessingFeatureSourceDefinition, QgsProcessingUtils, QgsVectorLayer

    value = parameters.get(key)
    if isinstance(value, QgsProcessingFeatureSourceDefinition):
        lyr = QgsProcessingUtils.mapLayerFromString(str(value.source.staticValue()), context)
        if isinstance(lyr, QgsVectorLayer):
            return lyr
    return alg.parameterAsVectorLayer(parameters, key, context)


def _clean_number(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def write_text(path, items):
    """Plain UTF-8 text: one block per parcel."""
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        for item in items:
            title, desc = item[0], item[1]
            local = item[2] if len(item) > 2 else None
            f.write(f"{title}\n{desc.text_ar()}\n\n{desc.text_en()}\n\n")
            f.write("رقم النقطة\tالشرقي (E)\tالشمالي (N)\n")
            for n, x, y in desc.corners:
                f.write(f"{n}\t{x:.3f}\t{y:.3f}\n")
            if local:
                f.write(f"\n{local['text']}\n\n")
                head, rows = local["table"]
                f.write("\t".join(head) + "\n")
                for r in rows:
                    f.write("\t".join(str(v) for v in r) + "\n")
            f.write("\n" + "-" * 60 + "\n\n")


def write_excel(path, items):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E79")
    sheets = [
        (tr("Boundaries"), [tr("Parcel"), tr("North"), tr("South"), tr("East"), tr("West"), tr("Area (m²)"),
                            tr("Area in words")],
         [[it[0], it[1].side_ar("N"), it[1].side_ar("S"), it[1].side_ar("E"), it[1].side_ar("W"),
           round(it[1].area, 2), area_words(it[1].area)] for it in items]),
        (tr("Corners"), [tr("Parcel"), tr("Point"), "E", "N"],
         [[it[0], n, round(x, 3), round(y, 3)] for it in items for n, x, y in it[1].corners]),
    ]
    locals_ = [(it[0], it[2]) for it in items if len(it) > 2 and it[2]]
    if locals_:
        columns = locals_[0][1]["table"][0]
        sheets.append((tr("Bearings and distances"), [tr("Parcel")] + list(columns),
                       [[t, *r] for t, loc in locals_ for r in loc["table"][1]]))
        sheets.append((tr("Description"), [tr("Parcel"), tr("Description")], [[t, loc["text"]] for t, loc in locals_]))
    for i, (title, header, rows) in enumerate(sheets):
        ws = wb.active if i == 0 else wb.create_sheet()
        ws.title = title[:31]
        ws.sheet_view.rightToLeft = is_rtl()
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = head, fill
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        for col in ws.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 70)
        ws.freeze_panes = "A2"
    wb.save(path)


class ParcelDescription(BiruniAlgorithm):
    ID = "parceldescription"
    NAME = "Write the boundaries text of a parcel"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Writes the boundaries of each parcel the way deeds and survey reports do: every side (north, south, "
            "east, west - or بحري/قبلي for Egypt) with its length and what lies next to it - the adjacent parcel "
            "by its number, a street by its name and width, or vacant land - then the area in figures and in "
            "Arabic words, and the corner coordinates numbered clockwise from the north-west corner. Broken sides "
            "and corner cuts (شطفة) are written out. Neighbours are found in the parcels layer itself (all of "
            "its parcels, even when only some are selected) and in an optional streets layer (polygons, or "
            "centre lines with a width field). «Parcel from deed text» reads the result back to the same parcel.")

    INPUT, NUMBER, STREETS, STREET_NAME, STREET_WIDTH, DEFAULT, STYLE, TOLERANCE, SEARCH, OUTPUT, TEXT, XLSX = (
        "INPUT", "NUMBER", "STREETS", "STREET_NAME", "STREET_WIDTH", "DEFAULT", "STYLE", "TOLERANCE", "SEARCH",
        "OUTPUT", "TEXT", "XLSX")
    LANGUAGE, NOTATION, AREA_UNITS, TIE_POINT, TIE_NAME = "LANGUAGE", "NOTATION", "AREA_UNITS", "TIE_POINT", "TIE_NAME"

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, tr("Parcels"),
                                                              [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterField(self.NUMBER, tr("Parcel number field"),
                                                      parentLayerParameterName=self.INPUT, optional=True))
        add_neighbour_parameters(self)
        add_language_parameters(self)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Parcels with boundaries text"),
                                                            Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.TEXT, tr("Text file"), tr("Text files (*.txt)"), optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.XLSX, tr("Excel file"), tr("Excel files (*.xlsx)"), optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        layer = whole_layer(self, parameters, self.INPUT, context)
        number_field = self.parameterAsString(parameters, self.NUMBER, context) or guess_field(source.fields(),
                                                                                                NUMBER_FIELDS)
        crs = source.sourceCrs()
        work = working_crs(crs, centre_wgs(crs, source.sourceExtent(), context))
        if work != crs:
            feedback.pushInfo(tr("Lengths and areas are measured in {crs} (the layer is in degrees).").format(
                crs=work.authid()))
        finder, parcels = build_finder(self, parameters, context, source, number_field, work, layer)
        style = ("saudi", "egypt")[self.parameterAsEnum(parameters, self.STYLE, context)]
        default_index = self.parameterAsEnum(parameters, self.DEFAULT, context)
        default = DEFAULTS[default_index]
        xf = QgsCoordinateTransform(crs, work, context.transformContext())
        lang = LANGUAGE_CODES[self.parameterAsEnum(parameters, self.LANGUAGE, context)] \
            if self.LANGUAGE in parameters else None
        notation = None
        if parameters.get(self.NOTATION):
            notation = brg.NOTATIONS[self.parameterAsEnum(parameters, self.NOTATION, context) - 1]
        area_units, unknown = area_unit_list(self.parameterAsString(parameters, self.AREA_UNITS, context))
        if unknown:
            feedback.pushWarning(tr("Unknown area units left out: {u}").format(u=", ".join(unknown)))
        tie = None
        if lang and parameters.get(self.TIE_POINT) not in (None, ""):
            tp = self.parameterAsPoint(parameters, self.TIE_POINT, context, work)
            tie = {"name": self.parameterAsString(parameters, self.TIE_NAME, context) or tr("the tie point"),
                   "x": tp.x(), "y": tp.y()}

        fields = QgsFields(source.fields())
        new = [("parcel_no", QMetaType.Type.QString), ("north", QMetaType.Type.QString),
               ("south", QMetaType.Type.QString), ("east", QMetaType.Type.QString), ("west", QMetaType.Type.QString),
               ("desc_ar", QMetaType.Type.QString), ("desc_en", QMetaType.Type.QString),
               ("area_m2", QMetaType.Type.Double), ("area_words", QMetaType.Type.QString),
               ("corners", QMetaType.Type.QString)]
        if lang:
            new += [("language", QMetaType.Type.QString), ("desc_local", QMetaType.Type.QString)]
        for name, t in new:
            if fields.lookupField(name) < 0:
                fields.append(QgsField(name, t))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(), crs)
        items, total, done = [], source.featureCount() or 1, 0
        for n, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            wg = QgsGeometry(g)
            wg.transform(xf)
            num = parcels[f.id()][1] if f.id() in parcels else ""
            desc = describe_geometry(wg, finder, f.id(), style, default, corner_geom=g)
            title = f"قطعة رقم {num}" if num else f"{tr('Parcel')} {f.id()}"
            local = None
            if lang:
                ltitle = dl.neighbour_text(dl.Neighbour("parcel", number=num), lang) if num else ""
                trav, ltext = local_description(wg, finder, f.id(), lang, default_index, notation, area_units,
                                                work.description(), tie, ltitle)
                local = {"text": ltext, "table": dl.vertex_table(trav, lang, notation)}
            items.append((title, desc, local))
            out = QgsFeature(fields)
            out.setGeometry(g)
            attrs = list(f.attributes()) + [None] * (fields.count() - len(f.attributes()))
            values = {"parcel_no": num or str(f.id()), "north": desc.side_ar("N"), "south": desc.side_ar("S"),
                      "east": desc.side_ar("E"), "west": desc.side_ar("W"), "desc_ar": desc.text_ar(),
                      "desc_en": desc.text_en(), "area_m2": round(desc.area, 3), "area_words": area_words(desc.area),
                      "corners": desc.corners_text(8 if crs.isGeographic() else 3)}
            if local:
                values.update({"language": lang, "desc_local": local["text"]})
            for k, v in values.items():
                attrs[fields.lookupField(k)] = v
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            done += 1
            feedback.setProgress(100 * n / total)
        result = {self.OUTPUT: dest, "PARCEL_COUNT": done}
        text = self.parameterAsFileOutput(parameters, self.TEXT, context)
        if text:
            write_text(text, items)
            result[self.TEXT] = text
        xlsx = self.parameterAsFileOutput(parameters, self.XLSX, context)
        if xlsx:
            write_excel(xlsx, items)
            result[self.XLSX] = xlsx
        feedback.pushInfo(tr("Boundaries written for {n} parcels.").format(n=done))
        return result
