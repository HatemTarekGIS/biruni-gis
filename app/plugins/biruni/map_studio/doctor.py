"""Map Studio's map doctor: checks a print layout the way an experienced cartographer would before it goes to
print or PDF, and fixes what it finds in one click.

    findings = check(layout, purpose="print")      # [Finding]
    fix(layout, finding)  -> message               # one finding
    fix_all(layout, findings) -> [messages]        # every fixable finding

Purposes set the reading distance: "print" (A4/A3 in the hand), "poster", "screen", "presentation",
"technical" and "survey" (technical sheets: date and coordinate system expected).
Deterministic and offline; a typical layout is checked in well under a second."""
import re
from dataclasses import dataclass, field

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateTransform, QgsGraduatedSymbolRenderer,
                       QgsLayoutFrame, QgsLayoutItem, QgsLayoutItemLabel, QgsLegendStyle,
                       QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemMapGrid, QgsLayoutItemPage,
                       QgsLayoutItemPicture, QgsLayoutItemScaleBar, QgsLayoutPoint, QgsLayoutSize, QgsProject,
                       QgsRasterLayer, QgsRectangle, QgsRuleBasedRenderer, QgsTextBufferSettings, QgsTextFormat,
                       QgsUnitTypes, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QRectF
from qgis.PyQt.QtGui import QColor, QFont

from ..i18n import tr
from . import themes

PT_TO_MM = 25.4 / 72.0
MARGIN_MM = 4.0

# reading distance (m) → smallest comfortable text (pt); 6 pt at 40 cm is the usual print minimum
PURPOSES = {
    "print": 0.4, "technical": 0.4, "survey": 0.4, "screen": 0.6, "poster": 1.5, "presentation": 3.0,
}
TECHNICAL = ("technical", "survey")

PLACEHOLDERS = re.compile(r"lorem|ipsum|^\s*(title|map title|text|label|subtitle|عنوان|عنوان الخريطة|نص)\s*$",
                          re.I)
CREDITS = {
    "OpenStreetMap": (r"openstreetmap|tile\.osm|osm\.org", "© OpenStreetMap contributors"),
    "Esri": (r"arcgisonline|arcgis\.com|esri", "Esri, Maxar, Earthstar Geographics"),
    "Google": (r"google", "Map data © Google"),
    "Copernicus": (r"copernicus|sentinel|s2cloudless|eox", "Contains modified Copernicus Sentinel data"),
    "Mapbox": (r"mapbox", "© Mapbox © OpenStreetMap"),
    "CARTO": (r"carto|cartocdn", "© CARTO © OpenStreetMap contributors"),
    "OpenTopoMap": (r"opentopomap", "© OpenTopoMap (CC-BY-SA)"),
}
RAW_NAME = re.compile(r"(^layer_?\d*$)|(\.(shp|gpkg|tif|tiff|csv|kml|kmz|geojson|json|dxf|xlsx?)$)|(_)|"
                      r"(^[a-z0-9]+$)|(^Q[A-Z_]+$)", re.I)


@dataclass
class Finding:
    id: str
    severity: str            # "error" | "warning" | "tip"
    title: str
    detail: str = ""
    item_uuid: str = ""
    fixable: bool = False
    data: dict = field(default_factory=dict)


# ---- helpers ----------------------------------------------------------------------------------------------------
def min_text_pt(purpose):
    return round(6.0 * PURPOSES.get(purpose or "print", 0.4) / 0.4, 1)


def _items(layout, cls=QgsLayoutItem):
    return [i for i in layout.items() if isinstance(i, cls) and not isinstance(i, QgsLayoutItemPage)]


def _by_uuid(layout, uuid):
    return layout.itemByUuid(uuid) if uuid else None


def _name(item):
    return item.displayName() if hasattr(item, "displayName") else item.id()


def _pt(fmt):
    """A text format's size in points (sizes in mm converted; map-unit sizes return None)."""
    u = fmt.sizeUnit()
    if u == Qgis.RenderUnit.Points:
        return fmt.size()
    if u == Qgis.RenderUnit.Millimeters:
        return fmt.size() / PT_TO_MM
    if u == Qgis.RenderUnit.Pixels:
        return fmt.size() * 72.0 / 96.0
    return None


def _set_pt(fmt, pt):
    fmt = QgsTextFormat(fmt)
    fmt.setSize(pt)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    return fmt


def _maps(layout):
    return _items(layout, QgsLayoutItemMap)


def _main_map(layout):
    maps = _maps(layout)
    return max(maps, key=lambda m: m.rect().width() * m.rect().height()) if maps else None


def _map_layers(mp):
    layers = mp.layers() if mp.keepLayerSet() or mp.layers() else []
    if not layers:
        root = QgsProject.instance().layerTreeRoot()
        layers = [n.layer() for n in root.findLayers() if n.isVisible() and n.layer() is not None]
    return [lyr for lyr in layers if lyr is not None and lyr.isValid()]


def _labels(layout):
    return _items(layout, QgsLayoutItemLabel)


def _label_text(lab):
    return lab.currentText() if hasattr(lab, "currentText") else lab.text()


def _page_rect(layout, item):
    pc = layout.pageCollection()
    page = pc.page(max(0, item.page()))
    return page.sceneBoundingRect() if page is not None else None


def _rect(item):
    return item.sceneBoundingRect()


