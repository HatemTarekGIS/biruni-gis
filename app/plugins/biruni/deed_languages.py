"""Boundary descriptions as land records around the world write them - and read back.

Pure Python (no qgis). A parcel is a closed *traverse*: vertices in order and,
between them, courses (bearing + distance + what lies beyond). This module
writes a traverse in the wording of a country and reads such texts back to
coordinates, with the misclosure, the precision ratio (1:N) and an optional
compass-rule (Bowditch) adjustment.

Languages / styles (``LANGUAGES``):
    ar-SA, ar-EG  sides north/south/east/west (written by parcel_description, read by deed_parcel)
    ar            traverse in Arabic (من النقطة … إلى النقطة … الاتجاه … والطول …)
    en            metes and bounds (thence N 45°30'15" E a distance of …)
    en-PH         Philippine technical description, with the tie line from a BLLM
    en-NG         schedule of beacons + bearings and distances (Nigeria, Ghana, Kenya …)
    pt-BR         memorial descritivo (vértice, azimute, distância, confrontante)
    es            memoria descriptiva + cuadro de construcción (rumbos)
    fr            description des limites (gisements en grades)
    tr            sınır tarifi (semt açısı, grad)
    fa            شرح حدود
    ur            حدود اربعہ
    id            uraian batas bidang tanah
"""
import math
import re
from dataclasses import dataclass, field

from . import bearings as brg
from . import units as un

LANGUAGES = {
    "ar-SA": "Arabic - Saudi deed sides (شمالاً، جنوباً، شرقاً، غرباً)",
    "ar-EG": "Arabic - Egyptian deed sides (بحري، قبلي، شرقي، غربي)",
    "ar": "Arabic - bearings and distances (من النقطة إلى النقطة)",
    "en": "English - metes and bounds",
    "en-PH": "English - Philippine technical description",
    "en-NG": "English - schedule of beacons (Nigeria, Ghana, Kenya …)",
    "pt-BR": "Português - memorial descritivo",
    "es": "Español - memoria descriptiva y cuadro de construcción",
    "fr": "Français - description des limites (bornage)",
    "tr": "Türkçe - sınır tarifi",
    "fa": "فارسی - شرح حدود",
    "ur": "اردو - حدود اربعہ",
    "id": "Bahasa Indonesia - uraian batas",
}
SIDE_STYLES = {"ar-SA": "saudi", "ar-EG": "egypt"}            # written side by side, not as a traverse
TRAVERSE_LANGUAGES = [c for c in LANGUAGES if c not in SIDE_STYLES]

_COUNTRY = {
    "SA": "ar-SA", "AE": "ar-SA", "KW": "ar-SA", "QA": "ar-SA", "BH": "ar-SA", "OM": "ar-SA", "YE": "ar-SA",
    "EG": "ar-EG", "SD": "ar-EG", "JO": "ar", "IQ": "ar", "SY": "ar", "LB": "ar", "PS": "ar", "LY": "ar",
    "MA": "fr", "DZ": "fr", "TN": "fr", "FR": "fr", "BE": "fr", "CH": "fr", "SN": "fr", "CI": "fr", "CM": "fr",
    "ML": "fr", "BF": "fr", "NE": "fr", "TG": "fr", "BJ": "fr", "GA": "fr", "CD": "fr", "MG": "fr", "HT": "fr",
    "BR": "pt-BR", "PT": "pt-BR", "AO": "pt-BR", "MZ": "pt-BR",
    "ES": "es", "MX": "es", "CO": "es", "AR": "es", "PE": "es", "CL": "es", "VE": "es", "EC": "es", "BO": "es",
    "PY": "es", "UY": "es", "GT": "es", "HN": "es", "SV": "es", "NI": "es", "CR": "es", "PA": "es", "DO": "es",
    "CU": "es", "PR": "es",
    "TR": "tr", "IR": "fa", "AF": "fa", "PK": "ur", "ID": "id", "PH": "en-PH",
    "NG": "en-NG", "GH": "en-NG", "KE": "en-NG", "UG": "en-NG", "TZ": "en-NG", "ZA": "en-NG", "ZM": "en-NG",
    "ZW": "en-NG", "RW": "en-NG", "SL": "en-NG", "LR": "en-NG",
}


def for_country(cc):
    """Default boundary-description language/style of a country (ISO alpha-2); English otherwise."""
    return _COUNTRY.get((cc or "").upper(), "en")


def defaults_for_country(cc):
    """Language, bearing notation and area units used on deeds in a country."""
    return {"language": for_country(cc), "notation": brg.for_country(cc), "area_units": un.for_country(cc)}


def default_notation(lang):
    return {"pt-BR": "azimuth", "es": "quadrant", "fr": "gon", "tr": "gon", "en": "quadrant", "en-PH": "quadrant_ph",
            "en-NG": "wcb", "ur": "wcb", "id": "azimuth", "fa": "azimuth", "ar": "azimuth"}.get(lang, "azimuth")


# ---- numbers ---------------------------------------------------------------------------------
_SEP = {"pt": (",", "."), "fr": (",", " "), "tr": (",", "."), "id": (",", "."), "fa": ("٫", "٬"),
        "es": (".", ","), "en": (".", ","), "ar": (".", ","), "ur": (".", ",")}
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _base(lang):
    return (lang or "en").split("-")[0].lower()


def fmt_num(v, decimals=2, lang="en", grouping=True):
    dec, tho = _SEP.get(_base(lang), (".", ","))
    s = f"{v:,.{decimals}f}" if grouping else f"{v:.{decimals}f}"
    s = s.replace(",", "\0").replace(".", dec).replace("\0", tho if grouping else "")
    return s.translate(_FA_DIGITS) if _base(lang) == "fa" else s


