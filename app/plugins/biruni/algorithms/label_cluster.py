"""One label for a group of nearby features, with leader lines to each of them (label clustering)."""
import math

from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsLineSymbol,
                       QgsMarkerSymbol, QgsPalLayerSettings, QgsPointXY, QgsProcessingException,
                       QgsProcessingLayerPostProcessorInterface, QgsProcessingParameterBoolean,
                       QgsProcessingParameterDistance, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsRectangle, QgsSpatialIndex,
                       QgsTextBufferSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor, QFont

from ..i18n import tr
from .base import BiruniAlgorithm

ANGLES = list(range(0, 360, 15))
RINGS = (1.0, 1.4, 1.9, 2.6)
CHAR_W = 0.62   # average glyph width / text height (Arabic and Latin)


def _anchor(geom, towards):
    """Where a leader ends on a feature: the point itself, the closest point of a line, or a point inside a
    polygon (its point on surface, unless the polygon's edge is closer to the label)."""
    if geom.type() == Qgis.GeometryType.Point:
        return geom.centroid().asPoint() if geom.isMultipart() else geom.asPoint()
    if geom.type() == Qgis.GeometryType.Line:
        return geom.nearestPoint(QgsGeometry.fromPointXY(towards)).asPoint()
    return geom.pointOnSurface().asPoint()


def group_features(items, distance, by_value):
    """Single-linkage groups: features within ``distance`` of a feature of the group (and, by value, with the
    same label). ``items``: {fid: (geometry, value)}. Returns a list of fid lists."""
    index = QgsSpatialIndex()
    for fid, (g, _v) in items.items():
        index.addFeature(fid, g.boundingBox())
    seen, groups = set(), []
    for fid in items:
        if fid in seen:
            continue
        seen.add(fid)
        comp, stack = [fid], [fid]
        while stack:
            cur = stack.pop()
            g, v = items[cur]
            box = g.boundingBox()
            box.grow(distance)
            for other in index.intersects(box):
                if other in seen:
                    continue
                og, ov = items[other]
                if by_value and ov != v:
                    continue
                if g.distance(og) <= distance:
                    seen.add(other)
                    comp.append(other)
                    stack.append(other)
        groups.append(sorted(comp))
    return groups


def label_box(center, text, height):
    w = max(1, len(str(text))) * CHAR_W * height + height * 0.6
    h = height * 1.3
    return QgsRectangle(center.x() - w / 2, center.y() - h / 2, center.x() + w / 2, center.y() + h / 2)


def choose_position(group_geoms, text, height, offset, obstacles, placed):
    """Free space near the group for a label box: candidates on rings around the group, scored by clearance from
    every feature (``obstacles``: spatial index + geometries) and no overlap with labels already placed."""
    index, geoms = obstacles
    hull = QgsGeometry.unaryUnion(group_geoms).convexHull() if len(group_geoms) > 1 else group_geoms[0]
    centre = hull.centroid().asPoint()
    reach = max(hull.boundingBox().width(), hull.boundingBox().height()) / 2
    best, best_score = None, None
    for ring in RINGS:
        for ang in ANGLES:
            r = reach + offset * ring
            p = QgsPointXY(centre.x() + r * math.cos(math.radians(ang)), centre.y() + r * math.sin(math.radians(ang)))
            box = label_box(p, text, height)
            bgeom = QgsGeometry.fromRect(box)
            search = QgsRectangle(box)
            search.grow(offset)
            clearance = offset
            hit = False
            for fid in index.intersects(search):
                d = bgeom.distance(geoms[fid])
                if d <= 0:
                    hit = True
                    break
                clearance = min(clearance, d)
            if hit:
                continue
            overlap = sum(1 for other in placed if other.intersects(box))
            if overlap:
                continue
            # prefer clear space, then closeness, then above-right (the reading start)
            score = clearance * 2 - r * 0.35 + 0.05 * offset * math.cos(math.radians(ang - 45))
            if best_score is None or score > best_score:
                best, best_score = (p, box), score
        if best is not None:
            break
    if best is None:  # nowhere free: straight above the group
        p = QgsPointXY(centre.x(), centre.y() + reach + offset)
        best = (p, label_box(p, text, height))
    return best


