"""Thesis map figures in the university's style (خرائط الرسالة).

Every map of a thesis is one print layout with a *figure record* stored in the layout itself (custom property
``biruni/thesis-figure``: {"chapter": int, "order": int, "caption": str, "caption_en": str} - other tools, such as
the defence slides, read this key). Numbers are never typed: they follow from the chapter and the order, so moving
or deleting a figure renumbers the others.

A *style* is the university's rules for figures: page and margins, Arabic and Latin fonts, where the caption goes and
how it is written («شكل (3-2): …» / "Figure 3.2: …"), numbering per chapter or continuous, the language(s), the
coordinate grid (degrees-minutes or projected metres) and whether an inset location map is drawn. Three styles come
with Biruni; students save their own as JSON and share them with classmates.

The figure template reuses Map Studio's parts: readable legend names, data credits, smart placement of the legend,
north arrow, scale bar and inset where the map has no data, the grid annotations of ``map_grid``.

API:  figures(project) -> [dict]  ·  add_figure(...)  ·  apply_style(layout, style)  ·  export_all(folder, dpi=300)
      list_of_figures(path)  ·  renumber(project)  ·  move_figure(layout, chapter, order)  ·  delete_figure(layout)
"""
import copy
import datetime as dt
import html
import json
import os
import re
import types

from qgis.core import (QgsCoordinateReferenceSystem, QgsGeometry, QgsLayoutItem, QgsLayoutItemLabel,
                       QgsLayoutItemMap, QgsLayoutItemPage, QgsLayoutItemPicture, QgsLayoutMeasurement,
                       QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout, QgsProject, QgsRectangle, QgsSettings,
                       QgsUnitTypes)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF

from .. import mapsheet
from ..i18n import tr
from ..map_studio import auto_layout, placement

KEY = "biruni/thesis-figure"            # the figure record (read by other tools - do not rename)
STYLE_KEY = "biruni/thesis-style"       # the style the figure was laid out with (JSON)
LAYERS_KEY = "biruni/thesis-layers"     # ids of the layers of the main map
EXTENT_KEY = "biruni/thesis-extent"     # WKT of the main map's extent
SETTINGS_KEY = "biruni/thesis/styles"   # the user's own styles (JSON list) in the QGIS settings
PT_MM = 0.3528
PAGES = {"A4": (210.0, 297.0), "A5": (148.0, 210.0), "B5": (176.0, 250.0), "A3": (297.0, 420.0),
         "Letter": (215.9, 279.4)}
GRIDS = ("degrees", "metres", "none")
LANGUAGES = ("ar", "en", "both")
NUMBERING = ("chapter", "continuous")
ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# ---- styles -----------------------------------------------------------------------------------------------------
DEFAULT = {
    "id": "custom", "name": "My style",
    "page": "A4", "orientation": "portrait",                # portrait | landscape | auto
    "margins": {"top": 25.0, "bottom": 25.0, "left": 25.0, "right": 25.0},
    "font_ar": "Simplified Arabic", "font_latin": "Times New Roman",
    "caption_pt": 12.0, "caption_bold": True, "caption_italic": False,
    "caption_position": "below",                           # below | above
    "caption_layout": "inline",                            # inline (number and text on one line) | stacked
    "caption_align": "center",                             # center | lead (right in Arabic, left in English)
    "language": "ar",                                      # ar | en | both
    "numbering": "chapter",                                # chapter (3-2) | continuous (7)
    "caption_ar": "شكل ({num}): {caption}", "caption_en": "Figure {num}: {caption}",
    "sep_ar": "-", "sep_en": ".", "arabic_digits": False,
    "source_ar": "المصدر: من إعداد الباحث اعتماداً على {sources}.", "source_ar_none": "المصدر: من إعداد الباحث.",
    "source_en": "Source: prepared by the author based on {sources}.",
    "source_en_none": "Source: prepared by the author.",
    "source_pt": 10.0, "text_pt": 9.0, "label_pt": 7.0,
    "legend_title_ar": "مفتاح الخريطة", "legend_title_en": "Legend",
    "grid": "degrees",                                     # degrees | metres | none
    "inset": True, "frame_mm": 0.35,
}

BUILTIN = {
    "ar_university": dict(DEFAULT, **{
        "id": "ar_university", "name": "Arabic university (common style)",
        "margins": {"top": 25.0, "bottom": 25.0, "left": 25.0, "right": 35.0},   # binding on the right
    }),
    "en_apa": dict(DEFAULT, **{
        "id": "en_apa", "name": "English (APA-like)",
        "margins": {"top": 25.4, "bottom": 25.4, "left": 38.0, "right": 25.4},   # binding on the left
        "font_ar": "Times New Roman", "font_latin": "Times New Roman",
        "caption_position": "above", "caption_layout": "stacked", "caption_align": "lead",
        "caption_bold": False, "caption_italic": True,
        "language": "en", "numbering": "continuous",
        "caption_en": "Figure {num}", "caption_ar": "شكل {num}",
        "source_en": "Note. Data from {sources}.", "source_en_none": "Note. Prepared by the author.",
        "source_pt": 10.0, "inset": True, "grid": "degrees",
    }),
    "plain": dict(DEFAULT, **{
        "id": "plain", "name": "Plain",
        "orientation": "auto", "margins": {"top": 15.0, "bottom": 15.0, "left": 15.0, "right": 15.0},
        "font_ar": "Arial", "font_latin": "Arial",
        "caption_bold": False, "caption_align": "lead", "caption_pt": 11.0,
        "language": "en", "caption_ar": "شكل {num}: {caption}",
        "source_en": "Source: {sources}.", "source_ar": "المصدر: {sources}.",
        "grid": "metres", "inset": False, "frame_mm": 0.25,
    }),
}
BUILTIN_ORDER = ["ar_university", "en_apa", "plain"]


