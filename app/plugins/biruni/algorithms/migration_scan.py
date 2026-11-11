"""Moving from ArcGIS, as tools: the readiness scan of folders (HTML + Excel report) and the import of the
ModelBuilder models of a toolbox (.atbx / .tbx) as models."""
import os

from qgis.core import (QgsProcessingException, QgsProcessingOutputNumber, QgsProcessingOutputString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFile,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterString, Qgis)

from ..i18n import tr
from .base import BiruniAlgorithm


class MigrationReadinessScan(BiruniAlgorithm):
    ID = "migrationscan"
    NAME = "Migration readiness scan"
    GROUP = "Project"
    GROUP_ID = "project"
    HELP = ("Looks through folders for everything made with ArcGIS - projects (.aprx), maps (.mapx, .mxd), layer "
            "files (.lyrx, .lyr), arcpy scripts (.py, .pyt), toolboxes and models (.atbx, .tbx), geodatabases "
            "(.gdb, .mdb), shapefiles and layouts (.pagx) - and checks each one, read-only: which layers open, "
            "which data is missing, which symbols change, which arcpy functions and model tools are missing. "
            "Writes an HTML report and an Excel workbook. Nothing is written next to your files.")

    FOLDER, MORE, LIMIT, HTML, EXCEL = "FOLDER", "MORE_FOLDERS", "TIME_LIMIT", "HTML", "EXCEL"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(self.FOLDER, tr("Folder to scan"),
                                                     Qgis.ProcessingFileParameterBehavior.Folder))
        self.addParameter(QgsProcessingParameterString(
            self.MORE, tr("More folders (separated by ;)"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.LIMIT, tr("Time limit per file (seconds)"), Qgis.ProcessingNumberParameterType.Double, 20.0,
            minValue=1.0))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.HTML, tr("HTML report"), tr("HTML files") + " (*.html)"))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.EXCEL, tr("Excel report"), tr("Excel files") + " (*.xlsx)", optional=True,
            createByDefault=False))
        self.addOutput(QgsProcessingOutputString("SUMMARY", tr("Summary")))
        self.addOutput(QgsProcessingOutputNumber("ITEMS", tr("Items checked")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..migrate import report, scanner

        folders = [self.parameterAsFile(parameters, self.FOLDER, context)]
        more = self.parameterAsString(parameters, self.MORE, context) or ""
        folders += [f.strip() for f in more.split(";") if f.strip()]
        folders = [f for f in folders if f and os.path.isdir(f)]
        if not folders:
            raise QgsProcessingException(tr("Choose at least one folder that exists."))
        limit = self.parameterAsDouble(parameters, self.LIMIT, context)

        def progress(fraction, text):
            if fraction >= 0:
                feedback.setProgress(100 * fraction)
            feedback.setProgressText(text)

        result = scanner.scan(folders, progress=progress, cancel=feedback.isCanceled, time_limit=limit)
        text = scanner.summary_text(result.items)
        feedback.pushInfo(text)
        for cat, key, files, _times in scanner.top_missing(result.items, 10):
            feedback.pushInfo(f"  {files} × {key} ({scanner.category_label(cat)})")
        html_path = self.parameterAsFileOutput(parameters, self.HTML, context)
        report.write_html(result, html_path)
        out = {self.HTML: html_path, "SUMMARY": text, "ITEMS": len(result.items)}
        xlsx = self.parameterAsFileOutput(parameters, self.EXCEL, context)
        if xlsx:
            report.write_excel(result, xlsx)
            out[self.EXCEL] = xlsx
        return out


class ImportModelBuilderModels(BiruniAlgorithm):
    ID = "importmodelbuildermodels"
    NAME = "Import ModelBuilder models"
    GROUP = "Project"
    GROUP_ID = "project"
    HELP = ("Converts the models of an ArcGIS toolbox (.atbx from ArcGIS Pro 3; .tbx toolboxes are listed only) "
            "into models of this program. Common tools (Buffer, Clip, Intersect, Dissolve, Select, Project, Merge, "
            "Union, Erase, Spatial Join, Calculate Field, Add Field, Copy Features, Feature To Point, Make Feature "
            "Layer, Select Layer By Attribute / Location) become native steps with their values; model parameters "
            "and source data become model inputs. Steps that cannot be converted stay in the model as a yellow note "
            "and their result becomes an input. A conversion report lists everything.")

    TOOLBOX, TO_MODELS, FOLDER, REPORT = "TOOLBOX", "TO_MODELS", "FOLDER", "REPORT"

    def flags(self):
        # the toolbox's list of models is refreshed at the end: main thread only
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(self.TOOLBOX, tr("ArcGIS toolbox"),
                                                     Qgis.ProcessingFileParameterBehavior.File,
                                                     fileFilter=tr("ArcGIS toolboxes") + " (*.atbx *.tbx)"))
        self.addParameter(QgsProcessingParameterBoolean(
            self.TO_MODELS, tr("Add the models to my toolbox (Models)"), True))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.FOLDER, tr("Also save the models in this folder"), optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.REPORT, tr("Conversion report"), tr("HTML files") + " (*.html)"))
        self.addOutput(QgsProcessingOutputNumber("MODELS", tr("Models converted")))
        self.addOutput(QgsProcessingOutputNumber("STEPS", tr("Steps converted")))
        self.addOutput(QgsProcessingOutputNumber("UNMAPPED", tr("Steps not converted")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..migrate import modelbuilder as mb
        from ..migrate.report import write_conversion_report

        path = self.parameterAsFile(parameters, self.TOOLBOX, context)
        try:
            box = mb.read_toolbox(path)
        except mb.ToolboxError as e:
            raise QgsProcessingException(tr("Could not read {p}").format(p=path) + f": {e}") from e
        to_models = self.parameterAsBoolean(parameters, self.TO_MODELS, context)
        folder = self.parameterAsString(parameters, self.FOLDER, context)
        convs = []
        models = box.models
        for i, info in enumerate(models):
            if feedback.isCanceled():
                break
            conv = mb.convert_model(info, path, group=tr("Imported from ArcGIS") + " - " + box.title)
            if conv.algorithm is not None:
                if folder:
                    mb.save_model(conv, folder)
                if to_models:
                    mb.save_model(conv, mb.models_folder())
            feedback.pushInfo(f"{conv.title}: " + tr("{done} of {total} steps were converted.").format(
                done=conv.converted, total=len(conv.steps)) + (f" {conv.error}" if conv.error else ""))
            convs.append(conv)
            feedback.setProgress(100 * (i + 1) / max(len(models), 1))
        if to_models:
            mb.refresh_models()
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)
        write_conversion_report(convs, report)
        return {self.REPORT: report, self.FOLDER: folder or None,
                "MODELS": sum(1 for c in convs if c.algorithm is not None),
                "STEPS": sum(c.converted for c in convs),
                "UNMAPPED": sum(len(c.unmapped) for c in convs)}
