"""Dialog for the quick map sheet."""
import os

from qgis.core import Qgis, QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
                                 QVBoxLayout)

from . import i18n, mapsheet
from .i18n import tr
from .project import project_folder, safe_name
from .version import APP_NAME

KEY = "biruni/mapsheet/"
SCALES = [0, 200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000]


class MapSheetDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        s = QgsSettings()
        self.setWindowTitle(tr("Quick Map Sheet"))
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        intro = QLabel(tr("A finished map in one step: title, coordinate frame, north arrow, scale bar, legend and "
                          "details. Select one polygon first to add its corner coordinates table."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.title = QLineEdit(QgsProject.instance().title())
        self.subtitle = QLineEdit()
        self.author = QLineEdit(s.value(KEY + "author", ""))
        self.notes = QLineEdit()
        form.addRow(tr("Title"), self.title)
        form.addRow(tr("Subtitle"), self.subtitle)
        form.addRow(tr("Prepared by"), self.author)
        form.addRow(tr("Notes"), self.notes)
        row = QHBoxLayout()
        self.paper = QComboBox()
        self.paper.addItems(list(mapsheet.PAPERS))
        self.paper.setCurrentText(s.value(KEY + "paper", "A4"))
        self.landscape = QRadioButton(tr("Landscape"))
        self.portrait = QRadioButton(tr("Portrait"))
        (self.landscape if s.value(KEY + "landscape", True, type=bool) else self.portrait).setChecked(True)
        row.addWidget(self.paper)
        row.addWidget(self.landscape)
        row.addWidget(self.portrait)
        row.addStretch(1)
        form.addRow(tr("Paper"), row)
        self.scale = QComboBox()
        for sc in SCALES:
            self.scale.addItem(tr("Fit the map") if sc == 0 else f"1:{sc:,}", sc)
        form.addRow(tr("Scale"), self.scale)
        self.extent = QComboBox()
        self.extent.addItem(tr("Current map view"), "view")
        self.extent.addItem(tr("All layers"), "all")
        form.addRow(tr("Area"), self.extent)
        logo_row = QHBoxLayout()
        self.logo = QLineEdit(s.value(KEY + "logo", ""))
        b = QPushButton(tr("Browse…"))
        b.clicked.connect(self._pick_logo)
        logo_row.addWidget(self.logo, 1)
        logo_row.addWidget(b)
        form.addRow(tr("Logo (optional)"), logo_row)
        v.addLayout(form)
        self.grid = QCheckBox(tr("Coordinate frame"))
        self.grid.setChecked(True)
        self.legend = QCheckBox(tr("Legend"))
        self.legend.setChecked(True)
        self.arabic_digits = QCheckBox(tr("Arabic-Indic digits (١٢٣)"))
        self.arabic_digits.setChecked(s.value(KEY + "arabic-digits", i18n.is_rtl(), type=bool))
        self.rtl = QCheckBox(tr("Right-to-left layout"))
        self.rtl.setChecked(i18n.is_rtl())
        opts = QHBoxLayout()
        for w in (self.grid, self.legend, self.rtl, self.arabic_digits):
            opts.addWidget(w)
        opts.addStretch(1)
        v.addLayout(opts)
        self.parcel_info = QLabel()
        self.parcel_info.setWordWrap(True)
        v.addWidget(self.parcel_info)
        self.parcel = self._selected_polygon()
        if self.parcel:
            self.parcel_info.setText("✓ " + tr("The selected polygon gets a corner coordinates table and "
                                               "numbered corners."))
        bb = QDialogButtonBox()
        self.pdf_btn = bb.addButton(tr("Save as PDF"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.edit_btn = bb.addButton(tr("Open in layout designer"), QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.pdf_btn.clicked.connect(lambda: self._make(pdf=True))
        self.edit_btn.clicked.connect(lambda: self._make(pdf=False))
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _pick_logo(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Logo (optional)"), self.logo.text(),
                                           "Images (*.png *.jpg *.jpeg *.svg)")
        if p:
            self.logo.setText(p)

    def _selected_polygon(self):
        lyr = self.iface.activeLayer()
        if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == Qgis.GeometryType.Polygon \
                and lyr.selectedFeatureCount() == 1:
            f = next(lyr.getSelectedFeatures())
            g = QgsGeometry(f.geometry())
            if not g.isEmpty():
                return g, lyr.crs()
        return None

    def options(self):
        ext = None
        if self.extent.currentData() == "view":
            ext = self.iface.mapCanvas().extent()
            canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            if canvas_crs != QgsProject.instance().crs():
                from qgis.core import QgsCoordinateTransform

                ext = QgsCoordinateTransform(canvas_crs, QgsProject.instance().crs(),
                                             QgsProject.instance()).transformBoundingBox(ext)
        return mapsheet.SheetOptions(
            title=self.title.text().strip(), subtitle=self.subtitle.text().strip(), author=self.author.text().strip(),
            notes=self.notes.text().strip(), paper=self.paper.currentText(), landscape=self.landscape.isChecked(),
            scale=self.scale.currentData(), extent=ext, grid=self.grid.isChecked(), legend=self.legend.isChecked(),
            logo=self.logo.text().strip(), parcel=self.parcel, arabic_digits=self.arabic_digits.isChecked(),
            rtl=self.rtl.isChecked())

    def _make(self, pdf):
        s = QgsSettings()
        s.setValue(KEY + "author", self.author.text())
        s.setValue(KEY + "paper", self.paper.currentText())
        s.setValue(KEY + "landscape", self.landscape.isChecked())
        s.setValue(KEY + "logo", self.logo.text())
        s.setValue(KEY + "arabic-digits", self.arabic_digits.isChecked())
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            layout = mapsheet.build_layout(QgsProject.instance(), self.options())
        finally:
            QApplication.restoreOverrideCursor()
        if not pdf:
            self.iface.openLayoutDesigner(layout)
            self.accept()
            return
        folder = project_folder()
        start = os.path.join(folder, "exports") if folder else os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(self, tr("Save as PDF"), os.path.join(
            start, (safe_name(layout.name()) or "map") + ".pdf"), "PDF (*.pdf)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok = mapsheet.export(layout, path)
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            self.iface.messageBar().pushSuccess(APP_NAME, tr("Map saved: {p}").format(p=path))
            self.accept()
        else:
            self.iface.messageBar().pushCritical(APP_NAME, tr("The map could not be saved."))


def open_mapsheet(plugin):
    MapSheetDialog(plugin).exec()