def _symbol_colors(layer):
    r = layer.renderer()
    if isinstance(r, QgsCategorizedSymbolRenderer):
        return [c.symbol().color() for c in r.categories() if c.symbol() is not None], "categorized"
    if isinstance(r, QgsGraduatedSymbolRenderer):
        return [g.symbol().color() for g in r.ranges() if g.symbol() is not None], "graduated"
    if isinstance(r, QgsRuleBasedRenderer):
        return [x.symbol().color() for x in r.rootRule().descendants() if x.symbol() is not None], "rules"
    return [], None


def _nice(x):
    """Round value 1, 2, 2.5, 5 × 10ⁿ just under x."""
    import math

    if x <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(x))
    for m in (5, 2.5, 2, 1):
        if m * e <= x:
            return m * e
    return e


def _is_nice(v):
    import math

    if v <= 0:
        return False
    e = 10 ** math.floor(math.log10(v))
    return any(abs(v - m * e) < 1e-6 * e for m in (1, 2, 2.5, 5, 10))


# ---- checks -----------------------------------------------------------------------------------------------------
def _check_text(layout, purpose, out):
    need = min_text_pt(purpose)
    for lab in _labels(layout):
        pt = _pt(lab.textFormat())
        if pt is not None and pt + 1e-6 < need and _label_text(lab).strip():
            out.append(Finding("text-small", "warning", tr("Text too small to read"),
                               tr("«{t}» is {s:.1f} pt; at this reading distance use at least {m:.0f} pt.").format(
                                   t=_label_text(lab)[:30], s=pt, m=need), lab.uuid(), True, {"pt": need}))
    for leg in _items(layout, QgsLayoutItemLegend):
        small = [st for st in (QgsLegendStyle.Style.SymbolLabel, QgsLegendStyle.Style.Title)
                 if (_pt(leg.style(st).textFormat()) or need) + 1e-6 < need * (0.9 if st == QgsLegendStyle.Style.SymbolLabel else 1)]
        if small:
            out.append(Finding("legend-text-small", "warning", tr("Legend text too small to read"),
                               tr("Legend entries should be at least {m:.0f} pt.").format(m=need * 0.9),
                               leg.uuid(), True, {"pt": need}))
    mp = _main_map(layout)
    if mp is None:
        return
    scale = mp.scale() or 1
    for lyr in _map_layers(mp):
        if not isinstance(lyr, QgsVectorLayer) or not lyr.labelsEnabled() or \
                not isinstance(lyr.labeling(), QgsVectorLayerSimpleLabeling):
            continue
        fmt = lyr.labeling().settings().format()
        pt = _pt(fmt)
        if pt is None and fmt.sizeUnit() == Qgis.RenderUnit.MapUnits:  # size on paper = map size / scale
            pt = fmt.size() / scale * 1000.0 / PT_TO_MM
        if pt is not None and pt + 1e-6 < need * 0.9:
            out.append(Finding("map-labels-small", "warning", tr("Map labels too small at this scale"),
                               tr("Labels of «{l}» print at {s:.1f} pt.").format(l=lyr.name(), s=pt), mp.uuid(),
                               True, {"layer": lyr.id(), "pt": need}))


def _check_placement(layout, out):
    items = [i for i in _items(layout) if i.isVisible() and not isinstance(i, QgsLayoutItemMap)
             and not isinstance(i, QgsLayoutFrame)]  # frames of tables follow their table
    for it in items:
        page = _page_rect(layout, it)
        if page is None:
            continue
        r = _rect(it)
        inner = page.adjusted(MARGIN_MM, MARGIN_MM, -MARGIN_MM, -MARGIN_MM)
        if not page.contains(r):
            out.append(Finding("off-page", "error", tr("Item outside the page"),
                               tr("«{n}» runs over the edge of the page and will be cut.").format(n=_name(it)),
                               it.uuid(), True))
        elif not inner.contains(r) and not isinstance(it, QgsLayoutItemPicture):
            out.append(Finding("in-margin", "tip", tr("Item in the page margin"),
                               tr("«{n}» is within {m:.0f} mm of the page edge; printers may cut it.").format(
                                   n=_name(it), m=MARGIN_MM), it.uuid(), True))
    small = [i for i in items if isinstance(i, (QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemScaleBar,
                                                QgsLayoutItemPicture))]
    seen = set()
    for a in small:
        for b in small:
            if a is b or (b.uuid(), a.uuid()) in seen:
                continue
            seen.add((a.uuid(), b.uuid()))
            inter = _rect(a).intersected(_rect(b))
            if inter.width() > 1.0 and inter.height() > 1.0:
                out.append(Finding("overlap", "warning", tr("Items overlap"),
                                   tr("«{a}» and «{b}» cover each other.").format(a=_name(a), b=_name(b)),
                                   b.uuid(), True, {"other": a.uuid()}))


def _legend_layers(leg):
    return [n for n in leg.model().rootGroup().findLayers()]


def _legend_label(node):
    return node.customProperty("legend/title-label") or node.name()


def clean_name(name):
    """«roads_2024.shp» → «Roads 2024»; Arabic names are kept as they are."""
    base = re.sub(r"\.(shp|gpkg|tif|tiff|csv|kml|kmz|geojson|json|dxf|xlsx?)$", "", name, flags=re.I)
    base = re.sub(r"^layer_?(\d*)$", lambda m: tr("Layer {n}").format(n=m.group(1) or ""), base, flags=re.I)
    base = re.sub(r"[_\-]+", " ", base).strip()
    if base and base.isascii() and (base.islower() or base.isupper()):
        base = base[:1].upper() + base[1:].lower()
    return base or name


