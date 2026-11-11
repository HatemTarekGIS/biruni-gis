"""Dialogs: make the plot sales map of a plan, and bring the statuses changed by the sales team back."""
import os

from qgis.core import QgsFieldProxyModel, QgsMapLayerProxyModel, QgsProject, QgsSettings
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                 QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                                 QRadioButton, QVBoxLayout, QWidget)

from ..i18n import tr
from ..version import APP_NAME
from .widgets import PathEdit, default_path, exports_dir, project_name

SETTINGS = "biruni/sales/"
MAP_ID_KEY = "biruni/sales_map_id"
NUMBER_NAMES = ("plot_no", "plot", "parcel_no", "parcel", "number", "no", "رقم_القطعة", "رقم القطعة", "رقم", "label")
STATUS_NAMES = ("status", "state", "الحالة", "حالة")
PRICE_NAMES = ("price", "السعر", "سعر")
AREA_NAMES = ("area_m2", "area", "المساحة", "مساحة")
NOTES_NAMES = ("notes", "note", "ملاحظات")
TABLE_FILTER = "Excel / CSV (*.xlsx *.xlsm *.xls *.ods *.csv)"


def _pick(combo, names):
    """Select the first field whose name is one of `names` (case-insensitive)."""
    layer = combo.layer()
    if layer is None:
        return
    low = {f.name().lower(): f.name() for f in layer.fields()}
    for n in names:
        if n.lower() in low:
            combo.setField(low[n.lower()])
            return


class _FileEdit(QWidget):
    """An open-file line edit with a Browse… button."""

    def __init__(self, file_filter, parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("Open"), self.edit.text() or exports_dir(), self.file_filter)
        if p:
            self.edit.setText(os.path.normpath(p))

    def path(self):
        return self.edit.text().strip()


class _PlotsForm:
    """The layer and plot-number pickers shared by both dialogs."""

    def _plots_form(self, form):
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        form.addRow(tr("Plots layer"), self.layer)
        self.number = QgsFieldComboBox()
        form.addRow(tr("Plot number field"), self.number)
        self.layer.layerChanged.connect(self._layer_changed)

    def _layer_changed(self, layer=None):
        layer = self.layer.currentLayer()
        for combo in self._field_combos():
            combo.setLayer(layer)
        _pick(self.number, NUMBER_NAMES)
        self._pick_fields()

    def _field_combos(self):
        return [self.number]

    def _pick_fields(self):
        pass


