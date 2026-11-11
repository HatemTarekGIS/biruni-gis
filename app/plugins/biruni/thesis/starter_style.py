"""Styles of the starter pack layers (see starter.py)."""
from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsColorRampShader, QgsFillSymbol, QgsLineSymbol,
                       QgsMarkerSymbol, QgsPalLayerSettings, QgsProperty, QgsRasterShader, QgsRendererCategory,
                       QgsRuleBasedRenderer, QgsSingleBandPseudoColorRenderer, QgsSingleSymbolRenderer,
                       QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor, QPainter

from ..i18n import tr

# order inside the layer tree (top first)
PRIORITY = ["area", "region", "admin2", "admin1", "country", "places", "roads", "waterways", "water", "buildings",
            "landuse", "contours", "hillshade", "dem", "slope", "aspect", "landcover", "table", "rgb", "ndvi",
            "population"]

ROADS = [  # (highway values, colour, width mm, label)
    (("motorway", "motorway_link", "trunk", "trunk_link"), "#E8505B", 1.1, "Motorway / trunk"),
    (("primary", "primary_link"), "#F28E2B", 0.9, "Primary"),
    (("secondary", "secondary_link"), "#F6C141", 0.7, "Secondary"),
    (("tertiary", "tertiary_link"), "#9C9C9C", 0.5, "Tertiary"),
    (("residential", "living_street", "unclassified", "service", "road"), "#B5B5B5", 0.3, "Local streets"),
    (("track", "path", "footway", "cycleway", "pedestrian", "steps", "bridleway"), "#A0896E", 0.2, "Tracks and paths"),
]
LANDUSE = {"residential": "#E8D8C8", "commercial": "#F3C9C9", "retail": "#F3C9C9", "industrial": "#D9CBE3",
           "farmland": "#EEF0B4", "farmyard": "#E8D9A8", "orchard": "#B8DC9A", "meadow": "#CDEBB0",
           "grass": "#CDEBB0", "forest": "#9CCB8A", "cemetery": "#B9D5B3", "construction": "#D6D1BE",
           "military": "#E7C0B8", "religious": "#E3D5C0", "railway": "#D9D2DC", "quarry": "#C9C3BA",
           "brownfield": "#D6CDB8", "greenfield": "#E2EBC5", "recreation_ground": "#D3F0C8", "park": "#C8EBB8"}


def _fill(color, outline, width=0.26, style="solid"):
    return QgsFillSymbol.createSimple({"color": color, "style": style, "outline_color": outline,
                                       "outline_width": str(width)})


def _labels(lyr, expr, size=7, color="#333333", buffer=True, max_scale=0):
    from .. import mapsheet

    pal = QgsPalLayerSettings()
    pal.fieldName = expr
    pal.isExpression = True
    fmt = mapsheet._text_format(size, False, color)
    if buffer:
        b = fmt.buffer()
        b.setEnabled(True)
        b.setSize(0.7)
        b.setColor(QColor("#FFFFFF"))
        fmt.setBuffer(b)
    pal.setFormat(fmt)
    if max_scale:
        pal.scaleVisibility = True
        pal.minimumScale = max_scale  # visible when zoomed in beyond 1:max_scale
        pal.maximumScale = 0
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    lyr.setLabelsEnabled(True)
    return pal


def _pseudo(lyr, items, discrete=False):
    fn = QgsColorRampShader(items[0][0], items[-1][0])
    fn.setColorRampType(QgsColorRampShader.Type.Discrete if discrete else QgsColorRampShader.Type.Interpolated)
    fn.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), lab) for v, c, lab in items])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fn)
    r = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)
    r.setClassificationMin(items[0][0])
    r.setClassificationMax(items[-1][0])
    lyr.setRenderer(r)


