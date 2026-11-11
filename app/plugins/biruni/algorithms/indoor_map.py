import os

from qgis.core import (Qgis, QgsProcessingContext, QgsProcessingException, QgsProcessingLayerPostProcessorInterface,
                       QgsProcessingParameterCrs, QgsProcessingParameterEnum, QgsProcessingParameterFile,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProject, QgsVectorLayer)

from ..i18n import tr
from ..survey import dxf as dxfmod
from .base import BiruniAlgorithm

UNITS = [("Automatic (from the drawing)", 0.0), ("Metres", 1.0), ("Centimetres", 0.01), ("Millimetres", 0.001)]


class _Styler(QgsProcessingLayerPostProcessorInterface):
    """Styles an indoor layer once it is in the project; the navigation graph starts hidden."""

    instances = []  # keep Python references alive until QGIS has run them

    def __init__(self, kind, hidden):
        super().__init__()
        self.kind, self.hidden = kind, hidden
        _Styler.instances.append(self)

    def postProcessLayer(self, layer, context, feedback):  # noqa: N802
        if layer is None:
            return
        from ..indoor import style
        style.style_layer(layer, self.kind)
        if self.hidden:
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(False)


class FloorPlanToIndoorMap(BiruniAlgorithm):
    ID = "floorplantoindoor"
    NAME = "Floor plan to indoor map"
    GROUP = "Survey & CAD"
    GROUP_ID = "survey"
    HELP = ("Turns architectural floor plans (DXF, or DWG) into an indoor map: rooms and corridors as units with "
            "their name, number and category, doors and openings with their width and the two units they join, "
            "and a navigation graph for routing (also wheelchair routes: no stairs, no narrow doors). "
            "Walls are found from wall layers (WALL, A-WALL, جدار, حوائط…), door gaps are closed at the door "
            "swing arcs and door blocks, room names and numbers are read from the texts and block attributes "
            "inside each room (Arabic and English; old Arabic CAD fonts are decoded). Give one plan per level: the "
            "main plan with its level number, and the other plans as 'file = level' pairs (0 = ground, 1, 2, -1 "
            "basement); stairs and elevators that overlap between levels are linked. The plans must be in the "
            "same coordinates; to place them on the map first use Move CAD/Scan to Real Coordinates. "
            "Writes indoor.gpkg, indoor_imdf.zip (Apple IMDF, when the coordinate system is known) and "
            "indoor_report.txt with what to check by hand.")

    INPUT, LEVEL, MORE, CRS, UNITS, DOOR_MIN, GAP, MIN_WIDTH, STOREY, ENCODING, OUTPUT = (
        "INPUT", "LEVEL", "MORE", "CRS", "UNITS", "DOOR_MIN", "GAP", "MIN_WIDTH", "STOREY", "ENCODING", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("Floor plan (DXF or DWG)"), fileFilter="CAD (*.dxf *.DXF *.dwg *.DWG);;All files (*.*)"))
        self.addParameter(QgsProcessingParameterNumber(
            self.LEVEL, tr("Level of this plan (0 = ground, 1, 2, -1 = basement)"),
            Qgis.ProcessingNumberParameterType.Integer, 0, minValue=-20, maxValue=200))
        self.addParameter(QgsProcessingParameterString(
            self.MORE, tr("Other levels: plan file = level (one per line, or separated by ';'; ':name' optional)"),
            multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the drawing"), "ProjectCrs"))
        self.addParameter(QgsProcessingParameterEnum(self.UNITS, tr("Drawing units"), [tr(u[0]) for u in UNITS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.DOOR_MIN, tr("Narrowest door a wheelchair can pass (m)"), Qgis.ProcessingNumberParameterType.Double,
            0.90, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.GAP, tr("Close wall gaps up to (m)"), Qgis.ProcessingNumberParameterType.Double, 1.2, minValue=0.0))
        p = QgsProcessingParameterNumber(
            self.MIN_WIDTH, tr("Narrowest space (m) - thinner faces are walls"),
            Qgis.ProcessingNumberParameterType.Double, 0.5, minValue=0.05)
        p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(p)
        p = QgsProcessingParameterNumber(
            self.STOREY, tr("Height between floors (m)"), Qgis.ProcessingNumberParameterType.Double, 3.5, minValue=0.5)
        p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(p)
        p = QgsProcessingParameterEnum(self.ENCODING, tr("Text encoding"), [tr(e[1]) for e in dxfmod.ENCODINGS],
                                       defaultValue=0)
        p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT, tr("Output folder")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..indoor import pipeline
        from ..indoor.recognize import Options

        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Could not read {p}").format(p=path))
        plans = [(path, self.parameterAsInt(parameters, self.LEVEL, context), "")]
        more = self.parameterAsString(parameters, self.MORE, context)
        try:
            plans += pipeline.parse_plans(more, os.path.dirname(path))
        except ValueError as e:
            raise QgsProcessingException(tr("Cannot read the plan list near '{t}': write it as 'file = level'.").format(
                t=str(e)))
        for p, _lvl, _n in plans:
            if not os.path.exists(p):
                raise QgsProcessingException(tr("Could not read {p}").format(p=p))
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if crs.isValid() and crs.isGeographic():
            raise QgsProcessingException(tr("The drawing must be in a projected coordinate system in metres, not in "
                                            "degrees."))
        out_dir = self.parameterAsString(parameters, self.OUTPUT, context)
        opts = Options(gap_tolerance=self.parameterAsDouble(parameters, self.GAP, context),
                       min_room_width=self.parameterAsDouble(parameters, self.MIN_WIDTH, context))
        try:
            result = pipeline.run(
                plans, crs, out_dir, opts,
                min_door_width=self.parameterAsDouble(parameters, self.DOOR_MIN, context),
                storey=self.parameterAsDouble(parameters, self.STOREY, context),
                units=UNITS[self.parameterAsEnum(parameters, self.UNITS, context)][1],
                encoding=dxfmod.ENCODINGS[self.parameterAsEnum(parameters, self.ENCODING, context)][0],
                feedback=feedback)
        except ValueError as e:
            if str(e) == "binary DXF":
                raise QgsProcessingException(tr("Binary DXF files are not supported: save the drawing as an ASCII "
                                                "DXF."))
            raise QgsProcessingException(str(e))
        except OSError as e:
            raise QgsProcessingException(tr("Could not read {p}").format(p=str(e)))
        gpkg = result["gpkg"]
        for line in result["report"].splitlines():
            feedback.pushInfo(line)
        if result["imdf_skipped"]:
            feedback.pushWarning(tr("IMDF was not written: {why}.").format(why=result["imdf_skipped"]))
        self._load(gpkg, result["levels"], context)
        n_units = sum(len(r.units) for *_x, r in result["levels"])
        n_doors = sum(len(r.doors) for *_x, r in result["levels"])
        feedback.setProgress(100)
        return {self.OUTPUT: out_dir, "GPKG": gpkg, "IMDF": result["imdf"], "REPORT": result["report_path"],
                "UNITS": n_units, "OPENINGS": n_doors, "LEVELS": len(result["levels"])}

    def _load(self, gpkg, levels, context):
        titles = {"levels": tr("Outline"), "units": tr("Rooms and spaces"), "openings": tr("Doors and openings"),
                  "nav_edges": tr("Navigation graph"), "nav_nodes": tr("Navigation nodes")}
        order = ["units", "openings", "nav_edges", "nav_nodes", "levels"]
        key = 0
        for level_id, name, _elev, _res in sorted(levels, key=lambda t: t[0]):
            group = tr("Indoor map - {name} ({level})").format(name=name, level=level_id)
            for kind in order:
                # filter in the source: setSubsetString() opens a failing second GeoPackage connection
                lyr = QgsVectorLayer(f'{gpkg}|layername={kind}|subset="level_id" = {int(level_id)}', titles[kind],
                                     "ogr")
                if not lyr.isValid():
                    continue
                context.temporaryLayerStore().addMapLayer(lyr)
                details = QgsProcessingContext.LayerDetails(titles[kind], context.project(), self.OUTPUT)
                try:
                    details.groupName = group
                    details.layerSortKey = 1000 - key
                except AttributeError:
                    pass
                key += 1
                details.setPostProcessor(_Styler(kind, kind.startswith("nav_")))
                context.addLayerToLoadOnCompletion(lyr.id(), details)
        if len(levels) > 1:
            title = tr("Links between levels")
            lyr = QgsVectorLayer(f'{gpkg}|layername=nav_edges|subset="level_id" = -999', title, "ogr")
            if lyr.isValid():
                context.temporaryLayerStore().addMapLayer(lyr)
                details = QgsProcessingContext.LayerDetails(title, context.project(), self.OUTPUT)
                try:
                    details.groupName = tr("Indoor map")
                except AttributeError:
                    pass
                details.setPostProcessor(_Styler("nav_edges", True))
                context.addLayerToLoadOnCompletion(lyr.id(), details)
