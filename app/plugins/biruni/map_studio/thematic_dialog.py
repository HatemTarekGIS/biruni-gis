"""The thematic map wizard window («عايز توضّح إيه؟»): pick a layer and a field → see what the data is and its
histogram → pick one of the recommended map types (each with its reason and warnings) → Apply / Undo, live."""
from qgis.core import QgsFieldProxyModel, QgsMapLayerProxyModel, QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget,
                                 QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..i18n import format_number, is_rtl, tr
from . import thematic

ACCENT = "#2563EB"
WARN = "#B45309"


class Histogram(QWidget):
    """A small bar chart of the values (20 bins), with the class breaks as vertical lines when known."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.counts, self.lo, self.hi, self.breaks = [], 0.0, 1.0, []

    def set_values(self, values, breaks=()):
        self.counts, self.breaks = [], list(breaks)
        if values:
            self.lo, self.hi = min(values), max(values)
            span = (self.hi - self.lo) or 1.0
            self.counts = [0] * 20
            for v in values:
                self.counts[min(int((v - self.lo) / span * 20), 19)] += 1
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height() - 16
        p.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self.counts:
            p.setPen(QColor("#9CA3AF"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("No numbers to chart"))
            return
        top = max(self.counts)
        bw = w / len(self.counts)
        for i, c in enumerate(self.counts):
            bh = (h - 4) * c / top
            p.fillRect(QRectF(i * bw + 1, h - bh, bw - 2, bh), QColor(ACCENT))
        span = (self.hi - self.lo) or 1.0
        p.setPen(QColor("#DC2626"))
        for b in self.breaks:
            if self.lo < b < self.hi:
                x = (b - self.lo) / span * w
                p.drawLine(int(x), 0, int(x), h)
        p.setPen(QColor("#374151"))
        p.drawText(QRectF(0, h, w / 2, 16), Qt.AlignmentFlag.AlignLeft, format_number(self.lo))
        p.drawText(QRectF(w / 2, h, w / 2, 16), Qt.AlignmentFlag.AlignRight, format_number(self.hi))


class ThematicDialog(QDialog):
    def __init__(self, plugin=None, layer=None):
        super().__init__(getattr(plugin, "mw", None))
        self.plugin = plugin
        self.setWindowTitle(tr("What do you want the map to show?"))
        self.setMinimumWidth(560)
        if is_rtl():
            self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.layer_box = QgsMapLayerComboBox()
        self.layer_box.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        self.field_box = QgsFieldComboBox()
        self.second_box = QgsFieldComboBox()
        self.second_box.setAllowEmptyFieldName(True)
        self.second_box.setFilters(QgsFieldProxyModel.Filter.Numeric)
        form.addRow(tr("Layer"), self.layer_box)
        form.addRow(tr("Field"), self.field_box)
        form.addRow(tr("Second field (optional)"), self.second_box)
        root.addLayout(form)
        self.kind = QLabel()
        self.kind.setWordWrap(True)
        root.addWidget(self.kind)
        self.hist = Histogram()
        root.addWidget(self.hist)
        root.addWidget(QLabel(tr("Recommended map types")))
        self.cards = QListWidget()
        self.cards.setWordWrap(True)
        self.cards.setSpacing(3)
        root.addWidget(self.cards, 1)
        opts = QHBoxLayout()
        self.classes = QSpinBox()
        self.classes.setRange(2, 9)
        self.classes.setValue(5)
        self.palette = QComboBox()
        for name in ("ylgnbu", "viridis", "blues", "magma", "cividis", "greys"):
            self.palette.addItem(name, name)
        self.normalise = QComboBox()
        self.normalise.addItem(tr("As it is"), "")
        self.normalise.addItem(tr("Per km²"), "area")
        opts.addWidget(QLabel(tr("Classes")))
        opts.addWidget(self.classes)
        opts.addWidget(QLabel(tr("Colours")))
        opts.addWidget(self.palette)
        opts.addWidget(QLabel(tr("Divide by")))
        opts.addWidget(self.normalise, 1)
        root.addLayout(opts)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        buttons = QHBoxLayout()
        self.apply_btn = QPushButton(tr("Apply"))
        self.apply_btn.setDefault(True)
        self.undo_btn = QPushButton(tr("Undo"))
        close = QPushButton(tr("Close"))
        buttons.addStretch(1)
        for b in (self.undo_btn, self.apply_btn, close):
            buttons.addWidget(b)
        root.addLayout(buttons)

        self.layer_box.layerChanged.connect(self._layer_changed)
        self.field_box.fieldChanged.connect(self.refresh)
        self.second_box.fieldChanged.connect(self.refresh)
        self.apply_btn.clicked.connect(self.apply)
        self.undo_btn.clicked.connect(self.undo)
        self.cards.itemDoubleClicked.connect(lambda _: self.apply())
        close.clicked.connect(self.accept)
        if layer is not None:
            self.layer_box.setLayer(layer)
        self._layer_changed(self.layer_box.currentLayer())

    def _layer_changed(self, layer):
        self.field_box.setLayer(layer)
        self.second_box.setLayer(layer)
        self.normalise.blockSignals(True)
        while self.normalise.count() > 2:
            self.normalise.removeItem(2)
        if layer is not None:
            for f in layer.fields():
                if f.isNumeric():
                    self.normalise.addItem(tr("Per {f}").format(f=f.name()), f.name())
        self.normalise.blockSignals(False)
        self.refresh()

    def refresh(self, *_):
        layer, field = self.layer_box.currentLayer(), self.field_box.currentField()
        self.cards.clear()
        self.undo_btn.setEnabled(bool(layer) and thematic.has_backup(layer))
        if layer is None or not field:
            self.kind.setText(tr("Choose a layer and the field to show."))
            self.hist.set_values([])
            return
        try:
            info = thematic.analyse(layer, field)
            recs = thematic.recommend(layer, field, self.second_box.currentField() or None)
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.kind.setText(str(e))
            return
        text = tr("The field holds: <b>{k}</b> · {n} values").format(k=thematic.kind_labels()[info["kind"]],
                                                                   n=format_number(info["n"], 0))
        if "skew" in info:
            method = thematic.method_for(info["skew"])[1]
            text += "<br>" + tr("From {a} to {b}, middle value {m}.").format(
                a=format_number(info["min"]), b=format_number(info["max"]), m=format_number(info["median"])) \
                + "<br><i>" + method + "</i>"
            self.hist.set_values([float(v) for v in thematic._values(layer, field)])
        else:
            self.hist.set_values([])
        self.kind.setText(text)
        for r in recs:
            lines = [f"★ {r['label']}" if r is recs[0] else r["label"], r["reason"]]
            lines += [f"⚠ {w}" for w in r["warnings"]]
            item = QListWidgetItem("\n".join(lines))
            item.setData(Qt.ItemDataRole.UserRole, r["type"])
            if r["warnings"]:
                item.setForeground(QColor(WARN))
            self.cards.addItem(item)
        if recs:
            self.cards.setCurrentRow(0)

    def apply(self):
        layer, field = self.layer_box.currentLayer(), self.field_box.currentField()
        item = self.cards.currentItem()
        if layer is None or not field or item is None:
            return
        map_type = item.data(Qt.ItemDataRole.UserRole)
        options = {"classes": self.classes.value(), "palette": self.palette.currentData(),
                   "second_field": self.second_box.currentField() or None}
        try:
            by = self.normalise.currentData()
            if by and map_type in ("choropleth", "unclassed", "diverging", "value_alpha"):
                field = thematic.normalise(layer, field, by)
            res = thematic.apply(layer, field, map_type, options)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        self.status.setText("✓ " + res["message"])
        if res.get("layout") is not None and getattr(self.plugin, "iface", None) is not None:
            self.plugin.iface.openLayoutDesigner(res["layout"])
        r = layer.renderer()
        breaks = [rg.upperValue() for rg in r.ranges()[:-1]] if hasattr(r, "ranges") else []
        if breaks:
            self.hist.set_values([float(v) for v in thematic._values(layer, field)], breaks)
        self.undo_btn.setEnabled(True)

    def undo(self):
        layer = self.layer_box.currentLayer()
        if layer is not None and thematic.undo(layer, QgsProject.instance()):
            self.status.setText(tr("The layer is back as it was."))
        self.refresh()


def open_dialog(plugin=None, layer=None):
    if layer is None and getattr(plugin, "iface", None) is not None:
        layer = plugin.iface.activeLayer()
    dlg = ThematicDialog(plugin, layer)
    dlg.exec()
