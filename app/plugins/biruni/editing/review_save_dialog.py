"""The Review & Save window: the changes in a list (by layer, each with a tick), the chosen one on a map -
the old shape in red, dashed, the new one in green - and Save for the ticked ones."""
from qgis.core import Qgis, QgsGeometry, QgsProject
from qgis.gui import QgsMapCanvas, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QSplitter,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..i18n import tr
from ..version import APP_NAME
from . import review_save as rs

ROLE = Qt.ItemDataRole.UserRole


class ReviewDialog(QDialog):
    def __init__(self, parent, layers, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle(tr("Review & Save"))
        self.resize(1100, 680)
        self.items = {}      # key -> Change
        self.saved = None    # (saved, left) after Save
        v = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)

        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(380)
        split.addWidget(self.tree)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.canvas = QgsMapCanvas()
        self.canvas.setCanvasColor(QColor("white"))
        main = iface.mapCanvas() if iface is not None else None
        self.canvas.setDestinationCrs(main.mapSettings().destinationCrs() if main else QgsProject.instance().crs())
        self.canvas.setLayers(main.layers() if main else list(layers))
        rv.addWidget(self.canvas, 1)
        legend = QLabel(tr("Red, dashed: as it is saved now. Green: after the change."))
        legend.setObjectName("ReviewLegend")
        rv.addWidget(legend)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        v.addWidget(split, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Changes not ticked:")))
        self.rest = QComboBox()
        self.rest.addItem(tr("keep them as unsaved edits"), "keep")
        self.rest.addItem(tr("discard them"), "drop")
        row.addWidget(self.rest)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox()
        self.save_btn = bb.addButton(tr("Save the ticked changes"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.before_band = self._band(QColor(220, 40, 40), Qt.PenStyle.DashLine)
        self.after_band = self._band(QColor(30, 160, 60), Qt.PenStyle.SolidLine)
        self._fill(layers)
        self.tree.currentItemChanged.connect(self._show)
        self.tree.itemChanged.connect(lambda *_: self._count())
        first = self.tree.topLevelItem(0)
        if first is not None and first.childCount():
            self.tree.setCurrentItem(first.child(0))
        self._count()

    def _band(self, color, style):
        b = QgsRubberBand(self.canvas, Qgis.GeometryType.Polygon)
        b.setStrokeColor(color)
        fill = QColor(color)
        fill.setAlpha(40)
        b.setFillColor(fill)
        b.setWidth(3)
        b.setLineStyle(style)
        return b

    def _fill(self, layers):
        for lyr in layers:
            top = QTreeWidgetItem(self.tree, [lyr.name()])
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            top.setCheckState(0, Qt.CheckState.Checked)
            top.setData(0, ROLE, None)
            whole = rs.schema_changed(lyr)
            for c in rs.changes(lyr):
                it = QTreeWidgetItem(top, [rs.describe(c)])
                it.setToolTip(0, rs.describe(c))
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                if whole:
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    it.setToolTip(0, tr("Fields of this layer changed: its changes are saved all together."))
                it.setCheckState(0, Qt.CheckState.Checked)
                it.setData(0, ROLE, c.key)
                self.items[c.key] = c
            top.setExpanded(True)

    def _count(self):
        keep = self.ticked()
        n = len(self.items)
        layers = self.tree.topLevelItemCount()
        self.summary.setText(tr("{n} unsaved changes in {l} layers. Untick what should not be saved; click a change "
                                "to see it on the map.").format(n=n, l=layers) + "  " +
                             tr("Saving {k} of {n}.").format(k=len(keep), n=n))
        self.save_btn.setEnabled(bool(keep) or self.rest.currentData() == "drop")

    def ticked(self):
        out = set()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                it = top.child(j)
                if it.checkState(0) == Qt.CheckState.Checked:
                    out.add(it.data(0, ROLE))
        return out

    def _show(self, item, _prev=None):
        self.before_band.reset(Qgis.GeometryType.Polygon)
        self.after_band.reset(Qgis.GeometryType.Polygon)
        key = item.data(0, ROLE) if item is not None else None
        c = self.items.get(key)
        if c is None:
            return
        crs = self.canvas.mapSettings().destinationCrs()
        before, after = rs.to_canvas(c.layer, c.before, crs), rs.to_canvas(c.layer, c.after, crs)
        box = None
        for band, g in ((self.before_band, before if c.kind != "add" else None),
                        (self.after_band, after if c.kind != "delete" else None)):
            if g is None:
                continue
            band.reset(g.type())
            band.setToGeometry(g, None)
            if box is None:
                box = g.boundingBox()
            else:
                box.combineExtentWith(g.boundingBox())
        if box is not None:
            if box.isEmpty():
                box = QgsGeometry.fromRect(box).buffer(10, 1).boundingBox()
            box.grow(max(box.width(), box.height()) * 0.4 + 1e-6)
            self.canvas.setExtent(box)
            self.canvas.refresh()

    def save(self):
        keep = self.ticked()
        rest = self.rest.currentData()
        saved = left = 0
        problems = []
        for lyr in {c.layer for c in self.items.values()}:
            mine = {k for k in keep if k[0] == lyr.id()}
            try:
                s, l_ = rs.save(lyr, mine, rest)
                saved, left = saved + s, left + l_
            except rs.SaveRefused as e:
                problems.append(str(e))
            lyr.triggerRepaint()
        self.saved = (saved, left)
        if problems:
            QMessageBox.warning(self, APP_NAME, "\n\n".join(problems))
        elif self.iface is not None:
            text = tr("{s} changes saved.").format(s=saved)
            if left:
                text += " " + tr("{n} changes are still unsaved edits.").format(n=left)
            self.iface.messageBar().pushSuccess(APP_NAME, text)
        self.accept()
