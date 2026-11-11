"""Ready map products: each builder styles the data the way the product needs and returns a finished layout
(or file): land-use map, topographic map, subdivision plan, before/after swipe page and statistical atlas."""
import html
import os

import numpy as np
from osgeo import gdal
from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateTransform, QgsFeature, QgsField,
                       QgsFillSymbol, QgsGeometry, QgsLayoutExporter, QgsLayoutFrame, QgsLayoutItemAttributeTable,
                       QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutObject, QgsLayoutPoint,
                       QgsLayoutSize, QgsMapRendererParallelJob, QgsMapSettings, QgsPalLayerSettings, QgsPointXY,
                       QgsProject, QgsProperty, QgsRasterLayer, QgsRendererCategory, QgsSingleSymbolRenderer,
                       QgsSymbol, QgsTextBufferSettings, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QMetaType, QSize, Qt
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPainter

from ..i18n import tr
from . import auto_layout

# ---- land use ----------------------------------------------------------------------------------------------
# (keywords in English/Arabic, colour) - common planning colours; national standards come from palettes.py
LAND_USE = [
    (("resid", "housing", "سكن", "سكني"), "#F6DD6E"),
    (("commerc", "retail", "تجار", "تجاري"), "#E05A47"),
    (("mixed", "مختلط"), "#F09A4B"),
    (("industr", "صناع"), "#9B7BB8"),
    (("educat", "school", "تعليم", "مدرس"), "#5B9BD5"),
    (("health", "hospital", "صح", "مستشف"), "#F2A3B3"),
    (("relig", "mosque", "church", "دين", "مسجد", "مساجد"), "#3FA796"),
    (("gov", "public", "حكوم", "عام", "خدمات"), "#C08A5B"),
    (("park", "green", "garden", "open space", "حدائق", "حديق", "خضراء", "منتزه"), "#79C36A"),
    (("agri", "farm", "زراع", "مزارع"), "#C5E09B"),
    (("road", "street", "parking", "طرق", "شوارع", "مواقف"), "#BDBDBD"),
    (("water", "lake", "river", "مياه", "بحيرة", "وادي"), "#7FB8E6"),
    (("vacant", "undeveloped", "فضاء", "بيضاء", "شاغر"), "#F2F2EE"),
    (("utilit", "infra", "مرافق", "بنية"), "#8C8C8C"),
]


def land_use_colour(value):
    text = str(value or "").strip().lower()
    for keys, colour in LAND_USE:
        if any(k in text for k in keys):
            return colour
    return None


def style_land_use(layer, field):
    """Categorized style with a national standard (palettes.apply_standard) when one fits, else planning colours."""
    try:
        from . import palettes  # noqa: PLC0415 - optional, built by another part of Map Studio
        if palettes.apply_standard(layer, field):
            return "standard"
    except Exception:  # noqa: BLE001
        pass
    idx = layer.fields().indexOf(field)
    values = sorted({v for v in layer.uniqueValues(idx) if v not in (None, "")}, key=str)
    spare = ["#D9C2A7", "#B5CFD8", "#E6D3E8", "#CFE3C9", "#EADCB4"]
    cats = []
    for i, v in enumerate(values):
        sym = QgsFillSymbol.createSimple({"color": land_use_colour(v) or spare[i % len(spare)],
                                          "outline_color": "#6b6b6b", "outline_width": "0.15"})
        cats.append(QgsRendererCategory(v, sym, str(v)))
    layer.setRenderer(QgsCategorizedSymbolRenderer(field, cats))
    layer.triggerRepaint()
    return "semantic"


def land_use_map(layer, field, project=None, purpose="wall", title=None, lang=None):
    project = project or QgsProject.instance()
    style_land_use(layer, field)
    return auto_layout.build(project, purpose, layers=[layer], title=title or tr("Land use"), lang=lang)


# ---- topographic map ---------------------------------------------------------------------------------------
def _run(alg, params):
    import processing  # noqa: PLC0415

    return processing.run(alg, params)