def _leader_start(box, target):
    """Point where the straight line from the box centre to ``target`` leaves the label box."""
    c = box.center()
    dx, dy = target.x() - c.x(), target.y() - c.y()
    if dx == 0 and dy == 0:
        return c
    tx = (box.width() / 2) / abs(dx) if dx else math.inf
    ty = (box.height() / 2) / abs(dy) if dy else math.inf
    t = min(tx, ty, 1.0)
    return QgsPointXY(c.x() + dx * t, c.y() + dy * t)


class _Styler(QgsProcessingLayerPostProcessorInterface):
    instances = []

    def __init__(self, kind, height):
        super().__init__()
        self.kind = kind
        self.height = height
        _Styler.instances.append(self)

    def postProcessLayer(self, layer, context, feedback):  # noqa: N802
        if layer is None:
            return
        if self.kind == "labels":
            style_label_layer(layer, self.height)
        else:
            style_leader_layer(layer)


def style_label_layer(layer, height):
    sym = QgsMarkerSymbol.createSimple({"name": "circle", "size": "0", "color": "0,0,0,0",
                                        "outline_style": "no"})
    layer.renderer().setSymbol(sym)
    fmt = QgsTextFormat()
    fmt.setFont(QFont("Segoe UI"))
    fmt.setSize(height)
    fmt.setSizeUnit(Qgis.RenderUnit.MapUnits)
    fmt.setColor(QColor("#1E252B"))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.12 * height)
    buf.setSizeUnit(Qgis.RenderUnit.MapUnits)
    buf.setColor(QColor("white"))
    fmt.setBuffer(buf)
    pal = QgsPalLayerSettings()
    pal.fieldName = "label"
    pal.setFormat(fmt)
    pal.placement = Qgis.LabelPlacement.OverPoint
    pal.multilineAlign = Qgis.LabelMultiLineAlignment.Center
    pal.displayAll = True  # the position was already chosen in free space
    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()


def style_leader_layer(layer):
    sym = QgsLineSymbol.createSimple({"line_color": "#5B6670", "line_width": "0.2", "line_width_unit": "MM"})
    from qgis.core import QgsMarkerLineSymbolLayer

    dot = QgsMarkerLineSymbolLayer()
    dot.setPlacements(Qgis.MarkerLinePlacement.LastVertex)
    dot.setSubSymbol(QgsMarkerSymbol.createSimple({"name": "circle", "size": "1.0", "color": "#5B6670",
                                                   "outline_style": "no"}))
    sym.appendSymbolLayer(dot)
    layer.renderer().setSymbol(sym)
    layer.triggerRepaint()


