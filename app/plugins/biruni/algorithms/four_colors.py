import heapq

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsFeature, QgsFeatureSink, QgsField, QgsFields,
                       QgsFillSymbol, QgsGeometry, QgsProcessingException, QgsProcessingLayerPostProcessorInterface,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber, QgsRendererCategory,
                       QgsSettings, QgsSpatialIndex)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

# soft colours that print well and keep labels readable; the first four are used most
PALETTE = ["#8FB9D9", "#F2C57C", "#A8D5A2", "#E8A3A3", "#C3B1E1", "#F6E39B", "#9ED9D3", "#D9B48F"]
FIELD = "color_id"
EPS = 1e-9


def is_dark():
    """The Biruni theme in use (light/dark/system), without needing the plugin object."""
    mode = QgsSettings().value("biruni/theme", "system")
    if mode in ("dark", "light"):
        return mode == "dark"
    try:
        from ..theme import system_is_dark

        return system_is_dark()
    except Exception:
        return False


def coloring_renderer(ids, dark=None):
    """Categorized renderer: one soft colour per colour id."""
    dark = is_dark() if dark is None else dark
    outline = "#2B3138" if dark else "#FFFFFF"
    cats = []
    for k, cid in enumerate(sorted(ids)):
        sym = QgsFillSymbol.createSimple({"color": PALETTE[k % len(PALETTE)], "outline_color": outline,
                                          "outline_width": "0.3", "outline_width_unit": "MM"})
        cats.append(QgsRendererCategory(cid, sym, tr("Colour {n}").format(n=k + 1)))
    return QgsCategorizedSymbolRenderer(FIELD, cats)


def color_graph(neighbours, n_colors, weight=None):
    """Colour the nodes of a graph so that neighbours differ, with ``n_colors`` colours where possible.

    DSATUR (the most constrained node first, kept in a heap) takes the least used allowed colour - by
    ``weight``: count or area - which balances the colours. A node that finds no free colour gets one anyway
    when a Kempe-chain swap of two colours among its neighbours frees it (the classic four-colour repair);
    only when that fails is an extra colour added. Returns {node: colour index from 0}."""
    weight = weight or {}
    k = max(1, n_colors)
    colors = {}
    used = {}
    sat = {v: set() for v in neighbours}
    heap = [(0, -len(neighbours[v]), i, v) for i, v in enumerate(neighbours)]
    heapq.heapify(heap)
    order = {v: i for i, v in enumerate(neighbours)}
    while heap:
        _, _, _, v = heapq.heappop(heap)
        if v in colors:
            continue
        free = [c for c in range(k) if c not in sat[v]]
        if not free:
            c = _kempe_free(v, neighbours, colors, k)
            if c is not None:
                free = [c]
                _resaturate(neighbours, colors, sat)  # the swap changed colours around
        if free:
            c = min(free, key=lambda x: (used.get(x, 0.0), x))
        else:
            c = k
            while c in sat[v]:
                c += 1
        colors[v] = c
        used[c] = used.get(c, 0.0) + weight.get(v, 1.0)
        for u in neighbours[v]:
            if u not in colors and c not in sat[u]:
                sat[u].add(c)
                heapq.heappush(heap, (-len(sat[u]), -len(neighbours[u]), order[u], u))
    return colors


def _resaturate(neighbours, colors, sat):
    for v in sat:
        if v not in colors:
            sat[v] = {colors[u] for u in neighbours[v] if u in colors}


def _kempe_free(v, neighbours, colors, k):
    """Free one of the first ``k`` colours for ``v`` by swapping two colours along a Kempe chain; the colour
    freed, or None."""
    nbr = [u for u in neighbours[v] if u in colors]
    for a in range(k):
        for b in range(k):
            if a == b:
                continue
            starts = [u for u in nbr if colors[u] == a]
            if not starts:
                return a
            changed = []
            seen = set()
            for s in starts:
                if s in seen:
                    continue
                stack = [s]
                comp = []
                while stack:
                    x = stack.pop()
                    if x in seen or colors.get(x) not in (a, b):
                        continue
                    seen.add(x)
                    comp.append(x)
                    stack.extend(y for y in neighbours[x] if y in colors and y not in seen)
                for x in comp:
                    colors[x] = b if colors[x] == a else a
                changed.extend(comp)
            if not any(colors[u] == a for u in nbr):
                return a
            for x in changed:  # undo
                colors[x] = b if colors[x] == a else a
    return None


