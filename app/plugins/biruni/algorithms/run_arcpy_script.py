import json
import os
import shlex

from qgis.core import (Qgis, QgsProcessingContext, QgsProcessingException, QgsProcessingOutputString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFile, QgsProcessingParameterString,
                       QgsRasterLayer, QgsVectorLayer)

from ..i18n import tr
from .base import BiruniAlgorithm


def split_arguments(text):
    """One argument per line when there are several lines, else shell-style words (backslashes kept)."""
    text = (text or "").strip()
    if not text:
        return []
    if "\n" in text:
        return [ln.strip().strip('"') for ln in text.splitlines() if ln.strip()]
    lex = shlex.shlex(text, posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    return list(lex)


class RunArcpyScript(BiruniAlgorithm):
    ID = "runarcpyscript"
    NAME = "Run ArcGIS Python (arcpy) script"
    GROUP = "Project"
    GROUP_ID = "project"
    HELP = ("Runs an existing ArcGIS Python script (.py using arcpy) inside Biruni, without ArcGIS. The common "
            "arcpy functions, cursors and tools are provided by Biruni; a call that is not available yet stops "
            "the script with a clear message naming it. Script arguments are what the script reads with "
            "GetParameterAsText (one per line, or separated by spaces with quotes around paths that contain "
            "spaces). Messages, warnings and errors of the script appear in the log. Old ArcGIS 10 (Python 2) "
            "scripts are converted on the fly for print statements and similar old syntax.")

    SCRIPT, ARGS, WORKSPACE, ADD = "SCRIPT", "ARGS", "WORKSPACE", "ADD_OUTPUTS"
    OUTPUTS, CREATED = "OUTPUTS", "CREATED"

    def flags(self):
        # scripts may touch the open project (arcpy.mp 'CURRENT'), so they run on the main thread
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.SCRIPT, tr("ArcGIS Python script"), Qgis.ProcessingFileParameterBehavior.File, "py"))
        self.addParameter(QgsProcessingParameterString(
            self.ARGS, tr("Script arguments (GetParameterAsText)"), "", multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.WORKSPACE, tr("Workspace (arcpy.env.workspace, optional)"),
            Qgis.ProcessingFileParameterBehavior.Folder, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ADD, tr("Add the datasets the script creates to the map"), True))
        self.addOutput(QgsProcessingOutputString(self.OUTPUTS, tr("Output parameters")))
        self.addOutput(QgsProcessingOutputString(self.CREATED, tr("Created datasets")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from .. import arcpy_compat

        path = self.parameterAsFile(parameters, self.SCRIPT, context)
        if not path or not os.path.isfile(path):
            raise QgsProcessingException(tr("Choose an existing .py script."))
        args = split_arguments(self.parameterAsString(parameters, self.ARGS, context))
        workspace = self.parameterAsFile(parameters, self.WORKSPACE, context) or None
        add = self.parameterAsBoolean(parameters, self.ADD, context)

        def on_message(sev, text):
            if sev == 2:
                feedback.reportError(text, False)
            elif sev == 1:
                feedback.pushWarning(text)
            else:
                feedback.pushInfo(text)

        def on_progress(kind, value):
            if kind == "percent":
                feedback.setProgress(float(value))
            elif kind == "label" and value:
                feedback.setProgressText(str(value))

        feedback.pushInfo(tr("Running {0}").format(os.path.basename(path)))
        res = arcpy_compat.run_script(path, args, on_message, on_progress, workspace, feedback.isCanceled)
        if res["error"] == "Cancelled":
            raise QgsProcessingException(tr("The script was cancelled."))
        if not res["ok"]:
            raise QgsProcessingException(tr("The script stopped: {0}").format(res["error"] or ""))
        if add:
            self._load(res["created"], context)
        outputs = {str(k): str(v) for k, v in res["outputs"].items()}
        if outputs:
            feedback.pushInfo(tr("Output parameters: {0}").format(json.dumps(outputs, ensure_ascii=False)))
        return {self.OUTPUTS: json.dumps(outputs, ensure_ascii=False), self.CREATED: ";".join(res["created"])}

    def _load(self, created, context):
        from ..arcpy_compat import data as D
        for path in created:
            try:
                ref = D.parse(path)
            except Exception:  # noqa: BLE001
                continue
            if ref.kind in ("mem", "folder", "gdbws", "gpkgws", "fd") or not D.exists_ref(ref):
                continue
            name = ref.base_name or os.path.basename(path)
            if ref.kind == "raster":
                lyr = QgsRasterLayer(ref.container, name)
            else:
                uri = ref.container + (f"|layername={ref.name}" if ref.name else "")
                lyr = QgsVectorLayer(uri, name, "ogr")
            if not lyr.isValid():
                continue
            context.temporaryLayerStore().addMapLayer(lyr)
            context.addLayerToLoadOnCompletion(lyr.id(), QgsProcessingContext.LayerDetails(
                name, context.project(), self.CREATED))
