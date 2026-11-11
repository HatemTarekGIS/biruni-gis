import os

from qgis.core import (QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFile, QgsProcessingParameterString, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm


class IndoorRoute(BiruniAlgorithm):
    ID = "indoorroute"
    NAME = "Route between two rooms"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Draws the shortest walking route between two rooms of an indoor map made with Floor plan to indoor "
            "map, across levels by stairs or elevator. Give each room by its id (L1-214), its number (214) or its "
            "name. With 'Wheelchair route' the route avoids stairs and doors narrower than the minimum width "
            "chosen when the map was made.")

    GPKG, FROM, TO, ACCESSIBLE, OUTPUT = "GPKG", "FROM", "TO", "ACCESSIBLE", "OUTPUT"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.GPKG, tr("Indoor map (indoor.gpkg)"), fileFilter="GeoPackage (*.gpkg)"))
        self.addParameter(QgsProcessingParameterString(self.FROM, tr("From room (id, number or name)")))
        self.addParameter(QgsProcessingParameterString(self.TO, tr("To room (id, number or name)")))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ACCESSIBLE, tr("Wheelchair route (no stairs, no narrow doors)"), defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Route")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..indoor import graph

        gpkg = self.parameterAsFile(parameters, self.GPKG, context)
        if not gpkg or not os.path.exists(gpkg):
            raise QgsProcessingException(tr("Could not read {p}").format(p=gpkg))
        a = self.parameterAsString(parameters, self.FROM, context)
        b = self.parameterAsString(parameters, self.TO, context)
        accessible = self.parameterAsBool(parameters, self.ACCESSIBLE, context)
        try:
            r = graph.find_route(gpkg, a, b, accessible)
        except KeyError as e:
            raise QgsProcessingException(tr("No room matches '{r}'.").format(r=str(e).strip("'\"")))
        except OSError:
            raise QgsProcessingException(tr("Could not read {p}").format(p=gpkg))
        if r is None:
            raise QgsProcessingException(
                tr("There is no wheelchair route between these rooms.") if accessible else
                tr("There is no route between these rooms: check the doors in the report."))
        crs = QgsVectorLayer(f"{gpkg}|layername=units", "units", "ogr").crs()
        fields = QgsFields()
        for name, t in (("step", QMetaType.Type.Int), ("from_node", QMetaType.Type.QString),
                        ("to_node", QMetaType.Type.QString), ("kind", QMetaType.Type.QString),
                        ("level_id", QMetaType.Type.Int), ("length_m", QMetaType.Type.Double),
                        ("total_m", QMetaType.Type.Double)):
            fields.append(QgsField(name, t))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.Type.LineString, crs)
        total = 0.0
        for i, (n, st) in enumerate(zip(r["nodes"], r["steps"]), 1):
            total += st["length_m"]
            f = QgsFeature(fields)
            pts = st["pts"] if len(st["pts"]) >= 2 else st["pts"] * 2
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts]))
            f.setAttributes([i, n, r["nodes"][i], st["kind"], st["level_id"], round(st["length_m"], 2),
                             round(total, 2)])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        changes = [st["kind"] for st in r["steps"] if st["kind"] in ("stairs", "elevator", "ramp")]
        feedback.pushInfo(tr("{a} → {b}: {m} m, {n} steps").format(a=r["from_unit"], b=r["to_unit"],
                                                                  m=f"{r['length']:.1f}", n=len(r["steps"])))
        if changes:
            feedback.pushInfo(tr("Changes level by: {how}").format(how=", ".join(changes)))
        return {self.OUTPUT: dest, "LENGTH": r["length"], "NODES": r["nodes"], "FROM_UNIT": r["from_unit"],
                "TO_UNIT": r["to_unit"]}