class _Styler(QgsProcessingLayerPostProcessorInterface):
    """Applies the colours to the output layer once it is loaded into the project."""

    instances = []  # keep Python references alive until QGIS has run them

    def __init__(self, ids):
        super().__init__()
        self.ids = ids
        _Styler.instances.append(self)

    def postProcessLayer(self, layer, context, feedback):  # noqa: N802
        if layer is not None and layer.fields().indexOf(FIELD) >= 0:
            layer.setRenderer(coloring_renderer(self.ids))
            layer.triggerRepaint()


class FourColors(BiruniAlgorithm):
    """Map colouring: polygons that share a border never share a colour."""

    ID = "fourcolors"
    NAME = "Colour polygons so neighbours differ (4 colours)"
    GROUP = "Cartography"
    GROUP_ID = "cartography"
    HELP = ("Gives every polygon a colour so that no two polygons that share a border have the same one - the "
            "classic four-colour map for parcels, districts or zones (polygons that only meet at a corner may "
            "share a colour, as on printed maps). The result gets soft colours that print well and keep labels "
            "readable. The colours can be balanced by the number of polygons or by their area.")

    INPUT, MIN_COLORS, BALANCE, OUTPUT = "INPUT", "MIN_COLORS", "BALANCE", "OUTPUT"
    ORDERS = [("Same number of polygons per colour", "count"), ("Same area per colour", "area")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Polygon layer"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_COLORS, tr("Number of colours"), Qgis.ProcessingNumberParameterType.Integer, 4,
            minValue=1, maxValue=len(PALETTE)))
        self.addParameter(QgsProcessingParameterEnum(
            self.BALANCE, tr("Balance the colours by"), [tr(o[0]) for o in self.ORDERS], defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Coloured polygons"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        n_colors = self.parameterAsInt(parameters, self.MIN_COLORS, context)
        by_area = self.ORDERS[self.parameterAsEnum(parameters, self.BALANCE, context)][1] == "area"

        feats, geoms, index = {}, {}, QgsSpatialIndex()
        for f in source.getFeatures():
            if feedback.isCanceled():
                return {}
            g = f.geometry()
            feats[f.id()] = f
            if g.isNull() or g.isEmpty():
                continue
            if not g.isGeosValid():
                g = g.makeValid()
            geoms[f.id()] = g
            index.addFeature(f.id(), g.boundingBox())
        feedback.setProgress(20)

        # neighbours share a stretch of border (or overlap), not just a corner
        neighbours = {fid: set() for fid in geoms}
        for n, (fid, g) in enumerate(geoms.items()):
            if feedback.isCanceled():
                return {}
            engine = QgsGeometry.createGeometryEngine(g.constGet())
            engine.prepareGeometry()
            for other in index.intersects(g.boundingBox()):
                if other <= fid or not engine.intersects(geoms[other].constGet()):
                    continue
                inter = g.intersection(geoms[other])
                if inter.isEmpty() or (inter.length() <= EPS and inter.area() <= EPS):  # a corner only
                    continue
                neighbours[fid].add(other)
                neighbours[other].add(fid)
            feedback.setProgress(20 + 50 * n / max(len(geoms), 1))
        weight = {fid: (g.area() if by_area else 1.0) for fid, g in geoms.items()}
        colors = color_graph(neighbours, n_colors, weight)
        feedback.setProgress(80)

        fields = QgsFields(source.fields())
        if fields.indexOf(FIELD) < 0:
            fields.append(QgsField(FIELD, QMetaType.Type.Int))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                          source.sourceCrs())
        idx = fields.indexOf(FIELD)
        for fid, f in feats.items():
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            attrs = list(f.attributes()) + [None] * (len(fields) - len(f.attributes()))
            attrs[idx] = colors[fid] + 1 if fid in colors else None
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
        ids = {c + 1 for c in colors.values()}
        used = len(ids)
        if used > n_colors:
            feedback.pushWarning(tr("{n} colours were needed for these polygons.").format(n=used))
        feedback.pushInfo(tr("{n} colours used for {f} polygons.").format(n=used, f=len(feats)))
        if context.willLoadLayerOnCompletion(dest):
            context.layerToLoadOnCompletionDetails(dest).setPostProcessor(_Styler(ids))
        feedback.setProgress(100)
        return {self.OUTPUT: dest, "COLOR_COUNT": used}
