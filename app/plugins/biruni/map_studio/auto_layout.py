"""Purpose-first automatic layout.

``build(project, "slide")`` gives a finished print layout for that purpose: the page and its orientation from the
data, the map frame as large as the page allows at a round scale, a clean title, a legend with readable names of
only the layers that are drawn, a north arrow, a scale bar in the units of the country, an inset location map
when it helps, a grid when the purpose wants one, and a credit line built from the data sources. Legend, scale
bar, north arrow and inset are placed where the map has no data (placement.py). Right-to-left languages get the
mirrored arrangement. Deed sketches and drawing sheets are made by the quick map sheet with the country's sheet
standard."""
import datetime as dt
import os
import re

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFillSymbol, QgsLayoutItemLegend,
                       QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutPoint, QgsLayoutSize, QgsLegendStyle,
                       QgsPrintLayout, QgsProject, QgsRectangle, QgsUnitTypes, QgsVectorLayer)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor

from .. import i18n, mapsheet
from ..i18n import tr
from . import intents, placement

RTL_LANGS = {"ar", "fa", "ur", "he", "ckb", "ps"}
PT_MM = 0.3528
PROP = "biruni/studio/"
INSET_LAYER_NAME = "Biruni inset (world)"
_WORLD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "data", "world.gpkg")


# ---- names and credits -----------------------------------------------------------------------------------------
_PREFIX = re.compile(r"^(?:\d+[_\-. ]+|(?:lyr|layer|tbl|shp|gis|fc|v|t)_)+", re.I)


def clean_name(name):
    """A legend/title name people can read: no folders, extensions, numeric or technical prefixes, underscores."""
    n = os.path.basename(str(name or "")).strip()
    n = re.sub(r"\.(shp|gpkg|geojson|json|kml|kmz|csv|xlsx?|tif|tiff|dxf|dwg|gdb)$", "", n, flags=re.I)
    n = re.sub(r"\s*[—|]\s*\w+$", "", n) if " — " in n else n
    n = _PREFIX.sub("", n)
    n = re.sub(r"[_]+", " ", n)
    n = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", n)       # camelCase → camel Case
    n = re.sub(r"\s+", " ", n).strip(" -")
    if n and n.isascii() and n == n.lower():
        n = n[:1].upper() + n[1:]
    return n or str(name or "")


def layer_title(layer):
    for t in (layer.metadata().title(), layer.serverProperties().title() if hasattr(layer, "serverProperties") else ""):
        if t:
            return t
    return clean_name(layer.name())


_CREDIT_RULES = [
    (r"openstreetmap|tile\.osm|osm\.org|\bosm\b|overpass", "© OpenStreetMap contributors"),
    (r"opentopomap", "© OpenTopoMap (CC-BY-SA)"),
    (r"arcgisonline|server\.arcgisonline|\besri\b|world_imagery", "Esri"),
    (r"google", "Google"),
    (r"sentinel|copernicus|eox", "Contains modified Copernicus Sentinel data"),
    (r"overture", "Overture Maps Foundation"),
    (r"worldcover", "ESA WorldCover"),
    (r"geoboundaries", "geoBoundaries (CC BY 4.0)"),
    (r"srtm|copernicus.?dem|glo-?30|opentopography", "Copernicus DEM / OpenTopography"),
    (r"natural.?earth|world\.gpkg", "Natural Earth"),
]


def credits(layers):
    """Data credits found in the layers (sources, names, attribution and rights metadata), in order, unique."""
    out = []

    def add(text):
        # the same source said twice (our rule and the provider's own attribution) is credited once
        if not text or text in out:
            return
        key = re.sub(r"[^a-z]", "", text.lower())
        for brand in ("openstreetmap", "esri", "google", "copernicus"):
            if brand in key and any(brand in re.sub(r"[^a-z]", "", o.lower()) for o in out):
                return
        out.append(text)

    for lyr in layers:
        hay = " ".join([lyr.source(), lyr.name(), lyr.providerType()]).lower()
        for pattern, credit in _CREDIT_RULES:
            if re.search(pattern, hay):
                add(credit)
        try:
            add(lyr.serverProperties().attribution().strip())
        except Exception:  # noqa: BLE001
            pass
        for r in lyr.metadata().rights() or []:
            add(r.strip())
    return out