def _num(v, default, lo=None, hi=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def normalise_style(data):
    """A complete, safe style from any dict (an imported file may be partial, old or hand-edited)."""
    data = data if isinstance(data, dict) else {}
    base = copy.deepcopy(BUILTIN.get(data.get("based_on")) or DEFAULT)
    out = copy.deepcopy(base)
    for key, default in base.items():
        if key not in data:
            continue
        v = data[key]
        if key == "margins":
            v = v if isinstance(v, dict) else {}
            out[key] = {s: _num(v.get(s), default[s], 0, 120) for s in ("top", "bottom", "left", "right")}
        elif key == "page":
            if isinstance(v, str) and v in PAGES:
                out[key] = v
            elif isinstance(v, (list, tuple)) and len(v) == 2:
                out[key] = [_num(v[0], 210, 50, 2000), _num(v[1], 297, 50, 2000)]
        elif isinstance(default, bool):
            out[key] = bool(v)
        elif isinstance(default, float):
            out[key] = _num(v, default, 0.05, 72)
        elif isinstance(default, str):
            out[key] = str(v)[:300]
    for key, allowed in (("grid", GRIDS), ("language", LANGUAGES), ("numbering", NUMBERING),
                         ("orientation", ("portrait", "landscape", "auto")), ("caption_position", ("below", "above")),
                         ("caption_layout", ("inline", "stacked")), ("caption_align", ("center", "lead"))):
        if out[key] not in allowed:
            out[key] = base[key]
    out["id"] = re.sub(r"[^\w\-]+", "_", str(out.get("id") or "custom"))[:60] or "custom"
    return out


def user_styles():
    try:
        raw = json.loads(QgsSettings().value(SETTINGS_KEY, "[]") or "[]")
    except (TypeError, ValueError):
        raw = []
    return [normalise_style(s) for s in raw if isinstance(s, dict)]


def all_styles():
    """Built-in styles first, then the user's own; a user style with a built-in id is skipped."""
    out = [copy.deepcopy(BUILTIN[k]) for k in BUILTIN_ORDER]
    out += [s for s in user_styles() if s["id"] not in BUILTIN]
    return out


def save_user_style(style):
    """Keep a style in the user's settings (replacing one with the same id); returns the saved style."""
    style = normalise_style(style)
    if style["id"] in BUILTIN:
        style["id"] = style["id"] + "_copy"
    mine = [s for s in user_styles() if s["id"] != style["id"]] + [style]
    QgsSettings().setValue(SETTINGS_KEY, json.dumps(mine, ensure_ascii=False))
    return style


def delete_user_style(style_id):
    mine = [s for s in user_styles() if s["id"] != style_id]
    QgsSettings().setValue(SETTINGS_KEY, json.dumps(mine, ensure_ascii=False))


def export_style(style, path):
    """Write a style as a JSON file to share with classmates."""
    style = normalise_style(style)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(style, format="biruni-thesis-style", version=1), f, ensure_ascii=False, indent=1)
    return path


def import_style(path, keep=True):
    """Read a shared style file; ``keep`` also saves it among the user's styles."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(tr("This file is not a figure style."))
    style = normalise_style(data)
    return save_user_style(style) if keep else style


def get_style(style=None):
    """A style dict from an id, a dict or None (Arabic university style)."""
    if isinstance(style, dict):
        return normalise_style(style)
    if isinstance(style, str):
        for s in all_styles():
            if s["id"] == style:
                return s
    return copy.deepcopy(BUILTIN["ar_university"])


def layout_style(layout):
    raw = layout.customProperty(STYLE_KEY)
    if raw:
        try:
            return normalise_style(json.loads(raw))
        except (TypeError, ValueError):
            pass
    return get_style(None)


def primary_language(style):
    return "en" if style["language"] == "en" else "ar"


# ---- the figure record ------------------------------------------------------------------------------------------
def read_record(layout):
    """The figure record of a layout, or None when the layout is not a thesis figure."""
    raw = layout.customProperty(KEY)
    if not raw:
        return None
    try:
        rec = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    try:
        rec["chapter"] = int(rec.get("chapter") or 1)
        rec["order"] = int(rec.get("order") or 1)
    except (TypeError, ValueError):
        return None
    rec["caption"] = str(rec.get("caption") or "")
    rec["caption_en"] = str(rec.get("caption_en") or "")
    return rec


def write_record(layout, rec):
    data = {"chapter": int(rec["chapter"]), "order": int(rec["order"]), "caption": str(rec.get("caption") or ""),
            "caption_en": str(rec.get("caption_en") or "")}
    if rec.get("source"):
        data["source"] = str(rec["source"])
    layout.setCustomProperty(KEY, json.dumps(data, ensure_ascii=False))


def _project(project):
    return project or QgsProject.instance()


def _records(project):
    items = []
    for lay in _project(project).layoutManager().printLayouts():
        rec = read_record(lay)
        if rec is not None:
            items.append((lay, rec))
    items.sort(key=lambda t: (t[1]["chapter"], t[1]["order"], t[0].name()))
    return items


def number_text(chapter, order, index, style, lang):
    """The figure number as written in ``lang``: 3-2 / 3.2 per chapter, or the running ``index``."""
    if style["numbering"] == "continuous":
        text = str(index)
    else:
        text = f"{chapter}{style['sep_ar'] if lang == 'ar' else style['sep_en']}{order}"
    if lang == "ar" and style.get("arabic_digits"):
        text = text.translate(ARABIC_INDIC)
    return text


def _fill(pattern, **values):
    """``pattern`` with {name} replaced (unknown braces are left as they are)."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), pattern)


