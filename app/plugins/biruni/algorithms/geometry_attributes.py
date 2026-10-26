from qgis.core import (Qgis, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessingException, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

AREA_UNITS = [  # (label, unit, field suffix)
    ("Square metres", Qgis.AreaUnit.SquareMeters, "m2"),
    ("Hectares", Qgis.AreaUnit.Hectares, "ha"),
    ("Square kilometres", Qgis.AreaUnit.SquareKilometers, "km2"),
    ("Square feet", Qgis.AreaUnit.SquareFeet, "ft2"),
    ("Acres", Qgis.AreaUnit.Acres, "ac"),
]
LENGTH_UNITS = [
    ("Metres", Qgis.DistanceUnit.Meters, "m"),
    ("Kilometres", Qgis.DistanceUnit.Kilometers, "km"),
    ("Feet", Qgis.DistanceUnit.Feet, "ft"),
    ("Miles", Qgis.DistanceUnit.Miles, "mi"),
]


class GeometryAttributes(BiruniAlgorithm):
    ID = "geometryattributes"
    NAME = "Add area, length and coordinates"
    GROUP = "Measurement"
    GROUP_ID = "measurement"
    HELP = ("Adds measurement fields to every feature in the units you choose: area and perimeter for "
            "polygons, length for lines, X/Y for points (centroid for polygons). 'True size on the Earth' "
            "measures on the ellipsoid and is correct in any coordinate system; 'Flat (layer CRS)' measures "
            "on the map plane of a projected CRS.")

    INPUT, METHOD, AREA_UNIT, LENGTH_UNIT, DECIMALS, OUTPUT = (
        "INPUT", "METHOD", "AREA_UNIT", "LENGTH_UNIT", "DECIMALS", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Input layer"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, tr("Measure"), [tr("True size on the Earth (ellipsoidal)"), tr("Flat (layer CRS)")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.AREA_UNIT, tr("Area unit"), [tr(u[0]) for u in AREA_UNITS], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.LENGTH_UNIT, tr("Length unit"), [tr(u[0]) for u in LENGTH_UNITS], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.DECIMALS, tr("Decimal places"), Qgis.ProcessingNumberParameterType.Integer, 3, minValue=0,
            maxValue=12))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Measured")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        ellipsoidal = self.parameterAsEnum(parameters, self.METHOD, context) == 0
        a_label, a_unit, a_sfx = AREA_UNITS[self.parameterAsEnum(parameters, self.AREA_UNIT, context)]
        l_label, l_unit, l_sfx = LENGTH_UNITS[self.parameterAsEnum(parameters, self.LENGTH_UNIT, context)]
        dec = self.parameterAsInt(parameters, self.DECIMALS, context)

        crs = source.sourceCrs()
        da = QgsDistanceArea()
        da.setSourceCrs(crs, context.transformContext())
        if ellipsoidal:
            ell = context.ellipsoid()
            if not ell or ell.upper() == "NONE":  # no project ellipsoid -> the layer's own
                ell = crs.ellipsoidAcronym() or "EPSG:7030"
            da.setEllipsoid(ell)
        else:
            da.setEllipsoid("NONE")
            if crs.isGeographic():
                feedback.reportError(tr("The layer uses geographic coordinates (degrees): flat measurements "
                                        "are not meaningful. Use 'True size on the Earth' instead."))

        gtype = QgsWkbTypes.geometryType(source.wkbType())
        new = []
        if gtype == Qgis.GeometryType.Polygon:
            new = [f"area_{a_sfx}", f"perim_{l_sfx}", "x_centroid", "y_centroid"]
        elif gtype == Qgis.GeometryType.Line:
            new = [f"length_{l_sfx}", "x_start", "y_start", "x_end", "y_end"]
        elif gtype == Qgis.GeometryType.Point:
            new = ["x", "y"]
        fields = QgsFields()
        kept = []
        for f in source.fields():
            if f.name() not in new:
                fields.append(f)
                kept.append(f.name())
        for name in new:
            fields.append(QgsField(name, QMetaType.Type.Double))

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(), crs)
        total = source.featureCount() or 1
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            g = f.geometry()
            vals = [None] * len(new)
            if not g.isNull() and not g.isEmpty():
                if gtype == Qgis.GeometryType.Polygon:
                    c = g.centroid().asPoint()
                    vals = [round(da.convertAreaMeasurement(da.measureArea(g), a_unit), dec),
                            round(da.convertLengthMeasurement(da.measurePerimeter(g), l_unit), dec),
                            round(c.x(), 8), round(c.y(), 8)]
                elif gtype == Qgis.GeometryType.Line:
                    verts = list(g.vertices())
                    vals = [round(da.convertLengthMeasurement(da.measureLength(g), l_unit), dec),
                            verts[0].x(), verts[0].y(), verts[-1].x(), verts[-1].y()]
                elif gtype == Qgis.GeometryType.Point:
                    p = g.asPoint() if not g.isMultipart() else g.centroid().asPoint()
                    vals = [p.x(), p.y()]
            out = QgsFeature(fields)
            out.setGeometry(g)
            out.setAttributes([f[name] for name in kept] + vals)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * i / total)
        return {self.OUTPUT: dest}