def apply(lyr, d, state):
    s = d["style"]
    stats = state.get("stats", {})
    if s == "area":
        lyr.setRenderer(QgsSingleSymbolRenderer(_fill("0,0,0,0", "#D7191C", 0.8, "no")))
    elif s == "country":
        lyr.setRenderer(QgsSingleSymbolRenderer(_fill("#F2EFE9", "#4D4D4D", 0.4)))
    elif s == "admin1":
        sym = _fill("0,0,0,0", "#7F7F7F", 0.2, "no")
        sym.symbolLayer(0).setStrokeStyle(Qt_dash())
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        name = "shapeName" if "shapeName" in lyr.fields().names() else "name"
        _labels(lyr, f'"{name}"', 6, "#555555")
    elif s == "region":
        lyr.setRenderer(QgsSingleSymbolRenderer(_fill("#FDD49E", "#B30000", 0.5)))
    elif s == "admin2":
        sym = _fill("0,0,0,0", "#6A51A3", 0.35, "no")
        sym.symbolLayer(0).setStrokeStyle(Qt_dash())
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        name = "shapeName" if "shapeName" in lyr.fields().names() else "name"
        _labels(lyr, f'"{name}"', 7, "#3F007D")
    elif s == "roads":
        cats = []
        for values, color, width, label in ROADS:
            for v in values:
                sym = QgsLineSymbol.createSimple({"line_color": color, "line_width": str(width),
                                                  "capstyle": "round", "joinstyle": "round"})
                cats.append(QgsRendererCategory(v, sym, f"{tr(label)} ({v})", True))
        other = QgsLineSymbol.createSimple({"line_color": "#C8C8C8", "line_width": "0.2"})
        cats.append(QgsRendererCategory(None, other, tr("Other"), True))
        lyr.setRenderer(QgsCategorizedSymbolRenderer("highway", cats))
        _labels(lyr, 'coalesce("name", "name_en")', 6, "#404040", max_scale=25000)
        pal = lyr.labeling().settings()
        pal.placement = Qgis.LabelPlacement.Line
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    elif s == "buildings":
        lyr.setRenderer(QgsSingleSymbolRenderer(_fill("#CDBFB2", "#8C7B6B", 0.1)))
    elif s == "landuse":
        cats = [QgsRendererCategory(k, _fill(c, "#FFFFFF", 0.1), k, True) for k, c in LANDUSE.items()
                if k != "park"]
        cats.append(QgsRendererCategory(None, _fill("#E6E6E6", "#FFFFFF", 0.1), tr("Other"), True))
        lyr.setRenderer(QgsCategorizedSymbolRenderer('coalesce("landuse", "leisure")', cats))
        lyr.setOpacity(0.75)
    elif s == "water":
        lyr.setRenderer(QgsSingleSymbolRenderer(_fill("#A6CEE3", "#4A90D9", 0.2)))
    elif s == "waterways":
        lyr.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({"line_color": "#4A90D9",
                                                                            "line_width": "0.4"})))
    elif s == "places":
        lyr.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "#E6550D", "outline_color": "#FFFFFF", "size": "1.8"})))
        _labels(lyr, 'coalesce("name", "name_en")', 6, "#7F2704", max_scale=15000)
    elif s == "contours":
        interval = float(d.get("interval") or stats.get("contour_interval") or 10)
        major = interval * 5
        root = QgsRuleBasedRenderer.Rule(None)
        for expr, color, width, label in (
                (f'abs("elev" / {major} - round("elev" / {major})) < 1e-6', "#8C510A", 0.35,
                 tr("Index contour ({i} m)").format(i=f"{major:g}")),
                ("ELSE", "#BF812D", 0.15, tr("Contour ({i} m)").format(i=f"{interval:g}"))):
            sym = QgsLineSymbol.createSimple({"line_color": color, "line_width": str(width)})
            rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, None if expr == "ELSE" else expr, label)
            if expr == "ELSE":
                rule.setIsElse(True)
            root.appendChild(rule)
        lyr.setRenderer(QgsRuleBasedRenderer(root))
        pal = _labels(lyr, 'format_number("elev", 0)', 6, "#8C510A")
        pal.placement = Qgis.LabelPlacement.Line
        props = pal.dataDefinedProperties()
        props.setProperty(QgsPalLayerSettings.Property.Show,
                          QgsProperty.fromExpression(f'abs("elev" / {major} - round("elev" / {major})) < 1e-6'))
        pal.setDataDefinedProperties(props)
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    elif s == "dem":
        from ..algorithms.terrain_style import style_terrain

        bare = sum(r["percent"] for r in stats.get("landcover", []) if r["code"] == 60)
        style_terrain(lyr, preset=1 if bare > 50 else 0, classes=10, smooth=True, hillshade=False)
    elif s == "hillshade":
        lyr.setOpacity(0.55)
        lyr.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)
    elif s == "slope":
        top = max(5.0, float(stats.get("slope_max", 45)))
        _pseudo(lyr, [(0, "#1A9641", "0°"), (2, "#A6D96A", "2°"), (5, "#FFFFBF", "5°"), (15, "#FDAE61", "15°"),
                      (max(30, top), "#D7191C", f"{max(30, top):.0f}°")])
    elif s == "aspect":
        dirs = [(22.5, "#E31A1C", "N"), (67.5, "#FF7F00", "NE"), (112.5, "#FFFF33", "E"), (157.5, "#4DAF4A", "SE"),
                (202.5, "#00CED1", "S"), (247.5, "#377EB8", "SW"), (292.5, "#984EA3", "W"),
                (337.5, "#F781BF", "NW"), (360, "#E31A1C", "N")]
        _pseudo(lyr, [(v, c, tr(lab)) for v, c, lab in dirs], discrete=True)
    elif s == "population":
        import numpy as np

        from .starter import raster_values

        a, _px, _gt = raster_values(lyr.source())
        v = a.compressed()
        v = v[v > 0]
        top = float(np.percentile(v, 98)) if v.size else 10.0
        top = max(top, 1.0)
        _pseudo(lyr, [(0.0, "#FFFFCC", "0"), (top * 0.25, "#FED976", f"{top * 0.25:.0f}"),
                      (top * 0.5, "#FD8D3C", f"{top * 0.5:.0f}"), (top, "#BD0026", f"≥ {top:.0f}")])
    # landcover, ndvi: default .qml written next to the file; rgb: 8-bit, already stretched
    if s == "rgb":
        from qgis.core import QgsContrastEnhancement

        lyr.setContrastEnhancement(QgsContrastEnhancement.ContrastEnhancementAlgorithm.NoEnhancement)


def Qt_dash():  # noqa: N802
    from qgis.PyQt.QtCore import Qt

    return Qt.PenStyle.DashLine
