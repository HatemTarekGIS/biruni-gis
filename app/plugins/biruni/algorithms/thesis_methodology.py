"""Write the methodology chapter of a thesis from what was done in the project."""
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputString,
                       QgsProcessingParameterEnum, QgsProcessingParameterFileDestination)

from ..i18n import lang, tr
from .base import BiruniAlgorithm

LANGS = ["Arabic", "English"]


class ThesisMethodology(BiruniAlgorithm):
    ID = "thesismethodology"
    NAME = "Write Methodology Chapter"
    GROUP = "Thesis"
    GROUP_ID = "thesis"
    HELP = ("Writes the methodology chapter of a thesis or report from what was done in this project: the data "
            "used and where it came from (with licences), the coordinate system, the Processing tools run with "
            "their settings in order, manual edits and the map layouts made. Produces the chapter as academic "
            "prose in Arabic or English, a workflow diagram (PNG and SVG), a data sources table and a references "
            "list - as a Word document when python-docx is available, otherwise as HTML (opens in Word) and "
            "Markdown.")

    def flags(self):
        # reads the project and draws with Qt: must run on the main thread
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterEnum("LANGUAGE", tr("Language of the chapter"),
                                                     [tr(x) for x in LANGS], defaultValue=0 if lang() == "ar" else 1))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUTPUT", tr("Chapter"), tr("Web page, opens in Word (*.html);;Word document (*.docx);;Markdown (*.md)")))
        self.addOutput(QgsProcessingOutputString("FILES", tr("Written files")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from ..thesis.methodology import export_methodology, methodology_steps

        path = self.parameterAsFileOutput(parameters, "OUTPUT", context)
        if not path:
            raise QgsProcessingException(tr("Choose where to save the chapter."))
        code = "ar" if self.parameterAsEnum(parameters, "LANGUAGE", context) == 0 else "en"
        project = context.project()
        steps = methodology_steps(project) if project is not None else methodology_steps()
        files = export_methodology(path, code, steps, project)
        for f in files:
            feedback.pushInfo(f)
        return {"OUTPUT": files[0], "FILES": " | ".join(files)}
