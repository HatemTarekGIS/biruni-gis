"""Colours that mean something: semantic colours for land-use style categories (Arabic and English values),
land-use colour standards (with their sources and how sure we are of them), a brand palette taken from a logo,
and image proofs (colour-blind vision, grayscale, a rough CMYK print proof) for the colour preview window.

Standards:
- APA LBCS (USA) Function and Activity dimensions, 1-digit codes - VERIFIED from the APA "LBCS Color Codes for
  1-Digit Level Coding" sheet (01-Apr-2001). Its RGBHex column has typos; the RGB values are used.
- Saudi Arabia and Egypt - no official colour table was found published online (MOMRAH/Balady and GOPP), so
  these are COMMON CONVENTIONS seen on approved subdivision and land-use plans, marked "approximate"."""
import re

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
                       QgsRendererCategory, QgsSettings, QgsVectorLayer)
from qgis.PyQt.QtGui import QColor, QImage

from .. import arabic_text
from ..i18n import tr

# ---- semantic classes ---------------------------------------------------------------------------------------------
# (class id, English label, Arabic label, fill colour, patterns matched on the normalised value)
SEMANTIC = [
    ("residential", "Residential", "سكني", "#F6E27A",
     r"resid|housing|dwell|villa|apartment|سكن|سكني|فلل|فيلا|شقق|عمائر سكنيه|اسكان"),
    ("commercial", "Commercial", "تجاري", "#E4524A",
     r"commerc|retail|shop|market|mall|business|office|تجار|محلات|سوق|اسواق|مكاتب|معارض"),
    ("mixed", "Mixed use", "مختلط", "#EE9A4D", r"mixed|mix|مختلط|سكني تجاري|تجاري سكني"),
    ("industrial", "Industrial", "صناعي", "#9B6FC3",
     r"indust|factory|manufact|warehouse|logist|صناع|مصنع|مصانع|مستودع|ورش|ورشه"),
    ("government", "Government", "حكومي", "#5B8DD6",
     r"govern|public admin|municipal|ministry|civic|حكوم|بلديه|اداري|اداره|وزار|امانه|شرطه|دفاع مدني"),
    ("education", "Education", "تعليمي", "#4FA3E0",
     r"educat|school|university|college|kindergarten|تعليم|مدرس|مدارس|جامع[هة]|كليه|روضه|معهد"),
    ("health", "Health", "صحي", "#E57FB0",
     r"health|hospital|clinic|medical|صحي|مستشفي|مستشفى|مستوصف|عياد|مركز صحي"),
    ("religious", "Religious", "ديني", "#2E8B57",
     r"relig|mosque|church|worship|cemetery|ديني|مسجد|مساجد|جامع(?!ه)|مصلي|مصلى|كنيس|مقابر|مقبر"),
    ("green", "Parks and open space", "حدائق ومساحات خضراء", "#9ED17B",
     r"park(?!ing)|green|garden|open space|playground|recreat|حديق|حدائق|منتزه|خضراء|ملاعب|ملعب|ترفيه"),
    ("agriculture", "Agriculture", "زراعي", "#C9DE8A", r"agri|farm|crop|orchard|زراع|مزرع|حقول|نخيل"),
    ("water", "Water", "مياه", "#8EC9EE", r"water|river|lake|sea|canal|wadi|مياه|نهر|بحر|بحيره|قناه|ترعه|وادي"),
    ("transport", "Roads and transport", "طرق ونقل", "#C8C8C8",
     r"road|street|transport|parking|rail|airport|طريق|طرق|شارع|شوارع|مواقف|موقف|نقل|سكه|مطار|ممر"),
    ("utilities", "Utilities", "مرافق", "#7FA7A7",
     r"utilit|infrastr|electric|substation|treatment|pump|مرافق|كهرباء|محطه|محول|صرف|خزان|اتصالات"),
    ("vacant", "Vacant land", "أرض فضاء", "#F2EFE6", r"vacant|empty|undevelop|bare|فضاء|بيضاء|غير مستغل|خاليه"),
]