class SalesMapDialog(QDialog, _PlotsForm):
    """Make the sales map: one HTML page, and optionally the editor copy for the sales coordinator."""

    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setWindowTitle(tr("Plot sales map"))
        self.setMinimumWidth(600)
        s = QgsSettings()
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        intro = QLabel(tr("Creates one web page of the plan for the sales team and buyers: plots coloured by "
                          "status (available, reserved, sold…), counts, search by plot number, filters by area and "
                          "price, and a WhatsApp button on every plot. It opens from a file on any phone - no "
                          "server. The editor copy lets one sales coordinator change statuses and send them back."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        v.addLayout(form)
        self._plots_form(form)

        self.from_field = QRadioButton(tr("A field of the layer"))
        self.from_sheet = QRadioButton(tr("An Excel / CSV sheet (plot number, status, price, notes)"))
        group = QButtonGroup(self)
        group.addButton(self.from_field)
        group.addButton(self.from_sheet)
        self.from_field.setChecked(True)
        self.status = QgsFieldComboBox()
        self.status.setAllowEmptyFieldName(True)
        box = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(self.from_field)
        row.addWidget(self.status, 1)
        box.addLayout(row)
        box.addWidget(self.from_sheet)
        self.sheet_file = _FileEdit(TABLE_FILTER)
        self.sheet_file.edit.textChanged.connect(self._sheets)
        self.sheet = QComboBox()
        row = QHBoxLayout()
        row.addWidget(self.sheet_file, 1)
        row.addWidget(self.sheet)
        box.addLayout(row)
        form.addRow(tr("Statuses from"), box)
        self.from_sheet.toggled.connect(self._source_changed)

        self.price = QgsFieldComboBox()
        self.price.setAllowEmptyFieldName(True)
        form.addRow(tr("Price field"), self.price)
        self.area = QgsFieldComboBox()
        self.area.setAllowEmptyFieldName(True)
        self.area.setFilters(QgsFieldProxyModel.Filter.Numeric)
        form.addRow(tr("Area field (empty = measured)"), self.area)
        self.notes = QgsFieldComboBox()
        self.notes.setAllowEmptyFieldName(True)
        form.addRow(tr("Notes field"), self.notes)

        self.title = QLineEdit(project_name())
        form.addRow(tr("Title"), self.title)
        self.phone = QLineEdit(s.value(SETTINGS + "phone", "", type=str))
        self.phone.setPlaceholderText("+966 5x xxx xxxx")
        self.phone.setToolTip(tr("The sales WhatsApp / phone number, with the country code."))
        form.addRow(tr("Sales phone (WhatsApp)"), self.phone)
        self.currency = QLineEdit(s.value(SETTINGS + "currency", "", type=str))
        self.currency.setPlaceholderText(tr("e.g. SAR"))
        form.addRow(tr("Currency"), self.currency)
        self.basemap = QComboBox()
        for key, text in (("satellite", tr("Satellite (Sentinel-2)")), ("osm", "OpenStreetMap"),
                          ("none", tr("No basemap"))):
            self.basemap.addItem(text, key)
        form.addRow(tr("Basemap"), self.basemap)
        self.show_prices = QCheckBox(tr("Show prices to buyers"))
        self.show_prices.setChecked(True)
        form.addRow("", self.show_prices)
        self.editor = QCheckBox(tr("Also create the editor copy for the sales coordinator"))
        self.editor.setChecked(s.value(SETTINGS + "editor", True, type=bool))
        form.addRow("", self.editor)
        self.out = PathEdit(default_path(self.title.text() + " - " + tr("sales map"), ".html"), "HTML (*.html)")
        form.addRow(tr("Save as"), self.out)

        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Create"), QDialogButtonBox.ButtonRole.AcceptRole)
        back = bb.addButton(tr("Bring back statuses…"), QDialogButtonBox.ButtonRole.ActionRole)
        back.clicked.connect(self._bring_back)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._run)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        layer = self.iface.activeLayer()
        if layer is not None and layer in self._polygon_layers():
            self.layer.setLayer(layer)
        self._layer_changed()
        self._source_changed()

    @staticmethod
    def _polygon_layers():
        from qgis.core import Qgis, QgsVectorLayer

        return [lyr for lyr in QgsProject.instance().mapLayers().values()
                if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == Qgis.GeometryType.Polygon]

    def _field_combos(self):
        return [self.number, self.status, self.price, self.area, self.notes]

    def _pick_fields(self):
        for combo, names in ((self.status, STATUS_NAMES), (self.price, PRICE_NAMES), (self.area, AREA_NAMES),
                             (self.notes, NOTES_NAMES)):
            combo.setField("")
            _pick(combo, names)

    def _source_changed(self, *_):
        sheet = self.from_sheet.isChecked()
        self.status.setEnabled(not sheet)
        self.sheet_file.setEnabled(sheet)
        self.sheet.setEnabled(sheet)

    def _sheets(self, path):
        from ..smart_table import list_sheets

        self.sheet.clear()
        if path and os.path.isfile(path):
            try:
                for name in list_sheets(path):
                    self.sheet.addItem(name or tr("(the file)"), name)
            except Exception:  # noqa: BLE001
                pass

    def _log(self, action, target, count, path):
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            act.log("share", action, target, "", count, path)

    def _run(self):
        from .sales_map import SalesFileError, export_sales_map
        from .webmap import SIZE_WARNING_MB, file_size_mb

        layer = self.layer.currentLayer()
        if layer is None or not self.number.currentField():
            QMessageBox.information(self, APP_NAME, tr("Choose the plots layer and its plot number field."))
            return
        table = self.sheet_file.path() if self.from_sheet.isChecked() else None
        if self.from_sheet.isChecked() and not (table and os.path.isfile(table)):
            QMessageBox.information(self, APP_NAME, tr("Choose the Excel / CSV sheet with the statuses."))
            return
        path = self.out.path()
        if not path:
            return
        if not path.lower().endswith((".html", ".htm")):
            path += ".html"
        map_id = layer.customProperty(MAP_ID_KEY) or None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = export_sales_map(
                layer, path, self.number.currentField(),
                status_field=None if table else (self.status.currentField() or None),
                price_field=self.price.currentField() or None, area_field=self.area.currentField() or None,
                notes_field=self.notes.currentField() or None, table=table,
                sheet=self.sheet.currentData() if table else None, title=self.title.text().strip(),
                phone=self.phone.text(), currency=self.currency.text().strip(), basemap=self.basemap.currentData(),
                show_prices=self.show_prices.isChecked(), editor=self.editor.isChecked(), map_id=map_id)
        except PermissionError:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, tr("The file is open in another program. Close it and try again."))
            return
        except (SalesFileError, ValueError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        s = QgsSettings()
        s.setValue(SETTINGS + "phone", self.phone.text().strip())
        s.setValue(SETTINGS + "currency", self.currency.text().strip())
        s.setValue(SETTINGS + "editor", self.editor.isChecked())
        layer.setCustomProperty(MAP_ID_KEY, result.map_id)
        size = file_size_mb(path)
        if size > SIZE_WARNING_MB:
            QMessageBox.warning(self, APP_NAME,
                                tr("The web map is {mb} MB - it may open slowly on phones. Simplify the shapes or "
                                   "publish fewer features to make it smaller.").format(mb=round(size)))
        self._log("sales_map", os.path.basename(path), len(result.data.plots), path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        text = tr("The sales map is ready:\n{p}").format(p=path)
        if result.editor_path:
            text += "\n\n" + tr("Editor copy (only for the sales coordinator):\n{p}").format(p=result.editor_path)
        text += "\n\n" + result.summary()
        QMessageBox.information(self, APP_NAME, text)
        self.accept()

    def _bring_back(self):
        dlg = BringBackSalesDialog(self.plugin, self.layer.currentLayer(), self.number.currentField(),
                                   self.status.currentField(), self.price.currentField(), self.notes.currentField())
        dlg.exec()


class BringBackSalesDialog(QDialog, _PlotsForm):
    """Write the statuses downloaded from the editor copy (or an Excel sheet) into the plots layer."""

    def __init__(self, plugin, layer=None, number=None, status=None, price=None, notes=None):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setWindowTitle(tr("Bring back plot statuses"))
        self.setMinimumWidth(540)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        intro = QLabel(tr("Choose the statuses file downloaded from the editor copy of the sales map (…biruni-sales"
                          ".json), or an Excel / CSV sheet with plot numbers and statuses. The statuses, prices and "
                          "notes are written into the plots layer; Undo takes them back."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        v.addLayout(form)
        self._plots_form(form)
        self.status = QgsFieldComboBox()
        self.status.setAllowEmptyFieldName(True)
        self.status.setToolTip(tr("Empty: a new field 'status' is added."))
        form.addRow(tr("Status field"), self.status)
        self.price = QgsFieldComboBox()
        self.price.setAllowEmptyFieldName(True)
        form.addRow(tr("Price field"), self.price)
        self.notes = QgsFieldComboBox()
        self.notes.setAllowEmptyFieldName(True)
        form.addRow(tr("Notes field"), self.notes)
        self.file = _FileEdit(tr("Statuses") + " (*.json *.txt *.xlsx *.xlsm *.xls *.ods *.csv)")
        form.addRow(tr("Statuses file"), self.file)
        bb = QDialogButtonBox()
        bb.addButton(tr("Bring back"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._run)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        if layer is not None:
            self.layer.setLayer(layer)
        self._layer_changed()
        for combo, value in ((self.number, number), (self.status, status), (self.price, price),
                             (self.notes, notes)):
            if value:
                combo.setField(value)

    def _field_combos(self):
        return [self.number, self.status, self.price, self.notes]

    def _pick_fields(self):
        for combo, names in ((self.status, STATUS_NAMES), (self.price, PRICE_NAMES), (self.notes, NOTES_NAMES)):
            combo.setField("")
            _pick(combo, names)

    def _run(self):
        from .sales_map import SalesFileError, import_sales_status

        layer = self.layer.currentLayer()
        path = self.file.path()
        if layer is None or not self.number.currentField() or not path or not os.path.isfile(path):
            QMessageBox.information(self, APP_NAME, tr("Choose the plots layer, its plot number field and the "
                                                       "statuses file."))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = import_sales_status(layer, path, self.number.currentField(),
                                         self.status.currentField() or "status", self.price.currentField() or None,
                                         self.notes.currentField() or None,
                                         expected_map_id=layer.customProperty(MAP_ID_KEY) or None)
        except (SalesFileError, ValueError) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            act.log("share", "sales_back", layer.name(), "", result.updated, path)
        layer.triggerRepaint()
        text = result.summary()
        if result.warnings:
            text += "\n\n" + "\n".join(result.warnings[:10])
            if len(result.warnings) > 10:
                text += "\n" + tr("… and {n} more.").format(n=len(result.warnings) - 10)
        QMessageBox.information(self, APP_NAME, text)
        self.accept()
