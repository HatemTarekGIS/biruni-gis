"""The readiness scan window: choose folders, scan in the background (progress, Stop), read the summary and the
sortable results, double-click an item to open it in Biruni, export HTML / Excel reports, import the models of a
toolbox."""
import os

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import Qt, QThread, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                                 QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QProgressBar,
                                 QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
                                 QVBoxLayout, QWidget)

from ..i18n import tr
from ..version import APP_NAME
from . import scanner

_FOLDERS_KEY = "biruni/migrate/folders"
_LIMIT_KEY = "biruni/migrate/time-limit"
_LAST_DIR = "biruni/migrate/last-dir"
_STATUS_ORDER = {"needs": 0, "fails": 1, "changes": 2, "unknown": 3, "ok": 4}
_COLOURS = {"ok": "#15803d", "changes": "#b45309", "needs": "#b91c1c", "fails": "#6b7280", "unknown": "#475569"}


def default_folders():
    home = os.path.expanduser("~")
    out = []
    for f in (os.path.join(home, "Documents", "ArcGIS"), os.path.join(home, "Documents"),
              os.path.join(home, "Desktop")):
        if os.path.isdir(f) and not any(f.startswith(o + os.sep) or f == o for o in out):
            out.append(f)
    return out


class ScanThread(QThread):
    progress = pyqtSignal(float, str)
    done = pyqtSignal(object)

    def __init__(self, folders, time_limit, parent=None):
        super().__init__(parent)
        self.folders = folders
        self.time_limit = time_limit
        self._stop = False
        self.result = None

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self.result = scanner.scan(self.folders, progress=lambda f, t: self.progress.emit(f, t),
                                       cancel=lambda: self._stop, time_limit=self.time_limit)
        except Exception as e:  # noqa: BLE001 - reported in the window
            self.result = e
        self.done.emit(self.result)