# ---- layers and extent -----------------------------------------------------------------------------------------
def drawn_layers(project):
    """The visible layers of the project, top first, without the studio's own inset layer."""
    root = project.layerTreeRoot()
    out = []
    for lyr in root.layerOrder():
        node = root.findLayer(lyr.id())
        if node is not None and node.isVisible() and lyr.isValid() and lyr.name() != INSET_LAYER_NAME:
            out.append(lyr)
    return out


def data_extent(project, layers, crs):
    ext = QgsRectangle()
    for lyr in layers:
        if not placement.is_data_layer(lyr):
            continue
        try:
            e = QgsCoordinateTransform(lyr.crs(), crs, project).transformBoundingBox(lyr.extent())
        except Exception:  # noqa: BLE001
            continue
        if e.isEmpty() or not e.isFinite():
            continue
        if ext.isEmpty():
            ext = QgsRectangle(e)
        else:
            ext.combineExtentWith(e)
    if ext.isEmpty():
        for lyr in layers:          # only basemaps: use their extent
            try:
                ext = QgsCoordinateTransform(lyr.crs(), crs, project).transformBoundingBox(lyr.extent())
                break
            except Exception:  # noqa: BLE001
                continue
    if not ext.isEmpty():
        ext.scale(1.06)
    return ext


# ---- helpers ---------------------------------------------------------------------------------------------------
def _unique_name(project, name):
    manager = project.layoutManager()
    base, k = name, 2
    while manager.layoutByName(name) is not None:
        name, k = f"{base} ({k})", k + 1
    return name


def _fmt(size, bold=False, color="#1E252B", font=None):
    return mapsheet._text_format(size, bold, color, font)


def _label(layout, text, rect, size, bold=False, align=Qt.AlignmentFlag.AlignLeft, color="#1E252B", font=None,
           valign=Qt.AlignmentFlag.AlignVCenter):
    from qgis.core import QgsLayoutItemLabel

    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    lab.setTextFormat(_fmt(size, bold, color, font))
    lab.setHAlign(align)
    lab.setVAlign(valign)
    lab.setMarginX(0)
    lab.setMarginY(0)
    layout.addLayoutItem(lab)
    lab.attemptMove(QgsLayoutPoint(rect.x(), rect.y()))
    lab.attemptResize(QgsLayoutSize(rect.width(), rect.height()))
    return lab


def _card_background(item, opacity=0.88):
    item.setBackgroundEnabled(True)
    item.setBackgroundColor(QColor(255, 255, 255, int(255 * opacity)))
    item.setFrameEnabled(True)
    item.setFrameStrokeColor(QColor("#C9D0D6"))
    item.setFrameStrokeWidth(mapsheet.QgsLayoutMeasurement(0.2))


def world_layer(project):
    """The bundled world countries layer used by inset maps (registered once, kept out of the layer tree)."""
    for lyr in project.mapLayersByName(INSET_LAYER_NAME):
        if lyr.isValid():
            return lyr
    lyr = QgsVectorLayer(f"{_WORLD}|layername=countries", INSET_LAYER_NAME, "ogr")
    if not lyr.isValid():
        return None
    lyr.setRenderer(lyr.renderer().clone())
    lyr.renderer().setSymbol(QgsFillSymbol.createSimple({"color": "#E9ECEF", "outline_color": "#9AA5AE",
                                                         "outline_width": "0.1"}))
    project.addMapLayer(lyr, False)
    return lyr


def _language(lang):
    return (lang or i18n.lang() or "en").split("_")[0].lower()


def _country(project, country):
    if country:
        return country.upper()
    try:
        from .. import regions

        return (regions.current_country(project) or "").upper()
    except Exception:  # noqa: BLE001
        return ""


def _scale_units(scale, country):
    try:
        from .. import units

        feet = units.length_for_country(country) in ("ft", "us_ft")
    except Exception:  # noqa: BLE001
        feet = country == "US"
    if feet:
        return ((QgsUnitTypes.DistanceUnit.DistanceMiles, "mi") if scale >= 100000
                else (QgsUnitTypes.DistanceUnit.DistanceFeet, "ft"))
    return ((QgsUnitTypes.DistanceUnit.DistanceKilometers, "km") if scale >= 50000
            else (QgsUnitTypes.DistanceUnit.DistanceMeters, "m"))


def default_title(project, layers):
    if project.title():
        return project.title()
    named = [l for l in layers if placement.is_data_layer(l)]
    return layer_title(named[0]) if named else tr("Map")


