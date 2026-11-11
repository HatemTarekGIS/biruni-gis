import os

from qgis.core import (Qgis, QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterMultipleLayers, QgsProject)

from ..i18n import tr
from .base import BiruniAlgorithm

FORMATS = [("ArcGIS Pro project (.aprx)", ".aprx"), ("ArcGIS Pro map file (.mapx)", ".mapx"),
           ("ArcGIS Pro layer file (.lyrx)", ".lyrx")]


class ExportToArcGISPro(BiruniAlgorithm):
    ID = "exporttoarcgispro"
    NAME = "Export to ArcGIS Pro"
    GROUP = "Data management"
    GROUP_ID = "data"
    HELP = ("Saves the map for ArcGIS Pro, so you can go back to Pro or share the work with Pro users: a project "
            "(.aprx) with the map and the print layouts, a map file (.mapx) or a layer file (.lyrx). Symbology "
            "(single symbol, categories, graduated classes), labels, filters, transparency, scale ranges and groups "
            "are kept. Shapefiles, file geodatabases, GeoPackages and rasters stay where they are; other data "
            "(temporary layers, GeoJSON, KML, PostGIS...) can be copied into a file geodatabase next to the output. "
            "Leave Layers empty to save every layer of the map. The log lists everything that could not be "
            "converted.")

    FORMAT, LAYERS, COPY, LAYOUTS, OUTPUT = "FORMAT", "LAYERS", "COPY_DATA", "LAYOUTS", "OUTPUT"

    def flags(self):
        # reads the project's layer tree, symbology and layouts - run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterEnum(self.FORMAT, tr("Format"), [tr(f[0]) for f in FORMATS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LAYERS, tr("Layers (empty = the whole map)"), Qgis.ProcessingSourceType.MapLayer, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.COPY, tr("Copy data ArcGIS Pro cannot open into a file geodatabase next to the output"), True))
        self.addParameter(QgsProcessingParameterBoolean(self.LAYOUTS, tr("Include print layouts (.aprx)"), True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, tr("Output file"), tr("ArcGIS Pro files") + " (*.aprx *.mapx *.lyrx)"))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..arcgis.writer import WriteError, write_aprx, write_lyrx, write_mapx

        project = context.project() or QgsProject.instance()
        label, ext = FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        layers = self.parameterAsLayerList(parameters, self.LAYERS, context) or None
        copy = self.parameterAsBoolean(parameters, self.COPY, context)
        layouts = self.parameterAsBoolean(parameters, self.LAYOUTS, context)
        path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        if os.path.splitext(path)[1].lower() != ext:
            path = os.path.splitext(path)[0] + ext
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            if ext == ".aprx":
                if layers:
                    feedback.pushWarning(tr("A project always contains the whole map: the layer choice is ignored."))
                report = write_aprx(path, project, copy, layouts)
            elif ext == ".mapx":
                report = write_mapx(path, project, copy, items=layers)
            else:
                if layers is None:
                    layers = [n.layer() for n in project.layerTreeRoot().findLayers() if n.layer() is not None]
                report = write_lyrx(path, layers, project, copy)
        except WriteError as e:
            raise QgsProcessingException(str(e)) from e
        for name, kind in report.written:
            feedback.pushInfo(tr("Written: {layer}").format(layer=name))
        for name, where in report.copied:
            feedback.pushInfo(tr("{layer}: data copied to {where}").format(layer=name, where=where))
        for name in report.layouts:
            feedback.pushInfo(tr("Layout written: {name}").format(name=name))
        for name, why in report.skipped:
            feedback.reportError(tr("Not converted: {layer} - {why}").format(layer=name, why=why))
        for w in report.warnings:
            feedback.pushWarning(w)
        return {self.OUTPUT: path, "WRITTEN": len(report.written), "SKIPPED": len(report.skipped)}
