"""Map styles for the indoor layers, and a PNG preview of one level (used by the tests)."""
from qgis.core import (QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
                       QgsPalLayerSettings, QgsRendererCategory, QgsSingleSymbolRenderer, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor

UNIT_COLORS = {"room": "#f2e6d0", "office": "#cfe3f7", "classroom": "#d8ecc8", "lab": "#e6d4f2",
               "corridor": "#f7f7f2", "lobby": "#fbe7b5", "stairs": "#f4b9a6", "elevator": "#f59c8c",
               "ramp": "#f2c8a0", "restroom": "#b8e3e9", "kitchen": "#f7d9c4", "storage": "#ddd6cc",
               "parking": "#d0d4da", "shaft": "#9e9e9e", "other": "#ececec"}
OPENING_COLORS = {"door": "#8a5a2b", "double_door": "#8a5a2b", "sliding_door": "#4f7fa8", "opening": "#9a9a9a",
                  "emergency_exit": "#d0312d"}
EDGE_COLORS = {"walk": "#5c8d3a", "door": "#5c8d3a", "stairs": "#d0312d", "elevator": "#2b6cb0", "ramp": "#c77c1e"}


def _labels(layer, expression, size=9.0, color="#333333"):
    s = QgsPalLayerSettings()
    s.fieldName = expression
    s.isExpression = True
    fmt = QgsTextFormat()
    fmt.setSize(size)
    fmt.setColor(QColor(color))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor("#ffffff"))
    fmt.setBuffer(buf)
    s.setFormat(fmt)
    try:
        s.placement = QgsPalLayerSettings.Placement.OverPoint
    except AttributeError:
        pass
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)


def _categorized(field, colors, make):
    cats = [QgsRendererCategory(k, make(c), k) for k, c in colors.items()]
    return QgsCategorizedSymbolRenderer(field, cats)


def style_layer(layer, kind):
    """kind: levels, units, openings, nav_nodes, nav_edges, route."""
    if kind == "levels":
        layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
            {"color": "#6e6e6e", "outline_color": "#3c3c3c", "outline_width": "0.5"})))
    elif kind == "units":
        layer.setRenderer(_categorized("category", UNIT_COLORS, lambda c: QgsFillSymbol.createSimple(
            {"color": c, "outline_color": "#5a5a5a", "outline_width": "0.2"})))
        _labels(layer, "trim(coalesce(\"name\", '') || '\\n' || coalesce(\"number\", ''))")
    elif kind == "openings":
        layer.setRenderer(_categorized("category", OPENING_COLORS, lambda c: QgsLineSymbol.createSimple(
            {"line_color": c, "line_width": "0.9", "capstyle": "flat"})))
    elif kind == "nav_nodes":
        layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "#2b6cb0", "outline_color": "#ffffff", "size": "1.4"})))
    elif kind == "nav_edges":
        layer.setRenderer(_categorized("kind", EDGE_COLORS, lambda c: QgsLineSymbol.createSimple(
            {"line_color": c, "line_width": "0.25"})))
    elif kind == "route":
        layer.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple(
            {"line_color": "#e5007d", "line_width": "1.2", "capstyle": "round", "joinstyle": "round"})))
    layer.triggerRepaint()


def level_layers(gpkg, level_id, names=None):
    """Styled layers of one level, filtered from indoor.gpkg (not added to any project)."""
    out = []
    for kind in ("levels", "units", "openings", "nav_edges", "nav_nodes"):
        # the filter goes in the source (|subset=): setSubsetString() on a fresh GeoPackage layer makes OGR
        # open a second connection that fails ("unable to open database file") in QGIS 4.2
        lyr = QgsVectorLayer(f'{gpkg}|layername={kind}|subset="level_id" = {int(level_id)}',
                             (names or {}).get(kind, kind), "ogr")
        style_layer(lyr, kind)
        out.append(lyr)
    return out


def render_png(gpkg, level_id, png, route=None, size=(1600, 1000)):
    """A picture of one level: units, doors, the navigation graph and an optional route (find_route dict)."""
    from qgis.core import (QgsFeature, QgsGeometry, QgsMapRendererCustomPainterJob, QgsMapSettings, QgsPointXY)
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QImage, QPainter

    layers = level_layers(gpkg, level_id)
    extra = []
    if route:
        crs = layers[1].crs().authid()
        rl = QgsVectorLayer(f"LineString?crs={crs}", "route", "memory")
        feats = []
        for st in route["steps"]:
            if st["level_id"] in (level_id,):
                f = QgsFeature()
                f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in st["pts"]]))
                feats.append(f)
        rl.dataProvider().addFeatures(feats)
        style_layer(rl, "route")
        extra.append(rl)
    ms = QgsMapSettings()
    ms.setLayers(extra + list(reversed(layers)))
    ms.setDestinationCrs(layers[1].crs())
    ext = layers[0].extent()
    ext.grow(1.0)
    ms.setOutputSize(QSize(*size))
    ms.setExtent(ext)
    ms.setBackgroundColor(QColor("white"))
    img = QImage(QSize(*size), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    job = QgsMapRendererCustomPainterJob(ms, p)
    job.renderSynchronously()
    p.end()
    img.save(png)
    return png
