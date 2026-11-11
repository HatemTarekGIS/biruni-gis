"""The thesis defence presentation (العرض للمناقشة).

From the project's print layouts, layers and (when present) the methodology written with Biruni, one click makes:

* a PowerPoint file (.pptx, written here as plain Office Open XML - nothing to install), Arabic slides set
  right-to-left, speaker notes with the caption and the source of every figure;
* one self-contained HTML presentation (arrows, swipe on phones, full screen, slide counter, light/dark) with an
  interactive web map slide, so the supervisor and the examiners can zoom into the study area on their phones;
* a PDF of the slides for printing.

Every slide is first composed as a few primitives (rectangles, text boxes, pictures) on a 1280 x 720 stage; the
three outputs draw the same primitives, so they look the same.
"""
import base64
import datetime as dt
import html
import json
import math
import os
import re
import zipfile
from xml.sax.saxutils import escape as _xesc

from qgis.core import (QgsCoordinateTransform, QgsDistanceArea, QgsLayoutExporter, QgsMapRendererParallelJob,
                       QgsMapSettings, QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QMarginsF, QRectF, QSize, QSizeF, Qt
from qgis.PyQt.QtGui import (QColor, QFont, QFontMetricsF, QImage, QImageReader, QPageLayout, QPageSize, QPainter,
                             QPainterPath, QPdfWriter)

from ..i18n import tr
from ..version import APP_NAME

W, H = 1280, 720                      # the stage, in CSS pixels (96 dpi): 13.333 x 7.5 inches
EMU = 9525                            # EMU per stage pixel
FIGURE_PROP = "biruni/thesis-figure"  # layout custom property written by the thesis figures tool
FONT = "Segoe UI"
FONT_CSS = '"Segoe UI", Tahoma, "Noto Sans Arabic", "Noto Naskh Arabic", Arial, sans-serif'
FIGURE_DPI = 300
MAX_FIGURE_PX = 7000                  # the long side of a figure rendered at FIGURE_DPI is capped (A0 maps)
SLIDE_IMAGE_PX = 2200                 # figure images on slides
WEBMAP_EMBED_MB = 25                  # bigger web maps are linked, not embedded in the presentation
LANGS = ("ar", "en", "both")

# ---- slide boilerplate, Arabic and English -------------------------------------------------------------------
TEXT = {
    "outline": ("محتويات العرض", "Outline"),
    "intro": ("المقدمة", "Introduction"),
    "study_area": ("منطقة الدراسة", "Study area"),
    "data_method": ("البيانات والمنهجية", "Data and methodology"),
    "data": ("البيانات", "Data"),
    "methodology": ("المنهجية", "Methodology"),
    "workflow": ("خطوات العمل", "Workflow"),
    "figures": ("الخرائط والنتائج", "Maps and results"),
    "results": ("النتائج بالأرقام", "Results in numbers"),
    "conclusions": ("الخلاصة والتوصيات", "Conclusions and recommendations"),
    "thanks": ("شكراً لحسن استماعكم", "Thank you"),
    "questions": ("الأسئلة والمناقشة", "Questions and discussion"),
    "webmap": ("الخريطة التفاعلية", "Interactive map"),
    "map_hint": ("قرّب الخريطة وحرّكها لاستكشاف منطقة الدراسة، واضغط على أي عنصر لعرض بياناته",
                 "Zoom and pan to explore the study area; tap a feature to see its data"),
    "open_map": ("فتح الخريطة في صفحة كاملة", "Open the map full screen"),
    "supervisor": ("إشراف", "Supervised by"),
    "prepared": ("إعداد", "Presented by"),
    "figure": ("شكل", "Figure"),
    "source": ("المصدر", "Source"),
    "made_with": ("أُعدّ باستخدام {app}", "Made with {app}"),
    "conclusion_n": ("النتيجة {n}: …", "Conclusion {n}: …"),
    "recommendation_n": ("التوصية {n}: …", "Recommendation {n}: …"),
    "count_label": ("عدد عناصر «{layer}»", "features in {layer}"),
    "area_label": ("المساحة الكلية «{layer}»", "total area of {layer}"),
    "length_label": ("الطول الكلي «{layer}»", "total length of {layer}"),
    "maps_label": ("خريطة منتجة", "maps produced"),
    "layers_label": ("طبقة بيانات", "data layers"),
    "features": ("{n} عنصر", "{n} features"),
    "raster": ("طبقة شبكية {w} × {h} خلية", "raster, {w} × {h} cells"),
    "crs": ("نظام الإحداثيات: {crs}", "Coordinate system: {crs}"),
    "scan": ("امسح الرمز لفتح العرض والخريطة على الهاتف", "Scan to open the slides and the map on a phone"),
    "study_note": ("خريطة منطقة الدراسة بطبقات المشروع الظاهرة.", "The study area with the visible project layers."),
    "conclusions_note": ("اكتب هنا أهم النتائج والتوصيات.", "Write the main conclusions and recommendations here."),
    "thanks_note": ("اشكر المشرف ولجنة المناقشة وافتح باب الأسئلة.",
                    "Thank the supervisor and the committee, then invite questions."),
    "results_note": ("أرقام محسوبة من طبقات المشروع.", "Figures computed from the project layers."),
    "units": {"m2": ("م²", "m²"), "ha": ("هكتار", "ha"), "km2": ("كم²", "km²"), "m": ("م", "m"),
              "km": ("كم", "km")},
}

# ---- themes ---------------------------------------------------------------------------------------------------
BASE = {
    "light": {"bg": "#FFFFFF", "text": "#1E252B", "muted": "#5B6670", "rule": "#E1E6EA", "onaccent": "#FFFFFF"},
    "dark": {"bg": "#12171D", "text": "#E8ECF0", "muted": "#9AA6B2", "rule": "#2A333D", "onaccent": "#FFFFFF"},
}


def _rgb(hex_color):
    c = QColor(hex_color)
    return c.red(), c.green(), c.blue()


def _hex(r, g, b):
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v))) for v in (r, g, b)))