# ---- the legend ------------------------------------------------------------------------------------------------
def _legend(layout, mp, layers, intent, rtl, font):
    legend = QgsLayoutItemLegend(layout)
    legend.setLinkedMap(mp)
    legend.setTitle("")
    layout.addLayoutItem(legend)
    legend.setAutoUpdateModel(False)
    keep = {l.id() for l in layers if placement.is_data_layer(l)}
    root = legend.model().rootGroup()
    for node in root.findLayers():
        if node.layerId() not in keep:
            node.parent().removeChildNode(node)
        else:
            node.setCustomProperty("legend/title-label", layer_title(node.layer()))
    for group in list(root.findGroups()):
        if not group.findLayers():
            group.parent().removeChildNode(group)
    legend.setLegendFilterByMapEnabled(True)
    for st in (QgsLegendStyle.Style.Title, QgsLegendStyle.Style.Group, QgsLegendStyle.Style.Subgroup,
               QgsLegendStyle.Style.SymbolLabel):
        style = legend.style(st)
        style.setTextFormat(_fmt(intent.body_pt * (1.1 if st != QgsLegendStyle.Style.SymbolLabel else 1.0),
                                 st in (QgsLegendStyle.Style.Group, QgsLegendStyle.Style.Subgroup), font=font))
        legend.setStyle(st, style)            # style() hands out a copy
    scale = max(0.8, intent.body_pt / 8.0)
    legend.setSymbolWidth(5 * scale)
    legend.setSymbolHeight(3.5 * scale)
    legend.setBoxSpace(1.8 * scale)
    if rtl:
        legend.setSymbolAlignment(Qt.AlignmentFlag.AlignRight)
        legend.setTitleAlignment(Qt.AlignmentFlag.AlignRight)
        for st in (QgsLegendStyle.Style.Group, QgsLegendStyle.Style.Subgroup, QgsLegendStyle.Style.SymbolLabel):
            style = legend.style(st)
            style.setAlignment(Qt.AlignmentFlag.AlignRight)
            legend.setStyle(st, style)
    if intent.legend == "compact":
        legend.setSplitLayer(True)
    _card_background(legend)
    legend.refresh()
    if not root.findLayers():
        layout.removeLayoutItem(legend)
        return None
    return legend


# ---- the main entry ---------------------------------------------------------------------------------------------
def build(project=None, purpose="report", layers=None, extent=None, title=None, lang=None, country=None, page=None,
          subtitle=None, parcel=None, add_to_manager=True, name=None, variant=0):
    """A finished print layout for ``purpose`` (an intents.INTENTS id). ``page`` (w, h mm) overrides the purpose's
    page (used to re-flow the same map to another size). Returns the layout."""
    project = project or QgsProject.instance()
    intent = intents.get(purpose)
    lang = _language(lang)
    rtl = lang in RTL_LANGS
    country = _country(project, country)
    layers = list(layers) if layers else drawn_layers(project)
    crs = project.crs() if project.crs().isValid() else QgsCoordinateReferenceSystem("EPSG:3857")
    ext = extent if extent is not None and not extent.isEmpty() else data_extent(project, layers, crs)
    title = title if title is not None else default_title(project, layers)

    if intent.delegate == "sheet" and page is None:
        layout = _sheet(project, intent, ext, title, lang, country, parcel, rtl)
    else:
        layout = _free(project, intent, layers, ext, crs, title, subtitle, lang, rtl, country, page, variant)
    for key, value in (("purpose", intent.id), ("title", title), ("lang", lang), ("country", country),
                       ("layers", ",".join(l.id() for l in layers)), ("extent", ext.asWktPolygon()),
                       ("variant", str(variant))):
        layout.setCustomProperty(PROP + key, value)
    manager = project.layoutManager()
    if layout in manager.layouts():          # the quick map sheet registers its own layouts
        if not add_to_manager:
            manager.removeLayout(layout)     # (deletes it) - rebuild unregistered instead
            return build(project, purpose, layers, extent, title, lang, country, page=_page_of_sheet(intent),
                         subtitle=subtitle, parcel=parcel, add_to_manager=False, name=name, variant=variant)
    elif add_to_manager:
        layout.setName(_unique_name(project, name or title or tr("Map")))
        manager.addLayout(layout)
    else:
        layout.setName(name or title or tr("Map"))
    return layout


def _page_of_sheet(intent):
    w, h = intent.page_mm()
    return (w, h) if intent.orientation != "landscape" else (max(w, h), min(w, h))


