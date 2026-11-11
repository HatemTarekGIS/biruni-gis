"""The spatial rules window: learn them from the data, tick the ones to keep, check the layer, keep them in the
data so every save keeps them."""
from qgis.core import Qgis, QgsFeature, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import core, guard

LEVELS = ("error", "warning")


def _level_text(lv):
    return tr("Stops the save") if lv == "error" else tr("Warns only")


class SpatialRulesDialog(QDialog):
    def __init__(self, plugin, layer=None):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(tr("Spatial Rules"))
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)
        intro = QLabel(tr("The rules the shapes of a layer keep, learned from the data: a save that breaks one does "
                          "not happen. They are kept in the data itself (inside the GeoPackage, beside the "
                          "shapefile), so any copy of the data keeps them wherever it goes."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Layer")))
        self.layer = QComboBox()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial():
                self.layer.addItem(lyr.name(), lyr.id())
        layer = layer or plugin.iface.activeLayer()
        if layer is not None and self.layer.findData(layer.id()) >= 0:
            self.layer.setCurrentIndex(self.layer.findData(layer.id()))
        row.addWidget(self.layer, 1)
        v.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Rule"), tr("Holds now"), tr("Breaking it now"), tr("When broken")])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 300)
        v.addWidget(self.tree, 1)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.learn_btn = QPushButton(tr("Learn from the Data"))
        self.check_btn = QPushButton(tr("Check the Layer Now"))
        self.save_btn = QPushButton(tr("Keep the Ticked Rules"))
        for b in (self.learn_btn, self.check_btn, self.save_btn):
            bb.addButton(b, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.learn_btn.clicked.connect(self.learn)
        self.check_btn.clicked.connect(self.check_now)
        self.save_btn.clicked.connect(self.keep)
        self.layer.currentIndexChanged.connect(self.load)
        self.load()

    def current(self):
        return QgsProject.instance().mapLayer(self.layer.currentData() or "")

    # ---- the list ---------------------------------------------------------------------------------------
    def _add(self, r, holds="", breaking="", checked=True):
        it = QTreeWidgetItem([guard.describe(r), holds, breaking, ""])
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        it.setData(0, Qt.ItemDataRole.UserRole, r)
        self.tree.addTopLevelItem(it)
        lv = QComboBox()
        for x in LEVELS:
            lv.addItem(_level_text(x), x)
        lv.setCurrentIndex(LEVELS.index(r.get("level", "error")))
        self.tree.setItemWidget(it, 3, lv)
        return it

    def items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def ids(self):
        return {it.data(0, Qt.ItemDataRole.UserRole)["id"] for it in self.items()}

    def ticked(self):
        out = []
        for it in self.items():
            if it.checkState(0) != Qt.CheckState.Checked:
                continue
            r = dict(it.data(0, Qt.ItemDataRole.UserRole))
            r["level"] = self.tree.itemWidget(it, 3).currentData()
            out.append(r)
        return out

    def load(self):
        self.tree.clear()
        lyr = self.current()
        if lyr is None:
            return
        doc = core.load(lyr) or {}
        for r in doc.get("rules") or []:
            self._add(r)
        self.out.setText(tr("{n} rules kept for this layer.").format(n=len(doc.get("rules") or [])) if doc.get("rules")
                         else tr("No rules yet: learn them from the data."))

    def learn(self):
        lyr = self.current()
        if lyr is None:
            return []
        props = core.learn(lyr)
        have = self.ids()
        new = [p for p in props if p.rule["id"] not in have]
        for p in new:
            self._add(p.rule, f"{p.holds * 100:.1f}%", str(len(p.exceptions)) if p.exceptions else "—")
        self.out.setText(tr("{n} rules the data keeps found, from {f} features. Untick what is not a rule, then keep "
                            "them.").format(n=len(new), f=props[0].n if props else lyr.featureCount())
                         if new else tr("No other rule found that the data keeps almost everywhere."))
        return new

    # ---- checking and keeping ---------------------------------------------------------------------------------
    def check_now(self):
        lyr = self.current()
        if lyr is None:
            return None
        ch = core.Checker(lyr, self.ticked())
        found = ch.check()
        if not found:
            self.out.setText(tr("The whole layer keeps the ticked rules."))
            return []
        crs = lyr.crs().authid() or lyr.crs().toWkt()
        groups = {}
        for v in found:
            if v.geom is None or v.geom.isNull():
                continue
            groups.setdefault(QgsWkbTypes.geometryType(v.geom.wkbType()), []).append(v)
        for gt, vs in groups.items():
            kind = {QgsWkbTypes.PointGeometry: "MultiPoint", QgsWkbTypes.LineGeometry: "MultiLineString"}.get(
                gt, "MultiPolygon")
            out = QgsVectorLayer(f"{kind}?crs={crs}&field=fid:integer&field=rule:string&field=why:string",
                                 tr("Breaks the rules: {l}").format(l=lyr.name()), "memory")
            feats = []
            for v in vs:
                f = QgsFeature(out.fields())
                g = v.geom
                g.convertToMultiType()
                f.setGeometry(g)
                f.setAttributes([int(v.fid), guard.describe(v.rule), guard.explain(v)])
                feats.append(f)
            out.dataProvider().addFeatures(feats)
            QgsProject.instance().addMapLayer(out)
        self.out.setText(tr("{n} breaks of the ticked rules in {f} features, shown on the map.").format(
            n=len(found), f=len({v.fid for v in found})))
        return found

    def keep(self):
        lyr = self.current()
        if lyr is None:
            return None
        rules = self.ticked()
        where = core.save(lyr, rules)
        g = guard.instance(self.plugin.iface)
        if rules:
            g.watch(lyr)
        else:
            g.unwatch(lyr)
        text = {"gpkg": tr("{n} rules kept inside the GeoPackage: they go with the file."),
                "file": tr("{n} rules kept in a file beside the layer's: copy them together."),
                "project": tr("{n} rules kept in the project (this layer is not a file).")}[where].format(n=len(rules))
        if not rules:
            text = tr("The layer has no rules now.")
        self.out.setText(text)
        self.plugin.iface.messageBar().pushMessage(APP_NAME, text, Qgis.MessageLevel.Success, 8)
        self.load()
        return where


def open_ui(plugin):
    dlg = SpatialRulesDialog(plugin)
    dlg.show()
    plugin._georules_dialog = dlg
    return dlg