def _lum(hex_color):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_color)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _mix(a, b, t):
    """a blended toward b by t (0..1)."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def palette(theme="light", accent="#0E7C74"):
    """Colour roles of a theme: bg, text, muted, rule, accent (fills), accent_text (readable on bg), onaccent,
    card."""
    base = dict(BASE.get(theme, BASE["light"]))
    accent = QColor(accent).name().upper() if QColor(accent).isValid() else "#0E7C74"
    toward = "#FFFFFF" if theme == "dark" else "#000000"
    acc_text, k = accent, 0
    while _contrast(acc_text, base["bg"]) < 4.5 and k < 20:
        acc_text = _mix(acc_text, toward, 0.12)
        k += 1
    fill = accent
    while _contrast("#FFFFFF", fill) < 3.0 and _lum(fill) > 0.05:  # white text must stay readable on it
        fill = _mix(fill, "#000000", 0.12)
    base.update(accent=fill, accent_text=acc_text, card=_mix(base["bg"], accent, 0.09 if theme == "light" else 0.2),
                onaccent_dim=_mix(fill, "#000000", 0.28))
    return base


# ---- small helpers ------------------------------------------------------------------------------------------
def _t(pair, code):
    return pair[0] if code == "ar" else pair[1]


def primary(lang):
    return "en" if lang == "en" else "ar"


def _safe_file(text, default="figure"):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(text or "")).strip(" .")
    s = re.sub(r"\s+", "_", s)
    return s[:50].strip("_") or default


def _num(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


def fmt_number(v):
    if v is None:
        return ""
    a = abs(v)
    if a >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    if a >= 10:
        return f"{v:,.1f}".rstrip("0").rstrip(".")
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _first_sentence(text, limit=150):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.match(r"(.+?[.!?؟؛])(\s|$)", text)
    s = m.group(1) if m else text
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip("،,;:") + "…"
    return s


def summarise(paragraphs, n_max=5):
    """3-5 short bullets from paragraphs (the first sentence of each)."""
    if isinstance(paragraphs, str):
        paragraphs = [p for p in re.split(r"\n\s*\n|\n", paragraphs) if p.strip()]
    out = []
    for p in paragraphs or []:
        s = _first_sentence(p)
        if s and s not in out:
            out.append(s)
        if len(out) >= n_max:
            break
    return out


# ---- what goes into the presentation ------------------------------------------------------------------------
def _figure_meta(layout):
    raw = layout.customProperty(FIGURE_PROP)
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def thesis_figures(project=None):
    """Every print layout as a figure, in thesis order: layouts tagged with FIGURE_PROP first by (chapter, order)
    and numbered «شكل (3-2)» / "Figure 3.2"; untagged layouts after them in project order, numbered 1, 2, 3…

    Returns dicts: layout (name), number (ar, en), caption (ar, en), tagged."""
    project = project or QgsProject.instance()
    items = []
    for k, layout in enumerate(project.layoutManager().printLayouts()):
        items.append((layout, _figure_meta(layout), k))
    tagged = sorted([i for i in items if i[1]],
                    key=lambda i: (_num(i[1].get("chapter"), 1e9), _num(i[1].get("order"), 1e9), i[2]))
    plain = [i for i in items if not i[1]]
    out, per_chapter = [], {}
    for layout, meta, _k in tagged:
        ch = meta.get("chapter")
        ch = "" if ch is None else str(ch).strip()
        per_chapter[ch] = per_chapter.get(ch, 0) + 1
        order = _num(meta.get("order"), None)
        order = int(order) if isinstance(order, (int, float)) and order >= 1 and float(order).is_integer() \
            else per_chapter[ch]
        number = (f"شكل ({ch}-{order})", f"Figure {ch}.{order}") if ch else (f"شكل ({order})", f"Figure {order}")
        cap_ar = str(meta.get("caption") or meta.get("caption_en") or layout.name())
        cap_en = str(meta.get("caption_en") or meta.get("caption") or layout.name())
        out.append({"layout": layout.name(), "number": number, "caption": (cap_ar, cap_en), "tagged": True})
    for n, (layout, _meta, _k) in enumerate(plain, 1):
        title = layout.customProperty("biruni/studio/title") or layout.name()
        out.append({"layout": layout.name(), "number": (f"شكل ({n})", f"Figure {n}"), "caption": (title, title),
                    "tagged": False})
    return out


def _visible_layers(project):
    root = project.layerTreeRoot()
    out = []
    for lyr in root.layerOrder():
        node = root.findLayer(lyr.id())
        if node is not None and node.isVisible() and lyr.isValid():
            out.append(lyr)
    return out


def _data_layers(project):
    """Visible layers, vector layers first; all valid layers when none is visible."""
    layers = _visible_layers(project) or [l for l in project.layerTreeRoot().layerOrder() if l.isValid()]
    return [l for l in layers if isinstance(l, (QgsVectorLayer, QgsRasterLayer))]


def _area_unit(m2):
    if m2 < 10_000:
        return m2, "m2"
    if m2 < 1_000_000:
        return m2 / 10_000, "ha"
    return m2 / 1_000_000, "km2"


def _length_unit(m):
    return (m, "m") if m < 1000 else (m / 1000, "km")


def key_stats(project=None, layers=None, limit=6, max_features=200_000):
    """Big numbers for the "Results in numbers" slide: per vector layer its feature count and its total area
    (polygons) or length (lines), measured on the ellipsoid; then the number of maps.

    Returns dicts: value (float), text, unit (ar, en) or ("", ""), label (ar, en), layer."""
    project = project or QgsProject.instance()
    if layers is None:
        layers = [l for l in _data_layers(project) if isinstance(l, QgsVectorLayer)]
    da = QgsDistanceArea()
    ell = project.ellipsoid()
    da.setEllipsoid(ell if ell and ell.upper() != "NONE" else "EPSG:7030")
    out = []
    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer):
            continue
        n = lyr.featureCount()
        if n is None or n < 0:
            n = sum(1 for _ in lyr.getFeatures())
        name = lyr.name()
        out.append({"value": float(n), "text": fmt_number(n), "unit": ("", ""), "layer": name,
                    "label": tuple(s.format(layer=name) for s in TEXT["count_label"])})
        geom = lyr.geometryType() if lyr.isSpatial() else None
        if geom not in (QgsWkbTypes.GeometryType.PolygonGeometry, QgsWkbTypes.GeometryType.LineGeometry) \
                or not lyr.crs().isValid() or n > max_features or n == 0:
            continue
        da.setSourceCrs(lyr.crs(), project.transformContext())
        total = 0.0
        poly = geom == QgsWkbTypes.GeometryType.PolygonGeometry
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            try:
                total += da.measureArea(g) if poly else da.measureLength(g)
            except Exception:  # noqa: BLE001
                continue
        if total <= 0:
            continue
        value, unit = _area_unit(total) if poly else _length_unit(total)
        key = "area_label" if poly else "length_label"
        out.append({"value": value, "text": fmt_number(value), "unit": TEXT["units"][unit], "layer": name,
                    "label": tuple(s.format(layer=name) for s in TEXT[key])})
    n_maps = len(project.layoutManager().printLayouts())
    if n_maps:
        out.append({"value": float(n_maps), "text": str(n_maps), "unit": ("", ""), "layer": "",
                    "label": TEXT["maps_label"]})
    return out[:limit]


def data_bullets(project, limit=6):
    """(ar, en) lines describing the data layers and the coordinate system."""
    out = []
    for lyr in _data_layers(project)[:limit]:
        if isinstance(lyr, QgsVectorLayer):
            n = max(lyr.featureCount(), 0)
            texts = (TEXT["features"][0], "{n} feature" if n == 1 else TEXT["features"][1])
            out.append(tuple(f"{lyr.name()}: " + s.format(n=fmt_number(n)) for s in texts))
        else:
            out.append(tuple(f"{lyr.name()}: " + s.format(w=lyr.width(), h=lyr.height()) for s in TEXT["raster"]))
    crs = project.crs()
    if crs.isValid():
        name = f"{crs.authid()} – {crs.description()}" if crs.authid() else crs.description()
        out.append(tuple(s.format(crs=name) for s in TEXT["crs"]))
    return out


def _methodology_module():
    try:
        from . import methodology  # written by the thesis methodology tool; may not exist
    except Exception:  # noqa: BLE001
        return None
    return methodology


def methodology_bullets(project=None):
    """(title (ar, en), bullets [(ar, en)]) from thesis.methodology, or None when it is not available."""
    mod = _methodology_module()
    if mod is None or not hasattr(mod, "methodology_text"):
        return None
    got = {}
    for code in ("ar", "en"):
        try:
            res = mod.methodology_text(code)
        except TypeError:
            try:
                res = mod.methodology_text(lang=code, project=project or QgsProject.instance())
            except Exception:  # noqa: BLE001
                continue
        except Exception:  # noqa: BLE001
            continue
        try:
            title, paragraphs = res
        except (TypeError, ValueError):
            continue
        bullets = summarise(paragraphs)
        if bullets:
            got[code] = (str(title or ""), bullets)
    if not got:
        return None
    ar = got.get("ar") or got.get("en")
    en = got.get("en") or got.get("ar")
    title = (ar[0] or TEXT["methodology"][0], en[0] or TEXT["methodology"][1])
    if len(ar[1]) == len(en[1]):
        bullets = list(zip(ar[1], en[1]))
    else:  # the two languages do not pair up: each language keeps its own bullets
        n = max(len(ar[1]), len(en[1]))
        bullets = [(ar[1][i] if i < len(ar[1]) else "", en[1][i] if i < len(en[1]) else "") for i in range(n)]
    return title, bullets[:5]


def has_workflow():
    mod = _methodology_module()
    return mod is not None and hasattr(mod, "workflow_image")


def build_slides(project=None, opts=None):
    """The default slide list (dicts). The user may reorder or drop slides before export_presentation()."""
    project = project or QgsProject.instance()
    opts = opts or {}
    slides = [{"key": "title", "kind": "title", "title": (opts.get("title", ""), opts.get("title_en", ""))},
              {"key": "outline", "kind": "outline", "title": TEXT["outline"]}]
    if _data_layers(project):
        slides.append({"key": "study_area", "kind": "study_area", "title": TEXT["study_area"]})
    method = methodology_bullets(project)
    slides.append({"key": "data_method", "kind": "data_method", "title": TEXT["data_method"],
                   "bullets": data_bullets(project), "method": method})
    if has_workflow():
        slides.append({"key": "workflow", "kind": "workflow", "title": method[0] if method else TEXT["workflow"]})
    for fig in thesis_figures(project):
        slides.append({"key": f"figure:{fig['layout']}", "kind": "figure", "title": fig["caption"], **fig})
    if any(isinstance(l, QgsVectorLayer) and l.isSpatial() for l in _data_layers(project)):
        slides.append({"key": "webmap", "kind": "webmap", "title": TEXT["webmap"], "html_only": True})
    stats = key_stats(project)
    if stats:
        slides.append({"key": "results", "kind": "results", "title": TEXT["results"], "stats": stats})
    slides.append({"key": "conclusions", "kind": "conclusions", "title": TEXT["conclusions"]})
    slides.append({"key": "thanks", "kind": "thanks", "title": TEXT["thanks"]})
    return slides


def slide_label(slide, lang="en"):
    code = primary(lang)
    if slide["kind"] == "figure":
        return f"{_t(slide['number'], code)}: {_t(slide['caption'], code)}"
    if slide["kind"] == "title":
        return _t(slide["title"], code) or slide["title"][0] or slide["title"][1] or tr("Title slide")
    return _t(slide["title"], code)


# ---- rendering the pictures ---------------------------------------------------------------------------------
def _scaled_copy(img, path, max_px=SLIDE_IMAGE_PX):
    if max(img.width(), img.height()) > max_px:
        img = img.scaled(max_px, max_px, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return path if img.save(path) else None


def render_figure(project, layout_name, figures_dir, media_dir, n):
    """The layout's first page at 300 dpi (capped for huge pages) into figures_dir, and a copy scaled for slides
    into media_dir. Returns the slide image path or None."""
    layout = project.layoutManager().layoutByName(layout_name)
    if layout is None or layout.pageCollection().pageCount() == 0:
        return None
    size = layout.pageCollection().page(0).pageSize()
    long_mm = max(size.width(), size.height()) or 297
    dpi = min(FIGURE_DPI, MAX_FIGURE_PX * 25.4 / long_mm)
    img = QgsLayoutExporter(layout).renderPageToImage(0, QSize(), dpi)
    if img.isNull():
        return None
    base = f"{n:02d}_{_safe_file(layout_name)}"
    img.save(os.path.join(figures_dir, base + ".png"))
    return _scaled_copy(img, os.path.join(media_dir, f"figure_{n:02d}.png"))


def render_study_area(project, path, size=(1900, 1100)):
    layers = _data_layers(project)
    if not layers:
        return None
    crs = project.crs() if project.crs().isValid() else layers[0].crs()
    ext = QgsRectangle()
    ext.setNull()
    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer):
            continue
        try:
            e = QgsCoordinateTransform(lyr.crs(), crs, project).transformBoundingBox(lyr.extent())
        except Exception:  # noqa: BLE001
            continue
        if e.isFinite() and not e.isNull():
            ext.combineExtentWith(e)
    if ext.isNull():
        for lyr in layers:
            try:
                ext = QgsCoordinateTransform(lyr.crs(), crs, project).transformBoundingBox(lyr.extent())
                break
            except Exception:  # noqa: BLE001
                continue
    if ext.isNull() or ext.isEmpty():
        return None
    ext.scale(1.08)
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(crs)
    ms.setTransformContext(project.transformContext())
    ms.setOutputSize(QSize(*size))
    ms.setOutputDpi(144)
    ms.setBackgroundColor(QColor("#FFFFFF"))
    ms.setExtent(ext)
    try:
        from qgis.core import Qgis

        ms.setFlag(Qgis.MapSettingsFlag.Antialiasing, True)
    except Exception:  # noqa: BLE001
        pass
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()
    if img.isNull():
        return None
    return path if img.save(path) else None


def render_workflow(path, lang):
    mod = _methodology_module()
    if mod is None or not hasattr(mod, "workflow_image"):
        return None
    try:
        res = mod.workflow_image(path, primary(lang))
    except Exception:  # noqa: BLE001
        return None
    if isinstance(res, str) and os.path.exists(res):
        return res
    return path if os.path.exists(path) else None


def render_qr(url, path):
    try:
        from ..vendor import ensure_vendor

        ensure_vendor()
        import segno
    except Exception:  # noqa: BLE001
        return None
    try:
        segno.make(url, error="m").save(path, kind="png", scale=12, border=2, dark="#111111", light="#FFFFFF")
    except Exception:  # noqa: BLE001
        return None
    return path


def _image_size(path):
    s = QImageReader(path).size()
    return (s.width(), s.height()) if s.isValid() else (0, 0)


def _fit(box, path):
    """The picture fitted inside the box (x, y, w, h), centred."""
    x, y, w, h = box
    iw, ih = _image_size(path)
    if iw <= 0 or ih <= 0:
        return box
    s = min(w / iw, h / ih)
    fw, fh = iw * s, ih * s
    return x + (w - fw) / 2, y + (h - fh) / 2, fw, fh


# ---- composing a slide: primitives on the 1280 x 720 stage ------------------------------------------------------
def P(text, size, role="text", bold=False, rtl=False, align="start", bullet=None, space=0, italic=False):
    return {"text": str(text or ""), "size": float(size), "role": role, "bold": bold, "rtl": rtl, "align": align,
            "bullet": bullet, "space": float(space), "italic": italic}


def _qfont(size, bold=False, italic=False):
    f = QFont(FONT)
    f.setPixelSize(max(1, int(round(size))))
    f.setBold(bold)
    f.setItalic(italic)
    return f


def _qt_align(para):
    a = para["align"]
    if a == "center":
        return Qt.AlignmentFlag.AlignHCenter
    if a in ("left", "right"):
        return Qt.AlignmentFlag.AlignRight if a == "right" else Qt.AlignmentFlag.AlignLeft
    start_right = para["rtl"]
    right = (a == "start" and start_right) or (a == "end" and not start_right)
    return Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft


def _rtl_of(text, default):
    """Right-to-left when the text has Arabic letters, left-to-right when it has only Latin ones."""
    if re.search("[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFE]", text or ""):
        return True
    if re.search(r"[A-Za-z]", text or ""):
        return False
    return default


def _bidi(para):
    return ("‏" if para["rtl"] else "‎") + para["text"]


def _measure(paras, width):
    """[(height, indent)] of every paragraph wrapped to width, and the total height (spaces included)."""
    out, total = [], 0.0
    for p in paras:
        f = _qfont(p["size"], p["bold"], p["italic"])
        fm = QFontMetricsF(f)
        indent = p["size"] * 1.25 if p["bullet"] else 0.0
        r = fm.boundingRect(QRectF(0, 0, max(10.0, width - indent), 1e6),
                            int(_qt_align(p) | Qt.TextFlag.TextWordWrap), _bidi(p) if p["text"] else " ")
        h = max(r.height(), fm.height())
        out.append((h, indent))
        total += h + p["space"]
    return out, total


def _shrink(paras, width, height):
    """Shrink the font sizes until the paragraphs fit the box."""
    for _ in range(14):
        _m, total = _measure(paras, width)
        if total <= height:
            break
        for p in paras:
            p["size"] *= 0.92
            p["space"] *= 0.92
    return paras


def T(x, y, w, h, paras, anchor="top"):
    return {"t": "text", "x": x, "y": y, "w": w, "h": h, "paras": _shrink(paras, w, h), "anchor": anchor}


def R(x, y, w, h, fill, radius=0):
    return {"t": "rect", "x": x, "y": y, "w": w, "h": h, "fill": fill, "radius": radius}


def I(box, path, descr=""):
    x, y, w, h = _fit(box, path)
    return {"t": "image", "x": x, "y": y, "w": w, "h": h, "path": path, "descr": descr}


class Composer:
    """Turns slide dicts into primitives for one output."""

    def __init__(self, opts):
        self.opts = opts
        self.lang = opts.get("lang", "ar") if opts.get("lang") in LANGS else "ar"
        self.code = primary(self.lang)
        self.rtl = self.code == "ar"

    # text in the presentation language(s): the primary language, and English below it when "both"
    def bi(self, pair, size, role="text", bold=False, align="start", bullet=None, space=0, second=0.62):
        main = _t(pair, self.code) or pair[0] or pair[1]
        rtl = _rtl_of(main, self.code == "ar")
        out = [P(main, size, role, bold, rtl, align, bullet, space)]
        if self.lang == "both" and pair[1] and pair[1] != main:
            side = align if align in ("center", "left", "right") else \
                ("right" if (align == "start") == rtl else "left")
            out.append(P(pair[1], size * max(second, 0.72 if bullet else second), "muted", False, False, side,
                         " " if bullet else None, size * 0.08))
            if bullet and rtl:  # the hanging indent is on the right, as for the first language
                out[-1]["indent_side"] = "right"
        return out

    def x_start(self, x, w, inner_w):
        """x of a box of inner_w at the start side of the (x, w) band."""
        return x + w - inner_w if self.rtl else x

    def chrome(self, slide, n, total, title_pair=None, title_size=34):
        o = self.opts
        prims = [R(W - 10 if self.rtl else 0, 0, 10, H, "accent")]
        title_pair = title_pair or slide["title"]
        prims.append(T(70, 28, 1140, 92, self.bi(title_pair, title_size, "accent_text", True), "middle"))
        prims.append(R(self.x_start(70, 1140, 120), 124, 120, 4, "accent"))
        prims.append(R(70, 127, 1140, 1, "rule"))
        left = (o.get("title_short") or (o.get("title_en") if self.code == "en" else "") or o.get("title")
                or o.get("title_en") or "").strip()
        who = (o.get("students") or [""])[0] if isinstance(o.get("students"), list) else (o.get("students") or "")
        foot = " · ".join(s for s in (left[:70], who) if s)
        prims.append(T(250 if self.rtl else 70, 684, 960, 22, [P(foot, 13, "muted", rtl=_rtl_of(foot, self.rtl),
                                                                  align="right" if self.rtl else "left")], "middle"))
        prims.append(T(70 if self.rtl else 1060, 684, 150, 22,
                       [P(f"{n} / {total}", 13, "muted", rtl=False, align="left" if self.rtl else "right")],
                       "middle"))
        return prims

    def compose(self, slide, n, total, outline=None):
        kind = slide["kind"]
        fn = getattr(self, "_" + kind, None)
        return fn(slide, n, total, outline) if fn else self.chrome(slide, n, total)

    # --- the kinds ---
    def _title(self, slide, n, total, outline):
        o = self.opts
        prims = [R(0, 0, W, H, "accent"), R(0, H - 14, W, 14, "onaccent_dim")]
        uni = " — ".join(s for s in (o.get("university", ""), o.get("faculty", "")) if s.strip())
        if uni:
            prims.append(T(100, 54, 1080, 70, [P(uni, 22, "onaccent", rtl=self.rtl, align="center")], "middle"))
        title = (o.get("title") or "").strip()
        title_en = (o.get("title_en") or "").strip()
        main = title_en if self.code == "en" and title_en else title or title_en or APP_NAME
        paras = [P(main, 48, "onaccent", True, self.code == "ar" and not re.match(r"^[A-Za-z]", main), "center")]
        if self.lang == "both" and title_en and title_en != main:
            paras.append(P(title_en, 26, "onaccent", False, False, "center", space=10))
        prims.append(T(100, 140, 1080, 250, paras, "middle"))
        prims.append(R(W / 2 - 60, 408, 120, 3, "onaccent"))
        people = []
        students = o.get("students") or []
        if isinstance(students, str):
            students = [s.strip() for s in re.split(r"[\n;]", students) if s.strip()]
        sep = "، " if self.code == "ar" else ", "
        if students:
            people.append(P(f"{_t(TEXT['prepared'], self.code)}: {sep.join(students)}", 24, "onaccent", True,
                            self.rtl, "center"))
        if o.get("supervisor"):
            people.append(P(f"{_t(TEXT['supervisor'], self.code)}: {o['supervisor']}", 22, "onaccent", False,
                            self.rtl, "center", space=8))
        if o.get("date"):
            people.append(P(o["date"], 18, "onaccent", False, self.rtl, "center", space=14))
        if people:
            prims.append(T(100, 428, 1080, 250, people, "top"))
        return prims

    def _outline(self, slide, n, total, outline):
        prims = self.chrome(slide, n, total)
        paras = []
        for k, pair in enumerate(outline or [], 1):
            paras += self.bi(pair, 28, "text", False, bullet=f"{k}.", space=0 if k == 1 else 16)
        prims.append(T(110, 160, 1060, 500, paras, "top"))
        return prims

    def _picture(self, slide, n, total, caption_pairs, title_size=34):
        prims = self.chrome(slide, n, total, title_size=title_size)
        cap_h = 34 if caption_pairs else 0
        box = (70, 142, 1140, 528 - cap_h)
        if slide.get("image"):
            prims.append(R(box[0], box[1], box[2], box[3], "card", 10))
            prims.append(I((box[0] + 8, box[1] + 8, box[2] - 16, box[3] - 16), slide["image"],
                           _t(slide["title"], self.code)))
        if caption_pairs:
            paras = []
            for pair in caption_pairs:
                paras += self.bi(pair, 16, "muted", align="center", second=0.85)
            prims.append(T(70, 670 - cap_h + 4, 1140, cap_h, paras, "middle"))
        return prims

    def _study_area(self, slide, n, total, outline):
        return self._picture(slide, n, total, [])

    def _workflow(self, slide, n, total, outline):
        return self._picture(slide, n, total, [])

    def _figure(self, slide, n, total, outline):
        num, cap = slide["number"], slide["caption"]
        pair = (f"{num[0]}: {cap[0]}", f"{num[1]}: {cap[1]}")
        return self._picture(slide, n, total, [pair], title_size=30)

    def _bullets_column(self, x, y, w, h, heading, bullets, size=22):
        paras = []
        if heading:
            paras += self.bi(heading, size + 2, "accent_text", True)
        for k, pair in enumerate(bullets):
            paras += self.bi(pair, size, "text", bullet="•", space=12 if (k or heading) else 0)
        return T(x, y, w, h, paras, "top") if paras else None

    def _data_method(self, slide, n, total, outline):
        prims = self.chrome(slide, n, total)
        method = slide.get("method")
        data = slide.get("bullets") or []
        if method:
            mx, dx, rule_x = (570, 70, 540) if self.rtl else (70, 770, 740)
            first = self._bullets_column(mx, 160, 640, 500, method[0], method[1])
            second = self._bullets_column(dx, 160, 440, 500, TEXT["data"], data, 19)
            prims.append(R(rule_x, 160, 1, 480, "rule"))
            prims += [p for p in (first, second) if p]
        else:
            col = self._bullets_column(110, 160, 1060, 500, TEXT["data"], data, 24)
            if col:
                prims.append(col)
        return prims

    def _webmap(self, slide, n, total, outline):
        prims = self.chrome(slide, n, total)
        prims.append(R(70, 142, 1140, 494, "card", 10))
        prims.append({"t": "webmap", "x": 76, "y": 148, "w": 1128, "h": 482})
        prims.append(T(290, 642, 700, 34, self.bi(TEXT["map_hint"], 15, "muted", align="center", second=0.9),
                       "middle"))
        return prims

    def _results(self, slide, n, total, outline):
        prims = self.chrome(slide, n, total)
        stats = slide.get("stats") or []
        k = len(stats)
        if not k:
            return prims
        cols = k if k <= 3 else (2 if k == 4 else 3)
        rows = math.ceil(k / cols)
        gap = 28
        area_x, area_y, area_w, area_h = 70, 165, 1140, 490
        cw = (area_w - gap * (cols - 1)) / cols
        ch = min(220, (area_h - gap * (rows - 1)) / rows)
        top = area_y + (area_h - (ch * rows + gap * (rows - 1))) / 2
        for i, st in enumerate(stats):
            r, c = divmod(i, cols)
            if self.rtl:
                c = cols - 1 - c
            x = area_x + c * (cw + gap)
            y = top + r * (ch + gap)
            prims.append(R(x, y, cw, ch, "card", 16))
            prims.append(R(x + cw / 2 - 30, y + 18, 60, 4, "accent"))
            unit = _t(st["unit"], self.code)
            value = f"{st['text']} {unit}".strip()
            paras = [P(value, 54 if rows == 1 else 48, "accent_text", True, _rtl_of(value, False), "center")]
            paras += self.bi(st["label"], 19, "text", align="center", space=6, second=0.8)
            prims.append(T(x + 16, y + 30, cw - 32, ch - 44, paras, "middle"))
        return prims

    def _conclusions(self, slide, n, total, outline):
        prims = self.chrome(slide, n, total)
        paras = []
        for k in range(1, 4):
            pair = tuple(s.format(n=k) for s in TEXT["conclusion_n"])
            paras += self.bi(pair, 26, "text", bullet="•", space=0 if k == 1 else 18)
        for k in range(1, 3):
            pair = tuple(s.format(n=k) for s in TEXT["recommendation_n"])
            paras += self.bi(pair, 26, "text", bullet="•", space=18)
        prims.append(T(110, 165, 1060, 490, paras, "top"))
        return prims

    def _thanks(self, slide, n, total, outline):
        o = self.opts
        prims = [R(0, 0, W, H, "accent"), R(0, H - 14, W, 14, "onaccent_dim")]
        qr = slide.get("image")
        top_h = 300 if qr else 420
        paras = self.bi(TEXT["thanks"], 62, "onaccent", True, "center")
        for p in paras[1:]:
            p["role"] = "onaccent"
        paras += [P(_t(TEXT["questions"], self.code), 28, "onaccent", False, self.rtl, "center", space=18)]
        prims.append(T(100, 70 if qr else 120, 1080, top_h, paras, "middle"))
        if qr:
            prims.append(R(W / 2 - 100, 392, 200, 200, "onaccent", 12))
            prims.append(I((W / 2 - 92, 400, 184, 184), qr, "QR"))
            prims.append(T(200, 606, 880, 50, [P(_t(TEXT["scan"], self.code), 18, "onaccent", False, self.rtl,
                                                 "center")], "middle"))
        else:
            students = o.get("students") or []
            if isinstance(students, list) and students:
                prims.append(T(100, 560, 1080, 60, [P("، ".join(students) if self.rtl else ", ".join(students), 22,
                                                      "onaccent", False, self.rtl, "center")], "middle"))
        return prims


def outline_items(slides):
    """Section titles (ar, en) in the order of the slides."""
    out, seen = [], set()
    for s in slides:
        kind = s["kind"]
        key = "figures" if kind == "figure" else kind
        if key in seen or kind in ("title", "outline", "thanks", "webmap"):
            continue
        seen.add(key)
        out.append(TEXT["figures"] if kind == "figure" else s["title"] if kind == "workflow" else
                   TEXT.get(kind, s["title"]))
    return out


def speaker_notes(slide, lang="ar"):
    """The notes of a slide in the presentation language(s)."""
    kind = slide["kind"]
    lines = []
    for code in (("ar", "en") if lang == "both" else (primary(lang),)):
        if kind == "figure":
            lines.append(f"{_t(slide['number'], code)}: {_t(slide['caption'], code)}")
            src = slide.get("source")
            if src:
                lines.append(f"{_t(TEXT['source'], code)}: {src}")
            lines.append(f"{'التخطيط' if code == 'ar' else 'Print layout'}: {slide['layout']}")
        elif kind == "study_area":
            lines.append(_t(TEXT["study_note"], code))
        elif kind == "results":
            lines.append(_t(TEXT["results_note"], code))
            for st in slide.get("stats") or []:
                lines.append(f"{st['text']} {_t(st['unit'], code)} – {_t(st['label'], code)}".replace("  ", " "))
        elif kind == "conclusions":
            lines.append(_t(TEXT["conclusions_note"], code))
        elif kind == "thanks":
            lines.append(_t(TEXT["thanks_note"], code))
        elif kind == "data_method":
            for pair in slide.get("bullets") or []:
                lines.append(_t(pair, code))
        else:
            lines.append(slide_label(slide, code))
    return "\n".join(l for l in lines if l)


def _figure_source(project, layout_name):
    try:
        from ..map_studio import outputs

        layout = project.layoutManager().layoutByName(layout_name)
        card = outputs.map_card(layout)
        parts = [s for s in card.get("sources") or [] if s] or card.get("layers") or []
        text = "; ".join(parts)
        if card.get("crs"):
            text = f"{text} ({card['crs']})" if text else card["crs"]
        return text
    except Exception:  # noqa: BLE001
        return ""


# ---- PowerPoint (Office Open XML, written directly) ---------------------------------------------------------
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = f'xmlns:a="{NS_A}" xmlns:r="{NS_R}" xmlns:p="{NS_P}"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT = "application/vnd.openxmlformats-officedocument."
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _x(text):
    return _xesc(_BAD_XML.sub("", str(text)), {'"': "&quot;"})


def _e(v):
    return str(int(round(v * EMU)))


def _clr(hex_color):
    return f'<a:solidFill><a:srgbClr val="{QColor(hex_color).name()[1:].upper()}"/></a:solidFill>'


GRP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
       '<a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>'
       '</p:grpSpPr>')
CLRMAP = ('<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
          'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
          'folHlink="folHlink"/>')


def _theme_xml(name, accent):
    acc = QColor(accent).name()[1:].upper()
    fill3 = "".join('<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' for _ in range(3))
    lines = "".join(f'<a:ln w="{w}"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
                    for w in (6350, 12700, 19050))
    fonts = (f'<a:latin typeface="{FONT}"/><a:ea typeface=""/><a:cs typeface="{FONT}"/>')
    return (XML_HEAD + f'<a:theme xmlns:a="{NS_A}" name="{_x(name)}"><a:themeElements>'
            f'<a:clrScheme name="Biruni"><a:dk1><a:srgbClr val="1E252B"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/>'
            f'</a:lt1><a:dk2><a:srgbClr val="2B3A47"/></a:dk2><a:lt2><a:srgbClr val="EEF1F3"/></a:lt2>'
            f'<a:accent1><a:srgbClr val="{acc}"/></a:accent1><a:accent2><a:srgbClr val="E07A2E"/></a:accent2>'
            f'<a:accent3><a:srgbClr val="5B8DB8"/></a:accent3><a:accent4><a:srgbClr val="8E6BB0"/></a:accent4>'
            f'<a:accent5><a:srgbClr val="C9A227"/></a:accent5><a:accent6><a:srgbClr val="4E9A51"/></a:accent6>'
            f'<a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
            f'</a:clrScheme><a:fontScheme name="Biruni"><a:majorFont>{fonts}</a:majorFont>'
            f'<a:minorFont>{fonts}</a:minorFont></a:fontScheme><a:fmtScheme name="Biruni">'
            f'<a:fillStyleLst>{fill3}</a:fillStyleLst><a:lnStyleLst>{lines}</a:lnStyleLst>'
            f'<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/>'
            f'</a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
            f'<a:bgFillStyleLst>{fill3}</a:bgFillStyleLst></a:fmtScheme></a:themeElements>'
            f'<a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


_ARABIC = re.compile("[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFE]")
_LATIN = re.compile(r"[A-Za-z0-9\u00C0-\u024F]")


def script_runs(text, rtl):
    """The text cut where the script changes: [(chunk, is_arabic)]. Spaces and punctuation stay with the run
    before them. PowerPoint draws Latin letters backwards inside a run marked Arabic, so each gets its own run."""
    out = []
    for ch in text:
        kind = True if _ARABIC.match(ch) else False if _LATIN.match(ch) else None
        if out and (kind is None or kind == out[-1][1] or out[-1][1] is None):
            chunk, k = out[-1]
            out[-1] = (chunk + ch, k if k is not None else kind)
        else:
            out.append((ch, kind))
    for i in range(len(out) - 1):  # a space between two scripts goes in the Arabic run (PowerPoint drops it else)
        chunk, k = out[i]
        stripped = chunk.rstrip(" ")
        if stripped and stripped != chunk and k is not True and out[i + 1][1] is True:
            out[i] = (stripped, k)
            out[i + 1] = (chunk[len(stripped):] + out[i + 1][0], out[i + 1][1])
    return [(c, rtl if k is None else k) for c, k in out]


def _pptx_paragraph(p, pal):
    lang = "ar-SA" if p["rtl"] else "en-US"
    algn = {"center": "ctr", "left": "l", "right": "r"}.get(p["align"]) or \
        ("r" if (p["align"] == "start") == p["rtl"] else "l")
    sz = max(100, int(round(p["size"] * 75)))  # px -> 1/100 pt
    attrs = f' algn="{algn}" rtl="{1 if p["rtl"] else 0}"'
    inner = ""
    if p["space"]:
        inner += f'<a:spcBef><a:spcPts val="{int(round(p["space"] * 75))}"/></a:spcBef>'
    if p["bullet"]:
        ind = int(round(p["size"] * 1.25 * EMU))
        attrs = f' marL="{ind}" indent="-{ind}"' + attrs
        inner += _clr(pal["accent_text"]).replace("solidFill", "buClr")
        if not p["bullet"].strip():
            attrs = attrs.replace(f' indent="-{ind}"', ' indent="0"')
            if p.get("indent_side") == "right" and not p["rtl"]:
                attrs = attrs.replace(f' marL="{ind}"', f' marR="{ind}"')
            inner = inner[:inner.index("<a:buClr>")] + "<a:buNone/>"
        elif p["bullet"] == "•":
            inner += '<a:buFont typeface="Arial"/><a:buChar char="•"/>'
        else:
            num = int(re.sub(r"\D", "", p["bullet"]) or 1)
            inner += f'<a:buFont typeface="{FONT}"/><a:buAutoNum type="arabicPeriod" startAt="{num}"/>'
    else:
        inner += "<a:buNone/>"
    runs = []
    for chunk, arabic in script_runs(p["text"], p["rtl"]):
        rlang = "ar-SA" if arabic else "en-US"
        rtl_el = '<a:rtl val="1"/>' if arabic else ""
        rpr = (f'<a:rPr lang="{rlang}" altLang="en-US" sz="{sz}" b="{1 if p["bold"] else 0}" '
               f'i="{1 if p["italic"] else 0}" dirty="0">{_clr(pal[p["role"]])}<a:latin typeface="{FONT}"/>'
               f'<a:cs typeface="{FONT}"/>{rtl_el}</a:rPr>')
        runs.append(f'<a:r>{rpr}<a:t>{_x(chunk)}</a:t></a:r>')
    return f'<a:p><a:pPr{attrs}>{inner}</a:pPr>{"".join(runs)}<a:endParaRPr lang="{lang}" sz="{sz}"/></a:p>'


def _pptx_shape(prim, sid, pal, rels):
    x, y, w, h = (_e(prim[k]) for k in ("x", "y", "w", "h"))
    xfrm = f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
    t = prim["t"]
    if t == "rect":
        if prim["radius"]:
            adj = int(min(50000, prim["radius"] / max(1, min(prim["w"], prim["h"])) * 100000))
            geom = f'<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val {adj}"/></a:avLst></a:prstGeom>'
        else:
            geom = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="Shape {sid}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
                f'<p:spPr>{xfrm}{geom}{_clr(pal[prim["fill"]])}<a:ln><a:noFill/></a:ln></p:spPr></p:sp>')
    if t == "text":
        anchor = {"top": "t", "middle": "ctr", "bottom": "b"}[prim["anchor"]]
        paras = "".join(_pptx_paragraph(p, pal) for p in prim["paras"]) or "<a:p/>"
        rtl_any = any(p["rtl"] for p in prim["paras"])
        return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="Text {sid}"/><p:cNvSpPr txBox="1"/><p:nvPr/>'
                f'</p:nvSpPr><p:spPr>{xfrm}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
                f'<p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="{anchor}" '
                f'rtlCol="{1 if rtl_any else 0}"><a:noAutofit/></a:bodyPr><a:lstStyle/>{paras}</p:txBody></p:sp>')
    if t == "image":
        rid = rels(prim["path"])
        return (f'<p:pic><p:nvPicPr><p:cNvPr id="{sid}" name="Picture {sid}" descr="{_x(prim.get("descr", ""))}"/>'
                f'<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
                f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
                f'<p:spPr>{xfrm}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')
    return ""


def _notes_xml(text, rtl):
    paras = []
    for line in (text or "").split("\n") or [""]:
        lang = "ar-SA" if rtl and re.search(r"[؀-ۿ]", line) else "en-US"
        r = 1 if lang == "ar-SA" else 0
        paras.append(f'<a:p><a:pPr algn="{"r" if r else "l"}" rtl="{r}"/><a:r><a:rPr lang="{lang}" dirty="0"/>'
                     f'<a:t>{_x(line)}</a:t></a:r></a:p>')
    return (XML_HEAD + f'<p:notes {NS}><p:cSld><p:spTree>{GRP}'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" '
            'noRot="1" noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr>'
            '<p:spPr/></p:sp><p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr>'
            '<a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/>{"".join(paras)}</p:txBody></p:sp></p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>')


NOTES_MASTER = (XML_HEAD + f'<p:notesMaster {NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/>'
                f'</p:bgRef></p:bg><p:spTree>{GRP}'
                '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks '
                'noGrp="1" noRot="1" noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg" idx="2"/>'
                '</p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="685800" y="1143000"/><a:ext cx="5486400" '
                'cy="3086100"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln '
                'w="12700"><a:solidFill><a:prstClr val="black"/></a:solidFill></a:ln></p:spPr></p:sp>'
                '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/>'
                '</p:cNvSpPr><p:nvPr><p:ph type="body" sz="quarter" idx="3"/></p:nvPr></p:nvSpPr><p:spPr>'
                '<a:xfrm><a:off x="685800" y="4400550"/><a:ext cx="5486400" cy="3600450"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/>'
                '<a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp></p:spTree></p:cSld>'
                f'{CLRMAP}</p:notesMaster>')


def write_pptx(path, composed, pal, opts):
    """composed: [(primitives, notes text)]. Writes a 16:9 PowerPoint file."""
    n = len(composed)
    rtl = primary(opts.get("lang", "ar")) == "ar"
    files = {}
    media, media_names = {}, []

    ct = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Default Extension="png" ContentType="image/png"/>',
          '<Default Extension="jpeg" ContentType="image/jpeg"/>',
          '<Default Extension="jpg" ContentType="image/jpeg"/>',
          f'<Override PartName="/ppt/presentation.xml" ContentType="{CT}presentationml.presentation.main+xml"/>',
          f'<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="{CT}presentationml.slideMaster+xml"/>',
          f'<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="{CT}presentationml.slideLayout+xml"/>',
          f'<Override PartName="/ppt/notesMasters/notesMaster1.xml" ContentType="{CT}presentationml.notesMaster+xml"/>',
          f'<Override PartName="/ppt/theme/theme1.xml" ContentType="{CT}theme+xml"/>',
          f'<Override PartName="/ppt/theme/theme2.xml" ContentType="{CT}theme+xml"/>',
          f'<Override PartName="/ppt/presProps.xml" ContentType="{CT}presentationml.presProps+xml"/>',
          f'<Override PartName="/ppt/viewProps.xml" ContentType="{CT}presentationml.viewProps+xml"/>',
          f'<Override PartName="/ppt/tableStyles.xml" ContentType="{CT}presentationml.tableStyles+xml"/>',
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
          f'<Override PartName="/docProps/app.xml" ContentType="{CT}extended-properties+xml"/>']
    for i in range(1, n + 1):
        ct.append(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="{CT}presentationml.slide+xml"/>')
        ct.append(f'<Override PartName="/ppt/notesSlides/notesSlide{i}.xml" '
                  f'ContentType="{CT}presentationml.notesSlide+xml"/>')
    files["[Content_Types].xml"] = (XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                                    'content-types">' + "".join(ct) + "</Types>")
    files["_rels/.rels"] = (XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                            f'relationships"><Relationship Id="rId1" Type="{REL}officeDocument" '
                            'Target="ppt/presentation.xml"/><Relationship Id="rId2" Type="http://schemas.'
                            'openxmlformats.org/package/2006/relationships/metadata/core-properties" '
                            f'Target="docProps/core.xml"/><Relationship Id="rId3" Type="{REL}extended-properties" '
                            'Target="docProps/app.xml"/></Relationships>')
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    students = opts.get("students") or []
    author = "; ".join(students) if isinstance(students, list) else str(students)
    files["docProps/core.xml"] = (
        XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{_x(opts.get("title") or opts.get("title_en") or "")}</dc:title>'
        f'<dc:creator>{_x(author)}</dc:creator><cp:lastModifiedBy>{_x(APP_NAME)}</cp:lastModifiedBy>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
    files["docProps/app.xml"] = (
        XML_HEAD + f'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f'<Application>{_x(APP_NAME)}</Application><PresentationFormat>Widescreen</PresentationFormat>'
        f'<Slides>{n}</Slides><Notes>{n}</Notes></Properties>')
    rels = [f'<Relationship Id="rId1" Type="{REL}slideMaster" Target="slideMasters/slideMaster1.xml"/>',
            f'<Relationship Id="rId2" Type="{REL}notesMaster" Target="notesMasters/notesMaster1.xml"/>',
            f'<Relationship Id="rId3" Type="{REL}theme" Target="theme/theme1.xml"/>',
            f'<Relationship Id="rId4" Type="{REL}presProps" Target="presProps.xml"/>',
            f'<Relationship Id="rId5" Type="{REL}viewProps" Target="viewProps.xml"/>',
            f'<Relationship Id="rId6" Type="{REL}tableStyles" Target="tableStyles.xml"/>']
    sld_ids = []
    for i in range(1, n + 1):
        rels.append(f'<Relationship Id="rId{10 + i}" Type="{REL}slide" Target="slides/slide{i}.xml"/>')
        sld_ids.append(f'<p:sldId id="{255 + i}" r:id="rId{10 + i}"/>')
    files["ppt/_rels/presentation.xml.rels"] = (XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/'
                                                'package/2006/relationships">' + "".join(rels) + "</Relationships>")
    files["ppt/presentation.xml"] = (
        XML_HEAD + f'<p:presentation {NS} saveSubsetFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" '
        'r:id="rId1"/></p:sldMasterIdLst><p:notesMasterIdLst><p:notesMasterId r:id="rId2"/></p:notesMasterIdLst>'
        f'<p:sldIdLst>{"".join(sld_ids)}</p:sldIdLst><p:sldSz cx="{W * EMU}" cy="{H * EMU}"/>'
        '<p:notesSz cx="6858000" cy="9144000"/><p:defaultTextStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr>'
        '</p:defaultTextStyle></p:presentation>')
    files["ppt/presProps.xml"] = XML_HEAD + f'<p:presentationPr {NS}/>'
    files["ppt/viewProps.xml"] = (XML_HEAD + f'<p:viewPr {NS}><p:normalViewPr><p:restoredLeft sz="15620"/>'
                                  '<p:restoredTop sz="94660"/></p:normalViewPr><p:gridSpacing cx="76200" '
                                  'cy="76200"/></p:viewPr>')
    files["ppt/tableStyles.xml"] = (XML_HEAD + f'<a:tblStyleLst xmlns:a="{NS_A}" '
                                    'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>')
    files["ppt/theme/theme1.xml"] = _theme_xml("Biruni", pal["accent"])
    files["ppt/theme/theme2.xml"] = _theme_xml("Biruni Notes", pal["accent"])
    files["ppt/slideMasters/slideMaster1.xml"] = (
        XML_HEAD + f'<p:sldMaster {NS}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef>'
        f'</p:bg><p:spTree>{GRP}</p:spTree></p:cSld>{CLRMAP}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" '
        'r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>')
    files["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = (
        XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        f'<Relationship Id="rId2" Type="{REL}theme" Target="../theme/theme1.xml"/></Relationships>')
    files["ppt/slideLayouts/slideLayout1.xml"] = (
        XML_HEAD + f'<p:sldLayout {NS} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>{GRP}'
        '</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    files["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = (
        XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        '</Relationships>')
    files["ppt/notesMasters/notesMaster1.xml"] = NOTES_MASTER
    files["ppt/notesMasters/_rels/notesMaster1.xml.rels"] = (
        XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}theme" Target="../theme/theme2.xml"/></Relationships>')

    for i, (prims, notes) in enumerate(composed, 1):
        slide_rels = [f'<Relationship Id="rId1" Type="{REL}slideLayout" Target="../slideLayouts/slideLayout1.xml"/>',
                      f'<Relationship Id="rId2" Type="{REL}notesSlide" Target="../notesSlides/notesSlide{i}.xml"/>']
        local = {}

        def rel_for(img_path, slide_rels=slide_rels, local=local):
            if img_path not in media:
                ext = os.path.splitext(img_path)[1].lower().lstrip(".") or "png"
                name = f"image{len(media) + 1}.{'jpeg' if ext == 'jpg' else ext}"
                media[img_path] = name
                media_names.append((img_path, name))
            if img_path not in local:
                rid = f"rId{len(slide_rels) + 1}"
                local[img_path] = rid
                slide_rels.append(f'<Relationship Id="{rid}" Type="{REL}image" Target="../media/{media[img_path]}"/>')
            return local[img_path]

        shapes, sid = [], 2
        for prim in prims:
            if prim["t"] == "webmap":
                continue
            shapes.append(_pptx_shape(prim, sid, pal, rel_for))
            sid += 1
        files[f"ppt/slides/slide{i}.xml"] = (
            XML_HEAD + f'<p:sld {NS}><p:cSld><p:bg><p:bgPr>{_clr(pal["bg"])}<a:effectLst/></p:bgPr></p:bg>'
            f'<p:spTree>{GRP}{"".join(shapes)}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
            '</p:sld>')
        files[f"ppt/slides/_rels/slide{i}.xml.rels"] = (
            XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(slide_rels) + "</Relationships>")
        files[f"ppt/notesSlides/notesSlide{i}.xml"] = _notes_xml(notes, rtl)
        files[f"ppt/notesSlides/_rels/notesSlide{i}.xml.rels"] = (
            XML_HEAD + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{REL}notesMaster" Target="../notesMasters/notesMaster1.xml"/>'
            f'<Relationship Id="rId2" Type="{REL}slide" Target="../slides/slide{i}.xml"/></Relationships>')

    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", files.pop("[Content_Types].xml"))
        for name, data in files.items():
            z.writestr(name, data)
        for src, name in media_names:
            z.write(src, f"ppt/media/{name}", compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, path)
    return path


# ---- QPainter: PDF pages and preview images -----------------------------------------------------------------
def paint_slide(painter, prims, pal, images=None):
    """Draws the primitives on a painter whose coordinates are the 1280 x 720 stage."""
    images = {} if images is None else images
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.fillRect(QRectF(0, 0, W, H), QColor(pal["bg"]))
    painter.setPen(Qt.PenStyle.NoPen)
    for prim in prims:
        t = prim["t"]
        rect = QRectF(prim["x"], prim["y"], prim["w"], prim["h"])
        if t == "rect":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal[prim["fill"]]))
            if prim["radius"]:
                path = QPainterPath()
                path.addRoundedRect(rect, prim["radius"], prim["radius"])
                painter.drawPath(path)
            else:
                painter.drawRect(rect)
        elif t == "image":
            img = images.get(prim["path"])
            if img is None:
                img = QImage(prim["path"])
                images[prim["path"]] = img
            if not img.isNull():
                # at most 2.5x the box: enough for print, and the PDF stays small
                target = QSize(int(prim["w"] * 2.5), int(prim["h"] * 2.5))
                if img.width() > target.width() * 1.2:
                    img = img.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                painter.drawImage(rect, img)
        elif t == "text":
            _paint_text(painter, prim, pal)


def _paint_text(painter, prim, pal):
    paras = prim["paras"]
    sizes, total = _measure(paras, prim["w"])
    y = prim["y"]
    if prim["anchor"] == "middle":
        y += (prim["h"] - total) / 2
    elif prim["anchor"] == "bottom":
        y += prim["h"] - total
    for p, (h, indent) in zip(paras, sizes):
        y += p["space"]
        if not p["text"]:
            y += h
            continue
        font = _qfont(p["size"], p["bold"], p["italic"])
        painter.setFont(font)
        painter.setPen(QColor(pal[p["role"]]))
        x, w = prim["x"], prim["w"]
        if p["bullet"]:
            bfont = _qfont(p["size"], True)
            painter.setFont(bfont)
            painter.setPen(QColor(pal["accent_text"]))
            right_side = p["rtl"] or p.get("indent_side") == "right"
            brect = QRectF(x + w - indent, y, indent, h) if right_side else QRectF(x, y, indent, h)
            fm = QFontMetricsF(font)
            painter.drawText(QRectF(brect.x(), y, brect.width(), fm.height()),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop), p["bullet"])
            painter.setFont(font)
            painter.setPen(QColor(pal[p["role"]]))
            if not right_side:
                x += indent
            w -= indent
        painter.drawText(QRectF(x, y, w, h + 2), int(_qt_align(p) | Qt.TextFlag.TextWordWrap), _bidi(p))
        y += h


def slide_image(prims, pal, width=1280):
    img = QImage(width, int(width * H / W), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(pal["bg"]))
    painter = QPainter(img)
    painter.scale(width / W, width / W)
    paint_slide(painter, prims, pal)
    painter.end()
    return img


def write_pdf(path, composed, pal, title=""):
    writer = QPdfWriter(path)
    writer.setTitle(title)
    writer.setCreator(APP_NAME)
    writer.setResolution(144)
    writer.setPageLayout(QPageLayout(QPageSize(QSizeF(W / 96 * 25.4, H / 96 * 25.4), QPageSize.Unit.Millimeter),
                                     QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
    painter = QPainter(writer)
    images = {}
    try:
        for i, (prims, _notes) in enumerate(composed):
            if i:
                writer.newPage()
            vp = painter.viewport()
            painter.save()
            painter.scale(vp.width() / W, vp.height() / H)
            paint_slide(painter, prims, pal, images)
            painter.restore()
    finally:
        painter.end()
    return path


# ---- the HTML presentation -----------------------------------------------------------------------------------
def _data_uri(path, cache):
    if path in cache:
        return cache[path]
    img = QImage(path)
    if img.isNull():
        cache[path] = ""
        return ""
    if max(img.width(), img.height()) > 1920:
        img = img.scaled(1920, 1920, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if img.hasAlphaChannel():
        img.save(buf, "PNG")
        mime = "image/png"
    else:
        img.save(buf, "JPEG", 88)
        mime = "image/jpeg"
    buf.close()
    uri = f"data:{mime};base64," + base64.b64encode(bytes(ba)).decode("ascii")
    cache[path] = uri
    return uri


def _css_align(p):
    if p["align"] in ("center", "left", "right"):
        return p["align"]
    right = (p["align"] == "start") == p["rtl"]
    return "right" if right else "left"


def _html_prims(prims, cache):
    out = []
    for prim in prims:
        pos = f'left:{prim["x"]:.1f}px;top:{prim["y"]:.1f}px;width:{prim["w"]:.1f}px;height:{prim["h"]:.1f}px'
        t = prim["t"]
        if t == "rect":
            rad = f';border-radius:{prim["radius"]}px' if prim["radius"] else ""
            out.append(f'<div class="r" style="{pos};background:var(--{prim["fill"].replace("_", "-")}){rad}"></div>')
        elif t == "image":
            out.append(f'<img class="i" style="{pos}" alt="{html.escape(prim.get("descr", ""))}" '
                       f'src="{_data_uri(prim["path"], cache)}">')
        elif t == "webmap":
            out.append(f'<div class="wm" style="{pos}"></div>')
        elif t == "text":
            just = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[prim["anchor"]]
            paras = []
            for p in prim["paras"]:
                style = (f'font-size:{p["size"]:.1f}px;color:var(--{p["role"].replace("_", "-")});'
                         f'text-align:{_css_align(p)};margin-top:{p["space"]:.1f}px')
                if p["bold"]:
                    style += ";font-weight:700"
                if p["italic"]:
                    style += ";font-style:italic"
                cls = ' class="b"' if p["bullet"] else ""
                if p["bullet"] and p.get("indent_side") == "right" and not p["rtl"]:
                    cls = ""
                    style += f";padding-right:{p['size'] * 1.25:.1f}px"
                data = f' data-b="{html.escape(p["bullet"])}"' if p["bullet"] else ""
                paras.append(f'<p dir="{"rtl" if p["rtl"] else "ltr"}"{cls}{data} style="{style}">'
                             f'{html.escape(p["text"])}</p>')
            out.append(f'<div class="t" style="{pos};justify-content:{just}">{"".join(paras)}</div>')
    return "".join(out)


def _palette_css(name, pal):
    return f"body.{name}{{" + ";".join(f"--{k.replace('_', '-')}:{v}" for k, v in pal.items()) + "}"


def write_html(path, composed, palettes, opts, webmap_html=None, webmap_file=None):
    """composed: [(primitives, notes)]; palettes: {"light": pal, "dark": pal}."""
    code = primary(opts.get("lang", "ar"))
    rtl = code == "ar"
    cache = {}
    sections = []
    for i, (prims, _notes) in enumerate(composed, 1):
        sections.append(f'<section class="slide" data-n="{i}" aria-label="{i}">{_html_prims(prims, cache)}</section>')
    title = opts.get("title") or opts.get("title_en") or APP_NAME
    wm_json = json.dumps(webmap_html or "", ensure_ascii=False).replace("<", "\\u003c")
    page = (HTML_TEMPLATE
            .replace("%%LANG%%", code)
            .replace("%%DIR%%", "rtl" if rtl else "ltr")
            .replace("%%TITLE%%", html.escape(title))
            .replace("%%FONT%%", FONT_CSS)
            .replace("%%PALETTES%%", _palette_css("light", palettes["light"]) + _palette_css("dark", palettes["dark"]))
            .replace("%%THEME%%", "dark" if opts.get("theme") == "dark" else "light")
            .replace("%%SLIDES%%", "\n".join(sections))
            .replace("%%WEBMAP%%", wm_json)
            .replace("%%WEBMAP_FILE%%", html.escape(webmap_file or ""))
            .replace("%%OPEN_MAP%%", html.escape(_t(TEXT["open_map"], code)))
            .replace("%%MADE%%", html.escape(_t(TEXT["made_with"], code).format(app=APP_NAME))))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return path


HTML_TEMPLATE = r"""<!doctype html>
<html lang="%%LANG%%" dir="%%DIR%%">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="generator" content="Biruni GIS">
<title>%%TITLE%%</title>
<style>
%%PALETTES%%
html,body{margin:0;height:100%;overflow:hidden;background:#000;font-family:%%FONT%%;-webkit-text-size-adjust:100%}
body{touch-action:pan-y pinch-zoom}
#deck{position:fixed;inset:0;background:var(--bg)}
#stage{position:absolute;left:0;top:0;width:1280px;height:720px;transform-origin:0 0;overflow:hidden;background:var(--bg)}
.slide{position:absolute;inset:0;display:none;background:var(--bg)}
.slide.on{display:block;animation:fade .35s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
.r,.i,.t,.wm{position:absolute;box-sizing:border-box}
.i{object-fit:contain}
.t{display:flex;flex-direction:column;overflow:hidden}
.t p{margin:0;line-height:normal;white-space:pre-wrap;overflow-wrap:anywhere;unicode-bidi:plaintext}
.t p.b{position:relative;padding-inline-start:1.25em}
.t p.b::before{content:attr(data-b);position:absolute;inset-inline-start:0;width:1.25em;text-align:center;
 color:var(--accent-text);font-weight:700}
#map{position:fixed;border:0;display:none;background:#fff;z-index:2}
#maplink{position:fixed;display:none;z-index:3;font-size:13px;padding:4px 10px;border-radius:6px;
 background:var(--accent);color:var(--onaccent);text-decoration:none;white-space:nowrap}
#ui{position:fixed;top:10px;right:10px;display:flex;gap:6px;align-items:center;z-index:5;
 opacity:.35;transition:opacity .2s;direction:ltr}
