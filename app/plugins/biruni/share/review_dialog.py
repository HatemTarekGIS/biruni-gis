"""Dialogs: send the map for review (one HTML page) and bring the reviewers' comments back."""
import os

from qgis.core import QgsProject, QgsReferencedRectangle
from qgis.PyQt.QtCore import QDate, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
                                 QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                 QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from .webmap_dialog import _ShareDialog
from .widgets import PathEdit, default_path, exports_dir, project_name


class SendForReviewDialog(_ShareDialog):
    """Write the review page: the reviewer comments on the map in any browser, without an account."""

    def __init__(self, plugin):
        super().__init__(plugin, tr("Send for review"),
                         tr("Creates one HTML file of the chosen layers. Whoever receives it - a client, a "
                            "supervisor, a colleague - opens it in any browser, on a phone too, taps the map and "
                            "writes notes, errors and questions, then sends the comments back as a small file. "
                            "No account, no server, no internet needed except for the background map."))
        self.title = QLineEdit(project_name())
        self.form.addRow(tr("Title"), self.title)
        self.instructions = QPlainTextEdit()
        self.instructions.setPlaceholderText(tr("What should the reviewer look at? (optional)"))
        self.instructions.setFixedHeight(70)
        self.form.addRow(tr("Instructions"), self.instructions)
        row = QHBoxLayout()
        self.use_deadline = QCheckBox(tr("Reply by"))
        self.deadline = QDateEdit(QDate.currentDate().addDays(7))
        self.deadline.setCalendarPopup(True)
        self.deadline.setEnabled(False)
        self.use_deadline.toggled.connect(self.deadline.setEnabled)
        row.addWidget(self.use_deadline)
        row.addWidget(self.deadline)
        row.addStretch(1)
        self.form.addRow("", row)
        self.basemap = QComboBox()
        for key, text in (("osm", "OpenStreetMap"), ("satellite", tr("Satellite (Sentinel-2)")),
                          ("topo", "OpenTopoMap"), ("none", tr("No basemap"))):
            self.basemap.addItem(text, key)
        self.form.addRow(tr("Basemap"), self.basemap)
        self.labels = QCheckBox(tr("Show the layers' labels"))
        self.labels.setChecked(True)
        self.form.addRow("", self.labels)
        self.view_only = QCheckBox(tr("Only what is inside the current map view"))
        self.form.addRow("", self.view_only)
        self.out = PathEdit(default_path(self.title.text() + " - " + tr("review"), ".html"), "HTML (*.html)")
        self.form.addRow(tr("Save as"), self.out)
        self._buttons(tr("Create"))

    def options(self):
        opts = {"labels": self.labels.isChecked()}
        if self.view_only.isChecked():
            canvas = self.iface.mapCanvas()
            opts["extent"] = QgsReferencedRectangle(canvas.extent(), canvas.mapSettings().destinationCrs())
        return opts

    def _run(self):
        from .review import export_review_page
        from .webmap import SIZE_WARNING_MB, file_size_mb

        layers = self._chosen()
        path = self.out.path()
        if not layers or not path:
            return
        if not path.lower().endswith((".html", ".htm")):
            path += ".html"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            path, review_id = export_review_page(
                layers, path, self.title.text().strip(), self.instructions.toPlainText().strip(),
                self.deadline.date() if self.use_deadline.isChecked() else None, self.basemap.currentData(),
                self.options())
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
        self._log("review_send", os.path.basename(path), len(layers), path)
        QgsProject.instance().writeEntry("biruni", "review/last_id", review_id)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        QMessageBox.information(
            self, APP_NAME,
            tr("The review page is ready:\n{p}\n\nSend it to the reviewers by WhatsApp or e-mail. When their "
               "comments come back, open them with 'Bring back review comments'.").format(p=path))
        self.accept()


