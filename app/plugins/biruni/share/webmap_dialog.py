"""Dialogs: publish a web map (one .html file) and export to Google Earth (KMZ)."""
import os

from qgis.core import QgsProject, QgsReferencedRectangle
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from .widgets import LayerPicker, PathEdit, default_path, project_name


class _ShareDialog(QDialog):
    def __init__(self, plugin, title, intro):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(22, 18, 22, 16)
        self.v.setSpacing(10)
        lbl = QLabel(intro)
        lbl.setWordWrap(True)
        self.v.addWidget(lbl)
        self.layers = LayerPicker(self)
        self.v.addWidget(QLabel(tr("Layers")))
        self.v.addWidget(self.layers, 1)
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(12)
        self.v.addLayout(self.form)

    def _buttons(self, ok_text):
        bb = QDialogButtonBox()
        self.ok = bb.addButton(ok_text, QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._run)
        bb.rejected.connect(self.reject)
        self.v.addWidget(bb)

    def _log(self, action, target, count, path):
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            act.log("share", action, target, "", count, path)

    def _chosen(self):
        layers = self.layers.selected_layers()
        if not layers:
            QMessageBox.information(self, APP_NAME, tr("Choose at least one vector layer."))
        return layers


class WebMapDialog(_ShareDialog):
    """Publish the map as one .html file that opens in any browser."""

    def __init__(self, plugin):
        super().__init__(plugin, tr("Publish web map"),
                         tr("Creates one HTML file with the chosen layers, their colours, a legend and pop-ups. "
                            "It opens in any browser - on a phone too - without a server: send it by e-mail or "
                            "WhatsApp, or put it on any website."))
        self.title = QLineEdit(project_name())
        self.form.addRow(tr("Title"), self.title)
        self.basemap = QComboBox()
        for key, text in (("osm", "OpenStreetMap"), ("topo", "OpenTopoMap"), ("none", tr("No basemap"))):
            self.basemap.addItem(text, key)
        self.form.addRow(tr("Basemap"), self.basemap)
        self.labels = QCheckBox(tr("Show the layers' labels"))
        self.labels.setChecked(True)
        self.form.addRow("", self.labels)
        self.view_only = QCheckBox(tr("Only what is inside the current map view"))
        self.form.addRow("", self.view_only)
        row = QHBoxLayout()
        self.simplify = QCheckBox(tr("Simplify shapes to"))
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.1, 1000)
        self.tolerance.setValue(1.0)
        self.tolerance.setSuffix(" " + tr("m"))
        self.tolerance.setEnabled(False)
        self.simplify.toggled.connect(self.tolerance.setEnabled)
        row.addWidget(self.simplify)
        row.addWidget(self.tolerance)
        row.addStretch(1)
        self.form.addRow("", row)
        self.out = PathEdit(default_path(self.title.text(), ".html"), "HTML (*.html)")
        self.form.addRow(tr("Save as"), self.out)
        self._buttons(tr("Publish"))

    def _run(self):
        from .webmap import SIZE_WARNING_MB, export_web_map, file_size_mb

        layers = self._chosen()
        path = self.out.path()
        if not layers or not path:
            return
        if not path.lower().endswith((".html", ".htm")):
            path += ".html"
        opts = {"labels": self.labels.isChecked(), "simplify_m": self.tolerance.value() if self.simplify.isChecked()
                else 0.0}
        if self.view_only.isChecked():
            canvas = self.iface.mapCanvas()
            opts["extent"] = QgsReferencedRectangle(canvas.extent(), canvas.mapSettings().destinationCrs())
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            export_web_map(layers, path, self.title.text().strip(), self.basemap.currentData(), opts)
        except PermissionError:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, tr("The file is open in another program. Close it and try again."))
            return
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        size = file_size_mb(path)
        if size > SIZE_WARNING_MB:
            QMessageBox.warning(self, APP_NAME,
                                tr("The web map is {mb} MB - it may open slowly on phones. Simplify the shapes or "
                                   "publish fewer features to make it smaller.").format(mb=round(size)))
        self._log("web_map", os.path.basename(path), len(layers), path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self.iface.messageBar().pushSuccess(APP_NAME, tr("Web map saved: {p}").format(p=path))
        self.accept()


class KmzDialog(_ShareDialog):
    """Export the chosen layers with their colours to Google Earth."""

    def __init__(self, plugin):
        super().__init__(plugin, tr("Export to Google Earth (KMZ)"),
                         tr("Saves the chosen layers in one KMZ file with their colours. Each feature is named "
                            "after its label and shows all of its attributes when clicked."))
        self.out = PathEdit(default_path(project_name(), ".kmz"), "KMZ (*.kmz)")
        self.form.addRow(tr("Save as"), self.out)
        self._buttons(tr("Export"))

    def _run(self):
        from .kmz import export_kmz

        layers = self._chosen()
        path = self.out.path()
        if not layers or not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path, written = export_kmz(layers, path, QgsProject.instance())
        except PermissionError:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, tr("The file is open in another program. Close it and try again."))
            return
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        total = sum(written.values())
        self._log("kmz", os.path.basename(path), total, path)
        self.iface.messageBar().pushSuccess(
            APP_NAME, tr("{n} features exported to {p}").format(n=total, p=path))
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        self.accept()