def _digits(text, lang):
    return str(text).translate(_FA_DIGITS) if _base(lang) == "fa" else str(text)


def parse_number(s, decimal_comma=None):
    """A number written with any separators: 7.456.123,456 / 7,456,123.456 / 125,50 / ۱۲۵٫۵۰."""
    t = brg.normalise(str(s)).strip().replace(" ", "").replace(" ", "").replace(" ", "").replace("٬", "")
    t = t.rstrip(".,")
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        if decimal_comma is False or t.count(",") > 1:
            t = t.replace(",", "")                     # thousands separators
        else:
            t = t.replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    elif decimal_comma and re.fullmatch(r"\d{1,3}\.\d{3}", t):
        t = t.replace(".", "")
    return float(t)


# ---- neighbours ------------------------------------------------------------------------------
_PARCEL = {"ar": ("قطعة رقم {n}", "قطعة مجاورة"), "en": ("parcel no. {n}", "adjoining parcel"),
           "en-PH": ("Lot {n}", "adjoining lot"), "en-NG": ("plot {n}", "adjoining plot"),
           "pt": ("lote nº {n}", "imóvel confrontante"), "es": ("lote No. {n}", "predio colindante"),
           "fr": ("parcelle n° {n}", "parcelle voisine"), "tr": ("{n} nolu parsel", "komşu parsel"),
           "fa": ("قطعه شماره {n}", "قطعه مجاور"), "ur": ("پلاٹ نمبر {n}", "ملحقہ پلاٹ"),
           "id": ("bidang no. {n}", "bidang tetangga")}
_STREET = {"ar": ("شارع {name}", "شارع", " عرض {w}م"), "en": ("street {name}", "street", " ({w} m wide)"),
           "en-PH": ("{name} Road", "road", " ({w} m wide)"), "en-NG": ("{name} Road", "road", " ({w} m wide)"),
           "pt": ("rua {name}", "via pública", ", com {w} m de largura"),
           "es": ("calle {name}", "vía pública", " de {w} m de ancho"),
           "fr": ("voie {name}", "voie publique", " ({w} m)"), "tr": ("{name} sokak", "yol", " ({w} m)"),
           "fa": ("خیابان {name}", "خیابان", " به عرض {w} متر"), "ur": ("سڑک {name}", "سڑک", " ({w} میٹر چوڑی)"),
           "id": ("jalan {name}", "jalan", " lebar {w} m")}
_DEFAULT = {"ar": ("أرض فضاء", "ملك الغير", "جار"), "en": ("vacant land", "property of others", "neighbour"),
            "pt": ("terreno vago", "propriedade de terceiros", "confrontante"),
            "es": ("terreno baldío", "propiedad de terceros", "colindante"),
            "fr": ("terrain nu", "propriété de tiers", "riverain"),
            "tr": ("boş arazi", "başkasına ait mülk", "komşu"), "fa": ("زمین بایر", "ملک غیر", "مجاور"),
            "ur": ("خالی زمین", "غیر کی ملکیت", "ہمسایہ"), "id": ("tanah kosong", "tanah milik orang lain", "tetangga")}


@dataclass
class Neighbour:
    """What lies beyond a course: kind 'parcel' (number), 'street' (name, width), 'default' (0 vacant land,
    1 property of others, 2 neighbour) or 'text' (written as it is)."""
    kind: str = "default"
    number: str = ""
    name: str = ""
    width: float = None
    default: int = 0
    text: str = ""


def _pack(table, lang):
    return table.get(lang) or table.get(_base(lang)) or table["en"]


def _width(w):
    return f"{w:g}" if abs(w - round(w)) > 1e-9 else str(int(round(w)))


def neighbour_text(n, lang="en"):
    if n is None:
        n = Neighbour()
    if isinstance(n, str):
        return n
    kind = getattr(n, "kind", None)
    if kind is None:                                  # a (arabic, english) tuple from older callers
        return n[0] if _base(lang) == "ar" else n[1]
    if kind == "text":
        return n.text
    if kind == "parcel":
        one, anon = _pack(_PARCEL, lang)
        return one.format(n=n.number) if n.number else anon
    if kind == "street":
        named, plain, wide = _pack(_STREET, lang)
        name = (n.name or "").strip()
        text = plain if not name else (name if _base(lang) == "ar" and name.startswith(("شارع", "طريق", "ممر"))
                                       else named.format(name=name))
        if n.width:
            text += wide.format(w=_digits(_width(n.width), lang))
        return text
    return _pack(_DEFAULT, lang)[min(max(int(n.default or 0), 0), 2)]


# ---- the traverse ----------------------------------------------------------------------------
@dataclass
class Vertex:
    name: str
    x: float                                        # east
    y: float                                        # north


@dataclass
class Course:
    frm: str
    to: str
    azimuth: float                                  # degrees, grid north, clockwise
    distance: float                                 # metres
    neighbour: object = None


@dataclass
class Traverse:
    vertices: list
    courses: list
    tie: dict = None                                # {"name", "x", "y", "azimuth", "distance"}
    misclosure_e: float = 0.0
    misclosure_n: float = 0.0
    adjusted: bool = False
    language: str = ""
    start_given: bool = True
    warnings: list = field(default_factory=list)

    @property
    def misclosure(self):
        return math.hypot(self.misclosure_e, self.misclosure_n)

    @property
    def perimeter(self):
        return sum(c.distance for c in self.courses)

    @property
    def precision(self):
        """N of the precision ratio 1:N (inf when it closes exactly)."""
        return math.inf if self.misclosure < 1e-9 else self.perimeter / self.misclosure

    @property
    def area(self):
        pts = [(v.x, v.y) for v in self.vertices]
        s = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                for i in range(len(pts)))
        return abs(s) / 2

    def ring(self):
        return [(v.x, v.y) for v in self.vertices]

    def vertex(self, name):
        return next((v for v in self.vertices if v.name == name), None)

    def precision_ok(self, required=5000):
        return self.precision >= required