def caption_text(style, lang, number, caption):
    """The whole caption line: «شكل (3-2): …» / "Figure 3.2: …" (stacked styles: the number line only)."""
    pattern = style["caption_ar"] if lang == "ar" else style["caption_en"]
    text = _fill(pattern, num=number, caption=caption)
    if not caption:          # no text: drop the dangling separator of "شكل (3-2): "
        text = re.sub(r"\s*[:.\-–]\s*$", "", text.strip())
    return text


def _caption_of(rec, lang):
    """The caption in ``lang``, falling back to the other language when that one is empty."""
    main, en = rec.get("caption") or "", rec.get("caption_en") or ""
    return (en or main) if lang == "en" else (main or en)


def figures(project=None):
    """The project's figures in thesis order: [{"layout", "chapter", "order", "number", "number_ar", "number_en",
    "label", "caption", "caption_en", "text_ar", "text_en", "style"}]. ``order`` is the place in the chapter
    (1, 2, …) whatever was stored, so the numbers stay right after a figure is deleted."""
    out = []
    rank = {}
    for index, (lay, rec) in enumerate(_records(project), start=1):
        ch = rec["chapter"]
        rank[ch] = rank.get(ch, 0) + 1
        style = layout_style(lay)
        n_ar = number_text(ch, rank[ch], index, style, "ar")
        n_en = number_text(ch, rank[ch], index, style, "en")
        lang = primary_language(style)
        text_ar = caption_text(style, "ar", n_ar, _caption_of(rec, "ar")) if style["caption_layout"] == "inline" \
            else f"{caption_text(style, 'ar', n_ar, '')}: {_caption_of(rec, 'ar')}".rstrip(": ")
        text_en = caption_text(style, "en", n_en, _caption_of(rec, "en")) if style["caption_layout"] == "inline" \
            else f"{caption_text(style, 'en', n_en, '')}. {_caption_of(rec, 'en')}".rstrip(". ")
        out.append({
            "layout": lay.name(), "chapter": ch, "order": rank[ch], "stored_order": rec["order"], "index": index,
            "number": n_ar if lang == "ar" else n_en, "number_ar": n_ar, "number_en": n_en,
            "label": caption_text(style, lang, n_ar if lang == "ar" else n_en, ""),
            "caption": rec["caption"], "caption_en": rec["caption_en"], "source": rec.get("source", ""),
            "text_ar": text_ar, "text_en": text_en, "style": style["id"],
            "file_stem": "Fig_" + number_text(ch, rank[ch], index, dict(style, sep_en="-"), "en"),
        })
    return out


def figure_layout(name, project=None):
    return _project(project).layoutManager().layoutByName(name)


def renumber(project=None):
    """Store 1, 2, 3 … as the orders of every chapter (closing the gaps of deleted or moved figures) and rewrite the
    numbers on the pages. Returns figures()."""
    project = _project(project)
    rank = {}
    for lay, rec in _records(project):
        rank[rec["chapter"]] = rank.get(rec["chapter"], 0) + 1
        if rec["order"] != rank[rec["chapter"]]:
            rec["order"] = rank[rec["chapter"]]
            write_record(lay, rec)
    refresh_captions(project)
    return figures(project)


def refresh_captions(project=None):
    """Write the current numbers into the caption items of every figure (no re-layout)."""
    project = _project(project)
    for fig in figures(project):
        lay = figure_layout(fig["layout"], project)
        if lay is None:
            continue
        style = layout_style(lay)
        rec = read_record(lay)
        for lang in ("ar", "en"):
            for item_id, text in _caption_lines(style, rec, lang, fig):
                item = lay.itemById(item_id)
                if item is not None and isinstance(item, QgsLayoutItemLabel):
                    item.setText(text)


def _caption_lines(style, rec, lang, fig):
    """[(item id, text)] of the caption of one language (stacked styles give a number line and a text line)."""
    number = fig["number_ar"] if lang == "ar" else fig["number_en"]
    caption = _caption_of(rec, lang)
    if style["caption_layout"] == "stacked":
        return [(f"thesis-number-{lang}", caption_text(style, lang, number, "")),
                (f"thesis-caption-{lang}", caption)]
    return [(f"thesis-caption-{lang}", caption_text(style, lang, number, caption))]


def move_figure(layout, chapter=None, order=None, project=None):
    """Put a figure in ``chapter`` at place ``order`` (1 = first; None = last); the others shift and every number
    is rewritten."""
    project = _project(project or layout.project())
    rec = read_record(layout)
    if rec is None:
        raise ValueError(tr("This layout is not a thesis figure."))
    chapter = int(chapter or rec["chapter"])
    others = [(lay, r) for lay, r in _records(project) if lay is not layout and r["chapter"] == chapter]
    pos = len(others) if order is None else max(0, min(len(others), int(order) - 1))
    seq = [lay for lay, _r in others]
    seq.insert(pos, layout)
    rec["chapter"] = chapter
    write_record(layout, rec)
    for k, lay in enumerate(seq, start=1):
        r = read_record(lay)
        r["order"] = k
        write_record(lay, r)
    return renumber(project)


