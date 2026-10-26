"""User interface for opening ArcGIS Pro files: a command (file dialog + map
picker + result summary), drag & drop onto Biruni, and .aprx/.mapx in the
standard "Open Project" dialog."""
import os

from qgis.core import QgsCoordinateTransform, QgsProject, QgsRectangle, QgsSettings
from qgis.gui import QgsCustomDropHandler, QgsCustomProjectOpenHandler
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog, QLabel,
                                 QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from .reader import ARCGIS_EXTENSIONS, ArcGISError, list_maps, open_arcgis_file

_LAST_DIR = "biruni/arcgis-last-dir"


def _file_filter():
    return (tr("ArcGIS files") + " (*.aprx *.mapx *.lyrx *.lpkx *.mpkx *.ppkx *.mxd *.mxt);;"
            + tr("ArcGIS Pro project") + " (*.aprx);;" + tr("Map file") + " (*.mapx);;"
            + tr("Layer file") + " (*.lyrx);;" + tr("ArcMap document") + " (*.mxd *.mxt)")


def open_arcgis_dialog(plugin):
    """File > Open ArcGIS Pro project/map/layer file."""
    s = QgsSettings()
    path, _ = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Open an ArcGIS Pro file"),
                                          s.value(_LAST_DIR, os.path.expanduser("~")), _file_filter())
    if not path:
        return None
    s.setValue(_LAST_DIR, os.path.dirname(path))
    return open_path(plugin, path)


class MapPicker(QDialog):
    """Choose which maps of a project to open (the first is pre-selected)."""

    def __init__(self, parent, names):
        super().__init__(parent)
        self.setWindowTitle(tr("Choose maps"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("This project has {n} maps. Which ones do you want to open?").format(n=len(names))))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for i, n in enumerate(names):
            it = QListWidgetItem(n)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if i == 0 else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self):
        return [self.list.item(i).text() for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class ResultDialog(QDialog):
    """What was added, what was skipped and why."""

    def __init__(self, parent, report):
        super().__init__(parent)
        self.setWindowTitle(tr("ArcGIS Pro file opened"))
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        head = QLabel(tr("{n} layers were added from {file}.").format(
            n=len(report.added), file=os.path.basename(report.source)))
        head.setWordWrap(True)
        lay.addWidget(head)
        tree = QTreeWidget()
        tree.setHeaderLabels([tr("Layer"), tr("Note")])
        tree.setRootIsDecorated(True)
        if report.skipped:
            top = QTreeWidgetItem([tr("Not added ({n})").format(n=len(report.skipped)), ""])
            for name, why in report.skipped:
                QTreeWidgetItem(top, [name, why])
            tree.addTopLevelItem(top)
            top.setExpanded(True)
        if report.warnings:
            top = QTreeWidgetItem([tr("Added with changes ({n})").format(n=len(report.warnings)), ""])
            for w in report.warnings:
                name, _, note = w.partition(": ")
                QTreeWidgetItem(top, [name, note])
            tree.addTopLevelItem(top)
            top.setExpanded(True)
        tree.resizeColumnToContents(0)
        lay.addWidget(tree)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


def open_path(plugin, path, maps=None):
    """Open one ArcGIS file into the current project; returns the Report (or None)."""
    mw = plugin.iface.mainWindow()
    project = QgsProject.instance()
    was_empty = not project.mapLayers()
    try:
        if maps is None and os.path.splitext(path)[1].lower() != ".lyrx":
            names = list_maps(path)
            if len(names) > 1:
                dlg = MapPicker(mw, names)
                if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.chosen():
                    return None
                maps = dlg.chosen()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = open_arcgis_file(path, project, maps)
        finally:
            QApplication.restoreOverrideCursor()
    except ArcGISError as e:
        plugin.iface.messageBar().pushWarning(APP_NAME, str(e))
        return None
    except (OSError, ValueError) as e:
        plugin.iface.messageBar().pushWarning(APP_NAME, tr("Could not read {p}").format(p=path) + f": {e}")
        return None

    canvas = plugin.iface.mapCanvas()
    if was_empty and report.layers:
        target = canvas.mapSettings().destinationCrs()
        rect = QgsRectangle()
        if report.extent is not None:
            rect = QgsCoordinateTransform(report.extent.crs(), target, project).transformBoundingBox(report.extent)
        else:
            for lyr in report.layers:
                rect.combineExtentWith(QgsCoordinateTransform(lyr.crs(), target, project)
                                       .transformBoundingBox(lyr.extent()))
        if not rect.isEmpty():
            canvas.setExtent(rect)
        canvas.refresh()
    if report.skipped or report.warnings:
        ResultDialog(mw, report).exec()
    else:
        plugin.iface.messageBar().pushSuccess(APP_NAME, tr("{n} layers were added from {file}.").format(
            n=len(report.added), file=os.path.basename(path)))
    return report


def _is_arcgis(path):
    return os.path.splitext(path or "")[1].lower() in ARCGIS_EXTENSIONS


class ArcGISDropHandler(QgsCustomDropHandler):
    """Dropping an ArcGIS Pro file on Biruni opens it."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def customUriProviderKey(self):  # noqa: N802
        return "biruni_arcgis"

    def handleFileDrop(self, file):  # noqa: N802
        if not _is_arcgis(file):
            return False
        open_path(self.plugin, file)
        return True


class ArcGISProjectOpenHandler(QgsCustomProjectOpenHandler):
    """Lets File > Open Project open .aprx, .mapx and .mxd files directly."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def filters(self):
        return [tr("ArcGIS Pro project") + " (*.aprx *.APRX)", tr("Map file") + " (*.mapx *.MAPX)",
                tr("ArcMap document") + " (*.mxd *.MXD)"]

    def handleProjectOpen(self, file):  # noqa: N802
        if os.path.splitext(file)[1].lower() not in (".aprx", ".mapx", ".mxd"):
            return False
        if self.plugin.iface.newProject(True):  # asks to save the current project first
            open_path(self.plugin, file)
        return True

    def createDocumentThumbnailAfterOpen(self):  # noqa: N802
        return False


def install(plugin):
    """Register drag & drop and Open Project support; keep the returned
    handlers (Python references) and pass them to uninstall()."""
    handlers = (ArcGISDropHandler(plugin), ArcGISProjectOpenHandler(plugin))
    plugin.iface.registerCustomDropHandler(handlers[0])
    plugin.iface.registerCustomProjectOpenHandler(handlers[1])
    return handlers


def uninstall(plugin, handlers):
    if not handlers:
        return
    plugin.iface.unregisterCustomDropHandler(handlers[0])
    plugin.iface.unregisterCustomProjectOpenHandler(handlers[1])
