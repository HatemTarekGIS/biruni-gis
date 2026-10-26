"""Dialogs: send the project to QField, and bring the field edits back."""
import os

from qgis.core import QgsFeedback, QgsProject, QgsReferencedRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                                 QProgressDialog, QPushButton, QRadioButton, QSpinBox, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import qfield
from .widgets import PathEdit, exports_dir

TILE_KB = 15  # average JPEG tile


class _Progress(QgsFeedback):
    def __init__(self, parent, title):
        super().__init__()
        self.dlg = QProgressDialog(title, tr("Cancel"), 0, 100, parent)
        self.dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.dlg.setMinimumDuration(0)
        self.dlg.canceled.connect(self.cancel)
        self.progressChanged.connect(self._update)

    def _update(self, v):
        self.dlg.setValue(int(v))
        QApplication.processEvents()

    def pushInfo(self, text):  # noqa: N802
        self.dlg.setLabelText(text)
        QApplication.processEvents()

    def close(self):
        self.dlg.close()


def _intro(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    lbl.setOpenExternalLinks(True)
    return lbl


class SendToFieldDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setWindowTitle(tr("Send to the field (QField)"))
        self.setMinimumWidth(580)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        v.addWidget(_intro(tr("Makes a copy of the project for <a href=\"https://qfield.org\">QField</a>, the free "
                              "field app for Android and iPhone: all layers in one database, styles and forms kept. "
                              "Edit on the phone - even without the internet - then bring the changes back here "
                              "with \"Bring back from the field\".")))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.out = PathEdit(exports_dir(), folder=True)
        form.addRow(tr("Save in"), self.out)
        self.whole = QRadioButton(tr("The whole project"))
        self.view = QRadioButton(tr("Only the current map view"))
        self.whole.setChecked(True)
        area = QVBoxLayout()
        area.addWidget(self.whole)
        area.addWidget(self.view)
        form.addRow(tr("Area"), area)
        self.basemap = QCheckBox(tr("Offline basemap from the project's online basemap"))
        has_online = bool(qfield.online_basemaps(QgsProject.instance()))
        self.basemap.setEnabled(has_online)
        if not has_online:
            self.basemap.setToolTip(tr("Add an online basemap (Data ▸ Basemap) to take it offline."))
        row = QHBoxLayout()
        self.zmin, self.zmax = QSpinBox(), QSpinBox()
        for sb, val in ((self.zmin, 12), (self.zmax, 17)):
            sb.setRange(0, 20)
            sb.setValue(val)
            sb.setEnabled(False)
            sb.valueChanged.connect(self._estimate)
        row.addWidget(QLabel(tr("Zoom from")))
        row.addWidget(self.zmin)
        row.addWidget(QLabel(tr("to")))
        row.addWidget(self.zmax)
        self.estimate = QLabel()
        row.addWidget(self.estimate, 1)
        self.basemap.toggled.connect(self._toggle_basemap)
        self.whole.toggled.connect(self._estimate)
        form.addRow("", self.basemap)
        form.addRow("", row)
        self.zip = QCheckBox(tr("Also make a ZIP file to send"))
        form.addRow("", self.zip)
        v.addLayout(form)
        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Create field copy"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._run)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _toggle_basemap(self, on):
        self.zmin.setEnabled(on)
        self.zmax.setEnabled(on)
        self._estimate()

    def _extent(self):
        if self.view.isChecked():
            canvas = self.iface.mapCanvas()
            return QgsReferencedRectangle(canvas.extent(), canvas.mapSettings().destinationCrs())
        return None

    def _estimate(self):
        if not self.basemap.isChecked():
            self.estimate.setText("")
            return
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

        prj = QgsProject.instance()
        area = self._extent() or qfield.data_extent(prj)
        if area is None or self.zmin.value() > self.zmax.value():
            self.estimate.setText("")
            return
        wgs = QgsCoordinateTransform(area.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                     prj.transformContext()).transformBoundingBox(area)
        n = qfield.estimate_tiles(wgs, self.zmin.value(), self.zmax.value())
        mb = n * TILE_KB / 1024
        text = tr("≈ {n} tiles, {mb} MB").format(n=f"{n:,}", mb=f"{mb:.0f}" if mb >= 1 else f"{mb:.1f}")
        if n > qfield.MAX_TILES:
            text = "<span style='color:#c0392b'>" + text + " - " + tr("too many") + "</span>"
        self.estimate.setText(text)

    def _ensure_saved(self):
        prj = QgsProject.instance()
        edited = [lyr for lyr in prj.mapLayers().values()
                  if isinstance(lyr, QgsVectorLayer) and lyr.isEditable() and lyr.isModified()]
        if edited:
            r = QMessageBox.question(self, APP_NAME, tr("Some layers have unsaved edits: {names}. Save them now?")
                                     .format(names=", ".join(lyr.name() for lyr in edited)))
            if r != QMessageBox.StandardButton.Yes:
                return False
            for lyr in edited:
                if not lyr.commitChanges(False):
                    QMessageBox.warning(self, APP_NAME, "\n".join(lyr.commitErrors()))
                    return False
        if not prj.fileName() or prj.isDirty():
            r = QMessageBox.question(self, APP_NAME, tr("The project must be saved before it is copied. Save now?"))
            if r != QMessageBox.StandardButton.Yes:
                return False
            if not self.plugin.project.save() or prj.isDirty():
                return False
        return True

    def _run(self):
        if not self._ensure_saved():
            return
        out_dir = self.out.path()
        if not out_dir:
            return
        os.makedirs(out_dir, exist_ok=True)
        zoom = (self.zmin.value(), self.zmax.value()) if self.basemap.isChecked() else None
        fb = _Progress(self, tr("Creating the field copy…"))
        try:
            result = qfield.package_for_qfield(QgsProject.instance(), out_dir, extent=self._extent(),
                                               basemap_zoom=zoom, feedback=fb, make_zip=self.zip.isChecked())
        except Exception as e:
            fb.close()
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        fb.close()
        folder = result[:-4] if result.lower().endswith(".zip") else result
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            act.log("share", "field_sent", os.path.basename(folder), "", 1, result)
        QMessageBox.information(
            self, APP_NAME,
            tr("The field copy is ready:\n{p}\n\nCopy this folder to the phone and open the project file in QField:"
               "\n• Android: connect the phone by USB and copy the folder into QField's \"Imported Projects\" "
               "folder.\n• iPhone: copy it with the Files app into QField's folder.\n\nWhen the field work is "
               "done, copy the folder back to this computer and use \"Bring back from the field\".")
            .format(p=folder))
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(folder)))
        self.accept()


class BringBackDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.diff = None
        self.setWindowTitle(tr("Bring back from the field"))
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        v.addWidget(_intro(tr("Choose the field copy returned from the phone (its folder, its GeoPackage or a ZIP of "
                              "it). The changes made in the field are listed first; nothing is changed until you "
                              "apply them.")))
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText(tr("Field copy folder, .gpkg or .zip"))
        b1 = QPushButton(tr("Folder…"))
        b2 = QPushButton(tr("File…"))
        b1.clicked.connect(self._pick_folder)
        b2.clicked.connect(self._pick_file)
        cmp_btn = QPushButton(tr("Compare"))
        cmp_btn.clicked.connect(self._compare)
        row.addWidget(self.path, 1)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(cmp_btn)
        v.addLayout(row)
        headers = [tr("Layer"), tr("Added"), tr("Modified"), tr("Deleted"), tr("Conflicts")]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        v.addWidget(self.table, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        self.force = QCheckBox(tr("Also apply the conflicting changes (the field version wins)"))
        self.force.setEnabled(False)
        v.addWidget(self.force)
        bb = QDialogButtonBox()
        self.apply_btn = bb.addButton(tr("Apply changes"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_btn.setEnabled(False)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _pick_folder(self):
        p = QFileDialog.getExistingDirectory(self, tr("Field copy"), self.path.text() or exports_dir())
        if p:
            self.path.setText(os.path.normpath(p))
            self._compare()

    def _pick_file(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Field copy"), self.path.text() or exports_dir(),
                                           "QField (*.gpkg *.zip)")
        if p:
            self.path.setText(os.path.normpath(p))
            self._compare()

    def _compare(self):
        p = self.path.text().strip()
        if not p or not os.path.exists(p):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.diff = qfield.compare_field_copy(QgsProject.instance(), p)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            self.diff = None
            self.table.setRowCount(0)
            self.apply_btn.setEnabled(False)
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        d = self.diff
        self.table.setRowCount(len(d.layers))
        conflicts = 0
        for r, ld in enumerate(d.layers):
            vals = [ld.name, len(ld.added), len(ld.modified), len(ld.deleted), len(ld.conflicts)]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(str(val))
                if c:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if ld.error:
                    it.setToolTip(ld.error)
                    if c == 0:
                        it.setText(f"{ld.name} - {ld.error}")
                elif c == 4 and ld.conflicts:
                    it.setToolTip("\n".join(f"#{x['src']}: {x['reason']}" for x in ld.conflicts[:30]))
                self.table.setItem(r, c, it)
            conflicts += len(ld.conflicts)
        notes = []
        if d.already_applied():
            notes.append(tr("These changes were already applied on {t}. Applying them again adds the new "
                            "features twice.").format(t=d.already_applied().replace("T", " ")))
        if conflicts:
            notes.append(tr("{n} features were also changed in the office after the copy was made - they are "
                            "left as they are unless you tick the box below.").format(n=conflicts))
        if not d.count():
            notes.append(tr("No changes were made in the field."))
        self.note.setText("\n".join(notes))
        self.force.setEnabled(conflicts > 0)
        self.apply_btn.setEnabled(d.count() > 0)

    def _apply(self):
        if self.diff is None:
            return
        if self.diff.already_applied():
            r = QMessageBox.question(self, APP_NAME, tr("These changes were already applied. Apply them again?"))
            if r != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            counts = qfield.apply_field_changes(QgsProject.instance(), self.diff,
                                                include_conflicts=self.force.isChecked())
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        total = sum(counts.values())
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            act.log("share", "field_returned", os.path.basename(self.diff.folder), "", total, self.diff.folder)
        self.iface.mapCanvas().refreshAllLayers()
        QMessageBox.information(
            self, APP_NAME,
            tr("{a} added, {m} modified and {d} deleted features were applied. The layers are in edit mode: "
               "review the changes, then Save Edits - or Undo to go back.")
            .format(a=counts["added"], m=counts["modified"], d=counts["deleted"]))
        self.accept()