class _Item(QTableWidgetItem):
    """Sorts by its sort key (numbers as numbers, status by severity)."""

    def __init__(self, text, key=None):
        super().__init__(str(text))
        self.setData(Qt.ItemDataRole.UserRole, text if key is None else key)
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other):
        a, b = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class MigrateDialog(QDialog):
    COLUMNS = ("Kind", "Name", "Status", "Score", "Details", "Folder")

    def __init__(self, plugin, parent=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin else None))
        self.plugin = plugin
        self.thread = None
        self.result = None
        self.setWindowTitle(tr("Migration readiness scan"))
        self.resize(1000, 680)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Choose the folders where your ArcGIS work lives. Every project, map, layer file, arcpy "
                          "script, toolbox and geodatabase found is checked, read-only, and you see what opens 100%, "
                          "what changes and what is missing."))
        intro.setWordWrap(True)
        v.addWidget(intro)

        from ..project_doctor_dialog import FolderList

        s = QgsSettings()
        saved = [f for f in (s.value(_FOLDERS_KEY, []) or []) if isinstance(f, str) and os.path.isdir(f)]
        self.folders = FolderList(saved or default_folders())
        v.addWidget(self.folders)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Time limit per file (seconds):")))
        self.limit = QDoubleSpinBox()
        self.limit.setRange(1, 600)
        self.limit.setValue(float(s.value(_LIMIT_KEY, 20.0)))
        row.addWidget(self.limit)
        row.addStretch(1)
        self.scan_button = QPushButton(tr("Scan"))
        self.scan_button.setDefault(True)
        self.scan_button.clicked.connect(self.start)
        row.addWidget(self.scan_button)
        self.stop_button = QPushButton(tr("Stop"))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        row.addWidget(self.stop_button)
        v.addLayout(row)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        v.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.status)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-weight: 600; padding: 6px;")
        v.addWidget(self.summary)

        self.tabs = QTabWidget()
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in self.COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.open_row(r))
        self.table.itemSelectionChanged.connect(self._show_details)
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        split.addWidget(self.details)
        self.tabs.addTab(split, tr("Items"))
        self.missing = QTableWidget(0, 4)
        self.missing.setHorizontalHeaderLabels([tr("Function or feature"), tr("Type"), tr("Files"), tr("Times")])
        self.missing.verticalHeader().setVisible(False)
        self.missing.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.missing, tr("Most missing"))
        v.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        self.html_button = QPushButton(tr("Export HTML report…"))
        self.html_button.clicked.connect(self.export_html)
        self.xlsx_button = QPushButton(tr("Export Excel report…"))
        self.xlsx_button.clicked.connect(self.export_excel)
        self.models_button = QPushButton(tr("Import models of this toolbox"))
        self.models_button.clicked.connect(self.import_selected)
        for b in (self.html_button, self.xlsx_button, self.models_button):
            b.setEnabled(False)
            bottom.addWidget(b)
        bottom.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        v.addLayout(bottom)

    # ---- scanning --------------------------------------------------------------------------------------------
    def busy(self):
        return self.thread is not None and self.thread.isRunning()

    def start(self):
        if self.busy():
            return
        folders = self.folders.folders()
        if not folders:
            QMessageBox.information(self, APP_NAME, tr("Add at least one folder to scan."))
            return
        s = QgsSettings()
        s.setValue(_FOLDERS_KEY, folders)
        s.setValue(_LIMIT_KEY, self.limit.value())
        self.table.setRowCount(0)
        self.missing.setRowCount(0)
        self.summary.setText("")
        self.bar.setRange(0, 0)   # busy until the walk is over
        self.scan_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.thread = ScanThread(folders, self.limit.value(), self)
        self.thread.progress.connect(self._progress)
        self.thread.done.connect(self._finished)
        self.thread.start(QThread.Priority.LowPriority)

    def stop(self):
        if self.thread is not None:
            self.thread.stop()
            self.status.setText(tr("Stopping…"))

    def _progress(self, fraction, text):
        if fraction < 0:
            self.bar.setRange(0, 0)
        else:
            if self.bar.maximum() == 0:
                self.bar.setRange(0, 1000)
            self.bar.setValue(int(fraction * 1000))
        self.status.setText(text)

    def _finished(self, result):
        self.scan_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000)
        if isinstance(result, Exception):
            self.status.setText(tr("The scan stopped with an error: {e}").format(e=result))
            return
        self.show_result(result)

    def show_result(self, result):
        self.result = result
        self.status.setText(tr("{n} items checked in {s} seconds.").format(n=len(result.items),
                                                                        s=round(result.seconds, 1))
                            + (" " + tr("Stopped before the end.") if result.cancelled else ""))
        self.summary.setText(scanner.summary_text(result.items))
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(result.items))
        for r, it in enumerate(result.items):
            cells = [_Item(scanner.kind_label(it.kind)), _Item(it.name, it.name.lower()),
                     _Item(scanner.status_label(it.status), _STATUS_ORDER.get(it.status, 9)),
                     _Item(it.score, it.score), _Item(it.note), _Item(os.path.dirname(it.path), it.path.lower())]
            cells[0].setData(Qt.ItemDataRole.UserRole + 1, r)
            cells[2].setForeground(QColor(_COLOURS.get(it.status, "#000")))
            cells[5].setToolTip(it.path)
            for c, cell in enumerate(cells):
                t.setItem(r, c, cell)
        t.setSortingEnabled(True)
        t.sortByColumn(2, Qt.SortOrder.AscendingOrder)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        top = scanner.top_missing(result.items, 200)
        m = self.missing
        m.setSortingEnabled(False)
        m.setRowCount(len(top))
        for r, (cat, key, files, times) in enumerate(top):
            for c, cell in enumerate((_Item(key), _Item(scanner.category_label(cat)), _Item(files, files),
                                      _Item(times, times))):
                m.setItem(r, c, cell)
        m.setSortingEnabled(True)
        m.resizeColumnsToContents()
        m.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.html_button.setEnabled(True)
        self.xlsx_button.setEnabled(True)

    # ---- items -----------------------------------------------------------------------------------------------
    def item_at(self, row):
        cell = self.table.item(row, 0)
        if cell is None or self.result is None:
            return None
        return self.result.items[cell.data(Qt.ItemDataRole.UserRole + 1)]

    def selected_item(self):
        rows = {i.row() for i in self.table.selectedItems()}
        return self.item_at(min(rows)) if rows else None

    def _show_details(self):
        it = self.selected_item()
        self.models_button.setEnabled(bool(it and it.kind == "toolbox" and it.counts.get("models")))
        if it is None:
            self.details.setPlainText("")
            return
        lines = [it.path, f"{scanner.status_label(it.status)} - {it.score}", it.note, ""] + list(it.details)
        self.details.setPlainText("\n".join(str(x) for x in lines))

    def open_row(self, row):
        it = self.item_at(row)
        if it is not None:
            open_item(self.plugin, it, self)

    def import_selected(self):
        it = self.selected_item()
        if it is not None and it.kind == "toolbox":
            import_models(self.plugin, it.path, self)

    # ---- reports ---------------------------------------------------------------------------------------------
    def _ask_path(self, title, name, flt):
        s = QgsSettings()
        start = os.path.join(s.value(_LAST_DIR, os.path.join(os.path.expanduser("~"), "Documents")), name)
        path, _ = QFileDialog.getSaveFileName(self, title, start, flt)
        if path:
            s.setValue(_LAST_DIR, os.path.dirname(path))
        return path

    def export_html(self):
        if self.result is None:
            return None
        path = self._ask_path(tr("Save the HTML report"), tr("Migration readiness") + ".html", "HTML (*.html)")
        if not path:
            return None
        from .report import write_html

        write_html(self.result, path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return path

    def export_excel(self):
        if self.result is None:
            return None
        path = self._ask_path(tr("Save the Excel report"), tr("Migration readiness") + ".xlsx", "Excel (*.xlsx)")
        if not path:
            return None
        from .report import write_excel

        write_excel(self.result, path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return path

    def closeEvent(self, e):  # noqa: N802
        self._halt()
        super().closeEvent(e)

    def reject(self):
        self._halt()
        super().reject()

    def _halt(self):
        if self.busy():
            self.thread.stop()
            self.thread.wait(30000)


# ---- opening items ---------------------------------------------------------------------------------------------
def open_item(plugin, it, parent=None):
    """Open one scanned item in Biruni: maps and layer files with the ArcGIS reader, data in Add Data, toolboxes
    through the model import, scripts in the script editor."""
    ext = os.path.splitext(it.path)[1].lower()
    if it.kind in ("project", "map", "layer") and ext in (".aprx", ".mapx", ".lyrx", ".mxd", ".mxt"):
        from ..arcgis.ui import open_path

        return open_path(plugin, it.path)
    if it.kind == "toolbox":
        return import_models(plugin, it.path, parent)
    if it.kind in ("geodatabase", "shapefiles"):
        from .. import add_data

        dlg = add_data.open_dialog(plugin)
        if it.kind == "shapefiles":
            paths = sorted(os.path.join(it.path, n) for n in os.listdir(it.path) if n.lower().endswith(".shp"))
        else:
            paths = [it.path]
        dlg.add_paths(paths)
        return dlg
    if it.kind == "script":
        try:
            from processing.script.ScriptEditorDialog import ScriptEditorDialog

            dlg = ScriptEditorDialog(filePath=it.path, parent=plugin.iface.mainWindow() if plugin else None)
            dlg.show()
            return dlg
        except Exception:  # noqa: BLE001 - editor not available: the system's editor
            QDesktopServices.openUrl(QUrl.fromLocalFile(it.path))
            return None
    QMessageBox.information(parent, APP_NAME, it.note or it.path)
    return None


# ---- model import -----------------------------------------------------------------------------------------------
class ConversionDialog(QDialog):
    def __init__(self, parent, html_text):
        super().__init__(parent)
        self.setWindowTitle(tr("ModelBuilder import report"))
        self.resize(820, 600)
        v = QVBoxLayout(self)
        self.view = QTextBrowser()
        self.view.setHtml(html_text)
        v.addWidget(self.view)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


def import_models(plugin, path, parent=None, show=True):
    """Convert every model of a toolbox, save them to the user's models (they appear in the toolbox) and show the
    conversion report. Returns the conversions."""
    from qgis.PyQt.QtWidgets import QApplication

    from . import modelbuilder as mb
    from .report import conversion_html

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        convs = mb.convert_toolbox(path)
    except mb.ToolboxError as e:
        QApplication.restoreOverrideCursor()
        QMessageBox.warning(parent, APP_NAME, tr("Could not read {p}").format(p=path) + f": {e}")
        return []
    finally:
        if QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
    if show:
        if not convs:
            QMessageBox.information(parent, APP_NAME, tr("This toolbox has no models."))
        else:
            ConversionDialog(parent, conversion_html(convs)).exec()
    return convs


def import_models_dialog(plugin):
    s = QgsSettings()
    path, _ = QFileDialog.getOpenFileName(plugin.iface.mainWindow() if plugin else None,
                                          tr("Import ModelBuilder models"),
                                          s.value(_LAST_DIR, os.path.expanduser("~")),
                                          tr("ArcGIS toolboxes") + " (*.atbx *.tbx)")
    if not path:
        return []
    s.setValue(_LAST_DIR, os.path.dirname(path))
    return import_models(plugin, path, plugin.iface.mainWindow() if plugin else None)


_OPEN = {}


def open_dialog(plugin):
    dlg = _OPEN.get(id(plugin))
    try:
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return dlg
    except RuntimeError:
        pass
    dlg = MigrateDialog(plugin)
    _OPEN[id(plugin)] = dlg
    dlg.show()
    return dlg
