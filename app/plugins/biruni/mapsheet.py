"""Quick map sheet: a finished print layout in one step.

Title block, map with a coordinate frame, north arrow, scale bar (single,
metric + imperial, or a fixed segment length such as 100 m), legend,
details (coordinate system, date, author, scale), an optional logo, and - when
a polygon is selected - its corner coordinates table with numbered corners on
the map, side lengths and area. Right-to-left when the language is Arabic, with
the choice of Arabic-Indic digits in the texts and the table.
"""
import datetime as dt
import math
import os
import re

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsFeature, QgsGeometry, QgsLayoutExporter, QgsLayoutFrame, QgsLayoutItemLabel,
                       QgsLayoutItemLegend, QgsLayoutItemManualTable, QgsLayoutItemMap, QgsLayoutItemMapGrid,
                       QgsLayoutItemPage, QgsLayoutItemPicture, QgsLayoutItemScaleBar, QgsLayoutItemShape,
                       QgsLayoutMeasurement, QgsLayoutPoint, QgsLayoutSize, QgsLayoutTableColumn, QgsPalLayerSettings,
                       QgsPointXY, QgsPrintLayout, QgsProject, QgsRectangle, QgsTableCell, QgsTextFormat,
                       QgsUnitTypes, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsWkbTypes, Qgis)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont

from . import i18n
from .i18n import tr

PAPERS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}
NICE_SCALES = [100, 200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 20000, 25000, 50000, 100000, 200000,
               250000, 500000, 1000000, 2000000, 2500000, 5000000, 10000000, 20000000, 50000000, 100000000]
ARABIC_INDIC = str.maketrans("0123456789.", "٠١٢٣٤٥٦٧٨٩٫")
MARGIN = 10.0
GAP = 4.0


def nice_scale(scale):
    for s in NICE_SCALES:
        if s >= scale:
            return s
    return NICE_SCALES[-1]


def digits(text, arabic):
    return str(text).translate(ARABIC_INDIC) if arabic else str(text)


def bearing_deg(p, q):
    """Grid bearing from p to q, clockwise from north, 0-360."""
    return (math.degrees(math.atan2(q.x() - p.x(), q.y() - p.y())) + 360) % 360


def dms_text(deg):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round((deg - d - m / 60) * 3600)
    if s == 60:
        s, m = 0, m + 1
    if m == 60:
        m, d = 0, d + 1
    return f"{d}°{m:02d}'{s:02d}\""


def polygon_corners(geom):
    """Corners of the outer ring of a polygon (closing point dropped)."""
    g = QgsGeometry(geom)
    if g.isMultipart():
        parts = g.asMultiPolygon()
        ring = max(parts, key=lambda p: QgsGeometry.fromPolygonXY(p).area())[0]
    else:
        ring = g.asPolygon()[0]
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    # start at the north-western corner and go clockwise, as on survey sheets
    if _signed_area(pts) > 0:
        pts = list(reversed(pts))
    start = min(range(len(pts)), key=lambda k: (-pts[k].y() + pts[k].x()))
    return pts[start:] + pts[:start]


def _signed_area(pts):
    return sum(pts[k].x() * pts[(k + 1) % len(pts)].y() - pts[(k + 1) % len(pts)].x() * pts[k].y()
               for k in range(len(pts))) / 2


def corner_rows(geom, crs, arabic=False):
    """Rows (no, x, y, side to next, bearing) and the totals (perimeter, area)."""
    pts = polygon_corners(geom)
    da = QgsDistanceArea()
    da.setSourceCrs(crs, QgsProject.instance().transformContext())
    da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7030")
    projected = not crs.isGeographic()
    rows = []
    perimeter = 0.0
    for k, p in enumerate(pts):
        q = pts[(k + 1) % len(pts)]
        side = math.hypot(q.x() - p.x(), q.y() - p.y()) if projected else da.measureLine(p, q)
        perimeter += side
        fmt = "{:.3f}" if projected else "{:.7f}"
        rows.append([str(k + 1), fmt.format(p.x()), fmt.format(p.y()), f"{side:.2f}", dms_text(bearing_deg(p, q))])
    area = geom.area() if projected else da.measureArea(geom)
    if arabic:
        rows = [[digits(c, True) if k == 0 else ltr(digits(c, True)) for k, c in enumerate(r)] for r in rows]
    return pts, rows, perimeter, area