def _rx(pattern):
    """A pattern compiled with the same letter unification as the values (ة→ه, ى→ي, أ→ا…)."""
    return re.compile(arabic_text.light_normalise(pattern)[0].lower())


_SEMANTIC_RX = [(cid, _rx(p)) for cid, _en, _ar, _c, p in SEMANTIC]
SEMANTIC_COLOURS = {cid: col for cid, _en, _ar, col, _p in SEMANTIC}
SEMANTIC_LABELS = {cid: (en, ar) for cid, en, ar, _c, _p in SEMANTIC}


def _norm(value):
    if value is None:
        return ""
    return arabic_text.normalise(str(value)).lower().replace("_", " ")


def classify(value):
    """Semantic class id of a category value ('سكني', 'Residential', 'R-Residential'…) or None."""
    text = _norm(value)
    if not text:
        return None
    if _SEMANTIC_RX[2][1].search(text):  # "mixed" before residential/commercial
        return "mixed"
    for cid, rx in _SEMANTIC_RX:
        if rx.search(text):
            return cid
    return None


def semantic_colors(values):
    """{value: colour} for the values whose meaning is recognised."""
    out = {}
    for v in values:
        cid = classify(v)
        if cid:
            out[v] = SEMANTIC_COLOURS[cid]
    return out