def delete_figure(layout, remove_layout=True, project=None):
    """Take a figure out of the thesis (and the layout out of the project when ``remove_layout``); renumbers."""
    project = _project(project or layout.project())
    if remove_layout:
        project.layoutManager().removeLayout(layout)
    else:
        layout.removeCustomProperty(KEY)
    return renumber(project)


def set_figure(layout, chapter=None, order=None, caption=None, caption_en=None, source=None, project=None):
    """Change a figure's record; the page is laid out again when its text changed."""
    rec = read_record(layout) or {"chapter": 1, "order": 1, "caption": "", "caption_en": ""}
    changed = False
    for key, value in (("caption", caption), ("caption_en", caption_en), ("source", source)):
        if value is not None and value != rec.get(key, ""):
            rec[key] = value
            changed = True
    write_record(layout, rec)
    if chapter is not None or order is not None:
        move_figure(layout, chapter, order, project)
    if changed:
        apply_style(layout, layout_style(layout), project)
    else:
        refresh_captions(project or layout.project())
    return read_record(layout)


# ---- text measuring ---------------------------------------------------------------------------------------------
_FAMILIES = []


def font_family(name, fallbacks=("Times New Roman", "Arial", "Segoe UI")):
    """``name`` when the font is installed, else the first installed fallback (all have Arabic letters)."""
    if not _FAMILIES:
        try:
            _FAMILIES.extend(QFontDatabase.families())
        except Exception:  # noqa: BLE001
            _FAMILIES.append("")
    have = {f.lower() for f in _FAMILIES}
    for f in (name, *fallbacks):
        if f and f.lower() in have:
            return f
    return name


def text_width_mm(text, family, pt, bold=False, italic=False):
    f = QFont(family)
    f.setPixelSize(100)
    f.setBold(bold)
    f.setItalic(italic)
    return QFontMetricsF(f).horizontalAdvance(text) / 100.0 * pt * PT_MM


def wrap(text, width_mm, family, pt, bold=False, italic=False):
    """Lines of ``text`` that fit ``width_mm`` (greedy, by words)."""
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if cur and text_width_mm(trial, family, pt, bold, italic) > width_mm:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
    return lines or [""]


def _text_format(family, pt, bold=False, italic=False, color="#111111"):
    fmt = mapsheet._text_format(pt, bold, color, family)
    if italic:
        f = fmt.font()
        f.setItalic(True)
        fmt.setFont(f)
    return fmt


def _text_item(layout, item_id, text, rect, family, pt, bold=False, italic=False, align=Qt.AlignmentFlag.AlignHCenter,
               color="#111111"):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    lab.setTextFormat(_text_format(family, pt, bold, italic, color))
    lab.setHAlign(align)
    lab.setVAlign(Qt.AlignmentFlag.AlignTop)
    lab.setMarginX(0)
    lab.setMarginY(0)
    layout.addLayoutItem(lab)
    lab.attemptMove(QgsLayoutPoint(rect.x(), rect.y()))
    lab.attemptResize(QgsLayoutSize(rect.width(), rect.height()))
    lab.setId(item_id)
    return lab


# ---- sources -----------------------------------------------------------------------------------------------------
def sources(layers, rec=None, inset=False):
    """Credits of the data shown, in order: the figure's own source text first, then what the layers say."""
    out = []
    own = (rec or {}).get("source") or ""
    for part in re.split(r"[;,،]\s*", own):
        if part.strip() and part.strip() not in out:
            out.append(part.strip())
    for c in auto_layout.credits(layers):
        if c not in out:
            out.append(c)
    if inset and not any("natural earth" in c.lower() for c in out):
        out.append("Natural Earth")
    return out


def source_text(style, lang, parts):
    # an Arabic comma between Latin names would make each name its own right-to-left run and reverse their order
    sep = "، " if lang == "ar" and any(re.search(r"[؀-ۿ]", p) for p in parts) else ", "
    if parts:
        return _fill(style["source_ar"] if lang == "ar" else style["source_en"], sources=sep.join(parts))
    return style["source_ar_none"] if lang == "ar" else style["source_en_none"]


# ---- laying out a figure -----------------------------------------------------------------------------------------
def page_size(style, aspect=1.4):
    page = style["page"]
    w, h = PAGES.get(page, (210.0, 297.0)) if isinstance(page, str) else (float(page[0]), float(page[1]))
    w, h = min(w, h), max(w, h)
    orient = style["orientation"]
    if orient == "auto":
        orient = "landscape" if aspect > 1.25 else "portrait"
    return (h, w) if orient == "landscape" else (w, h)


def grid_room(style):
    """Room (mm) kept on each side of the map frame for the grid labels: one line of text, its distance from the
    frame and the frame ticks. (QGIS's own bounding box of a grid counts the label length on every side.)"""
    return 0.0 if style["grid"] == "none" else style["label_pt"] * PT_MM * 1.5 + 3.2


def round_scales():
    """Scales a thesis map is printed at: 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5 × 10ⁿ (finer than the sheet scales,
    so rounding never leaves half of the frame empty)."""
    return [m * 10 ** e for e in range(2, 9) for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5)]


def round_scale(scale):
    for s in round_scales():
        if s >= scale - 1e-6:
            return s
    return scale


