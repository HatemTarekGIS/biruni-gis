"""The window that finds the same features in two layers (no shared id needed) and says which field of one is which
field of the other, then copies the ticked values across."""
from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateTransform, QgsFeature, QgsGeometry,
                       QgsLineSymbol, QgsPointXY, QgsProject, QgsRendererCategory, QgsVectorLayer)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QPushButton,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import fields, match

COLORS = {"pair": "42,157,143,255", "split": "233,196,106,255", "merge": "244,162,97,255"}


def _vectors():
    return [l for l in QgsProject.instance().mapLayers().values()
            if isinstance(l, QgsVectorLayer) and l.isValid() and l.isSpatial()]


def _factor_text(f):
    if f is None:
        return ""
    return "× " + (f"{f:g}" if f >= 1 else f"1/{1 / f:g}")


class MatchDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.result = None
        self.proposals = []
        self.setWindowTitle(tr("Match Two Layers"))
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)
        intro = QLabel(tr("The same features in two sources that share no id (another agency, another coordinate "
                          "system, a shift of a metre), found by their shapes; then which field of one is which field "
                          "of the other, found by their values, even in other units."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.a, self.b = QComboBox(), QComboBox()
        for lyr in _vectors():
            self.a.addItem(lyr.name(), lyr.id())
            self.b.addItem(lyr.name(), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None and self.a.findData(act.id()) >= 0:
            self.a.setCurrentIndex(self.a.findData(act.id()))
        if self.b.count() > 1 and self.b.currentIndex() == self.a.currentIndex():
            self.b.setCurrentIndex(1 if self.a.currentIndex() == 0 else 0)
        form.addRow(tr("This layer (A)"), self.a)
        form.addRow(tr("The other source (B)"), self.b)
        v.addLayout(form)
        self.run_btn = QPushButton(tr("Find the Same Features"))
        self.run_btn.clicked.connect(lambda: self.run())
        v.addWidget(self.run_btn)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        v.addWidget(QLabel(tr("Fields")))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Field of A"), tr("Field of B"), tr("Sure"), tr("Conversion")])
        self.tree.setRootIsDecorated(False)
        self.tree.setMinimumHeight(160)
        v.addWidget(self.tree)
        self.new = QCheckBox(tr("Also add B's other ticked fields to A as new fields"))
        v.addWidget(self.new)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy_btn = QPushButton(tr("Copy the Ticked Values from B into A"))
        self.copy_btn.setEnabled(False)
        bb.addButton(self.copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        self.copy_btn.clicked.connect(lambda: self.copy())
        v.addWidget(bb)

    def layers(self):
        p = QgsProject.instance()
        return p.mapLayer(self.a.currentData() or ""), p.mapLayer(self.b.currentData() or "")

    def run(self):
        a, b = self.layers()
        if a is None or b is None or a is b:
            self.summary.setText(tr("Choose two different layers."))
            return None
        try:
            r = match.match(a, b)
        except match.MatchError as e:
            self.summary.setText(tr("The two layers must be of the same kind (polygons, lines or points).")
                                 if str(e) == "kinds" else str(e))
            return None
        self.result = r
        c = r.counts()
        text = tr("{p} features found in both, {s} drawn as several in B, {m} drawn as one in B; {a} only in A, "
                  "{b} only in B (selected).").format(p=c["pairs"], s=c["splits"], m=c["merges"], a=c["only_a"],
                                                      b=c["only_b"])
        if abs(r.shift[0]) + abs(r.shift[1]) >= 0.01:
            text += " " + tr("B is offset as a whole by {x} m, {y} m: taken out before matching.").format(
                x=round(r.shift[0], 2), y=round(r.shift[1], 2))
        self.summary.setText(text)
        self._links(a, b, r)
        a.selectByIds(r.only_a)
        b.selectByIds(r.only_b)
        self.proposals = fields.crosswalk(a, b, r.pairs)
        self._fill(a, b)
        self.copy_btn.setEnabled(bool(r.pairs))
        return r

    def _fill(self, a, b):
        self.tree.clear()
        taken = set()
        for p in self.proposals:
            it = QTreeWidgetItem([p.a, p.b, f"{round(p.confidence * 100)}%", _factor_text(p.factor)])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if p.how == "values" and p.confidence >= 0.8
                             else Qt.CheckState.Unchecked)
            it.setData(0, Qt.ItemDataRole.UserRole, ("map", p))
            self.tree.addTopLevelItem(it)
            taken.add(p.b)
        for n in fields.field_list(b):
            if n in taken:
                continue
            it = QTreeWidgetItem([tr("(new field)"), n, "", ""])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Unchecked)
            it.setData(0, Qt.ItemDataRole.UserRole, ("new", n))
            self.tree.addTopLevelItem(it)
        for i in range(4):
            self.tree.resizeColumnToContents(i)

    def ticked(self):
        maps, new = [], []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.checkState(0) != Qt.CheckState.Checked:
                continue
            kind, x = it.data(0, Qt.ItemDataRole.UserRole)
            (maps if kind == "map" else new).append(x)
        return maps, new

    def _links(self, a, b, r):
        lyr = QgsVectorLayer(f"LineString?crs={a.crs().authid() or a.crs().toWkt()}&field=kind:string&field=score:double",
                             tr("Matches: {a} – {b}").format(a=a.name(), b=b.name()), "memory")
        ca = {f.id(): f.geometry() for f in a.getFeatures()}
        xb = QgsCoordinateTransform(b.crs(), a.crs(), QgsProject.instance())
        cb = {}
        for f in b.getFeatures():
            g = QgsGeometry(f.geometry())
            if g.isNull():
                continue
            g.transform(xb)
            cb[f.id()] = g

        def pt(g):
            c = g.pointOnSurface()
            return QgsPointXY(c.asPoint())

        feats = []

        def add(fa, fb, kind, s):
            if fa in ca and fb in cb:
                f = QgsFeature(lyr.fields())
                f.setAttributes([kind, s])
                f.setGeometry(QgsGeometry.fromPolylineXY([pt(ca[fa]), pt(cb[fb])]))
                feats.append(f)

        for fa, fb, s in r.pairs:
            add(fa, fb, "pair", s)
        for fa, fbs, s in r.splits:
            for fb in fbs:
                add(fa, fb, "split", s)
        for fas, fb, s in r.merges:
            for fa in fas:
                add(fa, fb, "merge", s)
        lyr.dataProvider().addFeatures(feats)
        labels = {"pair": tr("The same feature"), "split": tr("Drawn as several in B"), "merge": tr("Drawn as one in B")}
        cats = [QgsRendererCategory(k, QgsLineSymbol.createSimple({"color": c, "width": "0.5"}), labels[k])
                for k, c in COLORS.items()]
        lyr.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
        QgsProject.instance().addMapLayer(lyr)
        self.links = lyr
        return lyr

    def copy(self):
        a, b = self.layers()
        if self.result is None or a is None or b is None:
            return 0
        maps, new = self.ticked()
        if not self.new.isChecked():
            new = []
        if not maps and not new:
            self.summary.setText(tr("Tick the fields to copy first."))
            return 0
        try:
            n = fields.copy_values(a, b, self.result.pairs, maps, new, label=tr("Values copied from «{l}»").format(l=b.name()))
        except ValueError:
            self.summary.setText(tr("The layer cannot be edited."))
            return 0
        text = tr("{n} values copied into «{l}», in its edits: undo or save them as any edit.").format(n=n, l=a.name())
        self.summary.setText(text)
        self.plugin.iface.messageBar().pushMessage(APP_NAME, text, Qgis.MessageLevel.Success, 8)
        return n


def open_ui(plugin):
    if len(_vectors()) < 2:
        plugin.iface.messageBar().pushMessage(APP_NAME, tr("Add the two layers to the map first."),
                                              Qgis.MessageLevel.Warning, 8)
        return None
    dlg = MatchDialog(plugin)
    dlg.show()
    plugin._match_dialog = dlg
    return dlg
