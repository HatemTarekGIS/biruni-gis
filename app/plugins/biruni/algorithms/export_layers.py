import os
import re
import shutil

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterMultipleLayers,
                       QgsVectorFileWriter)

from ..i18n import tr
from .base import BiruniAlgorithm

# (label, OGR driver, extension, one file for all layers, extra layer options)
FORMATS = [
    ("GeoPackage - all layers in one file", "GPKG", "gpkg", True, []),
    ("Shapefile", "ESRI Shapefile", "shp", False, ["ENCODING=UTF-8"]),
    ("GeoJSON", "GeoJSON", "geojson", False, []),
    ("KML (Google Earth)", "KML", "kml", False, []),
    ("CSV with WKT geometry", "CSV", "csv", False, ["GEOMETRY=AS_WKT"]),
    ("Excel (xlsx)", "XLSX", "xlsx", False, []),
    ("DXF (CAD)", "DXF", "dxf", False, []),
    ("Esri File Geodatabase (.gdb)", "OpenFileGDB", "gdb", True, ["TARGET_ARCGIS_VERSION=ALL"]),
]


def safe_name(text, driver=""):
    if driver == "OpenFileGDB":  # ArcGIS table names: letters, digits, "_"; no leading digit
        s = re.sub(r"\W+", "_", text).strip("_")[:120]
        return s if s and not s[0].isdigit() else f"L_{s}"
    s = re.sub(r'[\\/:*?"<>|]+', "_", text).strip(" .")
    return s or "layer"


class ExportLayers(BiruniAlgorithm):
    ID = "exportlayers"
    NAME = "Export layers to a folder"
    GROUP = "Data management"
    GROUP_ID = "data"
    HELP = ("Exports several vector layers at once to a folder, in one format: GeoPackage, Shapefile, "
            "GeoJSON, KML, CSV, Excel, DXF or Esri File Geodatabase. Optionally reprojects them and exports "
            "only the selected features.")

    LAYERS, FORMAT, CRS, SELECTED, FOLDER = "LAYERS", "FORMAT", "CRS", "SELECTED", "FOLDER"

    def flags(self):
        # reads project layers directly (selection) - run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LAYERS, tr("Layers"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterEnum(self.FORMAT, tr("Format"), [tr(f[0]) for f in FORMATS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Reproject to (optional)"), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.SELECTED, tr("Selected features only"), False))
        self.addParameter(QgsProcessingParameterFolderDestination(self.FOLDER, tr("Output folder")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layers = self.parameterAsLayerList(parameters, self.LAYERS, context)
        label, driver, ext, single, layer_opts = FORMATS[self.parameterAsEnum(parameters, self.FORMAT, context)]
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        selected = self.parameterAsBoolean(parameters, self.SELECTED, context)
        folder = self.parameterAsString(parameters, self.FOLDER, context)
        os.makedirs(folder, exist_ok=True)
        if driver == "KML" and not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4326")

        if driver == "DXF":
            # QGIS's own DXF writer: one drawing, one CAD layer per GIS layer, with symbology.
            import processing

            path = os.path.join(folder, "export.dxf")
            processing.run("native:dxfexport", {
                "LAYERS": [lyr.id() for lyr in layers], "SYMBOLOGY_MODE": 0, "SYMBOLOGY_SCALE": 1000,
                "CRS": crs if crs.isValid() else layers[0].crs(), "SELECTED_FEATURES_ONLY": selected,
                "USE_LAYER_TITLE": True, "FORCE_2D": False, "MTEXT": True,
                "EXPORT_LINES_WITH_ZERO_WIDTH": False, "OUTPUT": path,
            }, context=context, feedback=feedback, is_child_algorithm=True)
            return {self.FOLDER: folder, "COUNT": len(layers)}

        used, written = set(), []
        single_path = os.path.join(folder, f"export.{ext}")
        if single and driver == "OpenFileGDB" and os.path.isdir(single_path):
            shutil.rmtree(single_path)  # a geodatabase is a folder: replace it like the GeoPackage file
        for i, layer in enumerate(layers):
            if feedback.isCanceled():
                break
            base = safe_name(layer.name(), driver)
            name, k = base, 2
            while name.lower() in used:
                name, k = f"{base}_{k}", k + 1
            used.add(name.lower())
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = driver
            opts.fileEncoding = "UTF-8"
            opts.layerName = name
            opts.layerOptions = layer_opts
            opts.onlySelectedFeatures = selected
            if crs.isValid() and crs != layer.crs():
                opts.ct = QgsCoordinateTransform(layer.crs(), crs, context.transformContext())
            if single:
                path = single_path
                opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                             if os.path.exists(single_path) and written
                                             else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
            else:
                path = os.path.join(folder, f"{name}.{ext}")
            err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, context.transformContext(), opts)
            if err != QgsVectorFileWriter.WriterError.NoError:
                feedback.reportError(tr("{layer}: {msg}").format(layer=layer.name(), msg=msg))
            else:
                written.append(path)
                feedback.pushInfo(tr("Exported {layer}").format(layer=layer.name()))
            feedback.setProgress(100 * (i + 1) / max(len(layers), 1))
        if not written:
            raise QgsProcessingException(tr("Nothing was exported."))
        return {self.FOLDER: folder, "COUNT": len(written)}