def _clear(layout):
    for mf in list(layout.multiFrames()):
        try:
            layout.removeMultiFrame(mf)
        except Exception:  # noqa: BLE001
            pass
    for item in [i for i in layout.items() if isinstance(i, QgsLayoutItem) and not isinstance(i, QgsLayoutItemPage)]:
        try:
            layout.removeLayoutItem(item)
        except RuntimeError:      # already gone with its group
            pass
    pages = layout.pageCollection()
    while pages.pageCount() > 1:
        pages.deletePage(pages.pageCount() - 1)
    if pages.pageCount() == 0:
        layout.initializeDefaults()


def _main_map(layout):
    item = layout.itemById("map")
    if isinstance(item, QgsLayoutItemMap):
        return item
    maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap) and i.id() != "inset"]
    return max(maps, key=lambda m: m.sizeWithUnits().width() * m.sizeWithUnits().height()) if maps else None


def _what_to_draw(layout, project, layers, extent):
    """(layers, extent, crs) of the figure: given > the layout's main map > the stored settings > the project."""
    crs = project.crs() if project.crs().isValid() else QgsCoordinateReferenceSystem("EPSG:3857")
    mp = _main_map(layout)
    if layers is None:
        if mp is not None and mp.keepLayerSet() and mp.layers():
            layers = list(mp.layers())
        else:
            ids = [i for i in (layout.customProperty(LAYERS_KEY) or "").split(",") if i]
            layers = [project.mapLayer(i) for i in ids if project.mapLayer(i) is not None] or None
    layers = [l for l in (layers or auto_layout.drawn_layers(project)) if l is not None and l.isValid()]
    if extent is None and mp is not None and not mp.extent().isEmpty():
        extent, crs = QgsRectangle(mp.extent()), mp.crs() if mp.crs().isValid() else crs
    if extent is None and layout.customProperty(EXTENT_KEY):
        extent = QgsGeometry.fromWkt(layout.customProperty(EXTENT_KEY)).boundingBox()
    if extent is None or extent.isEmpty():
        extent = auto_layout.data_extent(project, layers, crs)
    return layers, extent, crs


def _caption_blocks(style, rec, fig):
    """[(item id, text, family, pt, bold, italic, lang)] of the caption, in reading order."""
    langs = ["ar", "en"] if style["language"] == "both" else [primary_language(style)]
    out = []
    for lang in langs:
        family = font_family(style["font_ar"] if lang == "ar" else style["font_latin"])
        for item_id, text in _caption_lines(style, rec, lang, fig):
            if not text:
                continue
            is_number = item_id.startswith("thesis-number")
            bold = style["caption_bold"] or is_number
            italic = style["caption_italic"] and not is_number
            out.append((item_id, text, family, style["caption_pt"], bold, italic, lang))
    return out


def _fig_entry(layout, project, rec):
    for fig in figures(project):
        if fig["layout"] == layout.name():
            return fig
    style = layout_style(layout)
    return {"number_ar": number_text(rec["chapter"], rec["order"], 1, style, "ar"),
            "number_en": number_text(rec["chapter"], rec["order"], 1, style, "en")}