# ---- standards ------------------------------------------------------------------------------------------------------
# class: (code, English, Arabic, colour, patterns)
STANDARDS = {
    "lbcs_function": dict(
        label="USA - APA LBCS (Function)", country="US", status="verified",
        source="https://www.planning.org/lbcs/standards/ (LBCS Color Codes for 1-Digit Level Coding, 2001)",
        classes=[
            ("1000", "Residence or accommodation", "سكن وإقامة", "#FFFF00", r"resid|housing|accommod|hotel|سكن|فندق"),
            ("2000", "General sales or services", "مبيعات وخدمات", "#FF0000",
             r"commerc|retail|sales|service|shop|office|تجار|خدمات|محلات|مكاتب"),
            ("3000", "Manufacturing and wholesale trade", "صناعة وتجارة جملة", "#A020F0",
             r"indust|manufact|wholesale|warehouse|صناع|جمله|مستودع"),
            ("4000", "Transportation, communication, information, utilities", "نقل واتصالات ومرافق", "#BEBEBE",
             r"transport|road|parking|utilit|communic|طرق|نقل|مرافق|اتصالات|مواقف"),
            ("5000", "Arts, entertainment and recreation", "ترفيه وثقافة", "#90EE90",
             r"recreat|park(?!ing)|entertain|arts|sport|ترفيه|حديق|حدائق|ملاعب|ثقاف"),
            ("6000", "Education, public administration, health care, institutions", "تعليم وحكومة وصحة",
             "#0000FF", r"educat|school|govern|admin|health|hospital|relig|mosque|تعليم|مدرس|حكوم|صحي|مسجد|ديني"),
            ("7000", "Construction-related businesses", "أعمال إنشائية", "#008B8B", r"construct|انشاء|مقاولات"),
            ("8000", "Mining and extraction", "تعدين واستخراج", "#551A8B", r"mining|quarr|extract|تعدين|محاجر"),
            ("9000", "Agriculture, forestry, fishing and hunting", "زراعة وغابات وصيد", "#228B22",
             r"agri|farm|forest|fish|زراع|مزرع|غابات|صيد"),
        ]),
    "lbcs_activity": dict(
        label="USA - APA LBCS (Activity)", country="US", status="verified",
        source="https://www.planning.org/lbcs/standards/ (LBCS Color Codes for 1-Digit Level Coding, 2001)",
        classes=[
            ("1000", "Residential activities", "أنشطة سكنية", "#FFFF00", r"resid|housing|سكن"),
            ("2000", "Shopping, business or trade", "تسوق وأعمال وتجارة", "#FF0000",
             r"commerc|retail|shop|business|trade|office|تجار|محلات|سوق|مكاتب"),
            ("3000", "Industrial, manufacturing, waste-related", "صناعة ومخلفات", "#A020F0",
             r"indust|manufact|waste|صناع|مخلفات"),
            ("4000", "Social, institutional or infrastructure", "اجتماعي ومؤسسي وبنية تحتية", "#0000FF",
             r"institut|educat|school|health|govern|relig|utilit|تعليم|صحي|حكوم|ديني|مرافق"),
            ("5000", "Travel or movement", "حركة ونقل", "#BEBEBE", r"transport|road|travel|parking|طرق|نقل|مواقف"),
            ("6000", "Mass assembly of people", "تجمعات", "#2F4F4F", r"assembly|stadium|event|مسارح|ستاد|تجمع"),
            ("7000", "Leisure activities", "ترفيه", "#90EE90", r"leisure|park(?!ing)|recreat|ترفيه|حديق|حدائق"),
            ("8000", "Natural resources-related", "موارد طبيعية", "#228B22", r"agri|farm|forest|natural|زراع|غابات"),
            ("9000", "No human activity or unclassifiable", "بلا نشاط", "#FFFFFF", r"vacant|none|فضاء|غير مستغل"),
        ]),
    "sa_landuse": dict(
        label="Saudi Arabia - land-use plans (common convention)", country="SA", status="approximate",
        source="No official colour table found on balady.gov.sa / momah.gov.sa; colours follow the convention "
               "seen on approved subdivision plans. Check against the municipality's requirements.",
        classes=[
            ("RES", "Residential", "سكني", "#FFF2A8", r"resid|سكن|فلل|شقق"),
            ("COM", "Commercial", "تجاري", "#F08080", r"commerc|تجار|محلات"),
            ("MIX", "Residential-commercial", "سكني تجاري", "#F5B971", r"mixed|سكني تجاري|تجاري سكني|مختلط"),
            ("MOS", "Mosque", "مسجد / جامع", "#3C9A5F", r"mosque|مسجد|جامع(?!ه)|مصلي"),
            ("EDU", "Education", "تعليمي", "#7EB6E8", r"educat|school|تعليم|مدرس|روضه"),
            ("HLT", "Health", "صحي", "#F4A6C6", r"health|clinic|صحي|مستوصف|مستشفي"),
            ("GOV", "Government / public services", "حكومي / خدمات عامة", "#9FA8DA", r"govern|حكوم|خدمات عامه|بلديه"),
            ("PRK", "Parks and gardens", "حدائق", "#A8D88A", r"park(?!ing)|garden|حديق|حدائق|منتزه"),
            ("PKG", "Parking", "مواقف", "#D9D9D9", r"parking|مواقف|موقف"),
            ("UTL", "Utilities", "مرافق", "#B0BEC5", r"utilit|مرافق|كهرباء|محول|خزان"),
            ("ROAD", "Roads and walkways", "طرق وممرات", "#FFFFFF", r"road|street|walk|طرق|شارع|ممر|ممرات"),
        ]),
    "eg_landuse": dict(
        label="Egypt - planning land-use maps (common convention)", country="EG", status="approximate",
        source="No published GOPP colour table was found online; colours follow the usual Egyptian planning-map "
               "convention. Check against the GOPP legend of the plan you deliver.",
        classes=[
            ("RES", "Residential", "سكني", "#FFE680", r"resid|سكن|اسكان"),
            ("COM", "Commercial", "تجاري", "#E9474A", r"commerc|تجار|محلات|سوق"),
            ("MIX", "Mixed use", "مختلط", "#F39C4A", r"mixed|مختلط|سكني تجاري"),
            ("IND", "Industrial / crafts", "صناعي / حرفي", "#8E6BBF", r"indust|صناع|حرفي|ورش"),
            ("EDU", "Education", "تعليمي", "#4A90D9", r"educat|school|تعليم|مدرس"),
            ("HLT", "Health", "صحي", "#E66FA6", r"health|صحي|مستشفي|وحده صحيه"),
            ("REL", "Religious", "ديني", "#2E8B57", r"relig|mosque|church|ديني|مسجد|كنيسه"),
            ("ADM", "Administrative", "إداري / حكومي", "#6A8CC7", r"admin|govern|اداري|حكوم"),
            ("GRN", "Green / open space", "مناطق خضراء", "#8FCB6E", r"green|park(?!ing)|garden|خضراء|حديق|حدائق"),
            ("AGR", "Agricultural", "زراعي", "#D1E39B", r"agri|زراع"),
            ("ROAD", "Roads", "طرق", "#CFCFCF", r"road|street|طرق|شارع"),
            ("VAC", "Vacant", "فضاء", "#F2EFE6", r"vacant|فضاء"),
        ]),
}