def _sheet(project, intent, ext, title, lang, country, parcel, rtl):
    """Deed sketches and drawing sheets: the quick map sheet with the country's sheet standard."""
    from .. import sheet_presets

    ids = sheet_presets.for_country(country)
    if intent.id == "deed":
        preset = next((p for p in ids if "kroki" in p or "lot_plan" in p or "bornage" in p or "perimetrico" in p
                       or "survey_plan" in p), "iso_a4_portrait")
    else:
        preset = next((p for p in ids if not any(k in p for k in ("kroki", "lot_plan", "bornage"))),
                      "iso_a3_landscape")
    opt = mapsheet.SheetOptions(title=title, extent=ext, parcel=parcel, preset=preset, rtl=rtl,
                                arabic_digits=lang == "ar", grid=True, legend=intent.has(intents.LEGEND))
    return mapsheet.build_layout(project, opt)


def _free(project, intent, layers, ext, crs, title, subtitle, lang, rtl, country, page, variant):
    aspect = ext.width() / ext.height() if not ext.isEmpty() and ext.height() > 0 else 1.4
    pw, ph = page or intent.page_for(aspect, variant)
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(pw, ph))
    font = intent.font
    m = intent.margin
    lead = Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft
    big = intent.big_title

    # bands: title (and subtitle) on top, credits at the foot
    title_h = intent.title_pt * PT_MM * 1.45 if intent.has(intents.TITLE) and title else 0.0
    sub_text = subtitle if subtitle is not None else (_subtitle(lang) if intent.has(intents.SUBTITLE) else "")
    sub_h = intent.body_pt * 1.4 * PT_MM * 1.5 if sub_text else 0.0
    credit_pt = max(intent.min_pt, intent.body_pt * 0.75)
    cred_h = credit_pt * PT_MM * 2.6 if intent.has(intents.CREDITS) else 0.0
    grid_style = intent.grid if intent.has(intents.GRID) else "none"
    grid_room = 0.0 if grid_style in ("none", "ticks") else max(6.0, intent.body_pt * PT_MM * 2.4)
    top = m + title_h + sub_h + (1.5 if title_h else 0)
    frame = QRectF(m + grid_room, top + grid_room, pw - 2 * m - 2 * grid_room,
                   ph - top - m - cred_h - 2 * grid_room)

    mp = QgsLayoutItemMap(layout)
    mp.setId("map")
    layout.addLayoutItem(mp)
    mp.attemptMove(QgsLayoutPoint(frame.x(), frame.y()))
    mp.attemptResize(QgsLayoutSize(frame.width(), frame.height()))
    mp.setCrs(crs)
    mp.setLayers([l for l in layers if l.isValid()])
    mp.setKeepLayerSet(True)
    if not ext.isEmpty():
        mp.zoomToExtent(ext)
        if mp.scale() > 0:
            mp.setScale(mapsheet.nice_scale(mp.scale()))
    mp.setFrameEnabled(True)
    mp.setFrameStrokeWidth(mapsheet.QgsLayoutMeasurement(0.25))
    mp.setBackgroundColor(QColor("#FFFFFF"))

    if title_h:
        align = Qt.AlignmentFlag.AlignHCenter if big else lead
        _label(layout, title, QRectF(m, m, pw - 2 * m, title_h), intent.title_pt, True, align, font=font)
    if sub_text:
        align = Qt.AlignmentFlag.AlignHCenter if big else lead
        _label(layout, sub_text, QRectF(m, m + title_h, pw - 2 * m, sub_h), intent.body_pt * 1.4, False, align,
               "#4A5560", font=font)

    if grid_style != "none":
        try:
            from .. import map_grid

            map_grid.add_grid(mp, grid_style, lang, size=max(intent.min_pt, intent.body_pt * 0.8))
        except Exception:  # noqa: BLE001 - a map without grid is still a map
            pass

    items = []
    legend = None
    if intent.has(intents.LEGEND) and intent.legend != "none":
        legend = _legend(layout, mp, layers, intent, rtl, font)
        if legend is not None:
            bw, bh = placement.legend_box(frame.width(), frame.height())
            try:
                from .. import cartography

                cartography.fit_legend(legend, bw, bh, min_scale=max(0.5, intent.min_pt / max(intent.body_pt, 1)))
                need = cartography.legend_size(legend)    # fit_legend leaves the whole box: shrink to the content
                legend.setResizeToContents(False)
                legend.attemptResize(QgsLayoutSize(min(bw, need.width() * 1.04 + 1), min(bh, need.height() * 1.07 + 1)))
            except Exception:  # noqa: BLE001
                legend.adjustBoxSize()
            items.append(("legend", legend))

    inset = None
    if intent.has(intents.INSET) and not ext.isEmpty():
        inset = _inset(layout, mp, project, ext, crs, frame)
        if inset is not None:
            items.append(("inset", inset))

    if intent.has(intents.SCALE) and mp.scale() > 0:
        units = _scale_units(mp.scale(), country)
        sb = mapsheet._scale_bar(layout, mp, units, frame.x(), frame.y(), frame.width() * 0.28, "scalebar")
        sb.setTextFormat(_fmt(max(intent.min_pt, intent.body_pt * 0.85), font=font))
        sb.setHeight(max(1.5, intent.body_pt * 0.25))
        sb.setBackgroundEnabled(True)
        sb.setBackgroundColor(QColor(255, 255, 255, 220))
        sb.update()
        items.append(("scale", sb))

    if intent.has(intents.NORTH):
        svg = mapsheet._north_arrow_svg()
        if svg:
            north = QgsLayoutItemPicture(layout)
            north.setPicturePath(svg)
            north.setLinkedMap(mp)
            north.setNorthMode(QgsLayoutItemPicture.NorthMode.GridNorth)
            layout.addLayoutItem(north)
            size = max(7.0, min(35.0, min(frame.width(), frame.height()) * 0.07))
            north.attemptResize(QgsLayoutSize(size * 0.75, size))
            north.setId("north")
            items.append(("north", north))

    placement.place(mp, items, rtl=rtl, layers=layers)

    if cred_h:
        parts = credits(layers)
        line = " · ".join(([tr("Sources: {s}").format(s=", ".join(parts))] if parts else [])
                          + [crs.description() or crs.authid(), dt.date.today().isoformat(),
                             tr("Made with Biruni GIS")])
        _label(layout, line, QRectF(m, ph - m - cred_h, pw - 2 * m, cred_h), credit_pt, False, lead, "#5B6670",
               font=font, valign=Qt.AlignmentFlag.AlignBottom)
    return layout