def apply_style(layout, style=None, project=None, layers=None, extent=None):
    """Lay ``layout`` out as a thesis figure in ``style`` (an id or a dict): map frame with a coordinate grid around
    it, legend of the drawn layers, north arrow, round metric scale bar, optional inset location map, the numbered
    caption and the source line. The layout's items are replaced; its map extent and layers are kept.
    Returns {kind: item}."""
    project = _project(project or layout.project())
    style = get_style(style)
    rec = read_record(layout)
    if rec is None:
        rec = {"chapter": 1, "order": len(_records(project)) + 1, "caption": layout.name(), "caption_en": ""}
        write_record(layout, rec)
    layers, ext, crs = _what_to_draw(layout, project, layers, extent)
    layout.setCustomProperty(STYLE_KEY, json.dumps(style, ensure_ascii=False))
    fig = _fig_entry(layout, project, rec)
    _clear(layout)

    lang = primary_language(style)
    rtl = lang == "ar"
    family = font_family(style["font_ar"] if rtl else style["font_latin"])
    latin = font_family(style["font_latin"])
    aspect = ext.width() / ext.height() if not ext.isEmpty() and ext.height() > 0 else 1.4
    pw, ph = page_size(style, aspect)
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(pw, ph))
    mg = style["margins"]
    x0, y0, x1, y1 = mg["left"], mg["top"], pw - mg["right"], ph - mg["bottom"]
    text_w = x1 - x0
    lead = Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft

    # caption and source: measured lines
    blocks = []
    for item_id, text, fam, pt, bold, italic, blang in _caption_blocks(style, rec, fig):
        lines = wrap(text, text_w * 0.97, fam, pt, bold, italic)
        h = len(lines) * pt * PT_MM * 1.42
        align = Qt.AlignmentFlag.AlignHCenter if style["caption_align"] == "center" else (
            Qt.AlignmentFlag.AlignRight if blang == "ar" else Qt.AlignmentFlag.AlignLeft)
        blocks.append((item_id, "\n".join(lines), fam, pt, bold, italic, align, h))
    cap_h = sum(b[-1] for b in blocks) + 1.2 * max(0, len(blocks) - 1)
    src_langs = ["ar", "en"] if style["language"] == "both" else [lang]
    inset_wanted = bool(style["inset"])
    parts = sources(layers, rec, inset_wanted)
    src_blocks = []
    for slang in src_langs:
        fam = font_family(style["font_ar"] if slang == "ar" else style["font_latin"])
        text = source_text(style, slang, parts)
        lines = wrap(text, text_w * 0.97, fam, style["source_pt"])
        src_blocks.append((f"thesis-source-{slang}", "\n".join(lines), fam, style["source_pt"],
                           Qt.AlignmentFlag.AlignRight if slang == "ar" else Qt.AlignmentFlag.AlignLeft,
                           len(lines) * style["source_pt"] * PT_MM * 1.42))
    src_h = sum(b[-1] for b in src_blocks) + 0.8 * max(0, len(src_blocks) - 1)

    # the map frame: as large as the text block allows, in the data's proportions, with room for the grid labels
    gap = 4.0
    room = grid_room(style)
    avail_h = (y1 - y0) - cap_h - src_h - 2 * gap - 2 * room
    full_w = text_w - 2 * room
    frame_w, frame_h = full_w, min(avail_h, max(full_w / aspect, full_w * 0.72))
    if full_w / aspect > avail_h:          # a tall area: a narrower frame, centred
        frame_w = max(full_w * 0.55, min(full_w, avail_h * aspect))
        frame_h = avail_h
    block_h = cap_h + gap + 2 * room + frame_h + gap + src_h
    top = y0 + max(0.0, ((y1 - y0) - block_h) / 2.0)
    if style["caption_position"] == "above":
        cap_y = top
        frame_y = top + cap_h + gap + room
    else:
        frame_y = top + room
        cap_y = frame_y + frame_h + room + gap
    src_y = (frame_y + frame_h + room + gap) if style["caption_position"] == "above" else cap_y + cap_h + gap
    frame = QRectF(x0 + (text_w - frame_w) / 2.0, frame_y, frame_w, frame_h)

    mp = QgsLayoutItemMap(layout)
    mp.setId("map")
    layout.addLayoutItem(mp)
    mp.attemptMove(QgsLayoutPoint(frame.x(), frame.y()))
    mp.attemptResize(QgsLayoutSize(frame.width(), frame.height()))
    mp.setCrs(crs)
    mp.setLayers(layers)
    mp.setKeepLayerSet(True)
    if not ext.isEmpty():
        mp.zoomToExtent(ext)
        if mp.scale() > 0:
            mp.setScale(round_scale(mp.scale()))
    mp.setFrameEnabled(True)
    mp.setFrameStrokeWidth(QgsLayoutMeasurement(style["frame_mm"]))
    mp.setBackgroundColor(QColor("#FFFFFF"))
    made = {"map": mp}

    if style["grid"] != "none":
        made["grid"] = _grid(mp, style, lang, latin if not style.get("arabic_digits") else family)

    items = []
    legend = auto_layout._legend(layout, mp, layers, types.SimpleNamespace(body_pt=style["text_pt"], legend="normal",
                                                                           min_pt=6.0), rtl, family)
    if legend is not None:
        legend.setId("legend")
        legend.setTitle(style["legend_title_ar"] if rtl else style["legend_title_en"])
        from qgis.core import QgsLegendStyle

        st = legend.style(QgsLegendStyle.Style.Title)
        st.setTextFormat(_text_format(family, style["text_pt"] * 1.1, True))
        legend.setStyle(QgsLegendStyle.Style.Title, st)
        bw, bh = placement.legend_box(frame.width(), frame.height(), share=0.32)
        try:
            from .. import cartography

            cartography.fit_legend(legend, bw, bh, min_scale=0.6)
            need = cartography.legend_size(legend)
            legend.setResizeToContents(False)
            legend.attemptResize(QgsLayoutSize(min(bw, need.width() * 1.04 + 1), min(bh, need.height() * 1.07 + 1)))
        except Exception:  # noqa: BLE001
            legend.adjustBoxSize()
        items.append(("legend", legend))
        made["legend"] = legend

    if inset_wanted and not ext.isEmpty():
        inset = auto_layout._inset(layout, mp, project, ext, crs, frame)
        if inset is not None:
            inset.setFrameStrokeColor(QColor("#5B6670"))
            items.append(("inset", inset))
            made["inset"] = inset

    if mp.scale() > 0:
        km = mp.scale() >= 50000
        unit = (QgsUnitTypes.DistanceUnit.DistanceKilometers if km else QgsUnitTypes.DistanceUnit.DistanceMeters,
                ("كم" if km else "م") if rtl else ("km" if km else "m"))
        bar_w = frame.width() * 0.3
        # a round segment (1, 2, 2.5, 5 × 10ⁿ m) of which 2 to 4 fit the bar
        seg = mapsheet._nice_interval(mp.scale() * bar_w / 1000.0 / 4 * 0.9)
        sb = mapsheet._scale_bar(layout, mp, unit, frame.x(), frame.y(), bar_w, "scalebar", "Single Box", seg)
        sb.setTextFormat(_text_format(latin if not rtl else family, style["text_pt"] * 0.9))
        sb.setHeight(1.6)
        sb.setBackgroundEnabled(True)
        sb.setBackgroundColor(QColor(255, 255, 255, 225))
        sb.update()
        items.append(("scale", sb))
        made["scalebar"] = sb

    svg = mapsheet._north_arrow_svg()
    if svg:
        north = QgsLayoutItemPicture(layout)
        north.setPicturePath(svg)
        north.setLinkedMap(mp)
        north.setNorthMode(QgsLayoutItemPicture.NorthMode.GridNorth)
        layout.addLayoutItem(north)
        size = max(9.0, min(22.0, min(frame.width(), frame.height()) * 0.09))
        north.attemptResize(QgsLayoutSize(size * 0.75, size))
        north.setId("north")
        items.append(("north", north))
        made["north"] = north

    placement.place(mp, items, rtl=rtl, layers=layers)

    y = cap_y
    for item_id, text, fam, pt, bold, italic, align, h in blocks:
        made[item_id] = _text_item(layout, item_id, text, QRectF(x0, y, text_w, h), fam, pt, bold, italic, align)
        y += h + 1.2
    y = src_y
    for item_id, text, fam, pt, align, h in src_blocks:
        made[item_id] = _text_item(layout, item_id, text, QRectF(x0, y, text_w, h), fam, pt, False, False, align,
                                   "#333333")
        y += h + 0.8

    layout.setCustomProperty(LAYERS_KEY, ",".join(l.id() for l in layers))
    layout.setCustomProperty(EXTENT_KEY, ext.asWktPolygon())
    layout.setCustomProperty(auto_layout.PROP + "title", rec.get("caption") or rec.get("caption_en") or layout.name())
    layout.setCustomProperty(auto_layout.PROP + "purpose", "thesis")
    return made