class LabelCluster(BiruniAlgorithm):
    ID = "labelcluster"
    NAME = "One label for nearby features (with leader lines)"
    GROUP = "Cartography"
    GROUP_ID = "cartography"
    HELP = ("Instead of the same name written many times (wells, poles, shops, parcels of one owner), nearby "
            "features with the same label get one label in free space next to them, with a leader line to each "
            "feature. Without a label field every group of nearby features gets one label with its count. The "
            "labels are placed where they do not cover features or each other, and come out styled and ready to "
            "print; they can still be moved by hand.")

    INPUT, FIELD, DISTANCE, HEIGHT, OFFSET, SINGLES, COUNT = ("INPUT", "FIELD", "DISTANCE", "HEIGHT", "OFFSET",
                                                             "SINGLES", "COUNT")
    LABELS, LEADERS = "LABELS", "LEADERS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Features"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, tr("Label field (empty = group any nearby features and write their count)"),
            parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterDistance(
            self.DISTANCE, tr("Group features closer than"), 50.0, self.INPUT, minValue=0.0))
        self.addParameter(QgsProcessingParameterDistance(
            self.HEIGHT, tr("Text height on the ground (0 = a fifth of the grouping distance)"), 0.0, self.INPUT,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterDistance(
            self.OFFSET, tr("Distance of the label from the group (0 = automatic)"), 0.0, self.INPUT,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.SINGLES, tr("Also label features that stand alone (without a leader)"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.COUNT, tr("Add the number of features to the label, e.g. “Well (3)”"), True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.LABELS, tr("Group labels"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.LEADERS, tr("Leader lines"), Qgis.ProcessingSourceType.VectorLine))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        field = self.parameterAsString(parameters, self.FIELD, context)
        distance = self.parameterAsDouble(parameters, self.DISTANCE, context)
        height = self.parameterAsDouble(parameters, self.HEIGHT, context) or max(distance / 5, 1e-9)
        offset = self.parameterAsDouble(parameters, self.OFFSET, context) or max(height * 2.2, distance * 0.4)
        singles = self.parameterAsBoolean(parameters, self.SINGLES, context)
        with_count = self.parameterAsBoolean(parameters, self.COUNT, context)
        if source.sourceCrs().isGeographic() and distance > 1:
            feedback.pushWarning(tr("The layer is in degrees: the distances are in degrees too."))

        fi = source.fields().indexOf(field) if field else -1
        items, geoms, index = {}, {}, QgsSpatialIndex()
        for f in source.getFeatures():
            if feedback.isCanceled():
                return {}
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            value = f.attributes()[fi] if fi >= 0 else None
            if fi >= 0 and (value is None or str(value).strip() == ""):
                continue
            items[f.id()] = (g, value)
            geoms[f.id()] = g
            index.addFeature(f.id(), g.boundingBox())
        groups = group_features(items, distance, fi >= 0)
        feedback.setProgress(40)

        lf = QgsFields()
        lf.append(QgsField("label", QMetaType.Type.QString))
        lf.append(QgsField("value", QMetaType.Type.QString))
        lf.append(QgsField("count", QMetaType.Type.Int))
        lf.append(QgsField("members", QMetaType.Type.QString))
        df = QgsFields()
        df.append(QgsField("group_id", QMetaType.Type.Int))
        df.append(QgsField("feature_id", QMetaType.Type.LongLong))
        crs = source.sourceCrs()
        lsink, lid = self.parameterAsSink(parameters, self.LABELS, context, lf, Qgis.WkbType.Point, crs)
        dsink, did = self.parameterAsSink(parameters, self.LEADERS, context, df, Qgis.WkbType.LineString, crs)

        # larger groups first: they need the room most
        groups.sort(key=len, reverse=True)
        placed, n_groups, n_leaders = [], 0, 0
        for gi, members in enumerate(groups, start=1):
            if feedback.isCanceled():
                return {}
            if len(members) == 1 and not singles:
                continue
            value = items[members[0]][1]
            base = str(value) if fi >= 0 else ""
            if fi < 0:
                text = str(len(members))
            elif with_count and len(members) > 1:
                text = f"{base} ({len(members)})"
            else:
                text = base
            group_geoms = [geoms[m] for m in members]
            moved = True
            if len(members) == 1:
                g = group_geoms[0]
                p = _anchor(g, g.centroid().asPoint())
                if g.type() == Qgis.GeometryType.Point:
                    p = QgsPointXY(p.x(), p.y() + height * 1.2)
                box = label_box(p, text, height)
                moved = any(o.intersects(box) for o in placed)
                if moved:
                    p, box = choose_position(group_geoms, text, height, offset, (index, geoms), placed)
            else:
                p, box = choose_position(group_geoms, text, height, offset, (index, geoms), placed)
            placed.append(box)
            feat = QgsFeature(lf)
            feat.setGeometry(QgsGeometry.fromPointXY(p))
            feat.setAttributes([text, base, len(members), ",".join(str(m) for m in members)])
            lsink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            n_groups += 1
            if len(members) > 1 or moved:  # a lone feature labelled right beside it needs no leader
                for m in members:
                    end = _anchor(geoms[m], p)
                    start = _leader_start(box, end)
                    if start == end:
                        continue
                    line = QgsFeature(df)
                    line.setGeometry(QgsGeometry.fromPolylineXY([start, end]))
                    line.setAttributes([gi, m])
                    dsink.addFeature(line, QgsFeatureSink.Flag.FastInsert)
                    n_leaders += 1
            feedback.setProgress(40 + 60 * gi / max(len(groups), 1))

        for dest, kind in ((lid, "labels"), (did, "leaders")):
            if context.willLoadLayerOnCompletion(dest):
                context.layerToLoadOnCompletionDetails(dest).setPostProcessor(_Styler(kind, height))
        feedback.pushInfo(tr("{g} labels for {n} features, {l} leader lines.").format(
            g=n_groups, n=len(items), l=n_leaders))
        return {self.LABELS: lid, self.LEADERS: did, "LABEL_COUNT": n_groups, "LEADER_COUNT": n_leaders,
                "GROUP_COUNT": sum(1 for g in groups if len(g) > 1)}
