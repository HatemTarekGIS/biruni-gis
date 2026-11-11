"""The buttons of the shapefile that remembers its arcs: write the current layer, open such a file with its
arcs back, and the offer made when a shapefile that has arcs kept beside it is added to the map."""
import os

from qgis.core import Qgis, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox, QPushButton

from ..i18n import tr
from ..version import APP_NAME
from . import io

DIR_KEY = "biruni/shparc/dir"
MARK = "biruni/shparc-from"     # on a layer restored from a shapefile: that shapefile's path


def _bar(plugin, text, level=Qgis.MessageLevel.Success, seconds=14):
    plugin.iface.messageBar().pushMessage(APP_NAME, text, level, seconds)


def _start_dir():
    return QgsSettings().value(DIR_KEY, "") or QgsProject.instance().homePath() or os.path.expanduser("~")


def export_report(rep, path):
    """What was written, in words."""
    if rep["error"]:
        return tr("The shapefile could not be written: {e}").format(e=rep["error"]), Qgis.MessageLevel.Critical
    if not rep["arcs"]:
        return (tr("{f} features written. The layer has no arcs, so it is an ordinary shapefile.").format(f=rep["features"]),
                Qgis.MessageLevel.Success)
    text = tr("{f} features written with {a} arcs. Any program opens the file: the areas are exact and the lengths "
              "at most {d} mm longer. The arcs are kept in {c}.").format(
        f=rep["features"], a=rep["arcs"], d=f"{rep['max_length_diff'] * 1000:.2f}", c=os.path.basename(io.sidecar(path)))
    if rep["not_rounded"]:
        text += " " + tr("{n} very long arcs are a centimetre off when their length is rounded.").format(n=rep["not_rounded"])
    return text, Qgis.MessageLevel.Success


def export_ui(plugin):
    lyr = plugin.iface.activeLayer()
    if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial():
        _bar(plugin, tr("Open a vector layer first."), Qgis.MessageLevel.Warning, 8)
        return None
    mw = plugin.iface.mainWindow()
    start = lyr.customProperty(MARK, "") or os.path.join(_start_dir(), lyr.name() + ".shp")
    path, _f = QFileDialog.getSaveFileName(mw, tr("Shapefile with its arcs"), start, tr("Shapefile") + " (*.shp)")
    if not path:
        return None
    if not path.lower().endswith(".shp"):
        path += ".shp"
    only = False
    if lyr.selectedFeatureCount():
        only = QMessageBox.question(mw, tr("Shapefile with its arcs"), tr(
            "Write only the {n} selected features?").format(n=lyr.selectedFeatureCount())) == QMessageBox.StandardButton.Yes
    QgsSettings().setValue(DIR_KEY, os.path.dirname(path))
    rep = io.export(lyr, path, only_selected=only)
    text, level = export_report(rep, path)
    _bar(plugin, text, level)
    activity = getattr(plugin, "activity", None)
    if activity is not None and not rep["error"]:
        activity.log("shapefile_arcs", "export", os.path.basename(path), path, rep["features"])
    return rep


def add(plugin, path, replace=None):
    """Add the shapefile at ``path`` with its arcs back; ``replace``: the plain layer it takes the place of."""
    layer, rep = io.restore(path, replace.name() if replace is not None else None)
    if layer is None:
        _bar(plugin, tr("The shapefile could not be opened."), Qgis.MessageLevel.Warning, 8)
        return None
    layer.setCustomProperty(MARK, path)
    if replace is not None:
        try:
            layer.setRenderer(replace.renderer().clone())
        except Exception:  # noqa: BLE001 - a style that does not fit: the default one
            pass
    QgsProject.instance().addMapLayer(layer)
    if replace is not None:
        QgsProject.instance().removeMapLayer(replace.id())
    if not rep["restored"] and not rep["changed"]:
        text = tr("This shapefile has no arcs kept beside it; it is added as it is.")
    else:
        text = tr("{r} features got their arcs back.").format(r=rep["restored"])
        if rep["changed"]:
            text += " " + tr("{c} were changed by another program since: they are as that program left them.").format(
                c=rep["changed"])
    _bar(plugin, text)
    return layer


def open_ui(plugin):
    path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Open a shapefile with its arcs"), _start_dir(),
                                           tr("Shapefile") + " (*.shp)")
    if not path:
        return None
    QgsSettings().setValue(DIR_KEY, os.path.dirname(path))
    return add(plugin, path)


def install(plugin):
    """A shapefile added to the map that has arcs kept beside it: offer to bring them back."""
    project = QgsProject.instance()

    def offer(lyr):
        try:
            if project.mapLayer(lyr.id()) is None:
                return
            path = lyr.source().split("|")[0]
        except RuntimeError:
            return
        bar = plugin.iface.messageBar()
        item = bar.createMessage(APP_NAME, tr("{name} remembers its arcs.").format(name=lyr.name()))
        btn = QPushButton(tr("Restore the arcs"))

        def go(_=False):
            bar.popWidget(item)
            add(plugin, path, replace=project.mapLayer(lyr.id()))
        btn.clicked.connect(go)
        item.layout().addWidget(btn)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 20)

    def on_added(layers):
        for lyr in layers:
            if isinstance(lyr, QgsVectorLayer) and lyr.providerType() == "ogr" and io.remembers(lyr.source().split("|")[0]):
                QTimer.singleShot(0, lambda lyr=lyr: offer(lyr))

    project.layersAdded.connect(on_added)

    def undo():
        try:
            project.layersAdded.disconnect(on_added)
        except (TypeError, RuntimeError):
            pass
    return undo