def _check_legend(layout, out):
    for leg in _items(layout, QgsLayoutItemLegend):
        nodes = _legend_layers(leg)
        raw = [n for n in nodes if RAW_NAME.search(_legend_label(n) or "") and clean_name(_legend_label(n)) !=
               _legend_label(n)]
        if raw:
            out.append(Finding("legend-raw-names", "warning", tr("Legend shows technical layer names"),
                               ", ".join(_legend_label(n) for n in raw[:5]), leg.uuid(), True))
        ids = [n.layerId() for n in nodes]
        dup = len(ids) != len(set(ids))
        empty = [g for g in leg.model().rootGroup().findGroups(True) if not g.findLayers()]
        if dup or empty:
            out.append(Finding("legend-duplicates", "warning", tr("Legend has repeated or empty entries"), "",
                               leg.uuid(), True))
        mp = leg.linkedMap()
        if mp is not None and not leg.legendFilterByMapEnabled():
            hidden = []
            ext = QgsRectangle(mp.extent())
            for n in nodes:
                lyr = n.layer()
                if lyr is None:
                    continue
                if not n.isVisible():
                    hidden.append(_legend_label(n))
                    continue
                try:
                    tr_ = QgsCoordinateTransform(lyr.crs(), mp.crs(), QgsProject.instance().transformContext())
                    if not tr_.transformBoundingBox(lyr.extent()).intersects(ext):
                        hidden.append(_legend_label(n))
                except Exception:  # noqa: BLE001 - odd CRS: don't guess
                    pass
            if hidden:
                out.append(Finding("legend-invisible", "warning", tr("Legend lists layers that are not on the map"),
                                   ", ".join(hidden[:5]), leg.uuid(), True))
        page = _page_rect(layout, leg)
        try:
            from ..cartography import legend_size

            need = legend_size(leg)
            frame = leg.rect()
            over = need.height() > frame.height() * 1.02 or need.width() > frame.width() * 1.02
        except Exception:  # noqa: BLE001
            over = False
        if over or (page is not None and not page.contains(_rect(leg))):
            out.append(Finding("legend-overflow", "warning", tr("Legend does not fit its frame"), "", leg.uuid(),
                               True))


def _source_text(lyr):
    return f"{lyr.source()} {lyr.name()} {lyr.providerType()}".lower()


def _check_credits(layout, out):
    mp = _main_map(layout)
    if mp is None:
        return
    text = " ".join(_label_text(lab) for lab in _labels(layout)).lower()
    missing = []
    for provider, (pattern, credit) in CREDITS.items():
        used = any(re.search(pattern, _source_text(lyr)) for lyr in _map_layers(mp))
        if used and provider.lower() not in text and credit.lower() not in text and "©" not in text:
            missing.append(credit)
    if missing:
        out.append(Finding("credits", "error", tr("Data credits missing"),
                           tr("The basemap or data needs: {c}").format(c="; ".join(missing)), mp.uuid(), True,
                           {"credits": missing}))


_SCALE_TEXT = re.compile(r"1\s*:\s*([\d,.\s٠-٩٬]+)")


def _check_scale(layout, out):
    mp = _main_map(layout)
    bars = _items(layout, QgsLayoutItemScaleBar)
    for sb in bars:
        if sb.linkedMap() is None and mp is not None:
            out.append(Finding("scalebar-unlinked", "error", tr("Scale bar not linked to the map"),
                               tr("It shows a wrong distance."), sb.uuid(), True))
            continue
        lm = sb.linkedMap() or mp
        if lm is None or lm.scale() <= 0:
            continue
        seg_mm = sb.unitsPerSegment() * QgsUnitTypes.fromUnitToUnitFactor(sb.units(), Qgis.DistanceUnit.Meters) \
            / lm.scale() * 1000.0
        metres = sb.unitsPerSegment() * QgsUnitTypes.fromUnitToUnitFactor(sb.units(), Qgis.DistanceUnit.Meters)
        if sb.units() == Qgis.DistanceUnit.Meters and metres >= 1000:
            out.append(Finding("scalebar-units", "tip", tr("Scale bar in metres for long distances"),
                               tr("Kilometres read better."), sb.uuid(), True))
        elif sb.units() == Qgis.DistanceUnit.Kilometers and metres < 1000:
            out.append(Finding("scalebar-units", "tip", tr("Scale bar in kilometres for short distances"),
                               tr("Metres read better."), sb.uuid(), True))
        elif not _is_nice(sb.unitsPerSegment()) or seg_mm > 60 or seg_mm < 4:
            out.append(Finding("scalebar-segments", "tip", tr("Scale bar with awkward numbers"),
                               tr("Segments of {v:g} {u} are hard to read.").format(
                                   v=sb.unitsPerSegment(), u=sb.unitLabel() or ""), sb.uuid(), True))
    if mp is not None and mp.scale() > 0:
        for lab in _labels(layout):
            m = _SCALE_TEXT.search(_label_text(lab))
            if not m or "[%" in lab.text():
                continue
            digits = re.sub(r"[^\d٠-٩]", "", m.group(1)).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
            if digits and abs(float(digits) - mp.scale()) / mp.scale() > 0.05:
                out.append(Finding("scale-text", "error", tr("Written scale does not match the map"),
                                   tr("The text says 1:{a}, the map is at 1:{b:,.0f}.").format(a=digits, b=mp.scale()),
                                   lab.uuid(), True))


