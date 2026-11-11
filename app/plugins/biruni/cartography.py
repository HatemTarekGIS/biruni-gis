"""One-click cartography helpers: labels as small tables, contour labels that mask the lines under them, labels that
can be moved by hand at once, a legend that fits its frame, lean PDF export and clickable links in PDFs.

Everything here works on ordinary QGIS layers and layouts, so the result can still be fine-tuned in the layer
styling panel or the layout designer.
"""
import math
import re
from collections import Counter

from qgis.core import (Qgis, QgsAuxiliaryLayer, QgsField, QgsLayoutExporter, QgsLayoutItemLabel,
                       QgsLayoutRenderContext, QgsLegendStyle, QgsLineSymbol, QgsPalLayerSettings, QgsProject,
                       QgsProperty, QgsRuleBasedRenderer, QgsSymbolLayerReference, QgsTextBackgroundSettings,
                       QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QMetaType, QSizeF
from qgis.PyQt.QtGui import QColor, QFont

from .i18n import tr

PT_TO_MM = 25.4 / 72.0
CONTOUR_COLOR = "#9C6B3F"


def _quote_field(name):
    return '"' + name.replace('"', '""') + '"'


def _quote_text(text):
    return "'" + str(text).replace("\\", "\\\\").replace("'", "''") + "'"


def _text_format(size_pt, color="#1E252B", bold=False):
    f = QFont("Segoe UI")
    f.setBold(bold)
    fmt = QgsTextFormat()
    fmt.setFont(f)
    fmt.setSize(size_pt)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(QColor(color))
    return fmt


def _has_arabic(text):
    return any("؀" <= ch <= "ۿ" or "ݐ" <= ch <= "ݿ" for ch in str(text))


# ---- tabular labels -------------------------------------------------------------------------------------------------
def tabular_label_expression(layer, fields, header=None, use_aliases=True, rtl=None):
    """The label expression: one line per field, "name<TAB>value" (value<TAB>name for right-to-left names), with
    an optional first line (a header such as the parcel number)."""
    names = []
    for name in fields:
        i = layer.fields().indexOf(name)
        if i < 0:
            continue
        alias = layer.fields().at(i).alias() if use_aliases else ""
        names.append((name, alias or name))
    if not names:
        raise ValueError(tr("None of the chosen fields is in the layer."))
    if rtl is None:
        rtl = any(_has_arabic(a) for _, a in names)
    lines = []
    for name, alias in names:
        value = f"coalesce(to_string({_quote_field(name)}), '–')"
        lines.append(f"{value} || '\t' || {_quote_text(alias)}" if rtl else f"{_quote_text(alias)} || '\t' || {value}")
    expr = " || '\n' || ".join(lines)
    if header:
        head = _quote_field(header) if layer.fields().indexOf(header) >= 0 else _quote_text(header)
        expr = f"coalesce(to_string({head}), '') || '\n' || " + expr
    return expr, [a for _, a in names], rtl


def tabular_label(layer, fields, header=None, size=8.0, use_aliases=True, frame=True):
    """Label every feature with a small table of several fields (name and value per line, aligned with a tab
    stop, on a framed white background). Returns the label settings that were applied."""
    expr, aliases, rtl = tabular_label_expression(layer, fields, header, use_aliases)
    fmt = _text_format(size)
    longest = max(len(a) for a in aliases)
    fmt.setTabStopDistance(longest * size * 0.62 * PT_TO_MM + 2.5)
    fmt.setTabStopDistanceUnit(Qgis.RenderUnit.Millimeters)
    fmt.setLineHeight(1.15)
    if frame:
        bg = QgsTextBackgroundSettings()
        bg.setEnabled(True)
        bg.setType(QgsTextBackgroundSettings.ShapeType.ShapeRectangle)
        bg.setSizeType(QgsTextBackgroundSettings.SizeType.SizeBuffer)
        bg.setSize(QSizeF(1.2, 0.8))
        bg.setSizeUnit(Qgis.RenderUnit.Millimeters)
        bg.setFillColor(QColor(255, 255, 255, 235))
        bg.setStrokeColor(QColor("#5B6670"))
        bg.setStrokeWidth(0.2)
        bg.setStrokeWidthUnit(Qgis.RenderUnit.Millimeters)
        fmt.setBackground(bg)
    else:
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.8)
        buf.setColor(QColor("white"))
        fmt.setBuffer(buf)
    pal = QgsPalLayerSettings()
    pal.fieldName = expr
    pal.isExpression = True
    pal.setFormat(fmt)
    pal.multilineAlign = Qgis.LabelMultiLineAlignment.Right if rtl else Qgis.LabelMultiLineAlignment.Left
    geom = layer.geometryType()
    if geom == Qgis.GeometryType.Point:
        pal.placement = Qgis.LabelPlacement.AroundPoint
        pal.dist = 1.5
    elif geom == Qgis.GeometryType.Line:
        pal.placement = Qgis.LabelPlacement.Horizontal
    else:
        pal.placement = Qgis.LabelPlacement.OverPoint
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()
    return pal