def _text_format(size, bold=False, color="#1E252B", family=None):
    f = QFont(family or "Segoe UI")
    f.setPointSizeF(size)
    f.setBold(bold)
    fmt = QgsTextFormat()
    fmt.setFont(f)
    fmt.setSize(size)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(color))
    return fmt


_RTL = [False]


def ltr(text):
    """Keep numbers, degrees and dates in their written order when they use
    Arabic-Indic digits: a left-to-right mark on both sides of every symbol stops the
    bidirectional algorithm from reordering the groups (30°00' -> 00'30°, 1:1,000 -> 000,1:1)."""
    return "\u200e" + re.sub(r"([^0-9\u0660-\u0669])", lambda m: "\u200e" + m.group(1) + "\u200e", text)


def _label(layout, text, rect, size=10, bold=False, align=None, color="#1E252B", valign=None):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    lab.setTextFormat(_text_format(size, bold, color))
    lab.setHAlign(align if align is not None else (Qt.AlignmentFlag.AlignRight if _RTL[0]
                                                    else Qt.AlignmentFlag.AlignLeft))
    lab.setVAlign(valign or Qt.AlignmentFlag.AlignTop)
    lab.setMarginX(0)
    lab.setMarginY(0)
    layout.addLayoutItem(lab)
    lab.attemptMove(QgsLayoutPoint(rect.x(), rect.y()))
    lab.attemptResize(QgsLayoutSize(rect.width(), rect.height()))
    return lab


def _box(layout, rect, fill="#FFFFFF", stroke="#1E252B", width=0.35):
    from qgis.core import QgsFillSymbol

    shape = QgsLayoutItemShape(layout)
    shape.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
    shape.setSymbol(QgsFillSymbol.createSimple({"color": fill, "outline_color": stroke,
                                                "outline_width": str(width), "outline_width_unit": "MM"}))
    layout.addLayoutItem(shape)
    shape.attemptMove(QgsLayoutPoint(rect.x(), rect.y()))
    shape.attemptResize(QgsLayoutSize(rect.width(), rect.height()))
    return shape


def corners_layer(pts, crs, name, arabic=False):
    lyr = QgsVectorLayer(f"Point?crs={crs.authid()}&field=no:integer&field=label:string", name, "memory")
    feats = []
    for k, p in enumerate(pts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p)))
        f.setAttributes([k + 1, digits(k + 1, arabic)])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    from qgis.core import QgsMarkerSymbol, QgsTextBufferSettings

    lyr.renderer().setSymbol(QgsMarkerSymbol.createSimple({"name": "circle", "color": "#D64545", "size": "1.8",
                                                           "outline_color": "#FFFFFF", "outline_width": "0.3"}))
    pal = QgsPalLayerSettings()
    pal.fieldName = "label"
    fmt = _text_format(9, True)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor("white"))
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    pal.placement = Qgis.LabelPlacement.OrbitAroundPoint if hasattr(Qgis.LabelPlacement, "OrbitAroundPoint") \
        else Qgis.LabelPlacement.AroundPoint
    pal.dist = 1.2
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    lyr.setLabelsEnabled(True)
    return lyr


class SheetOptions:
    def __init__(self, **kw):
        self.title = kw.get("title", "")
        self.subtitle = kw.get("subtitle", "")
        self.author = kw.get("author", "")
        self.notes = kw.get("notes", "")
        self.paper = kw.get("paper", "A4")
        self.landscape = kw.get("landscape", True)
        self.scale = kw.get("scale", 0)          # 0 = fit
        self.extent = kw.get("extent")           # QgsRectangle in project CRS; None = all layers
        self.grid = kw.get("grid", True)
        self.legend = kw.get("legend", True)
        self.logo = kw.get("logo", "")
        self.parcel = kw.get("parcel")           # (QgsGeometry, QgsCoordinateReferenceSystem) or None
        self.arabic_digits = kw.get("arabic_digits", False)
        self.rtl = kw.get("rtl", i18n.is_rtl())
        self.scalebar = kw.get("scalebar", "single")         # single | dual (metric + imperial) | fixed
        self.scalebar_segment = kw.get("scalebar_segment", 0)  # metres per segment when fixed; 0 = round value
        self.legend_fit = kw.get("legend_fit", True)         # shrink / wrap / split the legend to fit its frame
        self.link = kw.get("link", "")                       # a web address printed and clickable in the PDF
        self.preset = kw.get("preset")                       # sheet_presets id: paper, margins, title block
        self.grid_style = kw.get("grid_style", "auto")       # map_grid.STYLES
        self.zone_mode = kw.get("zone_mode", "extended")     # map_grid.ZONE_MODES (map across two UTM zones)
        self.grid_lang = kw.get("grid_lang")                 # language of grid labels; None = from the sheet
        self.fields = kw.get("fields", {})                   # title-block values {field key: text}
        self.corners = kw.get("corners")                     # corner coordinates outside the map; None = preset


