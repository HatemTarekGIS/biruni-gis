import math
import warnings

from qgis.core import (Qgis, QgsColorRampShader, QgsHillshadeRenderer, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterLayer, QgsProcessingOutputString, QgsProject, QgsRasterLayer,
                       QgsRasterShader, QgsSingleBandPseudoColorRenderer)
from qgis.PyQt.QtGui import QColor, QPainter

from ..i18n import tr
from .base import BiruniAlgorithm

# relative position 0..1 on land (sea level → highest point) → colour
TERRAIN_PRESETS = [
    ("Classic (green, brown, white peaks)", [(0.0, "#5E9E5A"), (0.12, "#9CC27A"), (0.3, "#E8DC8C"),
                                              (0.5, "#D2A266"), (0.72, "#A26A42"), (0.88, "#8E7F78"),
                                              (1.0, "#F7F5F2")]),
    ("Desert (Arabian sand and rock)", [(0.0, "#F3E6C4"), (0.2, "#E9CF98"), (0.4, "#D9AF6F"), (0.6, "#BF8A52"),
                                         (0.8, "#8F6142"), (0.93, "#6E5345"), (1.0, "#D8D2CC")]),
    ("Sea and land (strong depths)", [(0.0, "#7FB77E"), (0.25, "#CFD9A0"), (0.55, "#D7B98A"), (0.8, "#A8846A"),
                                       (1.0, "#F2EFEA")]),
]
# depth 0..1 (deepest → sea level)
SEA = {0: [(0.0, "#2C5A8C"), (0.6, "#6FA3D2"), (1.0, "#C6E2F4")],
       1: [(0.0, "#3D6C8E"), (0.6, "#86B3CF"), (1.0, "#CFE6F1")],
       2: [(0.0, "#08306B"), (0.3, "#2171B5"), (0.6, "#4292C6"), (0.85, "#9ECAE1"), (1.0, "#DEEBF7")]}


def nice_step(span, n):
    """A round class width (1, 2, 2.5, 5 × 10^k) giving about n classes over span."""
    raw = max(span, 1e-9) / max(n, 1)
    k = 10 ** math.floor(math.log10(raw))
    return min((m * k for m in (1, 2, 2.5, 5, 10)), key=lambda s: abs(math.log(s / raw)))


def nice_breaks(zmin, zmax, n=10):
    """Round elevation breaks covering zmin..zmax; 0 (sea level) is always a break when the range crosses it."""
    step = nice_step(zmax - zmin, n)
    lo = math.floor(zmin / step) * step
    out = []
    v = lo
    while v < zmax + step * 0.999:
        out.append(round(v, 6))
        v += step
    return out


def _interp(stops, t):
    t = min(max(t, 0.0), 1.0)
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = 0.0 if b == a else (t - a) / (b - a)
            c1, c2 = QColor(ca), QColor(cb)
            return QColor(round(c1.red() + (c2.red() - c1.red()) * f), round(c1.green() + (c2.green() - c1.green()) * f),
                          round(c1.blue() + (c2.blue() - c1.blue()) * f))
    return QColor(stops[-1][1])


def tint_items(zmin, zmax, preset=0, n=10, smooth=False):
    """Colour-ramp items for a hypsometric tint: blues below sea level, the preset's colours above."""
    land, sea = TERRAIN_PRESETS[preset][1], SEA[preset]
    breaks = nice_breaks(zmin, zmax, n)
    if zmin < 0 < zmax and 0 not in breaks:
        breaks = sorted(set(breaks) | {0.0})
    lo_land = 0.0 if zmin < 0 else breaks[0]
    items = []
    for i, b in enumerate(breaks[1:] if not smooth else breaks):
        mid = b if smooth else (breaks[i] + b) / 2  # a stepped class takes the colour of its middle
        if mid < 0:
            col = _interp(sea, 1 - mid / zmin if zmin < 0 else 1)
        else:
            col = _interp(land, (mid - lo_land) / max(zmax - lo_land, 1e-9))
        prev = breaks[i] if not smooth else None
        label = f"{prev:g} – {b:g}" if prev is not None else f"{b:g}"
        items.append(QgsColorRampShader.ColorRampItem(b, col, label))
    return items, breaks