def _grid(mp, style, lang, family):
    from .. import map_grid

    kind = "geographic" if style["grid"] == "degrees" else "projected"
    latin_ar = lang == "ar" and not style.get("arabic_digits")
    # intervals from the short side, so that both sides get at least two labelled lines
    interval = geo_interval = None
    try:
        if kind == "geographic":
            lo, hi, la, lb = map_grid.map_lonlat_range(mp)
            short, long_ = sorted((hi - lo, lb - la))
            steps = map_grid.GEO_INTERVALS
            k = steps.index(map_grid.geographic_interval(short, 3))
            while k > 0 and short / steps[k] < 2 and long_ / steps[k - 1] <= 9:
                k -= 1
            geo_interval = steps[k]
        else:
            x0, x1, y0, y1 = map_grid._extent_in(mp, map_grid._metric_crs(mp))
            interval = map_grid.projected_interval(min(x1 - x0, y1 - y0), 3)
    except Exception:  # noqa: BLE001 - map_grid picks its own intervals
        pass
    grids = map_grid.add_grid(mp, kind, "en" if latin_ar else lang, interval=interval, geo_interval=geo_interval,
                              size=style["label_pt"])
    for g in grids:
        g.setAnnotationTextFormat(_text_format(family, style["label_pt"], color="#222222"))
        if kind == "geographic":   # light lines: the graticule must not compete with the data
            g.setGridLineColor(QColor(90, 100, 110, 110))
            g.setGridLineWidth(0.1)
        g.setAnnotationFrameDistance(1.0)
        if latin_ar:
            # Latin digits with Arabic hemisphere letters (31°15' ش), the way most Arab theses write coordinates
            g.setAnnotationExpression(g.annotationExpression().replace("'W', 'E'", "'غ', 'ق'")
                                      .replace("'S', 'N'", "'ج', 'ش'"))
    mp.updateBoundingRect()
    return grids


# ---- figures ---------------------------------------------------------------------------------------------------
def _unique_name(project, name):
    manager = project.layoutManager()
    base, k = name, 2
    while manager.layoutByName(name) is not None:
        name, k = f"{base} ({k})", k + 1
    return name


def add_figure(project=None, chapter=1, caption="", caption_en="", style=None, layers=None, extent=None, order=None,
               source="", name=None):
    """A new figure from the current map (or ``layers``/``extent``): a print layout laid out in ``style``, placed
    in ``chapter`` at ``order`` (None = last). Returns the layout."""
    project = _project(project)
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    chapter = max(1, int(chapter or 1))
    last = max([r["order"] for _l, r in _records(project) if r["chapter"] == chapter] or [0])
    rec = {"chapter": chapter, "order": last + 1, "caption": caption or "", "caption_en": caption_en or ""}
    if source:
        rec["source"] = source
    write_record(layout, rec)
    title = (caption or caption_en or tr("Figure")).strip()
    layout.setName(_unique_name(project, name or f"{tr('Figure')} - {title[:60]}"))
    project.layoutManager().addLayout(layout)
    if order is not None:
        move_figure(layout, chapter, order, project)
    apply_style(layout, style, project, layers, extent)
    renumber(project)
    watch(project)
    return layout


def apply_style_all(style, project=None):
    """Lay every figure of the project out again in ``style``."""
    project = _project(project)
    for lay, _rec in _records(project):
        apply_style(lay, style, project)
    return renumber(project)


_WATCHED = set()


def watch(project=None):
    """Renumber by itself when a layout is removed from the project (once per project)."""
    project = _project(project)
    key = id(project)
    if key in _WATCHED:
        return
    _WATCHED.add(key)
    try:
        from qgis.PyQt.QtCore import QTimer

        project.layoutManager().layoutRemoved.connect(lambda _name: QTimer.singleShot(0, lambda: _safe_renumber(project)))
    except Exception:  # noqa: BLE001
        _WATCHED.discard(key)


def _safe_renumber(project):
    try:
        renumber(project)
    except Exception:  # noqa: BLE001 - the project may be closing
        pass


# ---- exports --------------------------------------------------------------------------------------------------
def export_figure(layout, path, dpi=300):
    from ..map_studio import outputs

    return outputs.export_layout(layout, path, dpi=dpi)


