from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterCrs,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterPoint)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..survey.cogo import Traverse, course_from_text, format_bearing
from ..survey.cogo_geom import traverse_geometry
from .base import BiruniAlgorithm

OUTPUTS = ["Polygon (closed parcel)", "Line"]


class TraverseFromTable(BiruniAlgorithm):
    ID = "traverse"
    NAME = "Traverse / COGO from Table"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Draws a parcel or a line from a table of bearings and distances (a deed, a field book). Bearings "
            "may be azimuths (123.5, 123°30'15\", 123 30 15) or quadrant bearings (N45°30'E, S12 05 W); Arabic "
            "digits are accepted. An optional radius column turns a course into an arc (negative or 'L' = curving "
            "to the left); its distance is the chord. For a closed traverse the misclosure and the precision "
            "(1:N) are reported, and the compass (Bowditch) rule can distribute the error. Arcs stay true arcs.")

    INPUT, BEARING, DISTANCE, RADIUS, ORDER, START, CRS, CLOSED, ADJUST, KIND, OUTPUT, POINTS = (
        "INPUT", "BEARING_FIELD", "DISTANCE_FIELD", "RADIUS_FIELD", "ORDER_FIELD", "START", "CRS", "CLOSED",
        "ADJUST", "KIND", "OUTPUT", "POINTS")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Table of courses"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(self.BEARING, tr("Bearing field"), parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(self.DISTANCE, tr("Distance field"),
                                                      parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterField(self.RADIUS, tr("Radius field (arcs)"),
                                                      parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterField(self.ORDER, tr("Order field"),
                                                      parentLayerParameterName=self.INPUT, optional=True))
        self.addParameter(QgsProcessingParameterPoint(self.START, tr("Start point")))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system"), "ProjectCrs"))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CLOSED, tr("The traverse returns to the start point (closed)"), defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ADJUST, tr("Distribute the misclosure (compass rule)"), defaultValue=True))
        self.addParameter(QgsProcessingParameterEnum(self.KIND, tr("Create"), [tr(o) for o in OUTPUTS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Traverse")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POINTS, tr("Traverse stations"), Qgis.ProcessingSourceType.VectorPoint, optional=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        bf = self.parameterAsString(parameters, self.BEARING, context)
        df = self.parameterAsString(parameters, self.DISTANCE, context)
        rf = self.parameterAsString(parameters, self.RADIUS, context)
        of = self.parameterAsString(parameters, self.ORDER, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if crs.isGeographic():
            raise QgsProcessingException(tr("Choose a projected coordinate system in metres (e.g. UTM): bearings "
                                            "and distances cannot be laid out in degrees."))
        start = self.parameterAsPoint(parameters, self.START, context, crs)
        closed = self.parameterAsBool(parameters, self.CLOSED, context)
        adjust = self.parameterAsBool(parameters, self.ADJUST, context)
        polygon = self.parameterAsEnum(parameters, self.KIND, context) == 0

        rows = list(source.getFeatures())
        if of:
            rows.sort(key=lambda f: (f[of] is None, f[of]))
        courses = []
        for n, f in enumerate(rows, start=1):
            b, d = f[bf], f[df]
            if b in (None, "") or d in (None, ""):
                continue
            try:
                courses.append(course_from_text(b, d, f[rf] if rf else None))
            except ValueError:
                raise QgsProcessingException(tr("Row {n}: cannot read bearing '{b}' / distance '{d}'.").format(
                    n=n, b=b, d=d))
        if len(courses) < (3 if polygon else 1):
            raise QgsProcessingException(tr("Not enough courses: {n}.").format(n=len(courses)))

        trav = Traverse((start.x(), start.y()), courses)
        results = {}
        if closed:
            dx, dy, lin = trav.misclosure()
            prec = trav.precision()
            feedback.pushInfo(tr("Misclosure: dX {dx}, dY {dy}, linear {e} over {L} - precision 1:{p}").format(
                dx=f"{dx:+.4f}", dy=f"{dy:+.4f}", e=f"{lin:.4f}", L=f"{trav.total_length:.3f}",
                p="∞" if prec == float("inf") else f"{prec:,.0f}"))
            results.update({"MISCLOSURE": lin, "PRECISION": prec})
            if adjust:
                trav.adjust()
        geom = traverse_geometry(trav, polygon)
        if polygon and not closed:
            feedback.pushWarning(tr("The traverse is not closed: the polygon is closed with a straight line."))
        if polygon:
            results["AREA"] = trav.area()
            feedback.pushInfo(tr("Area: {a} m²").format(a=f"{trav.area():.4f}"))

        fields = QgsFields()
        for n, t in (("courses", QMetaType.Type.Int), ("length", QMetaType.Type.Double),
                     ("area", QMetaType.Type.Double), ("misclosure", QMetaType.Type.Double),
                     ("precision", QMetaType.Type.QString), ("adjusted", QMetaType.Type.Bool)):
            fields.append(QgsField(n, t))
        wkb = Qgis.WkbType.CurvePolygon if polygon else Qgis.WkbType.CompoundCurve
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, wkb, crs)
        o = QgsFeature(fields)
        o.setGeometry(geom)
        mis = trav.misclosure()[2] if closed else None
        prec = trav.precision() if closed else None
        o.setAttributes([len(courses), trav.perimeter(), trav.area() if polygon else None, mis,
                         None if prec is None else ("1:∞" if prec == float("inf") else f"1:{prec:.0f}"),
                         bool(closed and adjust)])
        sink.addFeature(o, QgsFeatureSink.Flag.FastInsert)
        results[self.OUTPUT] = dest

        pf = QgsFields()
        for n, t in (("station", QMetaType.Type.Int), ("x", QMetaType.Type.Double), ("y", QMetaType.Type.Double),
                     ("raw_x", QMetaType.Type.Double), ("raw_y", QMetaType.Type.Double),
                     ("bearing", QMetaType.Type.QString), ("distance", QMetaType.Type.Double)):
            pf.append(QgsField(n, t))
        psink, pdest = self.parameterAsSink(parameters, self.POINTS, context, pf, Qgis.WkbType.Point, crs)
        if psink is not None:
            for k, (p, raw) in enumerate(zip(trav.adjusted, trav.points)):
                c = trav.courses[k] if k < len(trav.courses) else None
                o = QgsFeature(pf)
                o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*p)))
                o.setAttributes([k + 1, p[0], p[1], raw[0], raw[1],
                                 format_bearing(c.bearing) if c else None, c.distance if c else None])
                psink.addFeature(o, QgsFeatureSink.Flag.FastInsert)
            results[self.POINTS] = pdest
        return results