def from_ring(pts, names=None, neighbours=None, name_format="{}", tie=None):
    """Traverse of a closed ring [(x, y)] in the order given (deeds go clockwise from the north-west corner).
    neighbours: one per course (course i runs from vertex i to vertex i+1)."""
    pts = [tuple(p) for p in pts]
    if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    if len(pts) < 3:
        raise ValueError("a parcel needs at least three corners")
    names = list(names) if names else [name_format.format(i + 1) for i in range(len(pts))]
    vs = [Vertex(str(n), x, y) for n, (x, y) in zip(names, pts)]
    cs = []
    for i, v in enumerate(vs):
        w = vs[(i + 1) % len(vs)]
        nb = neighbours[i] if neighbours and i < len(neighbours) else None
        cs.append(Course(v.name, w.name, brg.azimuth((v.x, v.y), (w.x, w.y)), math.hypot(w.x - v.x, w.y - v.y), nb))
    t = Traverse(vs, cs)
    if tie:
        t.tie = dict(tie)
        t.tie["azimuth"] = brg.azimuth((tie["x"], tie["y"]), (vs[0].x, vs[0].y))
        t.tie["distance"] = math.hypot(vs[0].x - tie["x"], vs[0].y - tie["y"])
    return t


def name_format_for(lang):
    return {"pt-BR": "V-{:02d}"}.get(lang, "{}")


# ---- writing ---------------------------------------------------------------------------------
def _bearing(az, notation, lang, sec_decimals=0):
    if notation == "gon":
        return f"{fmt_num(az / 0.9, 4, lang, False)} {'g' if _base(lang) == 'tr' else 'gr'}"
    return brg.format(az, notation, lang, sec_decimals)


def _dms_words(az, lang):
    d, m, s = brg._dms_parts(az % 360)
    words = {"fa": ("درجه", "دقیقه", "ثانیه"), "ur": ("درجے", "منٹ", "سیکنڈ")}[_base(lang)]
    return _digits(f"{d} {words[0]} {m:02d} {words[1]} {int(round(s)):02d} {words[2]}", lang)


def _area_extra(area, lang, area_units):
    if not area_units or list(area_units) == ["m2"]:
        return ""
    return f" ({un.area_text(area, area_units, _base(lang))})"