def style_terrain(layer, preset=0, classes=10, smooth=False, hillshade=True, opacity=0.55, project=None):
    """Hypsometric tint on a DEM layer and, when asked, a multidirectional hillshade layer laid over it with
    'multiply'. Returns the hillshade layer (or None)."""
    prov = layer.dataProvider()
    with warnings.catch_warnings():  # the binding flags its own enum as deprecated
        warnings.simplefilter("ignore", DeprecationWarning)
        stats = prov.bandStatistics(1, Qgis.RasterBandStatistic.All, layer.extent(), 250000)
    zmin, zmax = stats.minimumValue, stats.maximumValue
    if not (math.isfinite(zmin) and math.isfinite(zmax)) or zmax <= zmin:
        raise ValueError(tr("The elevation layer has no usable values."))
    items, _breaks = tint_items(zmin, zmax, preset, classes, smooth)
    fn = QgsColorRampShader(zmin, zmax)
    fn.setColorRampType(QgsColorRampShader.Type.Interpolated if smooth else QgsColorRampShader.Type.Discrete)
    fn.setColorRampItemList(items)
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(fn)
    r = QgsSingleBandPseudoColorRenderer(prov, 1, shader)
    r.setClassificationMin(zmin)
    r.setClassificationMax(zmax)
    layer.setRenderer(r)
    layer.triggerRepaint()
    if not hillshade:
        return None
    project = project or QgsProject.instance()
    shade = QgsRasterLayer(layer.source(), tr("Hillshade of {n}").format(n=layer.name()), layer.providerType())
    hr = QgsHillshadeRenderer(shade.dataProvider(), 1, 315.0, 45.0)
    hr.setMultiDirectional(True)
    z = 1.0
    if layer.crs().isGeographic():  # degrees across, metres up
        lat = layer.extent().center().y()
        z = 1.0 / (111320.0 * max(math.cos(math.radians(lat)), 0.05))
    hr.setZFactor(z)
    shade.setRenderer(hr)
    shade.setOpacity(opacity)
    shade.setBlendMode(QPainter.CompositionMode.CompositionMode_Multiply)
    project.addMapLayer(shade, False)
    root = project.layerTreeRoot()
    node = root.findLayer(layer.id())
    parent = node.parent() if node else root
    index = parent.children().index(node) if node else 0
    parent.insertLayer(index, shade)  # just above the DEM
    return shade


class TerrainStyle(BiruniAlgorithm):
    """One click: hypsometric tint + multidirectional hillshade."""

    ID = "terrainstyle"
    NAME = "Terrain colours and hillshade"
    GROUP = "Raster & terrain"
    GROUP_ID = "raster"
    HELP = ("Colours an elevation model (DEM) by height with round class limits (for example every 50 m) and lays "
            "a soft multidirectional hillshade over it, so the relief reads at once. Choose the colours: classic "
            "(green lowlands, brown mountains, white peaks), desert (sand and rock tones that suit the Arabian "
            "Peninsula and Egypt) or sea and land (stronger blues for depths below sea level). Areas below sea "
            "level are always blue. The DEM layer itself is restyled; the hillshade is added as a new layer "
            "right above it.")

    INPUT, PRESET, CLASSES, SMOOTH, HILLSHADE, OPACITY = "INPUT", "PRESET", "CLASSES", "SMOOTH", "HILLSHADE", "OPACITY"

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Elevation (DEM)")))
        self.addParameter(QgsProcessingParameterEnum(self.PRESET, tr("Colours"),
                                                     [tr(p[0]) for p in TERRAIN_PRESETS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(self.CLASSES, tr("About how many height classes"),
                                                       Qgis.ProcessingNumberParameterType.Integer, 10, minValue=3,
                                                       maxValue=40))
        self.addParameter(QgsProcessingParameterBoolean(self.SMOOTH, tr("Smooth colours instead of steps"), False))
        self.addParameter(QgsProcessingParameterBoolean(self.HILLSHADE, tr("Add a hillshade"), True))
        self.addParameter(QgsProcessingParameterNumber(self.OPACITY, tr("Hillshade strength (0–1)"),
                                                       Qgis.ProcessingNumberParameterType.Double, 0.55, minValue=0.0,
                                                       maxValue=1.0))
        self.addOutput(QgsProcessingOutputString("HILLSHADE_LAYER", tr("Hillshade layer id")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layer = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(tr("Choose an elevation layer."))
        project = context.project() or QgsProject.instance()
        if project.mapLayer(layer.id()) is None:  # a file path: load it so the styling is visible
            layer = QgsRasterLayer(layer.source(), layer.name())
            project.addMapLayer(layer)
        try:
            shade = style_terrain(layer, self.parameterAsEnum(parameters, self.PRESET, context),
                                  self.parameterAsInt(parameters, self.CLASSES, context),
                                  self.parameterAsBool(parameters, self.SMOOTH, context),
                                  self.parameterAsBool(parameters, self.HILLSHADE, context),
                                  self.parameterAsDouble(parameters, self.OPACITY, context), project)
        except ValueError as e:
            raise QgsProcessingException(str(e)) from e
        feedback.pushInfo(tr("Styled {n}.").format(n=layer.name()))
        return {"HILLSHADE_LAYER": shade.id() if shade else ""}