def _north_arrows(layout):
    out = []
    for pic in _items(layout, QgsLayoutItemPicture):
        hint = f"{pic.picturePath()} {pic.id()} {pic.displayName()}".lower()
        if re.search(r"north|arrow|شمال|nord|norte", hint):
            out.append(pic)
    return out


def _check_north(layout, purpose, out):
    mp = _main_map(layout)
    if mp is None:
        return
    arrows = _north_arrows(layout)
    rotated = abs(mp.mapRotation()) > 0.01
    if not arrows and (rotated or purpose != "screen"):
        out.append(Finding("north-missing", "error" if rotated else "warning", tr("North arrow missing"),
                           tr("The map is rotated: readers need to know where north is.") if rotated else "",
                           mp.uuid(), True))
    for a in arrows:
        if rotated and a.linkedMap() is None:
            out.append(Finding("north-not-linked", "error", tr("North arrow does not turn with the map"), "",
                               a.uuid(), True))
    for m in _maps(layout):
        for i in range(m.grids().size()):
            g = m.grids().grid(i)
            if g.annotationEnabled() and g.frameStyle() == QgsLayoutItemMapGrid.FrameStyle.NoFrame and any(
                    g.annotationPosition(side) == QgsLayoutItemMapGrid.AnnotationPosition.InsideMapFrame
                    for side in (QgsLayoutItemMapGrid.BorderSide.Left, QgsLayoutItemMapGrid.BorderSide.Top)):
                out.append(Finding("grid-labels", "tip", tr("Grid numbers sit on the map"),
                                   tr("Put them outside the frame so they don't hide data."), m.uuid(), True,
                                   {"grid": i}))


def _check_colours(layout, out):
    mp = _main_map(layout)
    if mp is None:
        return
    bg = mp.backgroundColor() if mp.hasBackground() else QColor("#FFFFFF")
    has_raster = any(isinstance(lyr, QgsRasterLayer) for lyr in _map_layers(mp))
    for lyr in _map_layers(mp):
        if not isinstance(lyr, QgsVectorLayer):
            continue
        cols, kind = _symbol_colors(lyr)
        if kind == "categorized" and len(cols) > 12:
            out.append(Finding("too-many-classes", "tip", tr("Too many colours to tell apart"),
                               tr("«{l}» has {n} classes; group them into 12 or fewer.").format(l=lyr.name(),
                                                                                             n=len(cols)),
                               mp.uuid(), False, {"layer": lyr.id()}))
        elif cols and kind in ("categorized", "rules"):
            pairs = themes.confusable_pairs(cols)
            if pairs:
                kinds = sorted({p[2] for p in pairs})
                out.append(Finding("colour-blind", "warning", tr("Colours confusing for colour-blind readers"),
                                   tr("In «{l}», {n} pair(s) look alike ({k}).").format(
                                       l=lyr.name(), n=len(pairs), k=", ".join(tr(k) for k in kinds)),
                                   mp.uuid(), True, {"layer": lyr.id()}))
        if lyr.labelsEnabled() and isinstance(lyr.labeling(), QgsVectorLayerSimpleLabeling):
            fmt = lyr.labeling().settings().format()
            if not fmt.buffer().enabled() and (has_raster or themes.contrast_ratio(fmt.color(), bg) < 4.5):
                out.append(Finding("label-contrast", "warning", tr("Labels hard to read on the background"),
                                   tr("«{l}»: add a halo around the letters.").format(l=lyr.name()), mp.uuid(),
                                   True, {"layer": lyr.id()}))


def _check_rasters(layout, out):
    mp = _main_map(layout)
    if mp is None:
        return
    dpi = layout.renderContext().dpi() or 300
    width_in = mp.rect().width() / 25.4
    if width_in <= 0:
        return
    map_units_per_px = mp.extent().width() / (width_in * dpi)
    for lyr in _map_layers(mp):
        if not isinstance(lyr, QgsRasterLayer) or lyr.providerType() != "gdal" or lyr.width() <= 0:
            continue
        try:
            xf = QgsCoordinateTransform(lyr.crs(), mp.crs(), QgsProject.instance().transformContext())
            ext = xf.transformBoundingBox(lyr.extent())
        except Exception:  # noqa: BLE001
            continue
        native = ext.width() / lyr.width()
        if native > map_units_per_px * 2.0:
            out.append(Finding("raster-blurry", "warning", tr("Image will print blurry"),
                               tr("«{l}» has {r:.0f}× too few pixels for {d:.0f} dpi at this size.").format(
                                   l=lyr.name(), r=native / map_units_per_px, d=dpi), mp.uuid(), False,
                               {"layer": lyr.id()}))
    for lyr in _map_layers(mp):
        if isinstance(lyr, QgsRasterLayer) and lyr.providerType() == "wms" and "type=xyz" in lyr.source() and \
                dpi > 200:
            out.append(Finding("tiles-dpi", "tip", tr("Web tiles are made for screens"),
                               tr("For {d:.0f} dpi print, use tiles at a higher zoom or a lower export dpi.").format(
                                   d=dpi), mp.uuid(), False, {"layer": lyr.id()}))