# ---- contours -------------------------------------------------------------------------------------------------------
def contour_interval(values):
    """The contour interval: the most common gap between neighbouring elevation values."""
    vals = sorted({round(float(v), 6) for v in values if v is not None})
    gaps = [round(b - a, 6) for a, b in zip(vals, vals[1:]) if b - a > 1e-9]
    if not gaps:
        return 0.0
    return Counter(gaps).most_common(1)[0][0]


def index_expression(field, interval, index_every):
    step = interval * index_every
    return f"abs({_quote_field(field)} / {step!r} - round({_quote_field(field)} / {step!r})) < 0.000001"


def guess_elevation_field(layer):
    for name in ("elev", "ELEV", "elevation", "ELEVATION", "z", "Z", "height", "level", "contour", "CONTOUR",
                 "Elevation", "LEVEL"):
        if layer.fields().indexOf(name) >= 0:
            return name
    for fld in layer.fields():
        if fld.isNumeric():
            return fld.name()
    return ""


def style_contours(layer, field=None, interval=0.0, index_every=5, size=7.5, decimals=None, dem=None,
                   color=CONTOUR_COLOR):
    """Cartographic contours in one step: thicker index contours every ``index_every`` lines, their elevations
    written along the line with the line masked (cut) under each number, labels repeated along long lines.
    With a DEM the lines are turned so that every number reads uphill (the surveyors' convention); the line
    directions of the layer are changed for that (an undoable edit is left open on editable layers).
    Returns a dict with the interval, the index step and how many lines were turned."""
    field = field or guess_elevation_field(layer)
    if not field:
        raise ValueError(tr("The contour layer needs a numeric elevation field."))
    if interval <= 0:
        idx = layer.fields().indexOf(field)
        interval = contour_interval(layer.uniqueValues(idx, 100000))
    if interval <= 0:
        raise ValueError(tr("Could not work out the contour interval; give it."))
    if decimals is None:
        decimals = 0 if abs(interval - round(interval)) < 1e-9 else max(1, min(3, -int(math.floor(math.log10(interval)))))
    is_index = index_expression(field, interval, index_every)

    root = QgsRuleBasedRenderer.Rule(None)
    for label, expr, width in ((tr("Index contours"), is_index, 0.45),
                               (tr("Intermediate contours"), "ELSE", 0.2)):
        sym = QgsLineSymbol.createSimple({"line_color": color, "line_width": str(width), "line_width_unit": "MM",
                                          "capstyle": "round", "joinstyle": "round"})
        rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, expr if expr != "ELSE" else "", label)
        if expr == "ELSE":
            rule.setIsElse(True)
        root.appendChild(rule)
    layer.setRenderer(QgsRuleBasedRenderer(root))
    refs = []
    for rule in layer.renderer().rootRule().children():
        sym = rule.symbol()
        for k in range(sym.symbolLayerCount()):
            refs.append(QgsSymbolLayerReference(layer.id(), sym.symbolLayer(k).id()))

    turned = orient_uphill(layer, dem, field) if dem is not None else 0

    fmt = _text_format(size, color=color)
    mask = fmt.mask()
    mask.setEnabled(True)
    mask.setSize(0.7)
    mask.setSizeUnit(Qgis.RenderUnit.Millimeters)
    mask.setMaskedSymbolLayers(refs)
    fmt.setMask(mask)
    pal = QgsPalLayerSettings()
    pal.fieldName = f"format_number({_quote_field(field)}, {decimals})"
    pal.isExpression = True
    pal.setFormat(fmt)
    pal.placement = Qgis.LabelPlacement.Curved
    pal.lineSettings().setPlacementFlags(Qgis.LabelLinePlacementFlag.OnLine)
    pal.repeatDistance = 70
    pal.repeatDistanceUnit = Qgis.RenderUnit.Millimeters
    pal.upsidedownLabels = (Qgis.UpsideDownLabelHandling.AlwaysAllowUpsideDown if dem is not None
                            else Qgis.UpsideDownLabelHandling.FlipUpsideDownLabels)
    pal.dataDefinedProperties().setProperty(QgsPalLayerSettings.Property.Show, QgsProperty.fromExpression(is_index))
    pal.priority = 7
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()
    return {"interval": interval, "index_step": interval * index_every, "turned": turned, "field": field}