# the standards' names, for the translation files (same order as STANDARDS)
STANDARD_NAMES = ["USA - APA LBCS (Function)", "USA - APA LBCS (Activity)",
                  "Saudi Arabia - land-use plans (common convention)",
                  "Egypt - planning land-use maps (common convention)"]


def standards_for(country):
    """Standard ids that fit a country first, then the others."""
    first = [k for k, s in STANDARDS.items() if s["country"] == (country or "").upper()]
    return first + [k for k in STANDARDS if k not in first]


def match_standard(values, standard_id):
    """({value: (code, colour, label)}, [unmatched values]) for a standard; codes typed as values match too."""
    std = STANDARDS[standard_id]
    compiled = [(code, en, ar, col, _rx(p)) for code, en, ar, col, p in std["classes"]]
    matched, unmatched = {}, []
    for v in values:
        text = _norm(v)
        hit = None
        for code, en, ar, col, rx in compiled:
            if text == code.lower() or (text[:1].isdigit() and text.startswith(code[0]) and len(text) == 4):
                hit = (code, col, en)
                break
        if hit is None:  # the class with the longest matching word wins ("سكني تجاري" is mixed, not residential)
            best = max(((len(mt.group()), (code, col, en)) for code, en, _ar, col, rx in compiled
                        for mt in [rx.search(text)] if mt), default=None, key=lambda b: b[0])
            hit = best[1] if best else None
        if hit is None and text:  # through the meaning: "فيلا" → residential → the standard's residential class
            cid = classify(v)
            if cid:
                probe = f"{cid} {SEMANTIC_LABELS[cid][0].lower()} {_norm(SEMANTIC_LABELS[cid][1])}"
                hit = next(((code, col, en) for code, en, _ar, col, rx in compiled if rx.search(probe)), None)
        if hit:
            matched[v] = hit
        elif v not in (None, ""):
            unmatched.append(v)
    return matched, unmatched


# ---- renderers ----------------------------------------------------------------------------------------------------
def _symbol(layer, colour):
    gt = layer.geometryType()
    if gt == Qgis.GeometryType.Polygon:
        return QgsFillSymbol.createSimple({"color": QColor(colour).name(), "outline_color": "#6b6b6b",
                                           "outline_width": "0.2"})
    if gt == Qgis.GeometryType.Line:
        return QgsLineSymbol.createSimple({"color": QColor(colour).name(), "width": "0.6"})
    return QgsMarkerSymbol.createSimple({"color": QColor(colour).name(), "outline_color": "#ffffff", "size": "2.6"})


def _values(layer, field):
    idx = layer.fields().indexOf(field)
    if idx < 0:
        raise ValueError(tr("Field not found: {f}").format(f=field))
    return sorted((v for v in layer.uniqueValues(idx)), key=lambda x: (x is None, str(x)))


def _apply(layer, field, colours, labels, rest_colour="#DDDDDD"):
    cats = []
    for v in _values(layer, field):
        col = colours.get(v, rest_colour)
        cats.append(QgsRendererCategory(v, _symbol(layer, col), labels.get(v, "" if v is None else str(v))))
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
    layer.triggerRepaint()


