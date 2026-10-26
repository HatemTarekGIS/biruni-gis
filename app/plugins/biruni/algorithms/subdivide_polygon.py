import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm, polygonal

MODES = ["Equal parts", "Parts of a given area (the rest is the last part)", "Cut off one area"]
DIRECTIONS = ["Parallel to the longest side", "Perpendicular to the longest side", "Fixed bearing"]
AREA_TOLERANCE = 1e-5     # m² (well inside the promised 0.0001 m²)


def utm_for(lon, lat):
    zone = min(60, max(1, int((lon + 180) // 6) + 1))
    return QgsCoordinateReferenceSystem(f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}")


def longest_side_azimuth(geom):
    """Azimuth (degrees from north) of the longest straight side of the polygon."""
    best, az = -1.0, 0.0
    for part in geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]:
        for ring in part[:1]:
            for p1, p2 in zip(ring, ring[1:]):
                d = math.hypot(p2.x() - p1.x(), p2.y() - p1.y())
                if d > best:
                    best, az = d, math.degrees(math.atan2(p2.x() - p1.x(), p2.y() - p1.y()))
    return az % 180


class Slicer:
    """Cuts one polygon with parallel lines of a given azimuth."""

    def __init__(self, geom, azimuth, reverse=False):
        self.geom = geom
        a = math.radians(azimuth)
        self.u = (math.sin(a), math.cos(a))           # along the cut lines
        self.n = (math.cos(a), -math.sin(a))          # across them (sweep direction)
        if reverse:
            self.n = (-self.n[0], -self.n[1])
        c = geom.boundingBox().center()
        self.c = (c.x(), c.y())
        ss = [self._s(v.x(), v.y()) for v in geom.vertices()]
        self.smin, self.smax = min(ss), max(ss)
        bb = geom.boundingBox()
        self.reach = math.hypot(bb.width(), bb.height()) + 1.0
        self.total = geom.area()

    def _s(self, x, y):
        return (x - self.c[0]) * self.n[0] + (y - self.c[1]) * self.n[1]

    def _strip(self, s0, s1):
        cx, cy = self.c
        ux, uy = self.u
        nx, ny = self.n
        r = self.reach
        pts = [(s0, -r), (s1, -r), (s1, r), (s0, r), (s0, -r)]
        return QgsGeometry.fromPolygonXY([[QgsPointXY(cx + nx * s + ux * t, cy + ny * s + uy * t)
                                           for s, t in pts]])

    def below(self, s):
        return self.geom.intersection(self._strip(self.smin - 1.0, s))

    def cut_at_area(self, target):
        """Offset of the cut line that leaves `target` area before it."""
        lo, hi = self.smin, self.smax
        for _ in range(200):
            mid = (lo + hi) / 2
            a = self.below(mid).area()
            if abs(a - target) <= AREA_TOLERANCE:
                return mid
            if a < target:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-12:
                break
        return (lo + hi) / 2

    def pieces(self, targets):
        """Pieces between consecutive cuts; `targets` are cumulative areas."""
        cuts = [self.cut_at_area(t) for t in targets]
        bounds = [self.smin - 1.0] + cuts + [self.smax + 1.0]
        out = []
        for s0, s1 in zip(bounds, bounds[1:]):
            out.append(polygonal(self.geom.intersection(self._strip(s0, s1))))
        return out


class SubdividePolygon(BiruniAlgorithm):
    ID = "subdividepolygon"
    NAME = "Subdivide Parcel"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Divides parcels with parallel cut lines: into equal parts, into parts of a given area (the "
            "remainder becomes the last part), or cuts off one part of a given area. The cut lines run parallel "
            "or perpendicular to the longest side, or along a bearing you choose. Each area is found to within "
            "0.0001 m². Works on concave and multi-part parcels; parcels in degrees are measured in their UTM "
            "zone. Tick 'Selected features only' to divide just the selected parcels.")

    INPUT, MODE, PARTS, AREA, DIRECTION, BEARING, REVERSE, OUTPUT = (
        "INPUT", "MODE", "PARTS", "AREA", "DIRECTION", "BEARING", "REVERSE", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Parcels"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterEnum(self.MODE, tr("Divide into"), [tr(m) for m in MODES],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.PARTS, tr("Number of equal parts"), Qgis.ProcessingNumberParameterType.Integer, 2, minValue=2,
            maxValue=1000))
        self.addParameter(QgsProcessingParameterNumber(
            self.AREA, tr("Area of a part (m²)"), Qgis.ProcessingNumberParameterType.Double, 500.0,
            minValue=0.0001))
        self.addParameter(QgsProcessingParameterEnum(
            self.DIRECTION, tr("Cut lines"), [tr(d) for d in DIRECTIONS], defaultValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            self.BEARING, tr("Bearing of the cut lines (degrees from north)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=-360, maxValue=360))
        self.addParameter(QgsProcessingParameterBoolean(
            self.REVERSE, tr("Start from the opposite side"), defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Subdivided"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        mode = self.parameterAsEnum(parameters, self.MODE, context)
        parts = self.parameterAsInt(parameters, self.PARTS, context)
        part_area = self.parameterAsDouble(parameters, self.AREA, context)
        direction = self.parameterAsEnum(parameters, self.DIRECTION, context)
        bearing = self.parameterAsDouble(parameters, self.BEARING, context)
        reverse = self.parameterAsBool(parameters, self.REVERSE, context)

        crs = source.sourceCrs()
        fields = QgsFields(source.fields())
        names = {f.name().lower() for f in fields}
        extra = []
        for n, t in (("parent_id", QMetaType.Type.LongLong), ("part_no", QMetaType.Type.Int),
                     ("part_area", QMetaType.Type.Double)):
            name = n
            while name.lower() in names:
                name += "_"
            fields.append(QgsField(name, t))
            extra.append(name)
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.MultiPolygon, crs)

        total = source.featureCount() or 1
        made = skipped = 0
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            if QgsGeometry(g).constGet().hasCurvedSegments():
                g = QgsGeometry(g.constGet().segmentize())
            back = None
            if crs.isGeographic():
                c = g.centroid().asPoint()
                utm = utm_for(c.x(), c.y())
                to_utm = QgsCoordinateTransform(crs, utm, context.transformContext())
                back = QgsCoordinateTransform(utm, crs, context.transformContext())
                g.transform(to_utm)
            g = polygonal(g.makeValid()) if not g.isGeosValid() else g
            if g is None:
                skipped += 1
                continue
            total_area = g.area()
            if direction == 0:
                az = longest_side_azimuth(g)
            elif direction == 1:
                az = longest_side_azimuth(g) + 90
            else:
                az = bearing
            if mode == 0:
                targets = [total_area * k / parts for k in range(1, parts)]
            elif mode == 1:
                if part_area >= total_area - AREA_TOLERANCE:
                    feedback.pushWarning(tr("Parcel {id}: the part area is not smaller than the parcel "
                                            "({a} m²) - left as it is.").format(id=f.id(), a=round(total_area, 4)))
                    skipped += 1
                    continue
                n = int(total_area // part_area)
                if total_area - n * part_area <= AREA_TOLERANCE:
                    n -= 1  # the last part is a whole part, no remainder
                targets = [part_area * k for k in range(1, n + 1)]
            else:
                if part_area >= total_area - AREA_TOLERANCE:
                    feedback.pushWarning(tr("Parcel {id}: the area to cut off is not smaller than the parcel "
                                            "({a} m²) - left as it is.").format(id=f.id(), a=round(total_area, 4)))
                    skipped += 1
                    continue
                targets = [part_area]
            pieces = Slicer(g, az, reverse).pieces(targets)
            for k, piece in enumerate(pieces, start=1):
                if piece is None:
                    continue
                area = piece.area()
                if back is not None:
                    piece.transform(back)
                o = QgsFeature(fields)
                o.setGeometry(piece)
                o.setAttributes(list(f.attributes()) + [f.id(), k, area])
                sink.addFeature(o, QgsFeatureSink.Flag.FastInsert)
                made += 1
            feedback.setProgress(100 * i / total)
        feedback.pushInfo(tr("{n} parts created.").format(n=made))
        return {self.OUTPUT: dest, "PARTS_CREATED": made, "SKIPPED": skipped}
