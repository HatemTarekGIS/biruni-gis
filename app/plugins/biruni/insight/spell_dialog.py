"""The window of the spatial spell checker: pick a layer, check it, and go through the suspicious values -
zoom to each one, accept the suggestion with one click, or leave it. The suspicious cells can also be marked
in the attribute table (the engine's own conditional styles, nothing patched)."""
from qgis.core import (Qgis, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest,
                       QgsFeedback, QgsProject, QgsVectorLayer)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView,
                                 QLabel, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import is_rtl, tr
from . import spellcheck

ROLE = Qt.ItemDataRole.UserRole


def feature_labels(layer, fids):
    """{fid: short label}: the layer's display expression when it has one, else «#fid»."""
    out = {fid: f"#{fid}" for fid in fids}
    expr_text = layer.displayExpression() if isinstance(layer, QgsVectorLayer) else ""
    if not expr_text or not fids:
        return out
    expr = QgsExpression(expr_text)
    ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    expr.prepare(ctx)
    for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(list(fids)[:5000])):
        ctx.setFeature(f)
        v = expr.evaluate(ctx)
        if v not in (None, "") and str(v) != "NULL":
            out[f.id()] = f"{v} (#{f.id()})"
    return out


class SpellDialog(QDialog):
    def __init__(self, plugin=None, parent=None):
        iface = getattr(plugin, "iface", None)
        super().__init__(parent or (iface.mainWindow() if iface is not None else None))
        self.plugin = plugin
        self.iface = iface
        self.result = None
        self.feedback = None
        self.setWindowTitle(tr("Spatial spell check"))
        self.setMinimumSize(QSize(820, 520))
        if is_rtl():
            self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Checks each value against where its feature lies: the district polygon around it, "
                          "the nearest street, the numbering of its neighbours, what its neighbours say, and "
                          "its measured area or length."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Layer")))
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(Qgis.LayerFilter.VectorLayer)
        row.addWidget(self.layer, 1)
        self.check_btn = QPushButton(tr("Check"))
        self.check_btn.setDefault(True)
        self.check_btn.clicked.connect(self.run)
        row.addWidget(self.check_btn)
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
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels([tr("Feature"), tr("Field"), tr("Value"), tr("Suggestion"), tr("Why"),
                                   tr("Check")])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setWordWrap(True)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self.zoom())
        v.addWidget(self.tree, 1)

        brow = QHBoxLayout()
        self.zoom_btn = QPushButton(tr("Zoom to"))
        self.zoom_btn.clicked.connect(self.zoom)
        self.accept_btn = QPushButton(tr("Accept suggestion"))
        self.accept_btn.clicked.connect(self.accept_selected)
        self.ignore_btn = QPushButton(tr("Ignore"))
        self.ignore_btn.clicked.connect(self.ignore_selected)
        self.table_btn = QPushButton(tr("Open attribute table"))
        self.table_btn.clicked.connect(self.open_table)
        for b in (self.zoom_btn, self.accept_btn, self.ignore_btn, self.table_btn):
            brow.addWidget(b)
        self.mark = QCheckBox(tr("Mark the cells in the attribute table"))
        self.mark.setChecked(True)
        self.mark.toggled.connect(lambda *_: self._apply_marks())
        brow.addWidget(self.mark)
        brow.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        brow.addWidget(close)
        v.addLayout(brow)
        self._update_buttons()

    # ---- running --------------------------------------------------------------------------------------
    def current_layer(self):
        lyr = self.layer.currentLayer()
        return lyr if isinstance(lyr, QgsVectorLayer) else None

    def run(self):
        layer = self.current_layer()
        if layer is None or self.feedback is not None:
            return
        self.feedback = QgsFeedback()
        self.feedback.progressChanged.connect(self._progress)
        self._busy(True)
        self.summary.setText(tr("Checking {layer}…").format(layer=layer.name()))
        try:
            self.result = spellcheck.check_layer(layer, feedback=self.feedback)
        finally:
            self.feedback = None
            self._busy(False)
        self._fill()
        self._apply_marks()

    def _progress(self, value):
        self.progress.setValue(int(value))
        QApplication.processEvents()

    def _busy(self, busy):
        self.progress.setVisible(busy)
        self.stop_btn.setVisible(busy)
        self.progress.setValue(0)
        self.check_btn.setEnabled(not busy)

    def _fill(self):
        self.tree.clear()
        res = self.result
        if res is None:
            return
        layer = QgsProject.instance().mapLayer(res.layer_id)
        pairs = "".join("<br>• " + p.text() for p in res.pairs)
        if not res.findings:
            head = tr("No suspicious value among {n} features.").format(n=res.checked)
        else:
            head = tr("{k} suspicious values among {n} features. Double-click one to see it on the map.").format(
                k=len(res.findings), n=res.checked)
        if not res.pairs:
            pairs = "<br>" + tr("No field could be checked: none names a polygon or line of another layer, "
                                "follows a numbering, is shared by neighbours or holds a measure.")
        else:
            pairs = "<br>" + tr("What the fields were found to mean:") + pairs
        self.summary.setText(head + pairs)
        labels = feature_labels(layer, {f.fid for f in res.findings}) if layer is not None else {}
        for i, f in enumerate(res.findings):
            it = QTreeWidgetItem([labels.get(f.fid, f"#{f.fid}"), f.field, str(f.value), str(f.suggestion),
                                  f.text(), f.kind_label()])
            it.setToolTip(4, f.text())
            it.setData(0, ROLE, i)
            self.tree.addTopLevelItem(it)
        for col in (0, 1, 2, 3, 5):
            self.tree.resizeColumnToContents(col)
        self._update_buttons()

    # ---- selection --------------------------------------------------------------------------------------
    def selected(self):
        if self.result is None:
            return []
        return [self.result.findings[it.data(0, ROLE)] for it in self.tree.selectedItems()
                if it.data(0, ROLE) is not None]

    def _layer(self):
        return QgsProject.instance().mapLayer(self.result.layer_id) if self.result is not None else None

    def _update_buttons(self):
        has = bool(self.selected())
        for b in (self.zoom_btn, self.accept_btn, self.ignore_btn):
            b.setEnabled(has)
        self.table_btn.setEnabled(self._layer() is not None)

    # ---- actions ----------------------------------------------------------------------------------------
    def zoom(self):
        fs, layer = self.selected(), self._layer()
        if not fs or layer is None or self.iface is None:
            return
        fids = sorted({f.fid for f in fs})
        canvas = self.iface.mapCanvas()
        canvas.zoomToFeatureIds(layer, fids)
        canvas.flashFeatureIds(layer, fids[:200])

    def _drop(self, chosen):
        ids = {id(f) for f in chosen}
        self.result.findings = [f for f in self.result.findings if id(f) not in ids]
        self._fill()
        self._apply_marks()

    def accept_selected(self):
        fs = self.selected()
        if not fs:
            return
        n = spellcheck.accept(fs)
        if self.iface is not None and n:
            self.iface.messageBar().pushSuccess(
                tr("Spatial spell check"),
                tr("{n} values changed in {layer}. The layer is in edit mode: save the edits to keep them, or "
                   "undo them.").format(n=n, layer=fs[0].layer_name))
        self._drop(fs)

    def ignore_selected(self):
        self._drop(self.selected())

    def _apply_marks(self):
        layer = self._layer()
        if layer is None:
            return
        if self.mark.isChecked() and self.result.findings:
            spellcheck.mark_table(layer, self.result.findings)
        else:
            spellcheck.clear_marks(layer)

    def open_table(self):
        layer = self._layer()
        if layer is not None and self.iface is not None:
            self.iface.showAttributeTable(layer)

    def closeEvent(self, e):  # noqa: N802
        layer = self._layer()
        if layer is not None:
            spellcheck.clear_marks(layer)     # marks are a working aid, not part of the project
        super().closeEvent(e)


_kept = []


def open_spellcheck(plugin):
    """Open the spatial spell checker on the active layer (not modal: the map stays usable)."""
    dlg = SpellDialog(plugin)
    iface = getattr(plugin, "iface", None)
    layer = iface.activeLayer() if iface is not None else None
    if isinstance(layer, QgsVectorLayer):
        dlg.layer.setLayer(layer)
    _kept.append(dlg)
    dlg.finished.connect(lambda *_: _kept.remove(dlg) if dlg in _kept else None)
    dlg.show()
    dlg.raise_()
    return dlg