def export_all(folder, dpi=300, formats=("png",), project=None, with_list=True, feedback=None):
    """Every figure as Fig_3-2.png (and .jpg / .pdf) at ``dpi`` in ``folder``, plus the list of figures.
    Returns {"files": [...], "failed": [layout names], "list": [paths]}."""
    project = _project(project)
    os.makedirs(folder, exist_ok=True)
    figs = figures(project)
    done, failed = [], []
    total = max(1, len(figs) * len(formats))
    step = 0
    for fig in figs:
        lay = figure_layout(fig["layout"], project)
        for fmt in formats:
            step += 1
            if feedback is not None:
                if hasattr(feedback, "isCanceled") and feedback.isCanceled():
                    return {"files": done, "failed": failed, "list": []}
                if hasattr(feedback, "setProgress"):
                    feedback.setProgress(100.0 * step / total)
            path = os.path.join(folder, f"{fig['file_stem']}.{fmt.lower().lstrip('.')}")
            if lay is not None and export_figure(lay, path, dpi):
                done.append(path)
            else:
                failed.append(f"{fig['layout']} ({fmt})")
    lists = list_of_figures(os.path.join(folder, "List_of_figures.docx"), project) if with_list and figs else []
    return {"files": done, "failed": failed, "list": lists}


_LIST_TITLE = {"ar": "قائمة الأشكال", "en": "List of Figures"}
_COLS = {"ar": ("رقم الشكل", "عنوان الشكل"), "en": ("Figure", "Title")}


def _list_rows(project, lang):
    rows = []
    for fig in figures(project):
        cap = fig["caption"] if lang == "ar" else fig["caption_en"]
        if not cap:
            continue
        style = layout_style(figure_layout(fig["layout"], project))
        label = caption_text(style, lang, fig["number_ar"] if lang == "ar" else fig["number_en"], "")
        rows.append((label, cap, fig["text_ar"] if lang == "ar" else fig["text_en"]))
    return rows


def list_of_figures(path, project=None):
    """The list of figures, Arabic and English parts (each listing the figures that have a caption in that
    language). ``.docx`` uses python-docx when it is available; otherwise (and for .html/.txt paths) an .html file
    that Word opens and a .txt file are written next to ``path``. Returns the written paths."""
    project = _project(project)
    parts = [(lang, _list_rows(project, lang)) for lang in ("ar", "en")]
    parts = [(lang, rows) for lang, rows in parts if rows]
    stem = os.path.splitext(path)[0]
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if path.lower().endswith(".docx"):
        try:
            return [_write_docx(path, parts)]
        except ImportError:
            pass
    return [_write_html(stem + ".html", parts), _write_txt(stem + ".txt", parts)]


def _write_docx(path, parts):
    from ..vendor import ensure_vendor

    ensure_vendor()
    import docx  # noqa: PLC0415 - optional
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    doc = docx.Document()
    for k, (lang, rows) in enumerate(parts):
        if k:
            doc.add_page_break()
        head = doc.add_heading(_LIST_TITLE[lang], level=1)
        if lang == "ar":
            head.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        table = doc.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        cells = table.rows[0].cells
        cols = _COLS[lang] if lang == "en" else _COLS[lang]
        for c, text in zip(cells, cols):
            c.text = text
            c.paragraphs[0].runs[0].bold = True
        for label, cap, _full in rows:
            cells = table.add_row().cells
            cells[0].text = label
            cells[1].text = cap
        if lang == "ar":
            tbl_pr = table._tbl.tblPr
            bidi = tbl_pr.makeelement(qn("w:bidiVisual"), {})
            tbl_pr.append(bidi)
            for row in table.rows:
                for c in row.cells:
                    for p in c.paragraphs:
                        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                        p_pr = p._p.get_or_add_pPr()
                        p_pr.append(p_pr.makeelement(qn("w:bidi"), {}))
                        for r in p.runs:
                            r_pr = r._r.get_or_add_rPr()
                            r_pr.append(r_pr.makeelement(qn("w:rtl"), {}))
    doc.save(path)
    return path


def _write_html(path, parts):
    out = ["<!DOCTYPE html>", "<html><head><meta charset=\"utf-8\"><title>List of Figures</title>",
           "<style>body{font-family:'Times New Roman','Simplified Arabic',serif;font-size:13pt;margin:2cm}"
           "table{border-collapse:collapse;width:100%;margin-bottom:1.5em}td,th{border:1px solid #888;"
           "padding:4px 8px;vertical-align:top}th{background:#eee}section[dir=rtl] td,section[dir=rtl] th"
           "{text-align:right}h1{font-size:16pt}</style></head><body>"]
    for lang, rows in parts:
        out.append(f"<section dir=\"{'rtl' if lang == 'ar' else 'ltr'}\" lang=\"{lang}\">")
        out.append(f"<h1>{html.escape(_LIST_TITLE[lang])}</h1><table><tr><th>{html.escape(_COLS[lang][0])}</th>"
                   f"<th>{html.escape(_COLS[lang][1])}</th></tr>")
        for label, cap, _full in rows:
            out.append(f"<tr><td>{html.escape(label)}</td><td>{html.escape(cap)}</td></tr>")
        out.append("</table></section>")
    out.append(f"<p style=\"color:#888;font-size:9pt\">{dt.date.today().isoformat()} · Biruni GIS</p></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


def _write_txt(path, parts):
    lines = []
    for lang, rows in parts:
        lines += [_LIST_TITLE[lang], ""]
        for _label, _cap, full in rows:
            lines.append(full)
        lines.append("")
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write("\n".join(lines))
    return path