def _check_scripts(layout, out):
    try:
        from .. import script_labels as sl
    except Exception:  # noqa: BLE001
        return
    mp = _main_map(layout)
    for lyr in (_map_layers(mp) if mp is not None else []):
        if isinstance(lyr, QgsVectorLayer):
            probs = sl.problems(lyr)
            if probs:
                out.append(Finding("script-labels", "error", tr("Labels will draw wrongly"),
                                   tr("«{l}»: {p}").format(l=lyr.name(), p=", ".join(sorted({p for _i, p in probs}))),
                                   mp.uuid(), True, {"layer": lyr.id()}))
    for lab in _labels(layout):
        text = _label_text(lab)
        scripts = sl.detect_scripts([text])
        if not scripts:
            continue
        main = max(scripts, key=scripts.get)
        if not sl.font_covers(lab.textFormat().font(), main):
            out.append(Finding("label-font", "error", tr("Font has no letters for this text"),
                               tr("«{t}» needs a font with {s} letters.").format(t=text[:30], s=tr(main)),
                               lab.uuid(), True, {"script": main}))
        if re.search(r"[0-9]", text) and re.search(r"[٠-٩۰-۹]", text):
            out.append(Finding("mixed-digits", "tip", tr("Mixed digit styles"),
                               tr("«{t}» mixes 0-9 with ٠-٩.").format(t=text[:30]), lab.uuid(), True))


def _check_titles(layout, purpose, out):
    labels = _labels(layout)
    texts = [_label_text(lab) for lab in labels]
    if not any((_pt(lab.textFormat()) or 0) >= 13 and _label_text(lab).strip() for lab in labels):
        out.append(Finding("title-missing", "warning", tr("No title"), tr("A map without a title is hard to file."),
                           "", True))
    for lab in labels:
        if PLACEHOLDERS.search(_label_text(lab).strip()):
            out.append(Finding("placeholder", "error", tr("Placeholder text left on the sheet"),
                               f"«{_label_text(lab)[:30]}»", lab.uuid(), True))
    if purpose in TECHNICAL:
        joined = " ".join(texts) + " " + " ".join(lab.text() for lab in labels)
        if not re.search(r"\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4}|now\(\)|format_date|\$now|@now|تاريخ|date", joined,
                         re.I):
            out.append(Finding("date-missing", "warning", tr("Date missing on a technical sheet"), "", "", True))
        if purpose == "survey" and not re.search(r"EPSG|UTM|WGS|KSA-GRF|Ain el Abd|ITRF|projection|مسقط|نظام الإحداثيات",
                                                 joined, re.I):
            out.append(Finding("crs-missing", "warning", tr("Coordinate system not written on the sheet"), "", "",
                               True))


def _check_export(layout, out):
    if layout.customProperty("rasterize", False) in (True, "true", 1, "1"):
        out.append(Finding("pdf-raster", "warning", tr("PDF will be one big picture"),
                           tr("«Print as raster» makes heavy files with text you cannot search."), "", True))


CHECKS = (_check_text, _check_placement, _check_legend, _check_credits, _check_scale, _check_north,
          _check_colours, _check_rasters, _check_scripts, _check_titles, _check_export)
_NEEDS_PURPOSE = {_check_text, _check_north, _check_titles}
_ORDER = {"error": 0, "warning": 1, "tip": 2}


def check(layout, purpose=None):
    """Everything worth fixing before printing, most serious first."""
    purpose = purpose or "print"
    out = []
    for fn in CHECKS:
        try:
            fn(layout, purpose, out) if fn in _NEEDS_PURPOSE else fn(layout, out)
        except Exception as e:  # noqa: BLE001 - one failing check must not hide the others
            out.append(Finding("check-failed", "tip", tr("A check could not run"), f"{fn.__name__}: {e}"))
    for f in out:
        f.data.setdefault("purpose", purpose)  # fixes need the same readability floor
    out.sort(key=lambda f: _ORDER.get(f.severity, 3))
    return out


# ---- fixes ------------------------------------------------------------------------------------------------------
def _add_label(layout, text, x, y, w, h, size=7.0, color="#3C4650", item_id=""):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    fmt = QgsTextFormat()
    fmt.setFont(QFont(themes.font_pair()[0 if re.search(r"[؀-ۿ]", text) else 1]))
    fmt.setSize(size)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(color))
    lab.setTextFormat(fmt)
    layout.addLayoutItem(lab)
    lab.attemptMove(QgsLayoutPoint(x, y))
    lab.attemptResize(QgsLayoutSize(w, h))
    if item_id:
        lab.setId(item_id)
    return lab


def _free_spot(layout, w, h, page_rect, avoid):
    """Top-left corner of a free w×h place on the page (scanning from the bottom), or None."""
    step = 2.0
    y = page_rect.bottom() - MARGIN_MM - h
    while y >= page_rect.top() + MARGIN_MM:
        x = page_rect.left() + MARGIN_MM
        while x + w <= page_rect.right() - MARGIN_MM:
            r = QRectF(x, y, w, h)
            if not any(r.intersects(a) for a in avoid):
                return x - page_rect.left(), y - page_rect.top()
            x += step * 4
        y -= step
    return None


def _fix_text(layout, f):
    it = _by_uuid(layout, f.item_uuid)
    pt = f.data.get("pt", 6)
    if f.id == "text-small" and isinstance(it, QgsLayoutItemLabel):
        it.setTextFormat(_set_pt(it.textFormat(), pt))
        it.adjustSizeToText()
        return tr("Text enlarged to {p:.0f} pt.").format(p=pt)
    if f.id == "legend-text-small" and isinstance(it, QgsLayoutItemLegend):
        for st, p in ((QgsLegendStyle.Style.SymbolLabel, pt * 0.9), (QgsLegendStyle.Style.Title, pt * 1.3),
                      (QgsLegendStyle.Style.Group, pt), (QgsLegendStyle.Style.Subgroup, pt)):
            style = it.style(st)
            if (_pt(style.textFormat()) or 0) < p:
                style.setTextFormat(_set_pt(style.textFormat(), p))
                it.setStyle(st, style)
        it.adjustBoxSize()
        return tr("Legend text enlarged.")
    if f.id == "map-labels-small":
        lyr = QgsProject.instance().mapLayer(f.data["layer"])
        s = lyr.labeling().settings()
        s.setFormat(_set_pt(s.format(), max(pt * 0.9, 6)))
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
        lyr.triggerRepaint()
        return tr("Map labels of «{l}» set to {p:.0f} pt.").format(l=lyr.name(), p=max(pt * 0.9, 6))
    return ""


