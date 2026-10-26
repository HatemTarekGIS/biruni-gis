"""Dialog for the smart table import: pick a file, see what Biruni found, add."""
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsSettings)
from qgis.gui import QgsCustomDropHandler, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout)

from . import smart_table as st
from .i18n import tr
from .project import copy_vector_into_gpkg, project_folder, project_gpkg
from .version import APP_NAME

CRS_KEY = "biruni/smart-table-crs"
DIR_KEY = "biruni/smart-table-dir"
FILTER = "Spreadsheets and text (*.xlsx *.xlsm *.xls *.ods *.csv *.txt *.tsv)"
EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".txt", ".tsv")


class SmartTableDialog(QDialog):
    def __init__(self, plugin, path=None):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.headers, self.data, self.det = [], [], st.Detection()
        self.setWindowTitle(tr("Add Points from a Table"))
        self.resize(820, 620)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        v.setSpacing(10)

        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText(tr("Excel, CSV or text file…"))
        self.path.editingFinished.connect(lambda: self.load(self.path.text()))
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        self.sheet = QComboBox()
        self.sheet.setMinimumWidth(140)
        self.sheet.currentIndexChanged.connect(lambda *_: self._read_sheet())
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        row.addWidget(self.sheet)
        v.addLayout(row)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("BiruniSummary")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setMinimumHeight(48)
        v.addWidget(self.summary)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.mode = QComboBox()
        for key, label in ((st.GEOGRAPHIC, tr("Longitude / latitude (degrees)")),
                           (st.PROJECTED, tr("Easting / northing (projected: UTM or a local grid)")),
                           (st.PAIR, tr("One column: map link or \"lat, lon\"")),
                           (st.WKT, tr("One column: WKT geometry"))):
            self.mode.addItem(label, key)
        self.mode.currentIndexChanged.connect(self._on_manual_change)
        form.addRow(tr("Coordinates"), self.mode)
        cols = QHBoxLayout()
        self.xcol, self.ycol = QComboBox(), QComboBox()
        self.xlabel, self.ylabel = QLabel(), QLabel()
        swap = QPushButton("⇄")
        swap.setToolTip(tr("Swap the two columns"))
        swap.setFixedWidth(36)
        swap.clicked.connect(self._swap)
        for w in (self.xlabel, self.xcol, swap, self.ylabel, self.ycol):
            cols.addWidget(w, 1 if isinstance(w, QComboBox) else 0)
        self.xcol.currentIndexChanged.connect(self._on_manual_change)
        self.ycol.currentIndexChanged.connect(self._on_manual_change)
        form.addRow(tr("Columns"), cols)
        self.crs = QgsProjectionSelectionWidget()
        self.crs.crsChanged.connect(lambda *_: self._refresh_summary())
        form.addRow(tr("Coordinate system"), self.crs)
        self.name = QLineEdit()
        form.addRow(tr("Layer name"), self.name)
        self.into_project = QCheckBox(tr("Save into the project database"))
        self.into_project.setChecked(project_folder() is not None)
        self.into_project.setEnabled(project_folder() is not None)
        form.addRow("", self.into_project)
        v.addLayout(form)

        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        v.addWidget(self.table, 1)

        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Add to map"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._add)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._busy = False
        self.layer = None
        if path:
            self.load(path)
        else:
            self._refresh_summary()

    # ---- loading -------------------------------------------------------------
    def _browse(self):
        start = QgsSettings().value(DIR_KEY, os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, tr("Add Points from a Table"), start, FILTER)
        if path:
            QgsSettings().setValue(DIR_KEY, os.path.dirname(path))
            self.load(path)

    def load(self, path):
        if not path or not os.path.exists(path):
            return
        self.path.setText(os.path.normpath(path))
        self.name.setText(os.path.splitext(os.path.basename(path))[0])
        self._busy = True
        self.sheet.clear()
        try:
            sheets = st.list_sheets(path)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, tr("Could not read {p}").format(p=path) + f"\n{e}")
            sheets = []
        self.sheet.addItems(sheets)
        self.sheet.setVisible(len(sheets) > 1)
        self._busy = False
        # the first sheet that has coordinates
        for k, s in enumerate(sheets):
            h, d = st.split_header(st.read_rows(path, s, limit=300))
            if st.detect(h, d).mode:
                self.sheet.setCurrentIndex(k)
                break
        self._read_sheet()

    def _read_sheet(self):
        if self._busy or not self.path.text():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rows = st.read_rows(self.path.text(), self.sheet.currentText() or None)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, tr("Could not read {p}").format(p=self.path.text()) + f"\n{e}")
            return
        QApplication.restoreOverrideCursor()
        self.headers, self.data = st.split_header(rows)
        self.det = st.detect(self.headers, self.data)
        if len(self.sheet) > 1 and self.sheet.currentText():
            self.name.setText(f"{os.path.splitext(os.path.basename(self.path.text()))[0]} - {self.sheet.currentText()}")
        self._apply_detection()

    def _apply_detection(self):
        self._busy = True
        for c in (self.xcol, self.ycol):
            c.clear()
            c.addItems(self.headers)
        if self.det.mode:
            self.mode.setCurrentIndex(self.mode.findData(self.det.mode))
            self.xcol.setCurrentIndex(self.det.x)
            if self.det.y >= 0:
                self.ycol.setCurrentIndex(self.det.y)
        self.crs.setCrs(self._suggest_crs())
        self._busy = False
        self._fill_table()
        self._refresh_summary()

    def _suggest_crs(self):
        if self.det.mode != st.PROJECTED:
            return QgsCoordinateReferenceSystem("EPSG:4326")
        prj_crs = QgsProject.instance().crs()
        if prj_crs.isValid() and not prj_crs.isGeographic() and prj_crs.authid() != "EPSG:3857":
            return prj_crs
        saved = QgsSettings().value(CRS_KEY, "")
        if saved:
            return QgsCoordinateReferenceSystem(saved)
        canvas = self.iface.mapCanvas()
        ext = canvas.extent()
        if canvas.layerCount() and ext.width() > 0:
            wgs = QgsCoordinateReferenceSystem("EPSG:4326")
            tr_ = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), wgs, QgsProject.instance())
            c = tr_.transform(ext.center())
            if abs(c.y()) < 84 and ext.width() < 2e6:
                return QgsCoordinateReferenceSystem(f"EPSG:{st.utm_epsg(c.x(), c.y())}")
        return QgsCoordinateReferenceSystem()

    # ---- manual corrections ------------------------------------------------------
    def _swap(self):
        x, y = self.xcol.currentIndex(), self.ycol.currentIndex()
        self._busy = True
        self.xcol.setCurrentIndex(y)
        self.ycol.setCurrentIndex(x)
        self._busy = False
        self._on_manual_change()

    def _on_manual_change(self, *_):
        if self._busy:
            return
        self.det = st.Detection(mode=self.mode.currentData(), x=self.xcol.currentIndex(), y=self.ycol.currentIndex())
        if self.det.mode == st.GEOGRAPHIC and self.crs.crs().authid() != "EPSG:4326" and \
                not self.crs.crs().isGeographic():
            self.crs.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self._fill_table()
        self._refresh_summary()

    # ---- display ---------------------------------------------------------------
    def _fill_table(self):
        shown = self.data[:200]
        self.table.clear()
        self.table.setColumnCount(len(self.headers))
        self.table.setRowCount(len(shown))
        self.table.setHorizontalHeaderLabels(self.headers)
        hot = {self.xcol.currentIndex()} | ({self.ycol.currentIndex()} if self.mode.currentData() in
                                            (st.GEOGRAPHIC, st.PROJECTED) else set())
        tint = QBrush(QColor(37, 99, 235, 40))
        for r, row in enumerate(shown):
            for c in range(len(self.headers)):
                v = row[c] if c < len(row) else None
                it = QTableWidgetItem("" if v is None else str(v))
                if c in hot:
                    it.setBackground(tint)
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()

    def _refresh_summary(self):
        mode = self.mode.currentData()
        two = mode in (st.GEOGRAPHIC, st.PROJECTED)
        self.ycol.setEnabled(two)
        self.xlabel.setText(tr("Longitude") if mode == st.GEOGRAPHIC else tr("Easting (X)") if two else tr("Column"))
        self.ylabel.setText(tr("Latitude") if mode == st.GEOGRAPHIC else tr("Northing (Y)"))
        self.ylabel.setEnabled(two)
        if not self.headers:
            self.summary.setText(tr("Choose an Excel or CSV file. Biruni GIS finds the coordinate columns by itself "
                                    "- degrees, degrees-minutes-seconds, UTM, map links or WKT."))
            self.ok.setEnabled(False)
            return
        if not self.det.mode:
            self.summary.setText("<b>" + tr("No coordinate columns were found.") + "</b> " +
                                 tr("Choose the columns below."))
            self.ok.setEnabled(False)
            return
        located = st.locate(self.data, self.det)
        n_ok, n_all = len(located.rows), len(self.data)
        parts = [tr("<b>{ok} of {all} rows</b> have a location.").format(ok=n_ok, all=n_all)]
        if self.det.dms:
            parts.append(tr("Degrees-minutes-seconds were read."))
        if self.det.swapped:
            parts.append(tr("The columns were named the other way round - Biruni GIS swapped them."))
        if mode == st.PROJECTED and not self.crs.crs().isValid():
            parts.append("<span style='color:#d97706'>" + tr("Choose the coordinate system of the easting/northing "
                                                             "values.") + "</span>")
        where = self._where(located)
        if where:
            parts.append(where)
        self.summary.setText(" ".join(parts))
        self.ok.setEnabled(n_ok > 0 and self.crs.crs().isValid())

    def _where(self, located):
        """Where the points fall, in degrees - a quick sanity check for the CRS."""
        pts = [(x, y) for _, x, y, w in located.rows if w is None][:500]
        if not pts or not self.crs.crs().isValid():
            return ""
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        try:
            t = QgsCoordinateTransform(self.crs.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
            c = t.transform(QgsPointXY(cx, cy))
        except Exception:
            return "<span style='color:#dc2626'>" + tr("These values do not fit the chosen coordinate system.") + \
                "</span>"
        if not (abs(c.y()) <= 90 and abs(c.x()) <= 180):
            return "<span style='color:#dc2626'>" + tr("These values do not fit the chosen coordinate system.") + \
                "</span>"
        place = ""
        try:
            from .places import country_at

            place = country_at(c.x(), c.y()) or ""
        except Exception:
            pass
        txt = tr("Centre: {lat}, {lon}").format(lat=f"{c.y():.4f}", lon=f"{c.x():.4f}")
        return txt + (f" — <b>{place}</b>" if place else "")

    # ---- result ----------------------------------------------------------------
    def _add(self):
        name = self.name.text().strip() or tr("Points")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            layer, located = st.build_layer(self.headers, self.data, self.det, self.crs.crs(), name)
            if self.into_project.isChecked() and project_gpkg():
                copy_vector_into_gpkg(layer, project_gpkg())
            QgsProject.instance().addMapLayer(layer)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        if self.det.mode == st.PROJECTED:
            QgsSettings().setValue(CRS_KEY, self.crs.crs().authid())
        self.layer = layer
        canvas = self.iface.mapCanvas()
        t = QgsCoordinateTransform(layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
        ext = t.transformBoundingBox(layer.extent())
        ext.scale(1.2)
        if ext.width() == 0:
            ext.grow(500 if not canvas.mapSettings().destinationCrs().isGeographic() else 0.005)
        canvas.setExtent(ext)
        canvas.refresh()
        msg = tr("{n} points were added.").format(n=layer.featureCount())
        if located.failed:
            rows = ", ".join(str(k + 2) for k in located.failed[:12]) + ("…" if len(located.failed) > 12 else "")
            msg += " " + tr("Rows without a location: {rows}").format(rows=rows)
            self.iface.messageBar().pushWarning(APP_NAME, msg)
        else:
            self.iface.messageBar().pushSuccess(APP_NAME, msg)
        self.accept()


def open_dialog(plugin, path=None):
    dlg = SmartTableDialog(plugin, path)
    dlg.show()
    plugin._smart_dialog = dlg  # keep a reference while it is open
    return dlg


def has_coordinates(path):
    try:
        for s in st.list_sheets(path):
            h, d = st.split_header(st.read_rows(path, s or None, limit=300))
            if st.detect(h, d).mode:
                return True
    except Exception:
        return False
    return False


class TableDropHandler(QgsCustomDropHandler):
    """Dropping a spreadsheet that has coordinates opens the smart import;
    other tables are left to QGIS (added as plain tables)."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def customUriProviderKey(self):  # noqa: N802
        return "biruni_smart_table"

    def handleFileDrop(self, path):  # noqa: N802
        if os.path.splitext(path)[1].lower() not in EXTENSIONS or not has_coordinates(path):
            return False
        QTimer.singleShot(0, lambda: open_dialog(self.plugin, path))
        return True