def spot_heights(dem_path, crs, cells=6):
    """The highest point in each cell of a cells×cells grid over the DEM, as a memory point layer."""
    ds = gdal.Open(dem_path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(float)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        arr[arr == nodata] = np.nan
    gt = ds.GetGeoTransform()
    lyr = QgsVectorLayer(f"Point?crs={crs.authid()}", tr("Spot heights"), "memory")
    lyr.dataProvider().addAttributes([QgsField("elev", QMetaType.Type.Double)])
    lyr.updateFields()
    feats = []
    rows, cols = arr.shape
    for i in range(cells):
        for j in range(cells):
            block = arr[i * rows // cells:(i + 1) * rows // cells, j * cols // cells:(j + 1) * cols // cells]
            if block.size == 0 or np.all(np.isnan(block)):
                continue
            r, c = np.unravel_index(np.nanargmax(block), block.shape)
            r += i * rows // cells
            c += j * cols // cells
            x = gt[0] + (c + 0.5) * gt[1] + (r + 0.5) * gt[2]
            y = gt[3] + (c + 0.5) * gt[4] + (r + 0.5) * gt[5]
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            f.setAttributes([round(float(arr[r, c]), 1)])
            feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    sym = QgsSymbol.defaultSymbol(lyr.geometryType())
    sym.setColor(QColor("#222222"))
    sym.setSize(1.2)
    lyr.renderer().setSymbol(sym)
    pal = QgsPalLayerSettings()
    pal.fieldName = "elev"
    fmt = pal.format()
    fmt.setSize(7)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    lyr.setLabelsEnabled(True)
    return lyr


def topographic_map(dem, project=None, interval=0.0, title=None, lang=None, purpose="wall"):
    """Hillshade + contours (index contours labelled, cut under their numbers) + spot heights + grid.
    Returns (layout, {'hillshade':…, 'contours':…, 'spots':…})."""
    from .. import cartography  # noqa: PLC0415

    project = project or QgsProject.instance()
    dem_layer = dem if isinstance(dem, QgsRasterLayer) else QgsRasterLayer(dem, os.path.basename(str(dem)))
    path = dem_layer.source()
    stats = dem_layer.dataProvider().bandStatistics(1)
    if not interval:
        relief = max(stats.maximumValue - stats.minimumValue, 1)
        interval = next(i for i in (0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500) if relief / i <= 40)
    shade = _run("native:hillshade", {"INPUT": path, "Z_FACTOR": 1.5, "AZIMUTH": 315, "V_ANGLE": 45,
                                      "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
    hill = QgsRasterLayer(shade, tr("Hillshade"))
    hill.setOpacity(0.55)
    cont = _run("gdal:contour", {"INPUT": path, "BAND": 1, "INTERVAL": interval, "FIELD_NAME": "elev",
                                 "OUTPUT": "TEMPORARY_OUTPUT"})["OUTPUT"]
    contours = QgsVectorLayer(cont, tr("Contours"), "ogr")
    cartography.style_contours(contours, "elev", interval=interval)
    spots = spot_heights(path, dem_layer.crs())
    dem_layer.setOpacity(0.0 if dem_layer.opacity() == 1 else dem_layer.opacity())
    for lyr in (hill, contours, spots):
        project.addMapLayer(lyr)
    layout = auto_layout.build(project, purpose, layers=[spots, contours, hill], title=title or tr("Topographic map"),
                               lang=lang)
    return layout, {"hillshade": hill, "contours": contours, "spots": spots, "interval": interval}


# ---- subdivision plan --------------------------------------------------------------------------------------
def _side_lines(parcels):
    """Each parcel side as a line with its length (m), for labels along the sides."""
    crs = parcels.crs()
    out = QgsVectorLayer(f"LineString?crs={crs.authid()}", tr("Side lengths"), "memory")
    out.dataProvider().addAttributes([QgsField("length", QMetaType.Type.Double)])
    out.updateFields()
    seen, feats = set(), []
    for f in parcels.getFeatures():
        g = f.geometry()
        for ring in (g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]):
            pts = ring[0] if ring else []
            for a, b in zip(pts, pts[1:]):
                key = tuple(sorted(((round(a.x(), 3), round(a.y(), 3)), (round(b.x(), 3), round(b.y(), 3)))))
                if key in seen or a == b:
                    continue
                seen.add(key)
                nf = QgsFeature(out.fields())
                nf.setGeometry(QgsGeometry.fromPolylineXY([a, b]))
                nf.setAttributes([round(a.distance(b), 2)])
                feats.append(nf)
    out.dataProvider().addFeatures(feats)
    sym = QgsSymbol.defaultSymbol(out.geometryType())
    sym.setOpacity(0)
    out.renderer().setSymbol(sym)
    pal = QgsPalLayerSettings()
    pal.isExpression = True
    pal.fieldName = "format_number(\"length\", 2)"
    pal.placement = QgsPalLayerSettings.Placement.Line
    pal.setFormat(auto_layout._fmt(6))
    out.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    out.setLabelsEnabled(True)
    return out


def _label_layer(layer, expression, size=8, line=False):
    pal = QgsPalLayerSettings()
    pal.isExpression = True
    pal.fieldName = expression
    if line:
        pal.placement = QgsPalLayerSettings.Placement.Line
    fmt = auto_layout._fmt(size)
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.7)
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def _number_field(layer):
    names = [f.name() for f in layer.fields()]
    for cand in ("parcel_no", "plot_no", "PARCEL_NO", "number", "no", "رقم", "رقم_القطعة", "id"):
        if cand in names:
            return cand
    return None


def subdivision_plan(parcels, project=None, number_field=None, streets=None, width_field=None, name_field=None,
                     title=None, lang=None, purpose="wall"):
    """Parcel numbers + areas, side lengths, streets «شارع عرض 15م», and a table of parcel areas."""
    project = project or QgsProject.instance()
    number_field = number_field or _number_field(parcels)
    num = f'"{number_field}"' if number_field else "$id"
    sym = QgsFillSymbol.createSimple({"color": "#FBF7EC", "outline_color": "#333333", "outline_width": "0.35"})
    parcels.setRenderer(QgsSingleSymbolRenderer(sym))
    # area as «600 م²» (or «600.5 م²»); the LRM keeps the number before the unit in a right-to-left line
    _label_layer(parcels, f"{num} || '\\n' || '‎' || regexp_replace(format_number($area, 2), '\\\\.00$', '') "
                          f"|| ' م²'", size=8)
    sides = _side_lines(parcels)
    project.addMapLayer(sides)
    layers = [sides, parcels]
    if streets is not None:
        width = f'"{width_field}"' if width_field else None
        name = f'"{name_field}"' if name_field else "''"
        expr = (f"trim(coalesce({name}, '') || ' ' || 'شارع عرض ' || format_number({width}, 0) || 'م')"
                if width else f"coalesce({name}, 'شارع')")
        _label_layer(streets, expr, size=7.5, line=streets.geometryType() == Qgis.GeometryType.Line)
        layers.insert(0, streets)
    layout = auto_layout.build(project, purpose, layers=layers, title=title or tr("Subdivision plan"), lang=lang)
    _areas_table(layout, parcels, number_field)
    return layout


def _make_room(layout, right_mm):
    """Narrow the main map so a column of right_mm on the right side of the page stays free, and refit it."""
    mp = next((i for i in layout.items() if isinstance(i, QgsLayoutItemMap)), None)
    if mp is None:
        return
    page_w = layout.pageCollection().page(0).pageSize().width()
    r = mp.rect()
    pos = mp.positionWithUnits()
    over = pos.x() + r.width() - (page_w - right_mm)
    if over > 0:
        extent = mp.extent()
        mp.attemptResize(QgsLayoutSize(r.width() - over, r.height()))
        mp.zoomToExtent(extent)
    for item in layout.items():                          # legends/boxes that sat in that column move left of it
        if item is not mp and hasattr(item, "positionWithUnits") and type(item).__name__ in (
                "QgsLayoutItemLegend", "QgsLayoutItemScaleBar"):
            p = item.positionWithUnits()
            if p.x() + item.rect().width() > page_w - right_mm:
                item.attemptMove(QgsLayoutPoint(max(5, page_w - right_mm - item.rect().width() - 4), p.y()))


def _areas_table(layout, parcels, number_field):
    table = QgsLayoutItemAttributeTable.create(layout)
    table.setVectorLayer(parcels)
    cols = table.columns()
    keep = [c for c in cols if c.attribute() == number_field] if number_field else []
    table.setColumns(keep)
    from qgis.core import QgsLayoutTableColumn  # noqa: PLC0415

    area = QgsLayoutTableColumn(tr("Area (m²)"))
    area.setAttribute("round($area, 2)")
    table.setColumns(table.columns() + [area])
    table.setMaximumNumberOfFeatures(60)
    table.setHeaderTextFormat(auto_layout._fmt(7, True))
    table.setContentTextFormat(auto_layout._fmt(7))
    layout.addMultiFrame(table)
    frame = QgsLayoutFrame(layout, table)
    page = layout.pageCollection().page(0).pageSize()
    _make_room(layout, 50)
    frame.attemptMove(QgsLayoutPoint(page.width() - 48, 22))
    frame.attemptResize(QgsLayoutSize(40, min(160, page.height() - 50)))
    table.addFrame(frame)
    return table


# ---- before / after swipe ---------------------------------------------------------------------------------
def _render(layer, extent, crs, width_px):
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setDestinationCrs(crs)
    ms.setExtent(extent)
    height_px = max(1, int(width_px * extent.height() / max(extent.width(), 1e-9)))
    ms.setOutputSize(QSize(width_px, height_px))
    ms.setBackgroundColor(QColor("white"))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def swipe_page(before, after, path, title="", width_px=1600, labels=None):
    """One self-contained HTML page with a slider that wipes between two rasters (before / after).
    Works offline: both images are embedded."""
    import base64  # noqa: PLC0415

    from qgis.PyQt.QtCore import QBuffer, QIODevice  # noqa: PLC0415

    crs = before.crs()
    ext = before.extent()
    if after.crs() != crs:
        ext2 = QgsCoordinateTransform(after.crs(), crs, QgsProject.instance()).transformBoundingBox(after.extent())
    else:
        ext2 = after.extent()
    ext = ext.intersect(ext2) if ext.intersects(ext2) else ext
    imgs = []
    for lyr in (before, after):
        img = _render(lyr, ext, crs, width_px)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "JPG", 88)
        imgs.append(base64.b64encode(bytes(buf.data())).decode("ascii"))
    a, b = labels or (before.name(), after.name())
    page = f"""<!doctype html><html lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title or "Before / after")}</title>
<style>body{{margin:0;font-family:Segoe UI,Tahoma,sans-serif;background:#111;color:#eee}}
h1{{font-size:18px;margin:10px 14px}}.w{{position:relative;max-width:100%;margin:0 auto;user-select:none}}
.w img{{display:block;width:100%}}.top{{position:absolute;inset:0;overflow:hidden}}
.bar{{position:absolute;top:0;bottom:0;width:3px;background:#fff;box-shadow:0 0 6px #000}}
.t{{position:absolute;top:8px;padding:3px 8px;background:#0009;border-radius:4px;font-size:13px}}
input{{width:100%;margin:8px 0}}</style></head><body><h1>{html.escape(title or "")}</h1>
<div class="w" id="w"><img src="data:image/jpeg;base64,{imgs[1]}" alt="">
<div class="top" id="top"><img src="data:image/jpeg;base64,{imgs[0]}" alt=""></div>
<div class="bar" id="bar"></div><span class="t" style="left:8px">{html.escape(a)}</span>
<span class="t" style="right:8px">{html.escape(b)}</span></div>
<input type="range" id="r" min="0" max="100" value="50">
<script>var r=document.getElementById('r'),t=document.getElementById('top'),bar=document.getElementById('bar');
function u(){{t.style.clipPath='inset(0 '+(100-r.value)+'% 0 0)';bar.style.left=r.value+'%';}}
r.oninput=u;document.getElementById('w').onmousemove=function(e){{if(e.buttons){{var b=this.getBoundingClientRect();
r.value=Math.max(0,Math.min(100,(e.clientX-b.left)/b.width*100));u();}}}};u();</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


# ---- statistical atlas -------------------------------------------------------------------------------------
def bar_chart(values, labels, highlight, path, size=(900, 520), title=""):
    """A simple bar chart PNG; the bar at index ``highlight`` is drawn in the accent colour."""
    w, h = size
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont("Segoe UI", 16)
    p.setFont(font)
    top = max(max(values), 1e-9)
    n = len(values)
    left, bottom, ch = 40, 70, h - 120
    bw = (w - left - 20) / max(n, 1)
    if title:
        p.drawText(0, 10, w, 30, Qt.AlignmentFlag.AlignHCenter, title)
    for i, (v, lab) in enumerate(zip(values, labels)):
        bh = ch * max(v, 0) / top
        x = left + i * bw
        p.fillRect(int(x + bw * 0.12), int(h - bottom - bh), int(bw * 0.76), int(bh),
                   QColor("#D55E00" if i == highlight else "#9DB4C8"))
        if n <= 24:
            p.save()
            p.translate(x + bw / 2, h - bottom + 8)
            p.rotate(35)
            p.drawText(0, 0, 160, 24, Qt.AlignmentFlag.AlignLeft, str(lab)[:18])
            p.restore()
    p.drawLine(left, h - bottom, w - 20, h - bottom)
    p.end()
    img.save(path)
    return path


def statistical_atlas(districts, name_field, fields, pdf_path, project=None, title=None, lang=None):
    """A PDF atlas: one page per district with its map, a bar chart comparing every district on the first field
    (this district highlighted) and a table of its values. Returns the PDF path."""
    project = project or QgsProject.instance()
    folder = os.path.splitext(pdf_path)[0] + "_charts"
    os.makedirs(folder, exist_ok=True)
    feats = list(districts.getFeatures())
    first = fields[0]

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    vals = [num(f[first]) for f in feats]
    names = [str(f[name_field]) for f in feats]
    for i, f in enumerate(feats):
        bar_chart(vals, names, i, os.path.join(folder, f"chart_{f.id()}.png"), title=first)
    layout = auto_layout.build(project, "report", layers=[districts], title=title or tr("Statistical atlas"),
                               lang=lang, add_to_manager=False)
    mp = next(i for i in layout.items() if isinstance(i, QgsLayoutItemMap))
    page = layout.pageCollection().page(0).pageSize()
    # map in the top part, chart and table below
    mp.attemptResize(QgsLayoutSize(mp.rect().width(), page.height() * 0.48))
    mp.setAtlasDriven(True)
    mp.setAtlasScalingMode(QgsLayoutItemMap.AtlasScalingMode.Auto)
    mp.setAtlasMargin(0.15)
    y = mp.pos().y() + page.height() * 0.5
    pic = QgsLayoutItemPicture(layout)
    pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
    folder_expr = folder.replace("\\", "/").replace("'", "''")
    pic.dataDefinedProperties().setProperty(
        QgsLayoutObject.DataDefinedProperty.PictureSource,
        QgsProperty.fromExpression(f"'{folder_expr}/chart_' || @atlas_featureid || '.png'"))
    layout.addLayoutItem(pic)
    pic.attemptMove(QgsLayoutPoint(10, y))
    pic.attemptResize(QgsLayoutSize(page.width() * 0.55, page.height() * 0.32))
    rows = "<br>".join(f"<b>{html.escape(fld)}</b>: [% \"{fld}\" %]" for fld in fields)
    lab = QgsLayoutItemLabel(layout)
    lab.setMode(QgsLayoutItemLabel.Mode.ModeHtml)
    lab.setText(f"<div style='font-family:Segoe UI;font-size:11pt'><h3>[% \"{name_field}\" %]</h3>{rows}</div>")
    layout.addLayoutItem(lab)
    lab.attemptMove(QgsLayoutPoint(page.width() * 0.6, y))
    lab.attemptResize(QgsLayoutSize(page.width() * 0.36, page.height() * 0.32))
    atlas = layout.atlas()
    atlas.setCoverageLayer(districts)
    atlas.setEnabled(True)
    atlas.setPageNameExpression(f'"{name_field}"')
    settings = QgsLayoutExporter.PdfExportSettings()
    res, err = QgsLayoutExporter.exportToPdf(atlas, pdf_path, settings)
    if res != QgsLayoutExporter.ExportResult.Success:
        raise RuntimeError(err or "atlas export failed")
    return pdf_path


PRODUCTS = {
    "land_use": "Land-use map",
    "topographic": "Topographic map",
    "subdivision": "Subdivision plan",
    "swipe": "Before / after comparison (web page)",
    "stat_atlas": "Statistical atlas (one page per district)",
}