def _fix_placement(layout, f):
    it = _by_uuid(layout, f.item_uuid)
    if it is None:
        return ""
    page = _page_rect(layout, it)
    inner = page.adjusted(MARGIN_MM, MARGIN_MM, -MARGIN_MM, -MARGIN_MM)
    r = _rect(it)
    if f.id in ("off-page", "in-margin"):
        w = min(r.width(), inner.width())
        h = min(r.height(), inner.height())
        x = min(max(r.left(), inner.left()), inner.right() - w) - page.left()
        y = min(max(r.top(), inner.top()), inner.bottom() - h) - page.top()
        it.attemptResize(QgsLayoutSize(w, h))
        it.attemptMove(QgsLayoutPoint(x, y), page=it.page())
        return tr("«{n}» moved inside the page.").format(n=_name(it))
    if f.id == "overlap":
        other = _by_uuid(layout, f.data.get("other"))
        avoid = [_rect(o) for o in _items(layout) if o is not it and o.isVisible()
                 and not isinstance(o, QgsLayoutItemMap)]
        spot = _free_spot(layout, r.width(), r.height(), page, avoid)
        if spot is None and other is not None:  # no free place: put it just under the other item
            o = _rect(other)
            spot = (r.left() - page.left(), min(o.bottom() + 1.0, inner.bottom() - r.height()) - page.top())
        if spot:
            it.attemptMove(QgsLayoutPoint(*spot), page=it.page())
            return tr("«{n}» moved to a free place.").format(n=_name(it))
    return ""


def _fix_legend(layout, f):
    leg = _by_uuid(layout, f.item_uuid)
    if not isinstance(leg, QgsLayoutItemLegend):
        return ""
    if leg.autoUpdateModel() and f.id in ("legend-raw-names", "legend-duplicates"):
        leg.setAutoUpdateModel(False)  # edit the legend's own copy, never the project's layer tree
    root = leg.model().rootGroup()
    if f.id == "legend-raw-names":
        n = 0
        for node in root.findLayers():
            old = _legend_label(node)
            new = clean_name(old)
            if new != old:
                node.setCustomProperty("legend/title-label", new)
                node.removeCustomProperty("cached_name")  # the legend model caches the old title
                leg.model().refreshLayerLegend(node)
                n += 1
        leg.updateLegend()
        leg.refresh()
        return tr("{n} legend name(s) cleaned.").format(n=n)
    if f.id == "legend-duplicates":
        seen = set()
        for node in list(root.findLayers()):
            if node.layerId() in seen:
                node.parent().removeChildNode(node)
            seen.add(node.layerId())
        for g in sorted(root.findGroups(True), key=lambda g: -len(g.children())):
            if not g.findLayers() and g.parent() is not None:
                g.parent().removeChildNode(g)
        leg.refresh()
        return tr("Repeated and empty legend entries removed.")
    if f.id == "legend-invisible":
        if leg.autoUpdateModel():
            leg.setAutoUpdateModel(False)
        for node in list(leg.model().rootGroup().findLayers()):
            if not node.isVisible():
                node.parent().removeChildNode(node)
        leg.setLegendFilterByMapEnabled(True)
        leg.refresh()
        return tr("Legend shows only what is on the map.")
    if f.id == "legend-overflow":
        from ..cartography import fit_legend, legend_size

        page = _page_rect(layout, leg)
        r = _rect(leg)
        room_w = page.right() - MARGIN_MM - r.left()
        room_h = page.bottom() - MARGIN_MM - r.top()
        others = [_rect(o) for o in _items(layout) if o is not leg and o.isVisible()
                  and not isinstance(o, QgsLayoutItemMap)]
        need = legend_size(leg)
        grown = QRectF(r.left(), r.top(), need.width() + 1, need.height() + 1)
        if need.width() + 1 <= room_w and need.height() + 1 <= room_h and \
                not any(grown.intersects(o) for o in others):
            leg.setResizeToContents(True)  # first choice: give the legend the room it needs
            leg.adjustBoxSize()
            return tr("Legend frame enlarged to fit its entries.")
        # no room: wrap, use columns and shrink - but never below readable text
        current = _pt(leg.style(QgsLegendStyle.Style.SymbolLabel).textFormat()) or 8.0
        floor = min(1.0, max(0.5, min_text_pt(f.data.get("purpose")) * 0.9 / current))
        cols, scale, fits = fit_legend(leg, max(min(r.width(), room_w), 20), max(min(r.height(), room_h), 20),
                                       min_scale=floor)
        return tr("Legend fitted: {c} column(s), text at {s:.0%}.").format(c=cols, s=scale) if fits else \
            tr("Legend kept readable but it needs more room: enlarge its frame or show fewer layers.")
    return ""