class BringBackReviewDialog(QDialog):
    """Read the comment files sent back by reviewers into the review layer; send the replies back."""

    FILTER = "{} (*.json *.txt)"

    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setWindowTitle(tr("Bring back review comments"))
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        intro = QLabel(tr("Choose the comment files the reviewers sent back (…biruni-review.json or .txt). Each "
                          "comment becomes a pin in the layer 'Review comments' with a status and a reply. A file "
                          "opened twice does not duplicate anything: changed comments are updated."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.files = QListWidget()
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.setMinimumHeight(120)
        v.addWidget(self.files, 1)
        row = QHBoxLayout()
        add = QPushButton(tr("Add files…"))
        add.clicked.connect(self._add)
        remove = QPushButton(tr("Remove"))
        remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        self.replies = QPushButton(tr("Send replies to the reviewers…"))
        self.replies.setToolTip(tr("Writes the status and reply of every comment to a file. The reviewers open it "
                                   "in their review page to see what was done."))
        self.replies.clicked.connect(self._replies)
        row.addWidget(self.replies)
        v.addLayout(row)
        self.zoom = QCheckBox(tr("Zoom to the comments"))
        self.zoom.setChecked(True)
        v.addWidget(self.zoom)
        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Bring back"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._run)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Review comments"), exports_dir(),
                                                self.FILTER.format(tr("Review comments")))
        have = {self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())}
        for p in paths:
            p = os.path.normpath(p)
            if p not in have:
                self.files.addItem(os.path.basename(p))
                self.files.item(self.files.count() - 1).setData(Qt.ItemDataRole.UserRole, p)

    def _remove(self):
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))

    def paths(self):
        return [self.files.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.files.count())]

    def _log(self, action, target, count, path):
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            act.log("share", action, target, "", count, path)

    def _run(self):
        from .review import import_reviews

        paths = self.paths()
        if not paths:
            QMessageBox.information(self, APP_NAME, tr("Add the comment files first."))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = import_reviews(paths)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        text = result.summary()
        problems = result.errors + result.warnings
        if problems:
            text += "\n\n" + "\n".join(problems[:10])
            if len(problems) > 10:
                text += "\n" + tr("… and {n} more.").format(n=len(problems) - 10)
        if result.layer is None:
            QMessageBox.warning(self, APP_NAME, text)
            return
        self._log("review_back", result.layer.name(), result.new + result.updated, "; ".join(paths))
        if self.zoom.isChecked() and result.layer.featureCount():
            canvas = self.iface.mapCanvas()
            ext = canvas.mapSettings().layerExtentToOutputExtent(result.layer, result.layer.extent())
            ext.scale(1.2)
            if ext.width() == 0 or ext.height() == 0:  # a single comment
                ext.grow(0.0005 if canvas.mapSettings().destinationCrs().isGeographic() else 50)
            canvas.setExtent(ext)
            canvas.refresh()
        self.iface.setActiveLayer(result.layer)
        QMessageBox.information(self, APP_NAME, text)
        self.accept()

    def _replies(self):
        from .review import COMMENTS_TABLE, ROLE_KEY, export_replies, review_ids

        layer = next((lyr for lyr in QgsProject.instance().mapLayers().values()
                      if lyr.customProperty(ROLE_KEY) == COMMENTS_TABLE), None)
        if layer is None or not layer.featureCount():
            QMessageBox.information(self, APP_NAME, tr("There are no review comments in this project yet."))
            return
        ids = review_ids(layer)
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Send replies to the reviewers…"), default_path(project_name() + " - " + tr("replies"),
                                                                       ".biruni-replies.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            n = export_replies(layer, path, ids[0] if len(ids) == 1 else None)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        self._log("review_replies", os.path.basename(path), n, path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        QMessageBox.information(
            self, APP_NAME,
            tr("{n} replies saved to:\n{p}\n\nSend this file to the reviewers: in their review page they press "
               "Send, then 'Open replies file'.").format(n=n, p=path))
