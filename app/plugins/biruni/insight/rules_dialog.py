"""The window of the rules learned from the data: learn, then read each rule with its support and open it to
see the features that break it - zoom to them or select them. The rules learned here are the ones the
drawing tool checks new features against (learned.broken_rules)."""
from qgis.core import Qgis, QgsFeedback, QgsProject, QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import is_rtl, tr
from . import learned
from .spell_dialog import feature_labels

ROLE = Qt.ItemDataRole.UserRole
SHOWN = 500     # exceptions listed under a rule (all of them are selected / zoomed to)


class RulesDialog(QDialog):
    def __init__(self, plugin=None, parent=None):
        iface = getattr(plugin, "iface", None)
        super().__init__(parent or (iface.mainWindow() if iface is not None else None))
        self.plugin = plugin
        self.iface = iface
        self.rules = []
        self.feedback = None
        self.setWindowTitle(tr("Rules learned from your data"))
        self.setMinimumSize(QSize(760, 520))
        if is_rtl():
            self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Finds the rules your data follows without anyone writing them down - features inside "
                          "others, no overlaps, typical values and widths, right angles, setbacks, fields that "
                          "decide other fields, unique ids - and lists the features that break them. Only rules "
                          "followed by at least 90% of the features are shown."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        row = QHBoxLayout()
        self.only = QCheckBox(tr("Only this layer"))
        row.addWidget(self.only)
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(Qgis.LayerFilter.VectorLayer)
        self.layer.setEnabled(False)
        self.only.toggled.connect(self.layer.setEnabled)
        row.addWidget(self.layer, 1)
        self.learn_btn = QPushButton(tr("Learn the rules"))
        self.learn_btn.setDefault(True)
        self.learn_btn.clicked.connect(self.run)
        row.addWidget(self.learn_btn)
        v.addLayout(row)
        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.setVisible(False)
        prow.addWidget(self.progress, 1)
        self.stop_btn = QPushButton(tr("Stop"))
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(lambda: self.feedback is not None and self.feedback.cancel())
        prow.addWidget(self.stop_btn)
        v.addLayout(prow)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr("Rule"), tr("Exceptions")])
        self.tree.setWordWrap(True)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.tree.itemDoubleClicked.connect(lambda *_: self.zoom())
        self.tree.itemExpanded.connect(self._expand)
        v.addWidget(self.tree, 1)

        brow = QHBoxLayout()
        self.zoom_btn = QPushButton(tr("Zoom to"))
        self.zoom_btn.clicked.connect(self.zoom)
        self.select_btn = QPushButton(tr("Select features"))
        self.select_btn.clicked.connect(self.select)
        brow.addWidget(self.zoom_btn)
        brow.addWidget(self.select_btn)
        brow.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        brow.addWidget(close)
        v.addLayout(brow)
        self._update_buttons()

    # ---- learning ---------------------------------------------------------------------------------------
    def run(self):
        if self.feedback is not None:
            return
        layers = None
        if self.only.isChecked():
            lyr = self.layer.currentLayer()
            if not isinstance(lyr, QgsVectorLayer):
                return
            layers = [lyr]
        self.feedback = QgsFeedback()
        self.feedback.progressChanged.connect(self._progress)
        self._busy(True)
        self.summary.setText(tr("Learning from the data…"))
        try:
            self.rules = learned.learn(layers=layers, feedback=self.feedback)
            cancelled = self.feedback.isCanceled()
        finally:
            self.feedback = None
            self._busy(False)
        self._fill()
        if cancelled:
            self.summary.setText(tr("Stopped - the list may be incomplete."))

    def _progress(self, value):
        self.progress.setValue(int(value))
        QApplication.processEvents()

    def _busy(self, busy):
        self.progress.setVisible(busy)
        self.stop_btn.setVisible(busy)
        self.progress.setValue(0)
        self.learn_btn.setEnabled(not busy)

    def _fill(self):
        self.tree.clear()
        if not self.rules:
            self.summary.setText(tr("No rule with exceptions was found: either the data follows its rules "
                                    "everywhere, or it follows none strongly enough."))
            return
        self.summary.setText(tr("{n} rules found. Open a rule to see the features that break it.")
                             .format(n=len(self.rules)))
        for i, r in enumerate(self.rules):
            top = QTreeWidgetItem([r.text(), str(len(r.exceptions))])
            top.setToolTip(0, r.kind_label() + ": " + r.text())
            top.setData(0, ROLE, (i, None))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            if r.exceptions:
                QTreeWidgetItem(top, ["…"])     # filled when opened (long lists cost nothing until then)
            self.tree.addTopLevelItem(top)
        self.tree.resizeColumnToContents(1)
        self._update_buttons()

    def _expand(self, top):
        data = top.data(0, ROLE)
        if data is None or data[1] is not None or top.childCount() != 1 or top.child(0).data(0, ROLE) is not None:
            return
        i = data[0]
        r = self.rules[i]
        top.takeChild(0)
        layer = QgsProject.instance().mapLayer(r.layer_id)
        shown = r.exceptions[:SHOWN]
        labels = feature_labels(layer, {e[0] for e in shown}) if layer is not None else {}
        for j, (fid, key, values) in enumerate(shown):
            it = QTreeWidgetItem(top, [labels.get(fid, f"#{fid}") + " - " + learned.Rule.note(key, values), ""])
            it.setData(0, ROLE, (i, j))
        if len(r.exceptions) > SHOWN:
            QTreeWidgetItem(top, [tr("… and {n} more").format(n=len(r.exceptions) - SHOWN), ""])

    # ---- actions ----------------------------------------------------------------------------------------
    def _current(self):
        """(rule, fids) of the selected row: one exception, or all exceptions of a rule."""
        it = self.tree.currentItem()
        data = it.data(0, ROLE) if it is not None else None
        if data is None:
            if it is not None and it.parent() is not None:
                data = it.parent().data(0, ROLE)
            if data is None:
                return None, []
        r = self.rules[data[0]]
        if data[1] is None:
            return r, r.fids
        return r, [r.exceptions[data[1]][0]]

    def _update_buttons(self):
        r, fids = self._current()
        ok = r is not None and bool(fids) and self.iface is not None
        self.zoom_btn.setEnabled(ok)
        self.select_btn.setEnabled(ok)

    def zoom(self):
        r, fids = self._current()
        layer = QgsProject.instance().mapLayer(r.layer_id) if r is not None else None
        if layer is None or not fids or self.iface is None:
            return
        canvas = self.iface.mapCanvas()
        canvas.zoomToFeatureIds(layer, fids[:5000])
        canvas.flashFeatureIds(layer, fids[:200])

    def select(self):
        r, fids = self._current()
        layer = QgsProject.instance().mapLayer(r.layer_id) if r is not None else None
        if not isinstance(layer, QgsVectorLayer) or self.iface is None:
            return
        layer.selectByIds(fids)
        self.iface.setActiveLayer(layer)
        self.iface.messageBar().pushInfo(tr("Rules learned from your data"),
                                         tr("{n} features selected in {layer}.").format(n=len(fids),
                                                                                         layer=layer.name()))


_kept = []


def open_rules(plugin):
    """Open the learned-rules window (not modal) with the active layer ready for "Only this layer"."""
    dlg = RulesDialog(plugin)
    iface = getattr(plugin, "iface", None)
    layer = iface.activeLayer() if iface is not None else None
    if isinstance(layer, QgsVectorLayer):
        dlg.layer.setLayer(layer)
    _kept.append(dlg)
    dlg.finished.connect(lambda *_: _kept.remove(dlg) if dlg in _kept else None)
    dlg.show()
    dlg.raise_()
    return dlg