def _fix_credits(layout, f):
    mp = _by_uuid(layout, f.item_uuid) or _main_map(layout)
    r = _rect(mp)
    page = _page_rect(layout, mp)
    text = " | ".join(f.data.get("credits", []))
    lab = _add_label(layout, text, r.left() - page.left() + 1.5, r.bottom() - page.top() - 5.0,
                     min(r.width() - 3, 140), 4.5, 6.0, "#4A5560", "biruni-credits")
    lab.adjustSizeToText()
    lab.setBackgroundEnabled(True)  # readable over a busy basemap
    lab.setBackgroundColor(QColor(255, 255, 255, 210))
    return tr("Credits added under the map.")


def _fix_scale(layout, f):
    it = _by_uuid(layout, f.item_uuid)
    mp = _main_map(layout)
    if f.id == "scalebar-unlinked" and isinstance(it, QgsLayoutItemScaleBar):
        it.setLinkedMap(mp)
        it.update()
        return tr("Scale bar linked to the map.")
    if f.id in ("scalebar-units", "scalebar-segments") and isinstance(it, QgsLayoutItemScaleBar):
        lm = it.linkedMap() or mp
        width_mm = 40.0
        metres = lm.scale() * width_mm / 1000.0 / max(it.numberOfSegments(), 1)
        unit, label = (Qgis.DistanceUnit.Kilometers, tr("km")) if metres >= 1000 else (Qgis.DistanceUnit.Meters,
                                                                                      tr("m"))
        factor = QgsUnitTypes.fromUnitToUnitFactor(Qgis.DistanceUnit.Meters, unit)
        it.setUnits(unit)
        it.setUnitLabel(label)
        it.setSegmentSizeMode(Qgis.ScaleBarSegmentSizeMode.Fixed)
        it.setUnitsPerSegment(_nice(metres * factor))
        it.update()
        return tr("Scale bar set to {v:g} {u} a segment.").format(v=it.unitsPerSegment(), u=label)
    if f.id == "scale-text" and isinstance(it, QgsLayoutItemLabel):
        new = _SCALE_TEXT.sub(lambda m: "1:" + f"{round(mp.scale()):,}", it.text())
        it.setText(new)
        return tr("Written scale updated to 1:{s:,.0f}.").format(s=mp.scale())
    return ""


def _fix_north(layout, f):
    mp = _main_map(layout)
    if f.id == "north-not-linked":
        it = _by_uuid(layout, f.item_uuid)
        it.setLinkedMap(mp)
        it.setNorthMode(QgsLayoutItemPicture.NorthMode.GridNorth)
        return tr("North arrow now turns with the map.")
    if f.id == "north-missing":
        try:
            from ..mapsheet import _north_arrow_svg

            svg = _north_arrow_svg()
        except Exception:  # noqa: BLE001
            svg = ""
        pic = QgsLayoutItemPicture(layout)
        if svg:
            pic.setPicturePath(svg)
        pic.setLinkedMap(mp)
        pic.setId("biruni-north-arrow")
        layout.addLayoutItem(pic)
        r = _rect(mp)
        page = _page_rect(layout, mp)
        pic.attemptMove(QgsLayoutPoint(r.right() - page.left() - 14, r.top() - page.top() + 3))
        pic.attemptResize(QgsLayoutSize(10, 13))
        return tr("North arrow added to the map corner.")
    if f.id == "grid-labels":
        g = mp.grids().grid(f.data.get("grid", 0))
        for side in (QgsLayoutItemMapGrid.BorderSide.Left, QgsLayoutItemMapGrid.BorderSide.Right,
                     QgsLayoutItemMapGrid.BorderSide.Top, QgsLayoutItemMapGrid.BorderSide.Bottom):
            g.setAnnotationPosition(QgsLayoutItemMapGrid.AnnotationPosition.OutsideMapFrame, side)
        mp.updateBoundingRect()
        mp.update()
        return tr("Grid numbers moved outside the map frame.")
    return ""


def _fix_colours(layout, f):
    lyr = QgsProject.instance().mapLayer(f.data.get("layer", ""))
    if lyr is None:
        return ""
    if f.id == "colour-blind":
        r = lyr.renderer().clone()
        if isinstance(r, QgsCategorizedSymbolRenderer):
            cols = themes.safe_qualitative(len(r.categories()))
            for i, cat in enumerate(r.categories()):
                sym = cat.symbol().clone()
                sym.setColor(QColor(cols[i]))
                r.updateCategorySymbol(i, sym)
        elif isinstance(r, QgsRuleBasedRenderer):
            rules = [x for x in r.rootRule().descendants() if x.symbol() is not None]
            for col, rule in zip(themes.safe_qualitative(len(rules)), rules):
                sym = rule.symbol().clone()
                sym.setColor(QColor(col))
                rule.setSymbol(sym)
        lyr.setRenderer(r)
        lyr.triggerRepaint()
        return tr("«{l}» recoloured with a colour-blind safe palette.").format(l=lyr.name())
    if f.id == "label-contrast":
        s = lyr.labeling().settings()
        fmt = QgsTextFormat(s.format())
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        dark_text = themes.relative_luminance(fmt.color()) < 0.4
        buf.setColor(QColor("#FFFFFF" if dark_text else "#1E1E1E"))
        buf.setSize(0.8)
        buf.setSizeUnit(Qgis.RenderUnit.Millimeters)
        fmt.setBuffer(buf)
        s.setFormat(fmt)
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
        lyr.triggerRepaint()
        return tr("Halo added to the labels of «{l}».").format(l=lyr.name())
    return ""


