"""The window of the spatial statistics advisor («مستشار الإحصاء المكاني»): question -> layer and fields ->
checklist -> the matching tool, opened with its parameters filled in."""
from qgis.core import Qgis, QgsFieldProxyModel, QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QListWidget,
                                 QListWidgetItem, QPushButton, QVBoxLayout)

from ..i18n import is_rtl, tr
from . import advisor as adv
from .common import say

MARKS = {adv.OK: "✓", adv.WARN: "⚠", adv.STOP: "✖"}
COLOURS = {adv.OK: "#12805c", adv.WARN: "#a86b00", adv.STOP: "#b42318"}


class AdvisorDialog(QDialog):
    def __init__(self, plugin=None, parent=None):
        if parent is None and plugin is not None and getattr(plugin, "iface", None) is not None:
            parent = plugin.iface.mainWindow()
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle(tr("Spatial statistics advisor"))
        self.setMinimumWidth(640)
        if is_rtl():
            self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>" + tr("What do you want to know?") + "</b>"))
        self.questions = QListWidget()
        self.questions.setMinimumHeight(220)
        self._qs = adv.available_questions()
        for q in self._qs:
            item = QListWidgetItem(adv.question_text(q))
            item.setData(Qt.ItemDataRole.UserRole, q["id"])
            self.questions.addItem(item)
        lay.addWidget(self.questions)
        if not self._qs:
            lay.addWidget(QLabel(tr("No spatial statistics tools are installed.")))

        form = QFormLayout()
        self.tool = QComboBox()
        form.addRow(tr("Tool"), self.tool)
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(Qgis.LayerFilter.VectorLayer)
        self.layer.setAllowEmptyLayer(True)
        form.addRow(tr("Layer"), self.layer)
        self.field_label = QLabel(tr("Field"))
        self.field = QgsFieldComboBox()
        self.field.setAllowEmptyFieldName(True)
        form.addRow(self.field_label, self.field)
        self.field2_label = QLabel(tr("Second field"))
        self.field2 = QgsFieldComboBox()
        self.field2.setAllowEmptyFieldName(True)
        form.addRow(self.field2_label, self.field2)
        self.multi_label = QLabel(tr("Explanatory variables"))
        self.multi = QListWidget()
        self.multi.setMaximumHeight(110)
        form.addRow(self.multi_label, self.multi)
        lay.addLayout(form)

        lay.addWidget(QLabel("<b>" + tr("What the advisor found") + "</b>"))
        self.checks = QListWidget()
        self.checks.setMinimumHeight(150)
        self.checks.setWordWrap(True)
        lay.addWidget(self.checks)

        box = QDialogButtonBox()
        self.open_btn = QPushButton(tr("Open the tool"))
        self.open_btn.setDefault(True)
        box.addButton(self.open_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        self.open_btn.clicked.connect(self.open_tool)
        lay.addWidget(box)

        self.questions.currentRowChanged.connect(self._question_changed)
        self.layer.layerChanged.connect(self._layer_changed)
        self.field.fieldChanged.connect(lambda *_: self.refresh())
        self.field2.fieldChanged.connect(lambda *_: self.refresh())
        self.multi.itemChanged.connect(lambda *_: self.refresh())
        self.tool.currentIndexChanged.connect(lambda *_: self.refresh())
        if self._qs:
            self.questions.setCurrentRow(0)

    # ----------------------------------------------------------- state
    def question_id(self):
        item = self.questions.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def select_question(self, qid):
        for i in range(self.questions.count()):
            if self.questions.item(i).data(Qt.ItemDataRole.UserRole) == qid:
                self.questions.setCurrentRow(i)
                return True
        return False

    def set_layer(self, layer):
        self.layer.setLayer(layer)
        self._layer_changed(layer)

    def set_fields(self, field=None, field2=None):
        """field2: a name, or a list of names for the explanatory variables."""
        self.field.setField(field or "")
        if isinstance(field2, (list, tuple)):
            for i in range(self.multi.count()):
                it = self.multi.item(i)
                it.setCheckState(Qt.CheckState.Checked if it.text() in field2 else Qt.CheckState.Unchecked)
        else:
            self.field2.setField(field2 or "")
        self.refresh()

    def tool_id(self):
        return self.tool.currentData()

    def chosen_field2(self):
        q = adv.question(self.question_id()) or {}
        if q.get("field2") == "numeric_multi":
            return [self.multi.item(i).text() for i in range(self.multi.count())
                    if self.multi.item(i).checkState() == Qt.CheckState.Checked]
        return self.field2.currentField() or None

    def current_parameters(self):
        return adv.parameters(self.tool_id(), self.layer.currentLayer(), self.field.currentField() or None,
                              self.chosen_field2(), self.question_id())

    # ----------------------------------------------------------- updates
    def _question_changed(self, *_):
        from qgis.core import QgsApplication

        qid = self.question_id()
        q = adv.question(qid) or {}
        self.tool.blockSignals(True)
        self.tool.clear()
        for t in adv.tools_for(qid):
            alg = QgsApplication.processingRegistry().algorithmById(t)
            self.tool.addItem(alg.displayName() if alg else t, t)
        self.tool.blockSignals(False)
        geo = q.get("geometry")
        self.layer.setFilters({"point": Qgis.LayerFilter.PointLayer, "polygon": Qgis.LayerFilter.PolygonLayer}
                              .get(geo, Qgis.LayerFilter.VectorLayer))
        w1, w2 = adv.field_words(qid)
        f1, f2 = q.get("field"), q.get("field2")
        self.field_label.setText(say(w1) if w1 else tr("Field"))
        self.field_label.setVisible(bool(f1))
        self.field.setVisible(bool(f1))
        self.field.setFilters(QgsFieldProxyModel.Filter.Numeric)
        single2 = f2 in ("numeric", "date", "category")
        self.field2_label.setVisible(single2)
        self.field2.setVisible(single2)
        if single2:
            self.field2_label.setText(say(w2) if w2 else tr("Second field"))
            self.field2.setFilters({"numeric": QgsFieldProxyModel.Filter.Numeric,
                                    "date": QgsFieldProxyModel.Filter.Date | QgsFieldProxyModel.Filter.String,
                                    "category": QgsFieldProxyModel.Filter.String | QgsFieldProxyModel.Filter.Int}
                                   [f2])
        self.multi_label.setVisible(f2 == "numeric_multi")
        self.multi.setVisible(f2 == "numeric_multi")
        if f2 == "numeric_multi" and w2:
            self.multi_label.setText(say(w2))
        self._layer_changed(self.layer.currentLayer())

    def _layer_changed(self, layer):
        self.field.setLayer(layer)
        self.field2.setLayer(layer)
        self.multi.blockSignals(True)
        self.multi.clear()
        if layer is not None and hasattr(layer, "fields"):
            for f in layer.fields():
                if f.isNumeric():
                    it = QListWidgetItem(f.name())
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(Qt.CheckState.Unchecked)
                    self.multi.addItem(it)
        self.multi.blockSignals(False)
        self.refresh()

    def refresh(self):
        self.checks.clear()
        layer = self.layer.currentLayer()
        if self.question_id() is None:
            self.open_btn.setEnabled(False)
            return
        found = adv.inspect(layer, self.field.currentField() or None, self.question_id(), self.chosen_field2())
        self.found = found
        for level, msg in found:
            it = QListWidgetItem(f"{MARKS[level]}  {msg}")
            it.setForeground(_brush(COLOURS[level]))
            self.checks.addItem(it)
        self.open_btn.setEnabled(bool(self.tool_id()) and layer is not None)

    def open_tool(self):
        tid = self.tool_id()
        if not tid:
            return None
        return adv.open_tool(self.plugin, tid, self.current_parameters())


def _brush(colour):
    from qgis.PyQt.QtGui import QBrush, QColor

    return QBrush(QColor(colour))


_kept = []


def open_dialog(plugin):
    """Open the advisor (not modal: the tool dialog it opens stays usable next to it)."""
    dlg = AdvisorDialog(plugin)
    layer = plugin.iface.activeLayer() if getattr(plugin, "iface", None) is not None else None
    if layer is not None and layer in QgsProject.instance().mapLayers().values():
        dlg.layer.setLayer(layer)
    _kept.append(dlg)
    dlg.finished.connect(lambda *_: _kept.remove(dlg) if dlg in _kept else None)
    dlg.show()
    dlg.raise_()
    return dlg