def apply_semantic(layer, field):
    """Colour a layer's categories by their meaning. Returns {'matched': n, 'unmatched': [values]}."""
    if not isinstance(layer, QgsVectorLayer):
        raise ValueError(tr("Choose a vector layer."))
    values = _values(layer, field)
    colours = semantic_colors(values)
    unmatched = [v for v in values if v not in colours and v not in (None, "")]
    rest = safe_rest(len(unmatched))
    for i, v in enumerate(unmatched):
        colours[v] = rest[i % len(rest)]
    _apply(layer, field, colours, {})
    return {"matched": len(values) - len(unmatched), "unmatched": unmatched}


def apply_standard(layer, field, standard_id):
    """Colour a layer by a land-use colour standard. Returns {'matched', 'unmatched', 'status', 'source'}."""
    values = _values(layer, field)
    matched, unmatched = match_standard(values, standard_id)
    colours = {v: m[1] for v, m in matched.items()}
    labels = {v: f"{v} ({m[0]})" for v, m in matched.items()}
    _apply(layer, field, colours, labels)
    std = STANDARDS[standard_id]
    return {"matched": len(matched), "unmatched": unmatched, "status": std["status"], "source": std["source"]}


def safe_rest(n):
    from . import themes

    return themes.safe_qualitative(max(n, 1))


# ---- brand palette ------------------------------------------------------------------------------------------------
BRAND_KEY = "biruni/palettes/brand"


def _image_pixels(image, max_side=160):
    import numpy as np

    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    if max(img.width(), img.height()) > max_side:
        from qgis.PyQt.QtCore import Qt
        img = img.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4)
    px = arr.reshape(-1, 4).astype(float)
    keep = (px[:, 3] > 128) & ~((px[:, 0] > 235) & (px[:, 1] > 235) & (px[:, 2] > 235)) \
        & ~((px[:, 0] < 20) & (px[:, 1] < 20) & (px[:, 2] < 20))
    return px[keep, :3]


