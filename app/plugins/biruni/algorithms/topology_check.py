from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields, QgsGeometry,
                       QgsProcessingException, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..quality import topology as topo
from .base import BiruniAlgorithm

P, L, A = Qgis.GeometryType.Point, Qgis.GeometryType.Line, Qgis.GeometryType.Polygon

# (rule name, key, geometry type of the checked layer, geometry type of the second layer or None)
TITLES = [
    ("Polygons must not overlap", "no_overlap", A, None),
    ("Polygons must not have gaps", "no_gaps", A, None),
    ("Geometries must be valid (no self-intersections)", "valid", None, None),
    ("Lines must not have dangles", "no_dangles", L, None),
    ("Lines must not overlap", "lines_no_overlap", L, None),
    ("Lines must not self-intersect", "lines_no_self_intersect", L, None),
    ("Points must be inside polygons (second layer)", "points_in_polygons", P, A),
    ("Polygons must be covered by polygons (second layer)", "covered_by", A, A),
    ("Points must be on lines (second layer)", "points_on_lines", P, L),
]


class TopologyCheck(BiruniAlgorithm):
    """Rule-based topology validation with one error layer per geometry type."""

    ID = "topologycheck"
    NAME = "Topology rules"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Checks a layer against the topology rules you pick - polygons that overlap or leave gaps, invalid "
            "or self-intersecting shapes, dangling or overlapping lines, points outside polygons or off lines, "
            "polygons not covered by another layer. Every problem becomes an error feature (point, line or "
            "area) with the rule and the feature ids, so you can zoom to it and fix it. Rules that need a "
            "second layer use the 'Second layer'. The tolerance is in the layer's units.")

    INPUT, RULES, SECOND, TOLERANCE, MIN_AREA = "INPUT", "RULES", "SECOND", "TOLERANCE", "MIN_AREA"
    POINTS, LINES, AREAS = "ERROR_POINTS", "ERROR_LINES", "ERROR_AREAS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer to check"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterEnum(
            self.RULES, tr("Rules"), [tr(r[0]) for r in TITLES], allowMultiple=True, defaultValue=[0, 1, 2]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SECOND, tr("Second layer (for rules that compare two layers)"),
            [Qgis.ProcessingSourceType.VectorAnyGeometry], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE, tr("Tolerance (layer units)"), Qgis.ProcessingNumberParameterType.Double, 0.0,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, tr("Ignore overlaps, gaps and uncovered areas smaller than (layer units²)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POINTS, tr("Point errors"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.LINES, tr("Line errors"), Qgis.ProcessingSourceType.VectorLine))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.AREAS, tr("Area errors"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        rules = [TITLES[i] for i in self.parameterAsEnums(parameters, self.RULES, context)]
        second = self.parameterAsSource(parameters, self.SECOND, context)
        tol = self.parameterAsDouble(parameters, self.TOLERANCE, context)
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context)
        crs = source.sourceCrs()
        gtype = QgsWkbTypes.geometryType(source.wkbType())
        other_type = None

        fields = QgsFields()
        fields.append(QgsField("rule", QMetaType.Type.QString))
        fields.append(QgsField("rule_name", QMetaType.Type.QString))
        fields.append(QgsField("feature_a", QMetaType.Type.LongLong))
        fields.append(QgsField("feature_b", QMetaType.Type.LongLong))
        fields.append(QgsField("description", QMetaType.Type.QString))
        fields.append(QgsField("size", QMetaType.Type.Double))
        psink, pid = self.parameterAsSink(parameters, self.POINTS, context, fields, Qgis.WkbType.Point, crs)
        lsink, lid = self.parameterAsSink(parameters, self.LINES, context, fields, Qgis.WkbType.MultiLineString, crs)
        asink, aid = self.parameterAsSink(parameters, self.AREAS, context, fields, Qgis.WkbType.MultiPolygon, crs)
        counts = {}

        def emit(rule, geom, a, b=None, desc="", size=None):
            if geom is None or geom.isEmpty():
                return
            t = geom.type()
            g = QgsGeometry(geom)
            if t == P:
                sink = psink
                if g.isMultipart():
                    g = QgsGeometry.fromPointXY(g.asMultiPoint()[0])
            else:
                sink = lsink if t == L else asink
                g.convertToMultiType()
            f = QgsFeature(fields)
            f.setGeometry(g)
            f.setAttributes([rule[1], tr(rule[0]), a, b, desc, size])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            counts[rule[1]] = counts.get(rule[1], 0) + 1

        feedback.pushInfo(tr("Reading features…"))
        geoms, index = topo.collect(source.getFeatures(), feedback, 0, 20, source.featureCount())
        other, oindex = {}, None
        if any(r[3] is not None for r in rules):
            if second is None:
                raise QgsProcessingException(tr("The selected rules need a second layer."))
            req = QgsFeatureRequest().setDestinationCrs(crs, context.transformContext())
            other, oindex = topo.collect(second.getFeatures(req), feedback)
            other_type = QgsWkbTypes.geometryType(second.wkbType())

        step = 80 / max(len(rules), 1)
        for n, rule in enumerate(rules):
            if feedback.isCanceled():
                break
            feedback.setProgress(20 + n * step)
            name, key, need, need2 = rule
            if need is not None and need != gtype:
                feedback.reportError(tr("Skipped '{r}': it does not apply to this layer's geometry type.")
                                     .format(r=tr(name)))
                continue
            if need2 is not None and other_type != need2:
                feedback.reportError(tr("Skipped '{r}': the second layer has the wrong geometry type.")
                                     .format(r=tr(name)))
                continue
            feedback.pushInfo(tr(name) + "…")
            if key == "no_overlap":
                for a, b, g in topo.find_overlaps(geoms, index, min_area, feedback):
                    emit(rule, g, a, b, tr("Overlap"), g.area())
            elif key == "no_gaps":
                for g in topo.find_gaps(geoms, min_area):
                    emit(rule, g, None, None, tr("Gap"), g.area())
            elif key == "valid":
                for fid, g in geoms.items():
                    for where, what in topo.invalid_details(g):
                        pg = QgsGeometry.fromPointXY(where) if where is not None else g.pointOnSurface()
                        emit(rule, pg, fid, None, what)
            elif key == "no_dangles":
                for fid, p in topo.find_dangles(geoms, index, tol, feedback):
                    emit(rule, QgsGeometry.fromPointXY(p), fid, None, tr("Dangling end"))
            elif key == "lines_no_overlap":
                for a, b, g in topo.find_line_overlaps(geoms, index, tol, feedback):
                    emit(rule, g, a, b, tr("Lines overlap"), g.length())
            elif key == "lines_no_self_intersect":
                for fid, g in geoms.items():
                    for p in topo.self_intersections(g):
                        emit(rule, QgsGeometry.fromPointXY(p), fid, None, tr("Self-intersection"))
            elif key == "points_in_polygons":
                for fid in topo.points_outside(geoms, other, oindex, feedback):
                    emit(rule, geoms[fid], fid, None, tr("Point outside every polygon"))
            elif key == "covered_by":
                for fid, g in topo.uncovered(geoms, other, oindex, min_area, feedback):
                    emit(rule, g, fid, None, tr("Not covered"), g.area())
            elif key == "points_on_lines":
                for fid, d in topo.points_off_lines(geoms, other, oindex, tol, feedback):
                    emit(rule, geoms[fid], fid, None, tr("Point not on a line"), d)

        total = sum(counts.values())
        for name, key, *_ in rules:
            feedback.pushInfo(f"{tr(name)}: {counts.get(key, 0)}")
        feedback.pushInfo(tr("Found {n} topology errors.").format(n=total))
        feedback.setProgress(100)
        res = {self.POINTS: pid, self.LINES: lid, self.AREAS: aid, "ERROR_COUNT": total}
        for _name, key, *_ in TITLES:
            res[key.upper()] = counts.get(key, 0)
        return res
