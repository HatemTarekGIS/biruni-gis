"""'Check my data' window: scans the project, lists the problems per layer and
offers zoom / select / fix for each."""
import csv
import html

from qgis.core import QgsFeedback, QgsIconUtils, QgsProject, QgsSettings, QgsVectorLayer
from qgis.gui import QgsProjectionSelectionDialog
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QProgressBar,
                                 QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import icons
from ..i18n import tr
from . import health

ROLE = Qt.ItemDataRole.UserRole
SEVERITY = {  # severity -> (icon, light colour, dark colour)
    "error": ("alert-triangle", "#C93636", "#F27474"),
    "warning": ("alert-triangle", "#B86E00", "#F5A524"),
    "info": ("info-circle", "#2F6FB3", "#7FB2EA"),
}


def health_qss(t):
    return f"""
#BiruniHealth QTreeWidget {{ border: 1px solid {t['border']}; border-radius: 6px; background: {t['base']}; }}
#BiruniHealth QTreeWidget::item {{ padding: 4px 2px; }}
#BiruniHealth QLabel#HealthTitle {{ font-size: 13pt; font-weight: 700; color: {t['text']}; }}
#BiruniHealth QLabel#HealthSummary {{ color: {t['text_dim']}; }}
#BiruniHealth QPushButton#HealthPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    border-radius: 6px; padding: 6px 16px; font-weight: 600; }}
#BiruniHealth QPushButton#HealthPrimary:hover {{ background: {t['accent_hover']}; }}
#BiruniHealth QPushButton#HealthPrimary:disabled {{ background: {t['mid']}; }}
"""


class HealthDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent or plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.issues = []
        self.feedback = None
        self.setObjectName("BiruniHealth")
        self.setWindowTitle(tr("Check my data"))
        self.setMinimumSize(QSize(560, 460))

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        self.title = QLabel(tr("Check my data"))
        self.title.setObjectName("HealthTitle")
        v.addWidget(self.title)
        self.summary = QLabel()
        self.summary.setObjectName("HealthSummary")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        prow.addWidget(self.progress, 1)
        self.cancel_btn = QPushButton(tr("Stop"))
        self.cancel_btn.clicked.connect(self._cancel)
        prow.addWidget(self.cancel_btn)
        v.addLayout(prow)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setWordWrap(True)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.tree.itemDoubleClicked.connect(lambda *_: self.zoom())
        v.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.zoom_btn = QPushButton(tr("Zoom to"))
        self.zoom_btn.clicked.connect(self.zoom)
        self.select_btn = QPushButton(tr("Select features"))
        self.select_btn.clicked.connect(self.select)
        self.fix_btn = QPushButton(tr("Fix"))
        self.fix_btn.setObjectName("HealthPrimary")
        self.fix_btn.clicked.connect(self.fix)
        for b in (self.zoom_btn, self.select_btn, self.fix_btn):
            row.addWidget(b)
        row.addStretch(1)
        self.export_btn = QPushButton(tr("Export list…"))
        self.export_btn.clicked.connect(self.export_csv)
        self.rescan_btn = QPushButton(tr("Check again"))
        self.rescan_btn.clicked.connect(self.scan)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        for b in (self.export_btn, self.rescan_btn, close):
            row.addWidget(b)
        v.addLayout(row)
        self.refresh_theme()

    # ---- theme -----------------------------------------------------------
    def _tokens(self):
        theme = getattr(self.plugin, "theme", None)
        return theme.tokens() if theme is not None else {
            "border": "#DDE2E7", "base": "#FFFFFF", "text": "#1E252B", "text_dim": "#5B6670", "accent": "#0E7C74",
            "accent_text": "#FFFFFF", "accent_hover": "#0A635D", "mid": "#C9D0D6", "icon": "#33414C"}

    def _dark(self):
        theme = getattr(self.plugin, "theme", None)
        return bool(theme is not None and theme.is_dark())

    def refresh_theme(self):
        self.setStyleSheet(health_qss(self._tokens()))
        if self.issues or self.tree.topLevelItemCount():
            self._fill()

    # ---- scanning ----------------------------------------------------------
    def scan(self):
        if self.feedback is not None:
            return
        self.feedback = QgsFeedback()
        self.feedback.progressChanged.connect(self._progress)
        self._busy(True)
        self.summary.setText(tr("Checking every layer of the project…"))
        try:
            self.issues = health.scan_project(QgsProject.instance(), self.feedback)
            cancelled = self.feedback.isCanceled()
        finally:
            self.feedback = None
            self._busy(False)
        self._fill()
        if cancelled:
            self.summary.setText(tr("Check stopped - the list may be incomplete."))

    def _progress(self, value):
        self.progress.setValue(int(value))
        QApplication.processEvents()

    def _cancel(self):
        if self.feedback is not None:
            self.feedback.cancel()

    def _busy(self, busy):
        self.progress.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        self.progress.setValue(0)
        for b in (self.rescan_btn, self.export_btn, self.zoom_btn, self.select_btn, self.fix_btn):
            b.setEnabled(not busy)
        if not busy:
            self._update_buttons()

    def _fill(self):
        t = self._tokens()
        dark = self._dark()
        self.tree.clear()
        counts = health.summary(self.issues)
        if not self.issues:
            it = QTreeWidgetItem(self.tree, [tr("No problems found.")])
            it.setIcon(0, icons.icon("shield-check", t["accent"]))
            self.summary.setText(tr("Every layer passed the checks."))
            self._update_buttons()
            return
        self.summary.setText(tr("{e} errors, {w} warnings, {i} notes. Double-click a problem to see it on the map.")
                             .format(e=counts["error"], w=counts["warning"], i=counts["info"]))
        groups = {}
        project = QgsProject.instance()
        for issue in self.issues:
            top = groups.get(issue.layer_id)
            if top is None:
                top = QTreeWidgetItem(self.tree, [issue.layer_name or tr("Project")])
                lyr = project.mapLayer(issue.layer_id)
                if lyr is not None:
                    top.setIcon(0, QgsIconUtils.iconForLayer(lyr))
                f = top.font(0)
                f.setBold(True)
                top.setFont(0, f)
                top.setData(0, ROLE, None)
                top.setExpanded(True)
                groups[issue.layer_id] = top
            name, light, darkc = SEVERITY[issue.severity]
            child = QTreeWidgetItem(top, [issue.text()])
            child.setIcon(0, icons.icon(name, darkc if dark else light))
            child.setToolTip(0, issue.text())
            child.setData(0, ROLE, self.issues.index(issue))
        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._update_buttons()

    # ---- current selection -------------------------------------------------
    def _current(self):
        """(issue or None, layer or None) of the selected row."""
        item = self.tree.currentItem()
        if item is None:
            return None, None
        idx = item.data(0, ROLE)
        if idx is None:
            child = item.child(0)
            idx = child.data(0, ROLE) if child is not None else None
            issue = None
        else:
            issue = self.issues[idx]
        lid = self.issues[idx].layer_id if idx is not None else ""
        return issue, QgsProject.instance().mapLayer(lid)

    def _extra_fix(self, issue, layer):
        """(label, callable) for problems fixed by a Biruni command rather than by an edit."""
        project = getattr(self.plugin, "project", None)
        if issue.kind == "temporary" and project is not None and hasattr(project, "store_temporary_layers"):
            return tr("Save into the project"), project.store_temporary_layers
        if issue.kind == "outside_folder" and project is not None and hasattr(project, "consolidate"):
            return tr("Copy into the project"), project.consolidate
        if issue.kind == "no_crs" and layer is not None:
            return tr("Set coordinate system…"), lambda: self._set_crs(layer)
        if issue.kind == "crs_mismatch" and hasattr(getattr(self.plugin, "runner", None), "open_algorithm"):
            return tr("Reproject…"), lambda: self.plugin.runner.open_algorithm("native:reprojectlayer")
        return None, None

    def _update_buttons(self):
        issue, layer = self._current()
        ok = layer is not None and layer.isValid()
        self.zoom_btn.setEnabled(ok)
        self.select_btn.setEnabled(bool(ok and issue is not None and issue.fids and isinstance(layer, QgsVectorLayer)))
        label, fn = self._extra_fix(issue, layer) if issue is not None else (None, None)
        if issue is not None and issue.fix is not None:
            label = issue.fix_label or tr("Fix")
        self.fix_btn.setText(label or tr("Fix"))
        self.fix_btn.setEnabled(bool(issue is not None and (issue.fix is not None or fn is not None)))

    # ---- actions -----------------------------------------------------------
    def zoom(self):
        issue, layer = self._current()
        if layer is None or not layer.isValid():
            return
        canvas = self.iface.mapCanvas()
        if issue is not None and issue.fids and isinstance(layer, QgsVectorLayer):
            canvas.zoomToFeatureIds(layer, issue.fids[:5000])
            canvas.flashFeatureIds(layer, issue.fids[:200])
        else:
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()

    def select(self):
        issue, layer = self._current()
        if issue is None or not isinstance(layer, QgsVectorLayer):
            return
        layer.selectByIds(issue.fids)
        self.iface.setActiveLayer(layer)
        self.iface.messageBar().pushInfo(tr("Check my data"), tr("{n} features selected in {layer}.")
                                         .format(n=len(issue.fids), layer=layer.name()))

    def fix(self):
        issue, layer = self._current()
        if issue is None:
            return
        if issue.fix is not None:
            n = issue.fix()
            if n:
                self.iface.messageBar().pushSuccess(
                    tr("Check my data"),
                    tr("Fixed {n} features in {layer}. The layer is in edit mode: save the edits to keep the "
                       "changes, or undo them.").format(n=n, layer=issue.layer_name))
            else:
                self.iface.messageBar().pushWarning(tr("Check my data"),
                                                    tr("Nothing could be fixed automatically."))
        else:
            _label, fn = self._extra_fix(issue, layer)
            if fn is None:
                return
            fn()
        self.scan()

    def _set_crs(self, layer):
        dlg = QgsProjectionSelectionDialog(self)
        dlg.setWindowTitle(tr("Coordinate system of {layer}").format(layer=layer.name()))
        if QgsProject.instance().crs().isValid():
            dlg.setCrs(QgsProject.instance().crs())
        if dlg.exec():
            layer.setCrs(dlg.crs())
            layer.triggerRepaint()

    def export_csv(self):
        s = QgsSettings()
        start = s.value("biruni/health/export_dir", QgsProject.instance().absolutePath() or "")
        path, _ = QFileDialog.getSaveFileName(self, tr("Export list…"), start, "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        s.setValue("biruni/health/export_dir", path.rsplit("/", 1)[0])
        labels = {"error": tr("Error"), "warning": tr("Warning"), "info": tr("Note")}
        # utf-8-sig so Excel shows Arabic text correctly
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow([tr("Severity"), tr("Layer"), tr("Problem"), tr("Count"), tr("Feature ids")])
            for it in self.issues:
                w.writerow([labels[it.severity], it.layer_name, it.text(), it.count,
                            " ".join(str(x) for x in it.fids[:5000])])
        self.iface.messageBar().pushSuccess(tr("Check my data"),
                                            tr("Saved {path}").format(path=html.escape(path)))


def show_health_dialog(plugin):
    """Open (or raise) the single 'Check my data' window and start a scan."""
    dlg = getattr(plugin, "_health_dialog", None)
    if dlg is None:
        dlg = HealthDialog(plugin)
        plugin._health_dialog = dlg
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    dlg.scan()
    return dlg