def palette_from_image(path_or_image, n=6, seed=1):
    """The n main colours of a logo or picture (k-means; white, black and transparent ignored), most common first."""
    import numpy as np

    image = path_or_image if isinstance(path_or_image, QImage) else QImage(path_or_image)
    if image.isNull():
        raise ValueError(tr("The image could not be opened."))
    px = _image_pixels(image)
    if len(px) < n:
        raise ValueError(tr("The image has too few coloured pixels."))
    # start from colours far apart (farthest-point seeding), so a small accent colour is not swallowed
    rng = np.random.default_rng(seed)
    centres = [px[rng.integers(len(px))]]
    for _ in range(n - 1):
        d = np.min([((px - c) ** 2).sum(axis=1) for c in centres], axis=0)
        centres.append(px[d.argmax()])
    centres = np.array(centres)
    for _ in range(25):
        d = ((px[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        lab = d.argmin(axis=1)
        new = np.array([px[lab == k].mean(axis=0) if (lab == k).any() else centres[k] for k in range(n)])
        if np.allclose(new, centres, atol=0.5):
            break
        centres = new
    counts = np.bincount(lab, minlength=n)
    order = counts.argsort()[::-1]
    out = []
    for k in order:
        if counts[k] == 0:
            continue
        c = QColor(*[int(round(v)) for v in centres[k]]).name()
        if all(_de(c, o) > 6 for o in out):  # merge near-duplicates
            out.append(c)
    return out


def _de(a, b):
    from . import themes

    return themes.delta_e(a, b)


def _lightness(colour, l_target):
    c = QColor(colour)
    h, s, _l, a = c.getHslF()
    return QColor.fromHslF(max(h, 0.0), s, min(max(l_target, 0.0), 1.0), a).name()


def make_accessible(colours, min_de=18.0, min_cvd_de=10.0, background="#FFFFFF"):
    """Adjust the lightness of brand colours (hue kept) so that they differ enough from each other, also for
    colour-blind readers, and stay visible on the background. Returns the adjusted list."""
    from . import themes

    out = []
    steps = [0.0, -0.08, 0.08, -0.16, 0.16, -0.24, 0.24, -0.32, 0.32]
    for col in colours:
        base_l = QColor(col).lightnessF()
        best, best_score = col, -1.0
        for st in steps:
            cand = _lightness(col, base_l + st)
            score = min([themes.delta_e(cand, o) for o in out] + [100.0])
            cvd = min([themes.delta_e(themes.simulate(cand, d), themes.simulate(o, d))
                       for o in out for d in themes.DEFICIENCIES] + [100.0])
            vis = themes.contrast_ratio(cand, background)
            if score >= min_de and cvd >= min_cvd_de and vis >= 1.3:
                best = cand
                break
            total = min(score, cvd * 1.5) + vis
            if total > best_score:
                best, best_score = cand, total
        out.append(best)
    return out


def save_brand_palette(colours, name="brand"):
    QgsSettings().setValue(BRAND_KEY, ",".join(QColor(c).name() for c in colours))
    QgsSettings().setValue(BRAND_KEY + "-name", name)


def brand_palette():
    raw = QgsSettings().value(BRAND_KEY, "", type=str)
    return [c for c in raw.split(",") if QColor.isValidColorName(c)] if raw else []


# ---- image proofs -------------------------------------------------------------------------------------------------
PROOFS = ["Normal vision", "Deuteranopia (green-blind)", "Protanopia (red-blind)", "Tritanopia (blue-blind)",
          "Grayscale (photocopy)", "Print proof (CMYK)"]
_PROOF_KEYS = ["normal", "deutan", "protan", "tritan", "gray", "cmyk"]


def _to_array(image):
    import numpy as np

    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return np.frombuffer(ptr, dtype=np.uint8).reshape(h, img.bytesPerLine())[:, :w * 4].reshape(h, w, 4).copy()


def _from_array(arr):
    h, w = arr.shape[:2]
    img = QImage(arr.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888)
    return img.copy()


def proof(image, key):
    """A copy of an image as seen with a colour-vision deficiency, in grayscale or as a rough CMYK print."""
    import numpy as np

    from . import themes

    if key == "normal":
        return QImage(image)
    arr = _to_array(image)
    rgb = arr[:, :, :3].astype(float) / 255.0
    if key in ("deutan", "protan", "tritan"):
        lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        m = np.array(themes._MACHADO[key])
        lin = np.clip(lin @ m.T, 0, 1)
        out = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    elif key == "gray":
        y = rgb @ np.array([0.2126, 0.7152, 0.0722])
        out = np.repeat(y[:, :, None], 3, axis=2)
    elif key == "cmyk":
        k = 1 - rgb.max(axis=2)
        denom = np.where(k < 1, 1 - k, 1)
        cmy = (1 - rgb - k[:, :, None]) / denom[:, :, None]
        ink = cmy.sum(axis=2) + k  # total ink limit 280 %, and pure screen blues/greens are out of gamut
        scale = np.where(ink > 2.8, 2.8 / np.maximum(ink, 1e-6), 1.0)
        cmy = cmy * scale[:, :, None] * 0.92  # dull the brightest screen colours like paper does
        out = (1 - cmy) * (1 - k[:, :, None] * scale[:, :, None])
        out = out * 0.96 + 0.02  # paper white is not screen white
    else:
        raise ValueError(key)
    arr[:, :, :3] = np.clip(np.round(out * 255), 0, 255).astype(np.uint8)
    return _from_array(arr)


def proofs(image):
    """[(title, image)] for every proof."""
    return [(tr(t), proof(image, k)) for t, k in zip(PROOFS, _PROOF_KEYS)]


def render_map(layers, extent, crs, width=480, height=360, background="#FFFFFF"):
    """Render layers to an image (no window needed)."""
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettings
    from qgis.PyQt.QtCore import QSize

    ms = QgsMapSettings()
    ms.setLayers(list(layers))
    ms.setDestinationCrs(crs)
    ms.setOutputSize(QSize(width, height))
    ms.setExtent(extent)
    ms.setBackgroundColor(QColor(background))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def render_layout_page(layout, page=0, dpi=60):
    from qgis.core import QgsLayoutExporter

    return QgsLayoutExporter(layout).renderPageToImage(page, dpi=dpi)