def _fix_scripts(layout, f):
    from .. import script_labels as sl

    if f.id == "script-labels":
        lyr = QgsProject.instance().mapLayer(f.data.get("layer", ""))
        return "; ".join(sl.fix_layer(lyr)) if lyr is not None else ""
    lab = _by_uuid(layout, f.item_uuid)
    if f.id == "label-font" and isinstance(lab, QgsLayoutItemLabel):
        fam = sl.pick_font(f.data.get("script"))
        if fam:
            fmt = QgsTextFormat(lab.textFormat())
            font = QFont(fmt.font())
            font.setFamily(fam)
            fmt.setFont(font)
            fmt.setNamedStyle("")
            lab.setTextFormat(fmt)
            return tr("Font changed to {f}.").format(f=fam)
    if f.id == "mixed-digits" and isinstance(lab, QgsLayoutItemLabel):
        text = lab.text()
        arabic = len(re.findall(r"[٠-٩]", text)) >= len(re.findall(r"[0-9]", text))
        lab.setText(sl.to_native_digits(text, "arabic") if arabic else sl.to_latin_digits(text))
        return tr("Digits made consistent.")
    return ""


def _top_free_y(layout, page):
    tops = [_rect(i).top() for i in _items(layout) if i.page() == 0]
    return (min(tops) if tops else page.top() + MARGIN_MM) - page.top()


def _fix_titles(layout, f):
    pc = layout.pageCollection()
    page = pc.page(0).sceneBoundingRect()
    title = QgsProject.instance().title() or getattr(layout, "name", lambda: "")() or tr("Map")
    if f.id == "title-missing":
        y = max(MARGIN_MM, _top_free_y(layout, page) - 12)
        _add_label(layout, title, MARGIN_MM + 2, y, page.width() - 2 * MARGIN_MM - 4, 10, 16, "#1E252B",
                   "biruni-title")
        return tr("Title added: «{t}».").format(t=title)
    if f.id == "placeholder":
        lab = _by_uuid(layout, f.item_uuid)
        lab.setText(title)
        return tr("Placeholder replaced with «{t}».").format(t=title)
    avoid = [_rect(i) for i in _items(layout) if i.isVisible()]
    spot = _free_spot(layout, 60, 5, page, avoid) or (MARGIN_MM, page.height() - MARGIN_MM - 5)
    if f.id == "date-missing":
        _add_label(layout, tr("Date:") + " [% format_date(now(), 'yyyy-MM-dd') %]", spot[0], spot[1], 60, 5, 7,
                   "#3C4650", "biruni-date")
        return tr("Date added.")
    if f.id == "crs-missing":
        mp = _main_map(layout)
        crs = mp.crs() if mp is not None else QgsProject.instance().crs()
        _add_label(layout, f"{crs.description()} ({crs.authid()})", spot[0], spot[1], 80, 5, 7, "#3C4650",
                   "biruni-crs")
        return tr("Coordinate system written on the sheet.")
    return ""


def _fix_export(layout, f):
    layout.setCustomProperty("rasterize", False)
    layout.setCustomProperty("forceVector", False)
    return tr("PDF set to keep text and lines as vectors.")


_FIXERS = {
    "text-small": _fix_text, "legend-text-small": _fix_text, "map-labels-small": _fix_text,
    "off-page": _fix_placement, "in-margin": _fix_placement, "overlap": _fix_placement,
    "legend-raw-names": _fix_legend, "legend-duplicates": _fix_legend, "legend-invisible": _fix_legend,
    "legend-overflow": _fix_legend, "credits": _fix_credits,
    "scalebar-unlinked": _fix_scale, "scalebar-units": _fix_scale, "scalebar-segments": _fix_scale,
    "scale-text": _fix_scale, "north-missing": _fix_north, "north-not-linked": _fix_north,
    "grid-labels": _fix_north, "colour-blind": _fix_colours, "label-contrast": _fix_colours,
    "script-labels": _fix_scripts, "label-font": _fix_scripts, "mixed-digits": _fix_scripts,
    "title-missing": _fix_titles, "placeholder": _fix_titles, "date-missing": _fix_titles,
    "crs-missing": _fix_titles, "pdf-raster": _fix_export,
}


def fix(layout, finding):
    """Apply the fix of one finding; returns what was done (empty when nothing could be done)."""
    if not finding.fixable or finding.id not in _FIXERS:
        return ""
    try:
        return _FIXERS[finding.id](layout, finding) or ""
    except Exception as e:  # noqa: BLE001 - shown to the user
        return tr("Could not fix: {e}").format(e=e)


def fix_all(layout, findings, purpose=None, rounds=3):
    """Fix every fixable finding; placement fixes go last because the other fixes add and resize items. Fixing
    can reveal new things (a scale bar once linked shows its units), so the layout is checked again and fixed
    for up to ``rounds`` passes. Returns what was done."""
    done = []
    failed = tr("Could not fix: {e}").split("{")[0]
    for _ in range(rounds):
        order = sorted((f for f in findings if f.fixable),
                       key=lambda f: 1 if f.id in ("overlap", "off-page", "in-margin") else 0)
        messages = [m for m in (fix(layout, f) for f in order) if m and not m.startswith(failed)]
        done += messages
        layout.refresh()
        if not messages:
            break
        findings = check(layout, purpose)
    return done


# kept for the dialog: icons and colours per severity
SEVERITY = {"error": ("alert-octagon", "#D64545"), "warning": ("alert-triangle", "#D99A1E"),
            "tip": ("bulb", "#3B82C4")}
