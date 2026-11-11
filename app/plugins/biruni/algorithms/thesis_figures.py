"""Thesis map figures as Processing tools: lay every figure out in a university style, and export all figures at
print resolution with the list of figures (see thesis/figures.py)."""
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputFolder,
                       QgsProcessingOutputNumber, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFile, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterNumber, Qgis)

from ..i18n import tr
from .base import BiruniAlgorithm

FORMATS = ["png", "jpg", "pdf"]


def _figures():
    from ..thesis import figures

    return figures


class ApplyThesisStyle(BiruniAlgorithm):
    ID = "thesisfigurestyle"
    NAME = "Apply a university style to the thesis figures"
    GROUP = "Thesis"
    GROUP_ID = "thesis"
    HELP = ("Lays out every thesis figure of the project (print layouts with a figure number) in one style: page "
            "and margins, Arabic and Latin fonts, coordinate grid, north arrow, scale bar, legend, source line and "
            "the numbered caption («شكل (3-2): …» / \"Figure 3.2: …\"). Choose a built-in style or a style file "
            "shared by a classmate.")

    STYLE, STYLE_FILE = "STYLE", "STYLE_FILE"

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self._styles = _figures().all_styles()
        self.addParameter(QgsProcessingParameterEnum(self.STYLE, tr("Style"), [tr(s["name"]) for s in self._styles],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(self.STYLE_FILE, tr("Or a style file (.json)"),
                                                     extension="json", optional=True))
        self.addOutput(QgsProcessingOutputNumber("FIGURES", tr("Figures laid out")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        fig = _figures()
        path = self.parameterAsFile(parameters, self.STYLE_FILE, context)
        if path:
            try:
                style = fig.import_style(path, keep=False)
            except (OSError, ValueError) as e:
                raise QgsProcessingException(str(e)) from e
        else:
            styles = getattr(self, "_styles", None) or fig.all_styles()
            k = self.parameterAsEnum(parameters, self.STYLE, context)
            style = styles[max(0, min(len(styles) - 1, k))]
        project = context.project()
        done = fig.apply_style_all(style, project)
        if not done:
            feedback.pushWarning(tr("The project has no thesis figures yet."))
        for f in done:
            feedback.pushInfo(f"{f['label']} - {f['layout']}")
        return {"FIGURES": len(done)}


class ExportThesisFigures(BiruniAlgorithm):
    ID = "thesisfiguresexport"
    NAME = "Export all thesis figures"
    GROUP = "Thesis"
    GROUP_ID = "thesis"
    HELP = ("Exports every thesis figure of the project at print resolution, named by its number (Fig_3-2.png, "
            "Fig_3-2.pdf …), and writes the list of figures with the captions in Arabic and English (Word when "
            "python-docx is installed, otherwise a web page that Word opens and a text file).")

    FOLDER, DPI, FORMATS_, LIST = "FOLDER", "DPI", "FORMATS", "LIST"

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFolderDestination(self.FOLDER, tr("Folder")))
        self.addParameter(QgsProcessingParameterNumber(self.DPI, tr("Resolution (dpi)"),
                                                       Qgis.ProcessingNumberParameterType.Integer, 300, minValue=72,
                                                       maxValue=1200))
        self.addParameter(QgsProcessingParameterEnum(self.FORMATS_, tr("Formats"), ["PNG", "JPG", "PDF"],
                                                     allowMultiple=True, defaultValue=[0]))
        self.addParameter(QgsProcessingParameterBoolean(self.LIST, tr("Write the list of figures"), True))
        self.addOutput(QgsProcessingOutputFolder("OUTPUT", tr("Folder")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        fig = _figures()
        folder = self.parameterAsString(parameters, self.FOLDER, context)
        dpi = self.parameterAsInt(parameters, self.DPI, context)
        formats = [FORMATS[k] for k in self.parameterAsEnums(parameters, self.FORMATS_, context)] or ["png"]
        project = context.project()
        if not fig.figures(project):
            raise QgsProcessingException(tr("The project has no thesis figures yet."))
        res = fig.export_all(folder, dpi, formats, project, self.parameterAsBool(parameters, self.LIST, context),
                             feedback)
        for name in res["failed"]:
            feedback.reportError(tr("Not exported: {what}").format(what=name))
        feedback.pushInfo(tr("{n} files written").format(n=len(res["files"])))
        return {"OUTPUT": folder, "FILES": res["files"], "LIST": res["list"]}