def _subtitle(lang):
    return dt.date.today().strftime("%Y-%m-%d")


def _inset(layout, mp, project, ext, crs, frame):
    """A small locator map of the world countries with the main map's extent outlined, when the map shows a
    small part of the world."""
    world = world_layer(project)
    if world is None:
        return None
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    try:
        e = QgsCoordinateTransform(crs, wgs, project).transformBoundingBox(ext)
    except Exception:  # noqa: BLE001
        return None
    span = max(e.width(), e.height())
    if span > 60:            # the map already shows a large part of the world: no locator needed
        return None
    ins = QgsLayoutItemMap(layout)
    ins.setId("inset")
    layout.addLayoutItem(ins)
    w = max(22.0, frame.width() * 0.2)
    ins.attemptResize(QgsLayoutSize(w, w * 0.75))
    ins.setCrs(wgs)
    ins.setLayers([world])
    ins.setKeepLayerSet(True)
    c = e.center()
    half = max(span * 6, 8.0)
    ins.zoomToExtent(QgsRectangle(c.x() - half * 1.333, c.y() - half, c.x() + half * 1.333, c.y() + half))
    ov = ins.overview()
    ov.setLinkedMap(mp)
    ov.setFrameSymbol(QgsFillSymbol.createSimple({"color": "255,0,0,40", "outline_color": "#D0021B",
                                                  "outline_width": "0.5"}))
    _card_background(ins, 1.0)
    return ins


def relayout(layout, page, project=None):
    """The same map re-flowed to another page size (w, h in mm): a new layout built from the stored settings."""
    project = project or layout.project() or QgsProject.instance()
    get = layout.customProperty
    ids = [i for i in (get(PROP + "layers") or "").split(",") if i]
    layers = [project.mapLayer(i) for i in ids if project.mapLayer(i) is not None] or None
    from qgis.core import QgsGeometry

    wkt = get(PROP + "extent") or ""
    ext = QgsGeometry.fromWkt(wkt).boundingBox() if wkt else None
    return build(project, get(PROP + "purpose") or "report", layers, ext, get(PROP + "title"), get(PROP + "lang"),
                 get(PROP + "country"), page=page, add_to_manager=False)