def _en_words(n):
    ones = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

    def below(k):
        h, r = divmod(k, 100)
        out = [f"{ones[h]} hundred"] if h else []
        if r:
            out.append(ones[r] if r < 20 else (f"{tens[r // 10]}-{ones[r % 10]}" if r % 10 else tens[r // 10]))
        return " ".join(out)

    n = int(n)
    if n == 0:
        return "zero"
    parts = []
    for size, name in ((1_000_000, "million"), (1000, "thousand")):
        k, n = divmod(n, size)
        if k:
            parts.append(f"{below(k)} {name}")
    if n:
        parts.append(below(n))
    return " ".join(parts)


def write(t, lang="en", title="", notation=None, area_units=None, crs_note="", decimals=2, coord_decimals=3,
          tie_name=""):
    """The boundary description of traverse t in a language/style of LANGUAGES (not the side styles)."""
    if lang in SIDE_STYLES:
        raise ValueError("side-by-side Arabic deeds are written by parcel_description")
    notation = notation or default_notation(lang)
    w = _WRITERS.get(lang) or _WRITERS.get(_base(lang)) or _w_en
    return w(t, dict(lang=lang, title=title, notation=notation, area_units=area_units or [], crs_note=crs_note,
                     d=decimals, c=coord_decimals, tie_name=tie_name or (t.tie or {}).get("name", "")))


def _n(v, o, decimals=None):
    return fmt_num(v, o["d"] if decimals is None else decimals, o["lang"])


def _c(v, o):
    return fmt_num(v, o["c"], o["lang"])


def _w_pt(t, o):
    out = ["MEMORIAL DESCRITIVO"]
    if o["title"]:
        out.append(f"Imóvel: {o['title']}")
    out.append(f"Área: {_n(t.area, o)} m² ({fmt_num(t.area / 10000, 4, 'pt')} ha){_area_extra(t.area, 'pt', o['area_units'])}"
               f" - Perímetro: {_n(t.perimeter, o)} m")
    v0 = t.vertices[0]
    s = f"Inicia-se a descrição deste perímetro no vértice {v0.name}, de coordenadas N {_c(v0.y, o)} m e E {_c(v0.x, o)} m"
    for c in t.courses:
        nb = neighbour_text(c.neighbour, "pt")
        s += (f"; deste, segue confrontando com {nb}, com azimute de {_bearing(c.azimuth, o['notation'], 'pt')} "
              f"e distância de {_n(c.distance, o)} m, até o vértice {c.to}")
        if c.to == v0.name:
            s += ", ponto inicial da descrição deste perímetro."
        else:
            v = t.vertex(c.to)
            s += f", de coordenadas N {_c(v.y, o)} m e E {_c(v.x, o)} m"
    out.append(s)
    if o["crs_note"]:
        out.append(f"Todas as coordenadas aqui descritas estão referenciadas ao sistema {o['crs_note']}. Todos os "
                   "azimutes e distâncias, área e perímetro foram calculados no plano de projeção.")
    return "\n".join(out)


def _w_es(t, o):
    out = ["MEMORIA DESCRIPTIVA"]
    if o["title"]:
        out.append(f"Predio: {o['title']}")
    v0 = t.vertices[0]
    parts = []
    for i, c in enumerate(t.courses):
        nb = neighbour_text(c.neighbour, "es")
        rumbo = _bearing(c.azimuth, o["notation"], "es")
        head = (f"Partiendo del vértice {c.frm}, de coordenadas X = {_c(v0.x, o)}, Y = {_c(v0.y, o)}, "
                if i == 0 else f"del vértice {c.frm}, ")
        end = f"se llega al vértice {c.to}, colindando con {nb}"
        parts.append(f"{head}con rumbo {rumbo} y una distancia de {_n(c.distance, o)} m {end}")
    out.append("; ".join(parts) + ", cerrando así el polígono.")
    out.append(f"Superficie: {_n(t.area, o)} m² ({fmt_num(t.area / 10000, 4, 'es')} ha)"
               f"{_area_extra(t.area, 'es', o['area_units'])}; perímetro: {_n(t.perimeter, o)} m.")
    out.append("")
    out.append("CUADRO DE CONSTRUCCIÓN")
    out.append("EST\tPV\tRUMBO\tDISTANCIA\tV\tY\tX")
    for c in t.courses:
        v = t.vertex(c.to)
        out.append(f"{c.frm}\t{c.to}\t{_bearing(c.azimuth, o['notation'], 'es')}\t{_n(c.distance, o)}\t{c.to}\t"
                   f"{_c(v.y, o)}\t{_c(v.x, o)}")
    if o["crs_note"]:
        out.append(f"Sistema de coordenadas: {o['crs_note']}.")
    return "\n".join(out)


def _contenance(area):
    ha, rest = divmod(area, 10000)
    a, ca = divmod(rest, 100)
    parts = ([f"{int(ha)} ha"] if ha else []) + ([f"{int(a):02d} a"] if ha or a else [])
    parts.append(f"{fmt_num(ca, 2, 'fr')} ca")
    return " ".join(parts)


def _w_fr(t, o):
    out = ["DESCRIPTION DES LIMITES (procès-verbal de bornage)"]
    if o["title"]:
        out.append(f"Parcelle : {o['title']}")
    for c in t.courses:
        v = t.vertex(c.frm)
        out.append(f"Du point {c.frm} (E = {_c(v.x, o)}, N = {_c(v.y, o)}) au point {c.to} : gisement "
                   f"{_bearing(c.azimuth, o['notation'], 'fr')}, distance {_n(c.distance, o)} m ; limite avec "
                   f"{neighbour_text(c.neighbour, 'fr')}.")
    out.append(f"Contenance : {_contenance(t.area)} ({_n(t.area, o)} m²) ; périmètre : {_n(t.perimeter, o)} m.")
    if o["crs_note"]:
        out.append(f"Système de coordonnées : {o['crs_note']}. {brg.grid_note('fr')}")
    return "\n".join(out)


def _w_tr(t, o):
    out = ["PARSEL SINIR TARİFİ"]
    if o["title"]:
        out.append(f"Parsel: {o['title']}")
    v0 = t.vertices[0]
    out.append(f"{v0.name} nolu nokta: Y = {_c(v0.x, o)}, X = {_c(v0.y, o)}")
    for c in t.courses:
        out.append(f"{c.frm} nolu noktadan {c.to} nolu noktaya: semt açısı {_bearing(c.azimuth, o['notation'], 'tr')}, "
                   f"mesafe {_n(c.distance, o)} m; komşu: {neighbour_text(c.neighbour, 'tr')}.")
    out.append(f"Yüzölçümü: {_n(t.area, o)} m²{_area_extra(t.area, 'tr', o['area_units'])}; "
               f"çevre: {_n(t.perimeter, o)} m.")
    if o["crs_note"]:
        out.append(f"Koordinat sistemi: {o['crs_note']}.")
    return "\n".join(out)


def _w_fa(t, o):
    out = ["شرح حدود"]
    if o["title"]:
        out.append(f"ملک: {o['title']}")
    v0 = t.vertices[0]
    out.append(f"نقطه {_digits(v0.name, 'fa')}: شرقی (E) = {_c(v0.x, o)}، شمالی (N) = {_c(v0.y, o)}")
    for c in t.courses:
        out.append(f"از نقطه {_digits(c.frm, 'fa')} به نقطه {_digits(c.to, 'fa')} به طول {_n(c.distance, o)} متر "
                   f"با آزیموت {_dms_words(c.azimuth, 'fa')} مجاور با {neighbour_text(c.neighbour, 'fa')}؛")
    out.append(f"مساحت: {_n(t.area, o)} متر مربع؛ محیط: {_n(t.perimeter, o)} متر.")
    if o["crs_note"]:
        out.append(f"سیستم مختصات: {o['crs_note']}.")
    return "\n".join(out)


def _w_ur(t, o):
    out = ["حدود اربعہ"]
    if o["title"]:
        out.append(f"جائیداد: {o['title']}")
    v0 = t.vertices[0]
    out.append(f"نقطہ {v0.name}: شرقی (E) = {_c(v0.x, o)}، شمالی (N) = {_c(v0.y, o)}")
    for c in t.courses:
        out.append(f"نقطہ {c.frm} سے نقطہ {c.to} تک: سمت {_dms_words(c.azimuth, 'ur')}، فاصلہ {_n(c.distance, o)} "
                   f"میٹر، ملحقہ: {neighbour_text(c.neighbour, 'ur')}۔")
    out.append(f"رقبہ: {_n(t.area, o)} مربع میٹر{_area_extra(t.area, 'ur', o['area_units'])}؛ "
               f"احاطہ: {_n(t.perimeter, o)} میٹر۔")
    if o["crs_note"]:
        out.append(f"کوآرڈینیٹ نظام: {o['crs_note']}۔")
    return "\n".join(out)


def _w_id(t, o):
    out = ["URAIAN BATAS BIDANG TANAH"]
    if o["title"]:
        out.append(f"Bidang: {o['title']}")
    v0 = t.vertices[0]
    out.append(f"Titik {v0.name}: X = {_c(v0.x, o)}, Y = {_c(v0.y, o)}")
    for c in t.courses:
        out.append(f"Dari titik {c.frm} ke titik {c.to} dengan azimut {_bearing(c.azimuth, o['notation'], 'id')} "
                   f"sejauh {_n(c.distance, o)} m, berbatasan dengan {neighbour_text(c.neighbour, 'id')};")
    out.append(f"Luas: {_n(t.area, o)} m²; keliling: {_n(t.perimeter, o)} m.")
    if o["crs_note"]:
        out.append(f"Sistem koordinat: {o['crs_note']}.")
    return "\n".join(out)


def _w_ar(t, o):
    out = ["وصف الحدود بالاتجاهات والأطوال"]
    if o["title"]:
        out.append(o["title"])
    v0 = t.vertices[0]
    out.append(f"النقطة ({v0.name}): الشرقي (E) = {_c(v0.x, o)}، الشمالي (N) = {_c(v0.y, o)}")
    for c in t.courses:
        out.append(f"من النقطة ({c.frm}) إلى النقطة ({c.to}): الاتجاه {_bearing(c.azimuth, o['notation'], 'ar')} "
                   f"والطول {_n(c.distance, o)}م، ويحدها {neighbour_text(c.neighbour, 'ar')}.")
    out.append(f"المساحة: {_n(t.area, o)} م²{_area_extra(t.area, 'ar', o['area_units'])}؛ "
               f"المحيط: {_n(t.perimeter, o)} م.")
    if o["crs_note"]:
        out.append(f"نظام الإحداثيات: {o['crs_note']}. {brg.grid_note('ar')}")
    return "\n".join(out)


def _w_en(t, o):
    out = ["BOUNDARY DESCRIPTION"]
    if o["title"]:
        out.append(f"Parcel: {o['title']}")
    v0 = t.vertices[0]
    s = f"Commencing at corner {v0.name} (E {_c(v0.x, o)}, N {_c(v0.y, o)})"
    for c in t.courses:
        s += (f"; thence {_bearing(c.azimuth, o['notation'], 'en')} a distance of {_n(c.distance, o)} m to corner "
              f"{c.to}, along {neighbour_text(c.neighbour, 'en')}")
    out.append(s + ", the point of commencement.")
    out.append(f"Area: {_n(t.area, o)} m² ({fmt_num(t.area / 10000, 4)} ha){_area_extra(t.area, 'en', o['area_units'])}"
               f"; perimeter: {_n(t.perimeter, o)} m.")
    if o["crs_note"]:
        out.append(f"Coordinate system: {o['crs_note']}. {brg.grid_note('en')}")
    return "\n".join(out)


def _w_ph(t, o):
    out = ["TECHNICAL DESCRIPTION"]
    title = o["title"] or "A parcel of land"
    bounded = "; ".join(f"along line {c.frm}-{c.to} by {neighbour_text(c.neighbour, 'en-PH')}" for c in t.courses)
    out.append(f"{title}. Bounded {bounded}.")
    v0 = t.vertices[0]
    if t.tie:
        s = (f"Beginning at a point marked \"{v0.name}\" on plan, being "
             f"{brg.format(t.tie['azimuth'], 'quadrant_ph')}, {_n(t.tie['distance'], o)} m. from {o['tie_name'] or 'the tie point'}")
    else:
        s = f"Beginning at a point marked \"{v0.name}\" on plan, with coordinates N {_c(v0.y, o)}, E {_c(v0.x, o)}"
    for c in t.courses:
        target = "the point of beginning" if c.to == v0.name else f"point {c.to}"
        s += f"; thence {brg.format(c.azimuth, 'quadrant_ph')}, {_n(c.distance, o)} m. to {target}"
    whole = int(round(t.area))
    s += (f"; containing an area of {_en_words(whole).upper()} ({whole:,}) SQUARE METERS"
          f"{_area_extra(t.area, 'en', o['area_units'])}.")
    out.append(s)
    if o["crs_note"]:
        out.append(f"All points referred to are indicated on the plan; bearings are grid ({o['crs_note']}).")
    return "\n".join(out)


def _w_ng(t, o):
    out = []
    if o["title"]:
        out.append(o["title"])
    out.append("SCHEDULE OF BEACONS")
    out.append("Beacon\tEasting (m)\tNorthing (m)")
    for v in t.vertices:
        out.append(f"{v.name}\t{fmt_num(v.x, o['c'], 'en', False)}\t{fmt_num(v.y, o['c'], 'en', False)}")
    out.append("")
    out.append("BEARINGS AND DISTANCES")
    out.append("From\tTo\tBearing\tDistance (m)\tAbutting")
    for c in t.courses:
        out.append(f"{c.frm}\t{c.to}\t{_bearing(c.azimuth, o['notation'], 'en')}\t"
                   f"{fmt_num(c.distance, o['d'], 'en', False)}\t{neighbour_text(c.neighbour, 'en-NG')}")
    out.append("")
    out.append(f"Area: {fmt_num(t.area / 10000, 4, 'en')} ha ({_n(t.area, o)} m²)"
               f"{_area_extra(t.area, 'en', o['area_units'])}; perimeter: {_n(t.perimeter, o)} m")
    if o["crs_note"]:
        out.append(f"Coordinates in {o['crs_note']}. {brg.grid_note('en')}")
    return "\n".join(out)


_WRITERS = {"pt-BR": _w_pt, "pt": _w_pt, "es": _w_es, "fr": _w_fr, "tr": _w_tr, "fa": _w_fa, "ur": _w_ur,
            "id": _w_id, "ar": _w_ar, "en": _w_en, "en-PH": _w_ph, "en-NG": _w_ng}


VERTEX_HEADERS = {
    "en": ("Vertex", "E (m)", "N (m)", "Bearing", "Distance (m)", "Adjoining"),
    "pt": ("Vértice", "E (m)", "N (m)", "Azimute", "Distância (m)", "Confrontante"),
    "es": ("Vértice", "X (m)", "Y (m)", "Rumbo", "Distancia (m)", "Colindancia"),
    "fr": ("Point", "E (m)", "N (m)", "Gisement", "Distance (m)", "Limite avec"),
    "tr": ("Nokta", "Y (m)", "X (m)", "Semt açısı", "Mesafe (m)", "Komşu"),
    "fa": ("نقطه", "E (m)", "N (m)", "آزیموت", "طول (m)", "مجاور"),
    "ur": ("نقطہ", "E (m)", "N (m)", "سمت", "فاصلہ (m)", "ملحقہ"),
    "id": ("Titik", "X (m)", "Y (m)", "Azimut", "Jarak (m)", "Berbatasan"),
    "ar": ("النقطة", "الشرقي E", "الشمالي N", "الاتجاه", "الطول (م)", "الجار"),
}


def vertex_table(t, lang="en", notation=None, decimals=2, coord_decimals=3):
    """(header, rows) - one row per vertex with the course that leaves it."""
    notation = notation or default_notation(lang)
    head = _pack(VERTEX_HEADERS, lang)
    rows = []
    for c in t.courses:
        v = t.vertex(c.frm)
        bearing = _dms_words(c.azimuth, lang) if _base(lang) in ("fa", "ur") else _bearing(c.azimuth, notation, lang)
        rows.append([v.name, round(v.x, coord_decimals), round(v.y, coord_decimals), bearing, round(c.distance, decimals),
                     neighbour_text(c.neighbour, lang)])
    return list(head), rows


# ---- reading ---------------------------------------------------------------------------------
_NUM = r"\d[\d.,  ]*\d|\d"
_COL = r"(?:\t|[ ]{2,})"                               # table columns: tabs, or two spaces or more
_LEN_UNITS = {"m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m", "ft": "ft", "feet": "ft",
              "foot": "ft", "chain": "chain", "chains": "chain", "ch": "chain", "link": "link", "links": "link",
              "rod": "rod", "rods": "rod", "pole": "pole", "poles": "pole", "perch": "perch", "perches": "perch",
              "vara": "vara", "varas": "vara"}

# (bearing group, distance group) patterns per style; DIST_FIRST styles have the distance before the bearing
_COURSES = {
    "pt-BR": (re.compile(rf"azimutes?\s+(?:de\s+|e\s+dist[âa]ncias?\s*:\s*)?(.+?)\s+e\s+(?:dist[âa]ncia\s+(?:de\s+)?)?"
                         rf"({_NUM})\s*m", re.I), False),
    "es": (re.compile(rf"rumbo\s+(.+?)\s+y\s+(?:una\s+)?distancia\s+de\s+({_NUM})\s*m", re.I), False),
    "fr": (re.compile(rf"gisement\s+(.+?),\s*distance\s+({_NUM})\s*m", re.I), False),
    "tr": (re.compile(rf"semt\s+açısı\s+(.+?),\s*mesafe\s+({_NUM})\s*m", re.I), False),
    "id": (re.compile(rf"azimut\s+(.+?)\s+sejauh\s+({_NUM})\s*m", re.I), False),
    "ar": (re.compile(rf"الاتجاه\s+(.+?)\s+و(?:ال)?طول\s+({_NUM})\s*م"), False),
    "ur": (re.compile(rf"سمت\s+(.+?)،\s*فاصلہ\s+({_NUM})\s*میٹر"), False),
    "fa": (re.compile(rf"به\s+طول\s+({_NUM})\s*متر\s+(?:و\s+)?با\s+آزیموت\s+(.+?)\s+(?:مجاور|؛|$)"), True),
    "en-PH": (re.compile(rf"thence\s+([NS]\.?\s*[^,;]+?\s*[EW]\.?)\s*,\s*({_NUM})\s*m\.?", re.I), False),
    "en": (re.compile(rf"thence\s+(.+?)\s+(?:a\s+)?distance\s+of\s+({_NUM})\s*([a-z]+)", re.I), False),
}
_DETECT = [("pt-BR", r"azimute|memorial descritivo"), ("es", r"\brumbo\b|cuadro de construcci"),
           ("fr", r"\bgisement\b"), ("tr", r"semt\s+açısı"), ("id", r"\bsejauh\b|\bazimut\b"),
           ("en-NG", r"schedule of beacons|bearings and distances"), ("en-PH", r"\bdeg\.|point of beginning"),
           ("fa", r"آزیموت"), ("ur", r"فاصلہ"), ("ar", r"الاتجاه"), ("en", r"\bthence\b")]


def detect(text):
    """The style of a boundary text, or None when it is not a bearings-and-distances description."""
    t = brg.normalise(text)
    for code, pat in _DETECT:
        if re.search(pat, t, re.I):
            return code
    return None


def _dist(value, unit, lang):
    d = parse_number(value, decimal_comma=_SEP.get(_base(lang), (".",))[0] in (",", "٫"))
    u = _LEN_UNITS.get((unit or "m").lower().rstrip("."), "m")
    return d * un.get(u).size


def _start(text, lang):
    """(name, x, y) of the first vertex when the text gives its coordinates."""
    dc = _SEP.get(_base(lang), (".",))[0] in (",", "٫")
    pats = {
        "pt-BR": (rf"v[ée]rtice\s+(\S+?),\s*de\s+coordenadas\s+N\s*({_NUM})\s*m?\s+e\s+E\s*({_NUM})", "n_e"),
        "es": (rf"v[ée]rtice\s+(\S+?),\s*de\s+coordenadas\s+X\s*=\s*({_NUM}),\s*Y\s*=\s*({_NUM})", "e_n"),
        "fr": (rf"point\s+(\S+)\s+\(E\s*=\s*({_NUM}),\s*N\s*=\s*({_NUM})\)", "e_n"),
        "tr": (rf"(\S+)\s+nolu\s+nokta:\s*Y\s*=\s*({_NUM}),\s*X\s*=\s*({_NUM})", "e_n"),
        "id": (rf"Titik\s+(\S+):\s*X\s*=\s*({_NUM}),\s*Y\s*=\s*({_NUM})", "e_n"),
        "ar": (rf"النقطة\s+\((\S+?)\):\s*الشرقي\s*\(E\)\s*=\s*({_NUM})،\s*الشمالي\s*\(N\)\s*=\s*({_NUM})", "e_n"),
        "fa": (rf"نقطه\s+(\S+):\s*شرقی\s*\(E\)\s*=\s*({_NUM})،\s*شمالی\s*\(N\)\s*=\s*({_NUM})", "e_n"),
        "ur": (rf"نقطہ\s+(\S+):\s*شرقی\s*\(E\)\s*=\s*({_NUM})،\s*شمالی\s*\(N\)\s*=\s*({_NUM})", "e_n"),
        "en": (rf"corner\s+(\S+)\s+\(E\s*({_NUM}),\s*N\s*({_NUM})\)", "e_n"),
        "en-PH": (rf"marked\s+\"(\S+?)\"\s+on\s+plan,\s*with\s+coordinates\s+N\s*({_NUM}),\s*E\s*({_NUM})", "n_e"),
    }
    p = pats.get(lang)
    if not p:
        return None
    m = re.search(p[0], text, re.I)
    if not m:
        return None
    a, b = parse_number(m.group(2), dc), parse_number(m.group(3), dc)
    x, y = (b, a) if p[1] == "n_e" else (a, b)
    return m.group(1), x, y


def _vertex_coords(text, lang):
    """{name: (x, y)} when the text lists the coordinates of its vertices (memorial, cuadro, bornage, beacons)."""
    out = {}
    dc = _SEP.get(_base(lang), (".",))[0] in (",", "٫")
    if lang == "pt-BR":
        for m in re.finditer(rf"v[ée]rtice\s+(\S+?),\s*de\s+coordenadas\s+N\s*({_NUM})\s*m?\s+e\s+E\s*({_NUM})",
                             text, re.I):
            out[m.group(1)] = (parse_number(m.group(3), True), parse_number(m.group(2), True))
    elif lang == "es":
        for m in re.finditer(rf"^\s*(\S+){_COL}(\S+){_COL}(.+?){_COL}({_NUM}){_COL}(\S+){_COL}({_NUM}){_COL}({_NUM})\s*$",
                             text, re.M):
            out[m.group(5)] = (parse_number(m.group(7), False), parse_number(m.group(6), False))
    elif lang == "fr":
        for m in re.finditer(rf"point\s+(\S+)\s+\(E\s*=\s*({_NUM}),\s*N\s*=\s*({_NUM})\)", text, re.I):
            out[m.group(1)] = (parse_number(m.group(2), dc), parse_number(m.group(3), dc))
    elif lang == "en-NG":
        block = re.split(r"bearings and distances", text, flags=re.I)[0]
        for m in re.finditer(rf"^\s*(\S+)[\t ]+({_NUM})[\t ]+({_NUM})[\t ]*$", block, re.M):
            out[m.group(1)] = (parse_number(m.group(2), False), parse_number(m.group(3), False))
    return out


def _courses(text, lang):
    """[(from, to, azimuth, distance)] - names are None when the text does not give them."""
    if lang == "en-NG":
        parts = re.split(r"bearings and distances", text, flags=re.I)
        block = parts[1] if len(parts) > 1 else text
        out = []
        for m in re.finditer(rf"^\s*(\S+){_COL}(\S+){_COL}(.+?){_COL}({_NUM})(?:{_COL}|\s*$)", block, re.M):
            try:
                out.append((m.group(1), m.group(2), brg.parse(m.group(3), "en"), parse_number(m.group(4), False)))
            except ValueError:
                continue
        return out
    if lang == "es" and re.search(r"cuadro de construcci", text, re.I):
        rows = re.findall(rf"^\s*(\S+){_COL}(\S+){_COL}(.+?){_COL}({_NUM}){_COL}", text, re.M)
        out = []
        for frm, to, rumbo, dist in rows:
            try:
                out.append((frm, to, brg.parse(rumbo, "es"), parse_number(dist, False)))
            except ValueError:
                continue
        if out:
            return out
    pat, dist_first = _COURSES.get(lang, _COURSES["en"])
    body = text
    if lang == "en-PH":
        body = re.sub(r"\bbeing\s+[NS]\.?.*?\bfrom\b[^;]*", "", text, count=1, flags=re.I | re.S)
    out = []
    letters = _base(lang)
    for m in pat.finditer(body):
        if dist_first:
            dist, bear, unit = m.group(1), m.group(2), "m"
        else:
            bear, dist = m.group(1), m.group(2)
            unit = m.group(3) if m.lastindex and m.lastindex >= 3 else "m"
        try:
            az = brg.parse(bear, letters)
        except ValueError:
            continue
        out.append((None, None, az, _dist(dist, unit, lang)))
    return out


_PAIRS = {
    "tr": r"(\S+)\s+nolu\s+noktadan\s+(\S+)\s+nolu\s+noktaya",
    "id": r"Dari\s+titik\s+(\S+)\s+ke\s+titik\s+(\S+)",
    "ar": r"من\s+النقطة\s+\((\S+?)\)\s+إلى\s+النقطة\s+\((\S+?)\)",
    "fa": r"از\s+نقطه\s+(\S+)\s+به\s+نقطه\s+(\S+)",
    "ur": r"نقطہ\s+(\S+)\s+سے\s+نقطہ\s+(\S+)\s+تک",
    "fr": r"Du\s+point\s+(\S+)\s+(?:\([^)]*\)\s+)?au\s+point\s+(\S+)\s*:",
}
_TO = {
    "pt-BR": r"até\s+o\s+v[ée]rtice\s+([^\s,;]+?)[,;.]",
    "es": r"se\s+llega\s+al\s+v[ée]rtice\s+([^\s,;]+?)[,;.]",
    "en": r"\bto\s+corner\s+([^\s,;]+?)[,;.]",
    "en-PH": r"\bto\s+(?:point\s+([^\s,;]+?)|the\s+point\s+of\s+beginning)\s*[;.]",
}


def _names(text, lang, n, first_name):
    """Vertex names in course order when the text gives them, else None."""
    if lang in _PAIRS:
        pairs = re.findall(_PAIRS[lang], text, re.I)
        if len(pairs) == n:
            return [p[0] for p in pairs]
    if lang in _TO:
        tos = re.findall(_TO[lang], text, re.I)
        if len(tos) == n:
            return [first_name] + [t or first_name for t in tos[:-1]]
    return None


def _tie(text):
    m = re.search(rf"being\s+([NS]\.?\s*[^,;]+?\s*[EW]\.?)\s*,\s*({_NUM})\s*m\.?\s+from\s+([^;]+?)(?:;|$)", text,
                  re.I | re.M)
    if not m:
        return None
    return {"azimuth": brg.parse(m.group(1)), "distance": parse_number(m.group(2), False), "name": m.group(3).strip()}


def parse(text, lang=None, start=None, tie_xy=None, adjust=True):
    """Coordinates of a bearings-and-distances description.

    start: (x, y) of the first vertex when the text does not give it; tie_xy: coordinates of the tie point
    (BLLM, control point) of a Philippine description. With adjust the misclosure is spread over the
    vertices by the compass rule; vertex coordinates written in the text win over computed ones."""
    raw = text
    text = brg.normalise(text)
    text = re.sub(r"(?<=\d)[  ٬](?=\d)", "", text)
    lang = lang or detect(text)
    if lang is None or lang in SIDE_STYLES:
        raise ValueError("this text is not a bearings-and-distances description")
    courses = _courses(text, lang)
    if len(courses) < 2:
        raise ValueError("fewer than two courses (bearing + distance) were found")
    warnings = []
    listed = _vertex_coords(text, lang)
    tie = None
    first = _start(text, lang)
    first_name = first[0] if first else (courses[0][0] or "1")
    if lang == "en-PH":
        tie = _tie(text)
        m = re.search(r"marked\s+\"(\S+?)\"", text)
        first_name = m.group(1) if m else first_name
    if first:
        sx, sy = first[1], first[2]
    elif first_name in listed:
        sx, sy = listed[first_name]
    elif tie and tie_xy is not None:
        tie["x"], tie["y"] = tie_xy
        sx = tie_xy[0] + tie["distance"] * math.sin(math.radians(tie["azimuth"]))
        sy = tie_xy[1] + tie["distance"] * math.cos(math.radians(tie["azimuth"]))
    elif start is not None:
        sx, sy = start
    else:
        sx, sy = 0.0, 0.0
        warnings.append("The text gives no coordinates for the first point: it is placed at 0,0.")
    if tie and tie_xy is None:
        warnings.append(f"Tie line from {tie['name']} read; give the tie point's coordinates to place the parcel.")
    # names of the vertices
    names = [frm or (first_name if i == 0 else str(i + 1)) for i, (frm, to, _, _) in enumerate(courses)]
    if not courses[0][0]:
        names = _names(text, lang, len(courses), first_name) or names
    if courses[-1][1] and courses[-1][1] != names[0]:
        warnings.append("The last course does not end at the first point; the parcel is closed with a straight line.")
    pts = [(sx, sy)]
    for _, _, az, d in courses:
        x, y = pts[-1]
        pts.append((x + d * math.sin(math.radians(az)), y + d * math.cos(math.radians(az))))
    mis_e, mis_n = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
    pts = pts[:-1]
    total = sum(c[3] for c in courses)
    adjusted = False
    if adjust and total > 0 and math.hypot(mis_e, mis_n) > 1e-9:
        cum, fixed = 0.0, [pts[0]]
        for i in range(1, len(pts)):
            cum += courses[i - 1][3]
            fixed.append((pts[i][0] - mis_e * cum / total, pts[i][1] - mis_n * cum / total))
        pts, adjusted = fixed, True
    if listed and all(n in listed for n in names):
        pts = [listed[n] for n in names]
    vs = [Vertex(n, x, y) for n, (x, y) in zip(names, pts)]
    cs = []
    for i, v in enumerate(vs):
        w = vs[(i + 1) % len(vs)]
        cs.append(Course(v.name, w.name, courses[i][2], courses[i][3]))
    t = Traverse(vs, cs, tie, mis_e, mis_n, adjusted, lang, bool(first or listed or tie_xy or start), warnings)
    if raw and t.precision < 5000:
        t.warnings.append(f"Precision 1:{t.precision:,.0f} is below 1:5,000.")
    return t
