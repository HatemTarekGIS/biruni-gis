"""Small pieces shared by the sharing dialogs."""
import os

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QAbstractItemView, QFileDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, \
    QPushButton, QWidget

from ..i18n import tr
from ..project import project_folder, safe_name


def project_name():
    prj = QgsProject.instance()
    return prj.title() or os.path.splitext(os.path.basename(prj.fileName()))[0] or tr("Map")


def exports_dir():
    folder = project_folder()
    if folder:
        return os.path.join(folder, "exports")
    return os.path.join(os.path.expanduser("~"), "Documents")


def default_path(name, ext):
    return os.path.join(exports_dir(), (safe_name(name) or "map") + ext)


class LayerPicker(QListWidget):
    """Vector layers of the project in map order (top first), checkable;
    layers visible on the map start checked."""

    def __init__(self, parent=None, spatial_only=True):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setMinimumHeight(140)
        root = QgsProject.instance().layerTreeRoot()
        for lyr in root.layerOrder():
            if not isinstance(lyr, QgsVectorLayer) or (spatial_only and not lyr.isSpatial()):
                continue
            node = root.findLayer(lyr.id())
            it = QListWidgetItem(lyr.name())
            it.setData(Qt.ItemDataRole.UserRole, lyr.id())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if node is None or node.isVisible() else Qt.CheckState.Unchecked)
            self.addItem(it)

    def selected_layers(self):
        prj = QgsProject.instance()
        out = []
        for i in range(self.count()):
            it = self.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                lyr = prj.mapLayer(it.data(Qt.ItemDataRole.UserRole))
                if lyr is not None:
                    out.append(lyr)
        return out


class PathEdit(QWidget):
    """A path line edit with a Browse… button (save file or pick a folder)."""

    def __init__(self, path, file_filter=None, folder=False, parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.folder = folder
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(path)
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self):
        if self.folder:
            p = QFileDialog.getExistingDirectory(self, tr("Location"), self.edit.text())
        else:
            p, _ = QFileDialog.getSaveFileName(self, tr("Save as"), self.edit.text(), self.file_filter or "")
        if p:
            self.edit.setText(os.path.normpath(p))

    def path(self):
        return self.edit.text().strip()
