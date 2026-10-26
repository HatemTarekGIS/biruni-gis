from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPolygon,
                       QgsProcessingException, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber, QgsSpatialIndex)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm, polygonal

EPS = 1e-10


class OverlapsGaps(BiruniAlgorithm):
    """Coverage check for polygon layers (parcels, zones, land use …)."""

    ID = "overlapsgaps"
    NAME = "Find overlaps and gaps"
    GROUP = "Quality"
    GROUP_ID = "quality"
    HELP = ("Checks a polygon layer that should cover an area without overlapping (parcels, zones, land use). "
            "Outputs every overlap between two polygons and every gap (hole) enclosed by the polygons, with "
            "their areas, so they can be reviewed and fixed. Tiny slivers can be ignored with the minimum "
            "area; real holes such as courtyards or lakes can be ignored with the maximum gap area.")

    INPUT, MIN_AREA, MAX_GAP, OVERLAPS, GAPS = "INPUT", "MIN_AREA", "MAX_GAP", "OVERLAPS", "GAPS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Polygon layer"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, tr("Ignore overlaps and gaps smaller than (layer units²)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_GAP, tr("Ignore gaps larger than (layer units², 0 = no limit)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OVERLAPS, tr("Overlaps"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.GAPS, tr("Gaps"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        min_area = max(self.parameterAsDouble(parameters, self.MIN_AREA, context), EPS)
        max_gap = self.parameterAsDouble(parameters, self.MAX_GAP, context)

        of = QgsFields()
        of.append(QgsField("feature_a", QMetaType.Type.LongLong))
        of.append(QgsField("feature_b", QMetaType.Type.LongLong))
        of.append(QgsField("area", QMetaType.Type.Double))
        gf = QgsFields()
        gf.append(QgsField("gap_id", QMetaType.Type.Int))
        gf.append(QgsField("area", QMetaType.Type.Double))
        crs = source.sourceCrs()
        osink, oid = self.parameterAsSink(parameters, self.OVERLAPS, context, of, Qgis.WkbType.MultiPolygon, crs)
        gsink, gid = self.parameterAsSink(parameters, self.GAPS, context, gf, Qgis.WkbType.MultiPolygon, crs)

        geoms = {}
        index = QgsSpatialIndex()
        total = source.featureCount() or 1
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            if not g.isGeosValid():
                g = g.makeValid()
            geoms[f.id()] = g
            index.addFeature(f.id(), g.boundingBox())
            feedback.setProgress(10 * i / total)

        feedback.pushInfo(tr("Checking {n} polygons for overlaps…").format(n=len(geoms)))
        overlaps = 0
        for n, (fid, g) in enumerate(geoms.items()):
            if feedback.isCanceled():
                return {}
            engine = QgsGeometry.createGeometryEngine(g.constGet())
            engine.prepareGeometry()
            for other in index.intersects(g.boundingBox()):
                if other <= fid:
                    continue
                og = geoms[other]
                if not engine.intersects(og.constGet()):
                    continue
                inter = polygonal(g.intersection(og))
                if inter is None:
                    continue
                area = inter.area()
                if area < min_area:
                    continue
                feat = QgsFeature(of)
                feat.setGeometry(inter)
                feat.setAttributes([fid, other, area])
                osink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
                overlaps += 1
            feedback.setProgress(10 + 70 * n / max(len(geoms), 1))

        feedback.pushInfo(tr("Looking for gaps…"))
        gaps = 0
        union = QgsGeometry.unaryUnion(list(geoms.values())) if geoms else QgsGeometry()
        parts = union.asGeometryCollection() if union.isMultipart() else [union]
        for part in parts:
            poly = part.constGet()
            if poly is None or not hasattr(poly, "numInteriorRings"):
                continue
            for r in range(poly.numInteriorRings()):
                hole = QgsPolygon()
                hole.setExteriorRing(poly.interiorRing(r).clone())
                hg = QgsGeometry(hole)
                area = hg.area()
                if area < min_area or (max_gap > 0 and area > max_gap):
                    continue
                hg.convertToMultiType()
                gaps += 1
                feat = QgsFeature(gf)
                feat.setGeometry(hg)
                feat.setAttributes([gaps, area])
                gsink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
        feedback.setProgress(100)
        feedback.pushInfo(tr("Found {o} overlaps and {g} gaps.").format(o=overlaps, g=gaps))
        return {self.OVERLAPS: oid, self.GAPS: gid, "OVERLAP_COUNT": overlaps, "GAP_COUNT": gaps}
