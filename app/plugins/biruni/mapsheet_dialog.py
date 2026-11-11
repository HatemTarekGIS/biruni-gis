"""Dialog for the quick map sheet."""
import os

from qgis.core import Qgis, QgsGeometry, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout)

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
        from . import map_grid, sheet_presets

        self.preset = QComboBox()
        self.preset.addItem(tr("Free layout (paper above)"), "")
        try:  # the user's country (chosen or detected) puts its sheet standards first
            from . import regions

            cc = regions.current_country() or ""
        except Exception:  # noqa: BLE001
            cc = ""
        first = sheet_presets.for_country(cc) if cc else []
        for pid in first + [p for p in sheet_presets.PRESETS if p not in first]:
            self.preset.addItem(sheet_presets.preset_label(pid), pid)
        i = self.preset.findData(s.value(KEY + "preset", ""))
        self.preset.setCurrentIndex(max(i, 0))
        form.addRow(tr("Sheet standard"), self.preset)
        self.fields = QTableWidget(0, 2)
        self.fields.setHorizontalHeaderLabels([tr("Title block"), tr("Value")])
        self.fields.horizontalHeader().setStretchLastSection(True)
        self.fields.verticalHeader().setVisible(False)
        self.fields.setMaximumHeight(150)
        form.addRow("", self.fields)
        grid_row = QHBoxLayout()
        self.grid_style = QComboBox()
        for key in map_grid.STYLES:
            if key != "none":
                self.grid_style.addItem(map_grid.style_label(key), key)
        i = self.grid_style.findData(s.value(KEY + "grid-style", "auto"))
        self.grid_style.setCurrentIndex(max(i, 0))
        self.zone_mode = QComboBox()
        for key in map_grid.ZONE_MODES:
            self.zone_mode.addItem(map_grid.zone_mode_label(key), key)
        i = self.zone_mode.findData(s.value(KEY + "zone-mode", "extended"))
        self.zone_mode.setCurrentIndex(max(i, 0))
        self.zone_mode.setToolTip(tr("When the map crosses two UTM zones."))
        grid_row.addWidget(self.grid_style, 1)
        grid_row.addWidget(self.zone_mode, 1)
        form.addRow(tr("Coordinate grid"), grid_row)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        self._preset_changed()
        self.scale = QComboBox()
        for sc in SCALES:
            self.scale.addItem(tr("Fit the map") if sc == 0 else f"1:{sc:,}", sc)
        form.addRow(tr("Scale"), self.scale)
        self.extent = QComboBox()
        self.extent.addItem(tr("Current map view"), "view")
        self.extent.addItem(tr("All layers"), "all")
        form.addRow(tr("Area"), self.extent)
        bar_row = QHBoxLayout()
        self.scalebar = QComboBox()
        self.scalebar.addItem(tr("Single (metres)"), "single")
        self.scalebar.addItem(tr("Dual (metres and feet)"), "dual")
        self.scalebar.addItem(tr("Fixed segment length"), "fixed")
        i = self.scalebar.findData(s.value(KEY + "scalebar", "single"))
        self.scalebar.setCurrentIndex(max(i, 0))
        self.segment = QDoubleSpinBox()
        self.segment.setRange(0, 1000000)
        self.segment.setDecimals(1)
        self.segment.setSuffix(" " + tr("m"))
        self.segment.setSpecialValueText(tr("Round value"))
        self.segment.setValue(s.value(KEY + "scalebar-segment", 0.0, type=float))
        self.segment.setToolTip(tr("Length of each segment of the scale bar, e.g. 100 m. 0 picks a round value."))
        self.scalebar.currentIndexChanged.connect(
            lambda: self.segment.setEnabled(self.scalebar.currentData() == "fixed"))
        self.segment.setEnabled(self.scalebar.currentData() == "fixed")
        bar_row.addWidget(self.scalebar, 1)
        bar_row.addWidget(self.segment)
        form.addRow(tr("Scale bar"), bar_row)
        logo_row = QHBoxLayout()
        self.logo = QLineEdit(s.value(KEY + "logo", ""))
        b = QPushButton(tr("Browse…"))
        b.clicked.connect(self._pick_logo)
        logo_row.addWidget(self.logo, 1)
        logo_row.addWidget(b)
        form.addRow(tr("Logo (optional)"), logo_row)
        self.link = QLineEdit(s.value(KEY + "link", ""))
        self.link.setPlaceholderText("https://")
        self.link.setToolTip(tr("Printed on the sheet and clickable in the PDF, e.g. the web map or your office site."))
        form.addRow(tr("Web link (optional)"), self.link)
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
        self.lean = QCheckBox(tr("Small PDF (text stays text, lighter images and lines)"))
        self.lean.setChecked(s.value(KEY + "lean-pdf", True, type=bool))
        v.addWidget(self.lean)
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

    def _preset_changed(self):
        from . import sheet_presets

        p = sheet_presets.get(self.preset.currentData() or "")
        for wdg in (self.paper, self.landscape, self.portrait):
            wdg.setEnabled(p is None)
        self.fields.setVisible(p is not None)
        self.fields.setRowCount(0)
        if p is None:
            return
        saved = QgsSettings().value(KEY + "fields", {}) or {}
        for key in p.fields:
            if key in ("scale", "area", "perimeter", "date"):
                continue  # filled from the map and the selected parcel
            r = self.fields.rowCount()
            self.fields.insertRow(r)
            label = QTableWidgetItem(sheet_presets.field_label(key, p.lang))
            label.setData(Qt.ItemDataRole.UserRole, key)
            label.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.fields.setItem(r, 0, label)
            self.fields.setItem(r, 1, QTableWidgetItem(str(saved.get(key, "")) if isinstance(saved, dict) else ""))

    def _field_values(self):
        out = {}
        for r in range(self.fields.rowCount()):
            key = self.fields.item(r, 0).data(Qt.ItemDataRole.UserRole)
            val = self.fields.item(r, 1)
            if val is not None and val.text().strip():
                out[key] = val.text().strip()
        return out

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
            rtl=self.rtl.isChecked(), scalebar=self.scalebar.currentData(), scalebar_segment=self.segment.value(),
            link=self.link.text().strip(), preset=self.preset.currentData() or None,
            grid_style=self.grid_style.currentData(), zone_mode=self.zone_mode.currentData(),
            fields=self._field_values())

    def _make(self, pdf):
        s = QgsSettings()
        s.setValue(KEY + "author", self.author.text())
        s.setValue(KEY + "paper", self.paper.currentText())
        s.setValue(KEY + "landscape", self.landscape.isChecked())
        s.setValue(KEY + "logo", self.logo.text())
        s.setValue(KEY + "arabic-digits", self.arabic_digits.isChecked())
        s.setValue(KEY + "scalebar", self.scalebar.currentData())
        s.setValue(KEY + "scalebar-segment", self.segment.value())
        s.setValue(KEY + "link", self.link.text().strip())
        s.setValue(KEY + "lean-pdf", self.lean.isChecked())
        s.setValue(KEY + "preset", self.preset.currentData() or "")
        s.setValue(KEY + "grid-style", self.grid_style.currentData())
        s.setValue(KEY + "zone-mode", self.zone_mode.currentData())
        saved = s.value(KEY + "fields", {}) or {}
        saved = dict(saved) if isinstance(saved, dict) else {}
        # office details are remembered; parcel-specific values are not
        saved.update({k: v for k, v in self._field_values().items() if k not in ("owner", "deed_no", "parcel_no",
                                                                                  "lot_no", "survey_no")})
        s.setValue(KEY + "fields", saved)
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
            ok = mapsheet.export(layout, path, lean=self.lean.isChecked())
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
