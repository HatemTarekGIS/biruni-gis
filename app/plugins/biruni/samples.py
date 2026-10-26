"""Sample data: the bundled world (countries, cities, rivers, lakes), styled and
labelled, for a first look, lessons and tests. Natural Earth, public domain."""
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
                       QgsPalLayerSettings, QgsProject, QgsProperty, QgsSymbolLayer, QgsTextBufferSettings,
                       QgsTextFormat, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor, QFont

from . import i18n
from .i18n import tr
from .places import WORLD, world_layer

GROUP = "Sample - World"
SAMPLE_POINTS = os.path.join(os.path.dirname(__file__), "resources", "data", "sample_points.xlsx")


def _labels(layer, field_expr, size=8, color="#1E252B", scale_max=None):
    pal = QgsPalLayerSettings()
    pal.fieldName = field_expr
    pal.isExpression = True
    fmt = QgsTextFormat()
    fmt.setFont(QFont("Segoe UI"))
    fmt.setSize(size)
    fmt.setColor(QColor(color))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor("#ffffff"))
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    if scale_max:
        pal.scaleVisibility = True
        pal.minimumScale = scale_max
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def load_world(plugin=None, zoom=True):
    """Add the sample world group (once) and return {key: layer}."""
    prj = QgsProject.instance()
    root = prj.layerTreeRoot()
    title = tr(GROUP)
    existing = root.findGroup(title)
    if existing is not None:
        return {lyr.layer().name(): lyr.layer() for lyr in existing.findLayers()}
    if not os.path.exists(WORLD):
        return {}
    first = not prj.mapLayers()
    if first:
        prj.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    name_expr = "coalesce(\"name_ar\", \"name\")" if i18n.lang() == "ar" else "\"name\""
    countries = world_layer("countries", tr("Countries"))
    countries.setRenderer(countries.renderer().clone())
    countries.renderer().setSymbol(QgsFillSymbol.createSimple(
        {"color": "#E9EFE6", "outline_color": "#8A9A86", "outline_width": "0.2"}))
    _labels(countries, name_expr, 8, "#4A5A48")
    lakes = world_layer("lakes", tr("Lakes"))
    lakes.renderer().setSymbol(QgsFillSymbol.createSimple({"color": "#A9CCE3", "outline_style": "no"}))
    rivers = world_layer("rivers", tr("Rivers"))
    rivers.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "#5D9BC6", "width": "0.3"}))
    cities = world_layer("cities", tr("Cities"))
    sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "#D64545", "outline_color": "#ffffff",
                                        "outline_width": "0.2"})
    sym.symbolLayer(0).setDataDefinedProperty(
        QgsSymbolLayer.Property.Size, QgsProperty.fromExpression("scale_linear(\"pop_max\", 0, 20000000, 1.2, 4.5)"))
    cities.renderer().setSymbol(sym)
    _labels(cities, "\"name\"", 7, "#1E252B", scale_max=40000000)
    group = root.insertGroup(0, title)
    for lyr in (cities, rivers, lakes, countries):
        prj.addMapLayer(lyr, False)
        group.addLayer(lyr)
    if zoom and plugin is not None:
        canvas = plugin.iface.mapCanvas()
        from qgis.core import QgsCoordinateTransform

        t = QgsCoordinateTransform(countries.crs(), canvas.mapSettings().destinationCrs(), prj)
        canvas.setExtent(t.transformBoundingBox(countries.extent()))
        canvas.refresh()
    return {"countries": countries, "cities": cities, "rivers": rivers, "lakes": lakes}