#ui:hover,body.touch #ui{opacity:.9}
#ui button{font:inherit;font-size:16px;min-width:36px;height:36px;border:0;border-radius:8px;cursor:pointer;
 background:var(--card);color:var(--text)}
#count{font-size:14px;color:var(--text);background:var(--card);padding:8px 10px;border-radius:8px;min-width:54px;
 text-align:center}
#bar{position:fixed;left:0;bottom:0;height:3px;background:var(--accent);z-index:4;transition:width .3s}
noscript{position:fixed;top:0;left:0;right:0;padding:8px;background:#fd0;color:#000;z-index:9}
@media print{#ui,#bar,#map,#maplink{display:none!important}}
</style>
</head>
<body class="%%THEME%%">
<div id="deck"><div id="stage">
%%SLIDES%%
</div></div>
<iframe id="map" title="map" allowfullscreen></iframe>
<a id="maplink" href="%%WEBMAP_FILE%%" target="_blank" rel="noopener">%%OPEN_MAP%% &#8599;</a>
<div id="bar"></div>
<div id="ui">
<button id="prev" aria-label="previous">&#8249;</button><span id="count"></span><button id="next" aria-label="next">&#8250;</button>
<button id="theme" aria-label="light / dark" title="light / dark (T)">&#9680;</button>
<button id="fs" aria-label="full screen" title="full screen (F)">&#x26F6;</button>
</div>
<script type="application/json" id="webmap-src">%%WEBMAP%%</script>
<script>
(function () {
  "use strict";
  var stage = document.getElementById("stage");
  var slides = Array.prototype.slice.call(document.querySelectorAll(".slide"));
  var count = document.getElementById("count"), bar = document.getElementById("bar");
  var frame = document.getElementById("map"), mapLink = document.getElementById("maplink");
  var rtl = document.documentElement.dir === "rtl";
  var total = slides.length, cur = 0, scale = 1, offX = 0, offY = 0, mapLoaded = false;
  var mapSrc = "";
  try { mapSrc = JSON.parse(document.getElementById("webmap-src").textContent || '""'); } catch (e) { mapSrc = ""; }

  function fit() {
    var w = window.innerWidth, h = window.innerHeight;
    scale = Math.min(w / 1280, h / 720);
    offX = (w - 1280 * scale) / 2;
    offY = (h - 720 * scale) / 2;
    stage.style.transform = "translate(" + offX + "px," + offY + "px) scale(" + scale + ")";
    placeMap();
  }

  function placeMap() {
    var box = slides[cur] && slides[cur].querySelector(".wm");
    if (!box) { frame.style.display = "none"; mapLink.style.display = "none"; return; }
    var x = parseFloat(box.style.left), y = parseFloat(box.style.top);
    var w = parseFloat(box.style.width), h = parseFloat(box.style.height);
    frame.style.left = (offX + x * scale) + "px";
    frame.style.top = (offY + y * scale) + "px";
    frame.style.width = (w * scale) + "px";
    frame.style.height = (h * scale) + "px";
    if (!mapLoaded) {
      if (mapSrc) frame.srcdoc = mapSrc;
      else if (mapLink.getAttribute("href")) frame.src = mapLink.getAttribute("href");
      mapLoaded = true;
    }
    frame.style.display = "block";
    if (mapLink.getAttribute("href")) {
      mapLink.style.display = "block";
      mapLink.style.top = (offY + (y + h + 14) * scale) + "px";
      if (rtl) mapLink.style.left = (offX + (x - 6) * scale) + "px";
      else mapLink.style.left = (offX + (x + w + 6) * scale - mapLink.offsetWidth) + "px";
    }
  }

  function show(i) {
    if (i < 0 || i >= total) return;
    if (slides[cur]) slides[cur].classList.remove("on");
    cur = i;
    slides[cur].classList.add("on");
    count.textContent = (cur + 1) + " / " + total;
    bar.style.width = (100 * (cur + 1) / total) + "%";
    if (rtl) { bar.style.left = "auto"; bar.style.right = "0"; }
    if (history.replaceState) history.replaceState(null, "", "#" + (cur + 1));
    placeMap();
  }
  function next() { show(cur + 1); }
  function prev() { show(cur - 1); }

  function fullscreen() {
    var d = document, el = d.documentElement;
    if (d.fullscreenElement || d.webkitFullscreenElement) {
      (d.exitFullscreen || d.webkitExitFullscreen).call(d);
    } else if (el.requestFullscreen) {
      el.requestFullscreen();
    } else if (el.webkitRequestFullscreen) {
      el.webkitRequestFullscreen();
    }
  }
  function toggleTheme() {
    var b = document.body;
    b.className = b.className.indexOf("dark") >= 0 ? b.className.replace("dark", "light") : b.className.replace("light", "dark");
  }

  document.addEventListener("keydown", function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    var k = e.key;
    if (k === "ArrowRight") { if (rtl) prev(); else next(); }
    else if (k === "ArrowLeft") { if (rtl) next(); else prev(); }
    else if (k === "PageDown" || k === " " || k === "ArrowDown" || k === "Enter") next();
    else if (k === "PageUp" || k === "ArrowUp" || k === "Backspace") prev();
    else if (k === "Home") show(0);
    else if (k === "End") show(total - 1);
    else if (k === "f" || k === "F") fullscreen();
    else if (k === "t" || k === "T") toggleTheme();
    else return;
    e.preventDefault();
  });

  var sx = null, sy = null;
  document.addEventListener("touchstart", function (e) {
    document.body.classList.add("touch");
    if (e.touches.length !== 1) { sx = null; return; }
    sx = e.touches[0].clientX; sy = e.touches[0].clientY;
  }, {passive: true});
  document.addEventListener("touchend", function (e) {
    if (sx === null) return;
    var t = e.changedTouches[0], dx = t.clientX - sx, dy = t.clientY - sy;
    sx = null;
    if (Math.abs(dx) < 50 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
    var forward = rtl ? dx > 0 : dx < 0;
    if (forward) next(); else prev();
  }, {passive: true});

  document.getElementById("next").onclick = function () { if (rtl) prev(); else next(); };
  document.getElementById("prev").onclick = function () { if (rtl) next(); else prev(); };
  document.getElementById("fs").onclick = fullscreen;
  document.getElementById("theme").onclick = toggleTheme;
  window.addEventListener("resize", fit);
  window.addEventListener("hashchange", function () {
    var n = parseInt(location.hash.slice(1), 10);
    if (n >= 1 && n <= total && n - 1 !== cur) show(n - 1);
  });

  if (rtl) {  // the buttons read in the slide direction, on the side away from the title
    document.getElementById("ui").style.right = "auto";
    document.getElementById("ui").style.left = "10px";
    document.getElementById("prev").innerHTML = "&#8250;";
    document.getElementById("next").innerHTML = "&#8249;";
    var ui = document.getElementById("ui");
    ui.insertBefore(document.getElementById("next"), ui.firstChild);
    ui.insertBefore(document.getElementById("count"), document.getElementById("next").nextSibling);
  }
  var start = parseInt(location.hash.slice(1), 10);
  fit();
  show(start >= 1 && start <= total ? start - 1 : 0);
})();
</script>
<noscript>%%MADE%%</noscript>
</body>
</html>
"""


# ---- the whole export ----------------------------------------------------------------------------------------
def _norm_opts(opts):
    o = dict(opts or {})
    if o.get("lang") not in LANGS:
        o["lang"] = "ar"
    if o.get("theme") not in ("light", "dark"):
        o["theme"] = "light"
    o["accent"] = o.get("accent") or "#0E7C74"
    st = o.get("students") or []
    if isinstance(st, str):
        st = [s.strip() for s in re.split(r"[\n;]", st) if s.strip()]
    o["students"] = st
    return o


def export_presentation(folder, slides=None, opts=None, project=None, outputs=("pptx", "html", "pdf"),
                        feedback=None):
    """Write the defence presentation into ``folder``.

    slides: the list from build_slides() after the user's changes (None: the default list).
    opts: title, title_en, students (list or lines), supervisor, university, faculty, date, lang ("ar", "en",
    "both"), theme ("light", "dark"), accent ("#rrggbb"), url (where the page will be published: a QR code on the
    last slide), basename.
    Returns dict(pptx, html, pdf, webmap, figures: [paths], slides: n, warnings: [text])."""
    project = project or QgsProject.instance()
    opts = _norm_opts(opts)
    slides = [dict(s) for s in (slides if slides is not None else build_slides(project, opts))]
    for s in slides:
        if s["kind"] == "title":
            s["title"] = (opts.get("title", ""), opts.get("title_en", ""))
    os.makedirs(folder, exist_ok=True)
    media = os.path.join(folder, "media")
    figures_dir = os.path.join(folder, "figures")
    os.makedirs(media, exist_ok=True)
    base = _safe_file(opts.get("basename") or (opts.get("title_en") if opts["lang"] == "en" else "")
                      or opts.get("title") or opts.get("title_en") or "defense", "defense")
    warnings = []
    result = {"figures": [], "warnings": warnings}

    jobs = [s for s in slides if s["kind"] in ("figure", "study_area", "workflow", "thanks", "webmap")]
    steps = len(jobs) + len(outputs)
    step = [0]

    def tick(text):
        if feedback is not None:
            feedback(step[0], steps, text)
        step[0] += 1

    # 1. the pictures
    fig_n = 0
    keep = []
    webmap_html, webmap_file = None, None
    for s in slides:
        kind = s["kind"]
        if kind == "figure":
            tick(s["layout"])
            fig_n += 1
            os.makedirs(figures_dir, exist_ok=True)
            img = render_figure(project, s["layout"], figures_dir, media, fig_n)
            if not img:
                warnings.append(tr("The print layout \"{name}\" could not be rendered.").format(name=s["layout"]))
                continue
            s["image"] = img
            s["source"] = _figure_source(project, s["layout"])
            result["figures"].append(img)
        elif kind == "study_area":
            tick(_t(TEXT["study_area"], "en"))
            s["image"] = render_study_area(project, os.path.join(media, "study_area.png"))
            if not s["image"]:
                continue
        elif kind == "workflow":
            tick(_t(TEXT["workflow"], "en"))
            s["image"] = render_workflow(os.path.join(media, "workflow.png"), opts["lang"])
            if not s["image"]:
                continue
        elif kind == "thanks" and opts.get("url"):
            tick("QR")
            s["image"] = render_qr(opts["url"], os.path.join(media, "qr.png"))
        elif kind == "webmap":
            tick(_t(TEXT["webmap"], "en"))
            webmap_html, webmap_file = _make_webmap(project, folder, opts, warnings)
            if not (webmap_html or webmap_file):
                continue
        keep.append(s)
    slides = keep

    # 2. compose, once for the HTML (with the web map slide) and once for the files without it
    comp = Composer(opts)
    pal = palette(opts["theme"], opts["accent"])

    def composed_for(items):
        outline = outline_items(items)
        return [(comp.compose(s, i, len(items), outline), speaker_notes(s, lang=opts["lang"]))
                for i, s in enumerate(items, 1)]

    paper = [s for s in slides if not s.get("html_only")]
    paper_composed = composed_for(paper)
    result["slides"] = len(paper)
    result["slide_list"] = [slide_label(s, opts["lang"]) for s in paper]

    if "pptx" in outputs:
        tick("PowerPoint")
        try:
            result["pptx"] = write_pptx(os.path.join(folder, base + ".pptx"), paper_composed, pal, opts)
        except Exception as e:  # noqa: BLE001
            warnings.append(tr("The PowerPoint file was not written: {error}").format(error=e))
    if "html" in outputs:
        tick("HTML")
        web_slides = slides if (webmap_html or webmap_file) else paper
        palettes = {"light": palette("light", opts["accent"]), "dark": palette("dark", opts["accent"])}
        result["html"] = write_html(os.path.join(folder, base + ".html"), composed_for(web_slides), palettes, opts,
                                    webmap_html, webmap_file)
        result["html_slides"] = len(web_slides)
    if "pdf" in outputs:
        tick("PDF")
        result["pdf"] = write_pdf(os.path.join(folder, base + ".pdf"), paper_composed, pal,
                                  opts.get("title") or opts.get("title_en") or "")
    if "png" in outputs:  # one picture per slide, to send on a phone or to put in a report
        tick("PNG")
        png_dir = os.path.join(folder, "slides")
        os.makedirs(png_dir, exist_ok=True)
        result["png"] = []
        for i, (prims, _notes) in enumerate(paper_composed, 1):
            path = os.path.join(png_dir, f"slide_{i:02d}.png")
            if slide_image(prims, pal, 1920).save(path):
                result["png"].append(path)
    if webmap_file:
        result["webmap"] = os.path.join(folder, webmap_file)
    if feedback is not None:
        feedback(steps, steps, "")
    return result


def _make_webmap(project, folder, opts, warnings):
    """The one-file web map of the visible vector layers, next to the presentation (webmap.html). Returns
    (page text to embed or None when too big, file name or None)."""
    from ..share.styles import geometry_kind
    from ..share.webmap import export_web_map, file_size_mb

    layers = [l for l in _data_layers(project) if isinstance(l, QgsVectorLayer) and l.isSpatial()
              and geometry_kind(l) is not None]
    if not layers:
        return None, None
    path = os.path.join(folder, "webmap.html")
    n = sum(max(l.featureCount(), 0) for l in layers)
    title = opts.get("title") or opts.get("title_en") or ""
    try:
        export_web_map(layers, path, title, opts.get("basemap", "osm"),
                       {"project": project, "simplify_m": 1.0 if n > 50_000 else 0.0})
    except Exception as e:  # noqa: BLE001
        warnings.append(tr("The web map was not made: {error}").format(error=e))
        return None, None
    if file_size_mb(path) > WEBMAP_EMBED_MB:
        warnings.append(tr("The web map is large, so the presentation links to webmap.html instead of holding it. "
                           "Keep the two files together."))
        return None, "webmap.html"
    with open(path, encoding="utf-8") as f:
        page = f.read()
    return page.replace("</head>", "<style>#bar{display:none!important}#map{top:0!important}</style></head>", 1), \
        "webmap.html"


def summary(result):
    """Lines for a message to the user."""
    files = [os.path.basename(result[k]) for k in ("pptx", "html", "pdf", "webmap") if result.get(k)]
    text = tr("{n} slides. Files written: {files}").format(n=result.get("slides", 0), files=", ".join(files))
    if result.get("warnings"):
        text += "\n\n" + "\n".join(result["warnings"])
    return text