def _sample(provider, x, y):
    from qgis.core import QgsPointXY

    val, ok = provider.sample(QgsPointXY(x, y), 1)
    return val if ok else None


def orient_uphill(layer, dem, field=None):
    """Turn contour lines so that higher ground is on their left: QGIS then writes the numbers with their top
    towards higher ground. Returns how many lines were turned (edits are left in the edit buffer)."""
    from qgis.core import QgsCoordinateTransform

    prov = dem.dataProvider()
    xform = QgsCoordinateTransform(layer.crs(), dem.crs(), QgsProject.instance())
    turns = {}
    for f in layer.getFeatures():
        g = f.geometry()
        if g.isEmpty():
            continue
        lines = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        votes = 0
        for line in lines:
            step = max(1, len(line) // 12)
            for k in range(0, len(line) - 1, step):
                a, b = line[k], line[k + 1]
                dx, dy = b.x() - a.x(), b.y() - a.y()
                ln = math.hypot(dx, dy)
                if ln == 0:
                    continue
                off = max(ln * 0.5, dem.rasterUnitsPerPixelX() * 1.5)
                mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
                left = xform.transform(mx - dy / ln * off, my + dx / ln * off)
                right = xform.transform(mx + dy / ln * off, my - dx / ln * off)
                zl, zr = _sample(prov, left.x(), left.y()), _sample(prov, right.x(), right.y())
                if zl is None or zr is None or zl == zr:
                    continue
                votes += 1 if zl > zr else -1
        if votes < 0:
            parts = [list(reversed(line)) for line in lines]
            from qgis.core import QgsGeometry

            turns[f.id()] = (QgsGeometry.fromMultiPolylineXY(parts) if g.isMultipart()
                             else QgsGeometry.fromPolylineXY(parts[0]))
    if not turns:
        return 0
    if not layer.isEditable():
        layer.startEditing()
    layer.beginEditCommand(tr("Turn contour lines uphill"))
    for fid, geom in turns.items():
        layer.changeGeometry(fid, geom)
    layer.endEditCommand()
    return len(turns)


# ---- labels that can be moved by hand ------------------------------------------------------------------------------
def _key_field(layer):
    pk = layer.primaryKeyAttributes()
    if pk:
        return layer.fields().at(pk[0])
    n = layer.featureCount()
    for fld in layer.fields():
        if fld.type() in (QMetaType.Type.Int, QMetaType.Type.LongLong, QMetaType.Type.QString):
            idx = layer.fields().indexOf(fld.name())
            values = layer.uniqueValues(idx, n + 1)
            if len(values) == n and None not in values and "" not in values:
                return fld
    return None


def enable_manual_labels(layer, project=None):
    """Make the labels of a layer movable, rotatable and hideable by hand right away: an auxiliary storage table
    keyed on a unique field (stored in the project), with position, rotation and show bound to it. QGIS's label
    tools then work without further questions. Returns (ok, message)."""
    project = project or QgsProject.instance()
    if not isinstance(layer, QgsVectorLayer):
        return False, tr("Choose a vector layer.")
    if not layer.labelsEnabled() or layer.labeling() is None:
        return False, tr("Turn the labels of this layer on first.")
    aux = layer.auxiliaryLayer()
    if aux is None:
        key = _key_field(layer)
        if key is None:
            return False, tr("The layer needs a field with a different value for every feature (an ID) to "
                             "remember moved labels.")
        aux = project.auxiliaryStorage().createAuxiliaryLayer(QgsField(key), layer)
        if aux is None:
            return False, tr("The label positions could not be stored: {e}").format(
                e=project.auxiliaryStorage().errorString())
        layer.setAuxiliaryLayer(aux)
    made = []
    for prop in (QgsPalLayerSettings.Property.PositionX, QgsPalLayerSettings.Property.PositionY,
                 QgsPalLayerSettings.Property.LabelRotation, QgsPalLayerSettings.Property.Show,
                 QgsPalLayerSettings.Property.LabelAllParts):
        try:
            QgsAuxiliaryLayer.createProperty(prop, layer)
            made.append(prop)
        except Exception:  # noqa: BLE001 - LabelAllParts is not available in every version
            continue
    layer.triggerRepaint()
    return True, tr("Labels of “{name}” can now be moved, rotated and hidden with the label tools; the positions "
                    "are kept in the project.").format(name=layer.name())


def manual_labels_ready(layer):
    labeling = layer.labeling()  # keep a reference: settings() points into it
    if layer.auxiliaryLayer() is None or labeling is None:
        return False
    settings = labeling.settings()
    props = settings.dataDefinedProperties()
    return all(props.isActive(p) for p in (QgsPalLayerSettings.Property.PositionX,
                                           QgsPalLayerSettings.Property.PositionY))


# ---- legend that fits its frame -------------------------------------------------------------------------------------
_LEGEND_STYLES = (QgsLegendStyle.Style.Title, QgsLegendStyle.Style.Group, QgsLegendStyle.Style.Subgroup,
                  QgsLegendStyle.Style.SymbolLabel)


def legend_size(legend):
    """The size (mm) the legend needs with its current settings. Measured with the legend renderer, which also
    works before the layout has ever been drawn (``adjustBoxSize`` only does after a paint)."""
    from qgis.core import QgsLayoutUtils, QgsLegendRenderer

    renderer = QgsLegendRenderer(legend.model(), legend.legendSettings())
    ctx = QgsLayoutUtils.createRenderContextForLayout(legend.layout(), None)
    return renderer.minimumSize(ctx)


def fit_legend(legend, width, height, min_scale=0.5, max_columns=4):
    """Make a layout legend fit ``width`` × ``height`` mm: long names wrap to the frame, then the text, symbols
    and spacing shrink and the legend splits into columns until it fits. Only the items visible in the linked map
    are kept. Returns (columns, scale, fits)."""
    from qgis.core import QgsLayoutSize

    if legend.linkedMap() is not None:
        legend.setLegendFilterByMapEnabled(True)
    sides = (QgsLegendStyle.Side.Top, QgsLegendStyle.Side.Bottom, QgsLegendStyle.Side.Left,
             QgsLegendStyle.Side.Right)
    base = {st: legend.style(st).textFormat().size() for st in _LEGEND_STYLES}
    base_margins = {st: {sd: legend.style(st).margin(sd) for sd in sides} for st in _LEGEND_STYLES}
    base_box, base_col = legend.boxSpace(), legend.columnSpace()
    sym_w, sym_h = legend.symbolWidth(), legend.symbolHeight()
    best = None
    for columns in range(1, max_columns + 1):
        col_w = (width - (columns - 1) * base_col) / columns
        for scale in (1.0, 0.92, 0.85, 0.78, 0.7, 0.62, 0.56, 0.5):
            if scale < min_scale - 1e-9:
                break
            for st, size in base.items():
                style = legend.style(st)
                fmt = QgsTextFormat(style.textFormat())
                fmt.setSize(size * scale)
                style.setTextFormat(fmt)
                for sd, m in base_margins[st].items():
                    style.setMargin(sd, m * scale)
                legend.setStyle(st, style)
            legend.setBoxSpace(base_box * scale)
            legend.setSymbolWidth(sym_w * scale)
            legend.setSymbolHeight(sym_h * scale)
            legend.setColumnCount(columns)
            legend.setSplitLayer(columns > 1)
            legend.setAutoWrapLinesAfter(max(10.0, col_w - sym_w * scale - 6))
            need = legend_size(legend)
            # a little room: the painted legend comes out a few percent taller than the measured one
            if need.width() <= width * 0.97 and need.height() <= height * 0.93:
                best = (columns, scale, True)
                break
        if best:
            break
    if best is None:
        best = (max_columns, min_scale, False)
    legend.setResizeToContents(False)
    legend.attemptResize(QgsLayoutSize(width, height))
    return best


# ---- PDF ------------------------------------------------------------------------------------------------------------
URL_RE = re.compile(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+|mailto:[^\s<>\"']+)", re.I)


def layout_links(layout):
    """Clickable areas of a layout: items with the custom property ``biruni_link`` and text labels that contain a
    web address. Returns [(page index, (x, y, w, h) in mm on the page, url)]."""
    links = []
    for item in layout.items():
        if not hasattr(item, "customProperty") or not hasattr(item, "page"):
            continue
        url = item.customProperty("biruni_link", "") or ""
        if not url and isinstance(item, QgsLayoutItemLabel):
            found = URL_RE.findall(item.currentText() if hasattr(item, "currentText") else item.text())
            if found:
                url = found[0]
        if not url:
            continue
        if url.lower().startswith("www."):
            url = "https://" + url
        page = item.page()
        pos = item.pagePositionWithUnits()
        size = item.sizeWithUnits()
        links.append((page, (pos.x(), pos.y(), size.width(), size.height()), url))
    return links


def _pdf_objects(data):
    """Latest version of every indirect object: {number: (start, end, body bytes)}."""
    objs = {}
    for m in re.finditer(rb"(?<![0-9])(\d+)\s+0\s+obj\b(.*?)endobj", data, re.S):
        objs[int(m.group(1))] = (m.start(), m.end(), m.group(2))
    return objs


def add_pdf_links(path, links, page_sizes_mm):
    """Add clickable link areas to a PDF written by the layout exporter (an incremental update, so the original
    content is untouched). ``links``: [(page index, (x, y, w, h) mm, url)]; ``page_sizes_mm``: [(w, h)] per page.
    Returns the number of links written."""
    if not links:
        return 0
    data = open(path, "rb").read()
    objs = _pdf_objects(data)
    pages = []  # in document order: objects with /Type /Page, ordered by the /Kids of the page tree
    kids_m = None
    for num, (_s, _e, body) in objs.items():
        if re.search(rb"/Type\s*/Pages\b", body):
            kids_m = re.search(rb"/Kids\s*\[([^\]]*)\]", body)
            break
    if kids_m:
        pages = [int(n) for n in re.findall(rb"(\d+)\s+0\s+R", kids_m.group(1))]
    else:
        pages = sorted(n for n, (_s, _e, b) in objs.items() if re.search(rb"/Type\s*/Page\b(?!s)", b))
    size_m = re.findall(rb"/Size\s+(\d+)", data)
    next_num = max(int(size_m[-1]) if size_m else 0, max(objs) + 1 if objs else 1)
    startxref = re.findall(rb"startxref\s+(\d+)", data)
    prev = int(startxref[-1]) if startxref else 0
    root = re.findall(rb"/Root\s+(\d+\s+0\s+R)", data)
    info = re.findall(rb"/Info\s+(\d+\s+0\s+R)", data)

    new = []           # (number, body bytes)
    annots_by_page = {}
    for page, (x, y, w, h), url in links:
        if page >= len(pages):
            continue
        pw, ph = page_sizes_mm[page] if page < len(page_sizes_mm) else page_sizes_mm[-1]
        k = 72.0 / 25.4
        x1, y1 = x * k, (ph - y - h) * k
        x2, y2 = (x + w) * k, (ph - y) * k
        safe = url.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1", "replace")
        body = (b"<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] "
                b"/A << /Type /Action /S /URI /URI (" % (x1, y1, x2, y2) + safe + b") >> >>")
        new.append((next_num, body))
        annots_by_page.setdefault(pages[page], []).append(next_num)
        next_num += 1
    for pnum, ann in annots_by_page.items():
        body = objs[pnum][2].strip()
        refs = b" ".join(b"%d 0 R" % n for n in ann)
        m = re.search(rb"/Annots\s*\[([^\]]*)\]", body)
        m2 = re.search(rb"/Annots\s+(\d+)\s+0\s+R", body)
        if m:
            new.append((pnum, body[:m.start()] + b"/Annots [" + m.group(1) + b" " + refs + b"]" + body[m.end():]))
        elif m2 and int(m2.group(1)) in objs:  # an indirect annotation array: only the array changes
            arr_num = int(m2.group(1))
            new.append((arr_num, objs[arr_num][2].strip().rstrip(b"]") + b" " + refs + b"]"))
        else:
            end = body.rfind(b">>")
            new.append((pnum, body[:end] + b"/Annots [" + refs + b"] " + body[end:]))
    if not new:
        return 0
    out = bytearray(data)
    if not out.endswith(b"\n"):
        out += b"\n"
    offsets = {}
    for num, body in new:
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n"
    for num in sorted(offsets):
        out += b"%d 1\n%010d 00000 n \n" % (num, offsets[num])
    trailer = b"<< /Size %d /Prev %d" % (max(next_num, max(offsets) + 1), prev)
    if root:
        trailer += b" /Root " + root[-1]
    if info:
        trailer += b" /Info " + info[-1]
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref_pos
    with open(path, "wb") as f:
        f.write(bytes(out))
    return len(links)


def pdf_settings(lean=True, dpi=300):
    """Export settings for small, sharp PDFs: vector text, simplified geometries, no full-page rasterising."""
    s = QgsLayoutExporter.PdfExportSettings()
    s.dpi = dpi
    s.exportMetadata = True
    if lean:
        s.simplifyGeometries = True
        s.forceVectorOutput = False
        s.rasterizeWholeImage = False
        s.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
    return s


def export_pdf(layout, path, lean=True, links=True, dpi=None):
    """Export a layout to PDF: lean settings (lossy image compression for photos and basemaps, 200 dpi rasters)
    and clickable web addresses. Returns (ok, number of links)."""
    ctx = layout.renderContext()
    old_lossless = ctx.testFlag(QgsLayoutRenderContext.Flag.FlagLosslessImageRendering)
    if lean:
        ctx.setFlag(QgsLayoutRenderContext.Flag.FlagLosslessImageRendering, False)
    try:
        s = pdf_settings(lean, dpi or (200 if lean else 300))
        ok = QgsLayoutExporter(layout).exportToPdf(path, s) == QgsLayoutExporter.ExportResult.Success
    finally:
        ctx.setFlag(QgsLayoutRenderContext.Flag.FlagLosslessImageRendering, old_lossless)
    n = 0
    if ok and links:
        found = layout_links(layout)
        if found:
            pc = layout.pageCollection()
            sizes = [(pc.page(i).pageSize().width(), pc.page(i).pageSize().height()) for i in range(pc.pageCount())]
            try:
                n = add_pdf_links(path, found, sizes)
            except Exception:  # noqa: BLE001 - the PDF itself is fine; links are a bonus
                n = 0
    return ok, n