def _grid_lang(opt, preset):
    """Language of the grid labels: Arabic digits win; then the sheet standard; then the interface."""
    from .map_grid import GRID_LETTERS

    if opt.grid_lang:
        return opt.grid_lang
    if opt.arabic_digits:
        return "ar"
    if preset is not None and preset.lang != "ar":
        return preset.lang
    ui = i18n.lang()
    return ui if ui in GRID_LETTERS and ui != "ar" else "en"


def build_layout(project, opt):
    """Create the print layout (added to the project's layout manager)."""
    rtl = opt.rtl
    _RTL[0] = rtl
    ad = opt.arabic_digits
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    name = opt.title or tr("Map")
    manager = project.layoutManager()
    base, k = name, 2
    while manager.layoutByName(name) is not None:
        name, k = f"{base} ({k})", k + 1
    layout.setName(name)
    from . import sheet_presets

    preset = sheet_presets.get(opt.preset) if opt.preset else None
    if preset is not None:
        pw, ph = preset.page_size()
        landscape = preset.landscape
        fl, ft, fr, fb = preset.margins
        frame = QRectF(fl, ft, pw - fl - fr, ph - ft - fb)
        # content sits 4 mm inside the drawing frame
        ml, mt, mr, mb = fl + 4, ft + 4, fr + 4, fb + 4
    else:
        pw, ph = PAPERS.get(opt.paper, PAPERS["A4"])
        landscape = opt.landscape
        if not landscape:
            pw, ph = ph, pw
        frame = QRectF(MARGIN / 2, MARGIN / 2, pw - MARGIN, ph - MARGIN)
        ml = mt = mr = mb = MARGIN
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(pw, ph))
    _box(layout, frame, fill="#FFFFFF", width=0.6)
    corners = opt.corners if opt.corners is not None else bool(preset is not None and preset.corners)
    cpad = 22.0 if corners else 0.0  # room for the corner coordinates beside the map frame

    # panel: beside the map (landscape) or below it (portrait)
    if landscape:
        panel_w = max(70.0, pw * 0.27)
        map_rect = QRectF(ml, mt, pw - ml - mr - panel_w - GAP, ph - mt - mb)
        px = ml if rtl else pw - mr - panel_w
        if rtl:
            map_rect.moveLeft(ml + panel_w + GAP)
        panel = QRectF(px, mt, panel_w, ph - mt - mb)
    else:
        panel_h = max(70.0, ph * 0.26) + (4.6 * len(preset.fields) if preset is not None else 0)
        map_rect = QRectF(ml, mt, pw - ml - mr, ph - mt - mb - panel_h - GAP)
        panel = QRectF(ml, ph - mb - panel_h, pw - ml - mr, panel_h)

    corner_pts, rows, perimeter, area = None, [], 0, 0
    parcel_layer = None
    if opt.parcel is not None:
        geom, pcrs = opt.parcel
        corner_pts, rows, perimeter, area = corner_rows(geom, pcrs, ad)
        parcel_layer = corners_layer(corner_pts, pcrs, tr("Corners - {name}").format(name=name), ad)

    # map
    mp = QgsLayoutItemMap(layout)
    mp.setFrameEnabled(True)
    mp.setFrameStrokeWidth(QgsLayoutMeasurement(0.4))
    layout.addLayoutItem(mp)
    grid_style = opt.grid_style if opt.grid_style != "auto" or preset is None else preset.grid
    grid_pad = (9.0 if grid_style == "both" else 7.0) if opt.grid and grid_style != "none" else 0.0
    inner = map_rect.adjusted(grid_pad + cpad, grid_pad + (8 if corners else 0), -grid_pad - cpad,
                              -grid_pad - (8 if corners else 0))
    mp.attemptMove(QgsLayoutPoint(inner.x(), inner.y()))
    mp.attemptResize(QgsLayoutSize(inner.width(), inner.height()))
    mp.setCrs(project.crs())
    extent = opt.extent
    if extent is None or extent.isEmpty():
        extent = _layers_extent(project)
    if parcel_layer is not None:
        t = QgsCoordinateTransform(parcel_layer.crs(), project.crs(), project)
        pe = t.transformBoundingBox(parcel_layer.extent())
        extent = pe
    if extent is not None and not extent.isEmpty():
        e = QgsRectangle(extent)
        e.scale(1.15)
        mp.zoomToExtent(e)
    if opt.scale:
        mp.setScale(opt.scale)
    elif mp.scale() > 0:
        mp.setScale(nice_scale(mp.scale()))
    if parcel_layer is not None:
        for old in project.mapLayersByName(parcel_layer.name()):
            project.removeMapLayer(old.id())
        visible = [lyr for lyr in project.layerTreeRoot().layerOrder()
                   if project.layerTreeRoot().findLayer(lyr.id()).isVisible()]
        project.addMapLayer(parcel_layer)
        mp.setLayers([parcel_layer] + visible)
        mp.setKeepLayerSet(True)
    glang = _grid_lang(opt, preset)
    if opt.grid:
        from . import map_grid

        if grid_style == "auto" and preset is None and not mp.crs().isGeographic():
            _add_grid(mp, ad)  # the classic frame of the quick sheet
        else:
            map_grid.add_grid(mp, grid_style, glang, opt.zone_mode)
    if corners:
        from . import map_grid

        map_grid.add_corner_labels(layout, mp, glang, geographic=mp.crs().isGeographic())

    # panel contents
    _box(layout, panel, fill="#FFFFFF", width=0.4)
    x, y, w = panel.x() + 4, panel.y() + 4, panel.width() - 8
    if opt.logo and os.path.exists(opt.logo):
        pic = QgsLayoutItemPicture(layout)
        pic.setPicturePath(opt.logo)
        layout.addLayoutItem(pic)
        pic.attemptMove(QgsLayoutPoint(x + (0 if rtl else w - 22), y))
        pic.attemptResize(QgsLayoutSize(22, 16))
        pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
    _label(layout, digits(opt.title or name, ad), QRectF(x, y, w - (24 if opt.logo else 0), 12), 16, True)
    y += 12
    if opt.subtitle:
        _label(layout, digits(opt.subtitle, ad), QRectF(x, y, w, 8), 10, False, color="#5B6670")
        y += 8
    _rule(layout, x, y + 1, w)
    y += 4

    # north arrow + scale bar on one row
    arrow = QgsLayoutItemPicture(layout)
    svg = _north_arrow_svg()
    if svg:
        arrow.setPicturePath(svg)
    arrow.setLinkedMap(mp)
    layout.addLayoutItem(arrow)
    arrow.attemptMove(QgsLayoutPoint(x + (w - 14 if rtl else 0), y))
    arrow.attemptResize(QgsLayoutSize(14, 18))
    bar_x = x + (0 if rtl else 18)
    bar_w = max(25.0, w - 22)
    small = mp.scale() < 50000
    metric = (QgsUnitTypes.DistanceUnit.DistanceMeters, tr("m")) if small else \
        (QgsUnitTypes.DistanceUnit.DistanceKilometers, tr("km"))
    if preset is not None and preset.unit == "ft":  # sheets in feet: the main bar in feet / miles
        metric = (QgsUnitTypes.DistanceUnit.DistanceFeet, tr("ft")) if small else \
            (QgsUnitTypes.DistanceUnit.DistanceMiles, tr("mi"))
    if opt.scalebar == "dual":
        imperial = (QgsUnitTypes.DistanceUnit.DistanceFeet, tr("ft")) if small else \
            (QgsUnitTypes.DistanceUnit.DistanceMiles, tr("mi"))
        _scale_bar(layout, mp, metric, bar_x, y + 1, bar_w, "biruni-scalebar")
        _scale_bar(layout, mp, imperial, bar_x, y + 9, bar_w, "biruni-scalebar-imperial", style="Line Ticks Down")
        text_y, row_h = y + 17, 24
    else:
        seg = opt.scalebar_segment if opt.scalebar == "fixed" else 0
        if opt.scalebar == "fixed" and seg <= 0:
            seg = fixed_segment_length(mp.scale(), bar_w)
        _scale_bar(layout, mp, metric, bar_x, y + 2, bar_w, "biruni-scalebar", segment_m=seg)
        text_y, row_h = y + 13, 21
    scale_num = f"1:{int(round(mp.scale())):,}"
    scale_txt = _label(layout, tr("Scale {s}").format(s=ltr(digits(scale_num, True)) if ad else scale_num),
                       QRectF(bar_x, text_y, w - 18, 5), 8, False, color="#5B6670")
    scale_txt.setId("biruni-scale")
    y += row_h
    _rule(layout, x, y, w)
    y += 3
    if preset is not None:
        y = _title_block(layout, preset, opt, x, y, w, scale_num, area if rows else None,
                         perimeter if rows else None) + 2

    bottom = panel.bottom() - 4
    # details at the bottom of the panel
    crs = project.crs()
    details = [tr("Coordinate system: {crs}").format(crs=crs.description() + (f" ({crs.authid()})"
                                                                              if crs.authid() else "")),
               tr("Date: {d}").format(d=ltr(digits(dt.date.today().strftime("%d/%m/%Y"), True)) if ad
                                      else dt.date.today().isoformat())]
    if opt.author:
        details.append(tr("Prepared by: {a}").format(a=opt.author))
    if opt.notes:
        details.append(opt.notes)
    details.append(tr("Made with Biruni GIS"))
    det_h = 4.6 * len(details) + 2
    _label(layout, "\n".join(details), QRectF(x, bottom - det_h, w, det_h), 7, False, color="#5B6670")
    bottom -= det_h + 2
    if opt.link:
        url = opt.link.strip()
        link = _label(layout, url, QRectF(x, bottom - 5, w, 5), 7.5, False, color="#1F6FB2")
        link.setCustomProperty("biruni_link", url if "://" in url or url.startswith("mailto:") else "https://" + url)
        link.setId("biruni-link")
        bottom -= 6
    _rule(layout, x, bottom, w)
    bottom -= 2

    # corner table (when a parcel is given)
    if rows:
        heads = [tr("Point"), tr("X / Easting"), tr("Y / Northing"), tr("Side (m)"), tr("Bearing")]
        if rtl:
            heads = list(reversed(heads))
            rows = [list(reversed(r)) for r in rows]
        line_h = 4.4
        max_rows = int((bottom - y - 20) // line_h) - 1
        shown = rows[:max(3, max_rows)]
        table = QgsLayoutItemManualTable.create(layout)
        table.setHeaders([QgsLayoutTableColumn(h) for h in heads])
        table.setTableContents([[QgsTableCell(c) for c in r] for r in shown])
        table.setContentTextFormat(_text_format(6.5))
        table.setHeaderTextFormat(_text_format(6.5, True))
        table.setCellMargin(0.8)
        table.setIncludeTableHeader(True)
        ratios = [0.09, 0.27, 0.29, 0.14, 0.21]
        if rtl:
            ratios = list(reversed(ratios))
        table.setColumnWidths([w * r - 1.7 for r in ratios])
        if rtl:
            for col in table.headers():
                col.setHAlignment(Qt.AlignmentFlag.AlignRight)
        table.setGridStrokeWidth(0.15)
        layout.addMultiFrame(table)
        tbl_h = line_h * (len(shown) + 1) + 2
        frame = QgsLayoutFrame(layout, table)
        frame.attemptMove(QgsLayoutPoint(x, y))
        frame.attemptResize(QgsLayoutSize(w, tbl_h))
        table.addFrame(frame)
        y += tbl_h + 1
        more = len(rows) - len(shown)
        fa, fp = f"{area:,.2f}", f"{perimeter:,.2f}"
        if ad:
            fa, fp = ltr(digits(fa, True)), ltr(digits(fp, True))
        totals = tr("Area: {a} m²  ·  Perimeter: {p} m").format(a=fa, p=fp)
        if more > 0:
            totals += "  ·  " + tr("{n} more points in the corners layer").format(n=more)
        _label(layout, totals, QRectF(x, y, w, 9), 8, True)
        y += 10

    if opt.legend and y < bottom - 15:
        leg = QgsLayoutItemLegend(layout)
        leg.setTitle(tr("Legend"))
        leg.setLinkedMap(mp)
        leg.setLegendFilterByMapEnabled(True)
        leg.setResizeToContents(False)
        layout.addLayoutItem(leg)
        leg.attemptMove(QgsLayoutPoint(x, y))
        leg.attemptResize(QgsLayoutSize(w, bottom - y))
        if rtl:
            from qgis.core import QgsLegendStyle

            leg.setSymbolAlignment(Qt.AlignmentFlag.AlignRight)
            leg.setTitleAlignment(Qt.AlignmentFlag.AlignRight)
            for st in (QgsLegendStyle.Style.Title, QgsLegendStyle.Style.Group, QgsLegendStyle.Style.Subgroup,
                       QgsLegendStyle.Style.SymbolLabel):
                leg.rstyle(st).setAlignment(Qt.AlignmentFlag.AlignRight)
        leg.setId("biruni-legend")
        if opt.legend_fit:
            from .cartography import fit_legend

            fit_legend(leg, w, bottom - y)
        else:
            leg.adjustBoxSize()
            if leg.rect().height() > bottom - y:
                leg.setResizeToContents(False)
                leg.attemptResize(QgsLayoutSize(w, bottom - y))
    manager.addLayout(layout)
    return layout


def _title_block(layout, preset, opt, x, y, w, scale_num, area=None, perimeter=None):
    """The preset's title block (field / value table in the sheet's language); returns the y below it."""
    from . import sheet_presets

    values = dict(opt.fields or {})
    values.setdefault("scale", scale_num)
    values.setdefault("date", dt.date.today().strftime("%d/%m/%Y"))
    if area is not None:
        values.setdefault("area", f"{area:,.2f} m²")
    if perimeter is not None:
        values.setdefault("perimeter", f"{perimeter:,.2f} m")
    rows = sheet_presets.title_rows(preset, values)
    rtl = preset.lang in ("ar", "fa", "ur", "he")
    if opt.arabic_digits:
        # numbers, dates and codes keep their order with direction marks; words (names) must not get them,
        # or their letters come apart and reverse
        rows = [(k, (digits(v, True) if re.search(r"[؀-ۿ]", v) and re.search(r"[^\W\d_]{2}", v)
                     else ltr(digits(v, True))) if v else v) for k, v in rows]
    if rtl:
        rows = [(v, k) for k, v in rows]
    table = QgsLayoutItemManualTable.create(layout)
    table.setIncludeTableHeader(False)
    table.setTableContents([[QgsTableCell(a), QgsTableCell(b)] for a, b in rows])
    table.setContentTextFormat(_text_format(7))
    table.setCellMargin(0.8)
    ratios = (0.62, 0.38) if rtl else (0.38, 0.62)
    table.setColumnWidths([w * r - 1.7 for r in ratios])
    table.setGridStrokeWidth(0.15)
    layout.addMultiFrame(table)
    h = 4.6 * len(rows) + 1
    frame = QgsLayoutFrame(layout, table)
    frame.attemptMove(QgsLayoutPoint(x, y))
    frame.attemptResize(QgsLayoutSize(w, h))
    table.addFrame(frame)
    frame.setId("biruni-title-block")
    return y + h


def fixed_segment_length(scale, bar_width_mm, segments=2):
    """A round segment length in metres (1, 2, 2.5, 5 × 10ⁿ) so that ``segments`` of them fit the bar."""
    raw = scale * bar_width_mm / 1000.0 / max(segments, 1)
    return _nice_interval(raw * 0.8)


def _scale_bar(layout, mp, units, x, y, max_width, item_id, style="Single Box", segment_m=0):
    """A scale bar linked to the map. ``segment_m`` > 0 fixes the length of each segment (in metres) and adds as
    many segments as fit; otherwise the bar picks a round length that fits."""
    unit, label = units
    sb = QgsLayoutItemScaleBar(layout)
    sb.setStyle(style)
    sb.setLinkedMap(mp)
    sb.setUnits(unit)
    sb.setNumberOfSegments(2)
    sb.setNumberOfSegmentsLeft(0)
    sb.setUnitLabel(label)
    sb.setTextFormat(_text_format(7))
    layout.addLayoutItem(sb)
    if segment_m > 0 and mp.scale() > 0:
        per_unit = QgsUnitTypes.fromUnitToUnitFactor(QgsUnitTypes.DistanceUnit.DistanceMeters, unit)
        sb.setSegmentSizeMode(Qgis.ScaleBarSegmentSizeMode.Fixed)
        sb.setUnitsPerSegment(segment_m * per_unit)
        seg_mm = segment_m / mp.scale() * 1000.0
        sb.setNumberOfSegments(max(1, min(5, int(max_width // max(seg_mm, 0.1)))))
    else:
        sb.applyDefaultSize(unit)
        sb.setMaximumBarWidth(max_width)
    sb.setId(item_id)
    sb.attemptMove(QgsLayoutPoint(x, y))
    sb.update()
    return sb


def _rule(layout, x, y, w):
    _box(layout, QRectF(x, y, w, 0.01), fill="#C9D0D6", stroke="#C9D0D6", width=0.2)


def _north_arrow_svg():
    for base in QgsApplication.svgPaths():
        for name in ("arrows/NorthArrow_11.svg", "arrows/NorthArrow_02.svg", "arrows/NorthArrow_01.svg"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
    return ""


def _layers_extent(project):
    ext = QgsRectangle()
    for lyr in project.layerTreeRoot().layerOrder():
        node = project.layerTreeRoot().findLayer(lyr.id())
        if node is None or not node.isVisible() or lyr.providerType() == "wms":
            continue
        try:
            t = QgsCoordinateTransform(lyr.crs(), project.crs(), project)
            e = t.transformBoundingBox(lyr.extent())
        except Exception:
            continue
        if not e.isEmpty() and e.isFinite():
            if ext.isEmpty():
                ext = QgsRectangle(e)
            else:
                ext.combineExtentWith(e)
    return ext


def _add_grid(mp, arabic):
    grid = QgsLayoutItemMapGrid("Coordinates", mp)
    mp.grids().addGrid(grid)
    ext = mp.extent()
    span = max(ext.width(), ext.height())
    interval = _nice_interval(span / 5)
    grid.setUnits(QgsLayoutItemMapGrid.GridUnit.MapUnit)
    grid.setIntervalX(interval)
    grid.setIntervalY(interval)
    grid.setStyle(QgsLayoutItemMapGrid.GridStyle.Cross)
    grid.setCrossLength(2)
    grid.setFrameStyle(QgsLayoutItemMapGrid.FrameStyle.Zebra)
    grid.setFrameWidth(1.2)
    grid.setAnnotationEnabled(True)
    grid.setAnnotationTextFormat(_text_format(6))
    geographic = mp.crs().isGeographic()
    grid.setAnnotationPrecision(4 if geographic and interval < 1 else 0)
    if geographic:
        grid.setAnnotationFormat(QgsLayoutItemMapGrid.AnnotationFormat.DegreeMinute)
    if arabic:
        grid.setAnnotationFormat(QgsLayoutItemMapGrid.AnnotationFormat.CustomFormat)
        dec = 4 if geographic and interval < 1 else 0
        grid.setAnnotationExpression(
            "replace(format_number(@grid_number, %d, 'en', true), array('0','1','2','3','4','5','6','7','8','9','.'), "
            "array('٠','١','٢','٣','٤','٥','٦','٧','٨','٩','٫'))" % dec)
    for side in (QgsLayoutItemMapGrid.BorderSide.Left, QgsLayoutItemMapGrid.BorderSide.Right):
        grid.setAnnotationDirection(QgsLayoutItemMapGrid.AnnotationDirection.VerticalDescending, side)
    for side in (QgsLayoutItemMapGrid.BorderSide.Left, QgsLayoutItemMapGrid.BorderSide.Right,
                 QgsLayoutItemMapGrid.BorderSide.Top, QgsLayoutItemMapGrid.BorderSide.Bottom):
        grid.setAnnotationPosition(QgsLayoutItemMapGrid.AnnotationPosition.OutsideMapFrame, side)
    mp.updateBoundingRect()


def _nice_interval(raw):
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def export(layout, path, lean=True, links=True):
    """PDF (small and sharp when ``lean``; web addresses clickable) or an image, by the file extension."""
    exp = QgsLayoutExporter(layout)
    if path.lower().endswith(".pdf"):
        from .cartography import export_pdf

        ok, _n = export_pdf(layout, path, lean=lean, links=links, dpi=None if lean else 300)
        return ok
    s = QgsLayoutExporter.ImageExportSettings()
    s.dpi = 150
    return exp.exportToImage(path, s) == QgsLayoutExporter.ExportResult.Success
