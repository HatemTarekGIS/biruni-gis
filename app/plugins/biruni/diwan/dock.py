"""The Diwan panel: turn a GeoPackage into a Diwan, see the history of the selected feature (and put an old version
back as an ordinary, undoable edit), the layer as it was at any moment, and the round trip through other formats
(export with the identities, bring the edited copy back)."""
import datetime as dt
import json
import os

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QDateTime, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QDateTimeEdit, QDockWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                                 QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from .. import background
from ..i18n import tr
from ..version import APP_NAME
from . import branch, live
from .store import Diwan, DiwanError, wkb_of

_DOCK = {}
ROLE = Qt.ItemDataRole.UserRole
FORMATS = [("Shapefile (*.shp)", ".shp"), ("GeoJSON (*.geojson)", ".geojson"), ("GeoPackage (*.gpkg)", ".gpkg"),
           ("KML (*.kml)", ".kml"), ("DXF (*.dxf)", ".dxf"), ("CSV (*.csv)", ".csv"),
           ("File geodatabase (*.gdb)", ".gdb"), ("FlatGeobuf (*.fgb)", ".fgb")]


def how_words(how):
    if how == "initial":
        return tr("first version")
    if how == "biruni":
        return tr("saved in Biruni")
    if how == "external":
        return tr("changed in another program")
    if how == "restore":
        return tr("an older version put back")
    if how.startswith("import:"):
        return tr("brought back from {f}").format(f=how.split(":", 1)[1])
    return how


def op_words(op):
    return {"add": tr("added"), "change": tr("changed"), "delete": tr("deleted")}.get(op, op)


def local_time(at):
    try:
        t = dt.datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone()
        return t.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return at


class DiwanDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Diwan"))
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniDiwan")
        w = QWidget()
        v = QVBoxLayout(w)
        self.state = QLabel()
        self.state.setWordWrap(True)
        v.addWidget(self.state)
        self.enable_btn = QPushButton(tr("Turn on the Diwan for this file"))
        self.enable_btn.setToolTip(tr("Every feature gets a permanent identity and its history is kept inside the "
                                      "file. The file stays a GeoPackage that every program opens and edits."))
        self.enable_btn.clicked.connect(self.enable)
        v.addWidget(self.enable_btn)

        v.addWidget(QLabel("<b>" + tr("History of the selected feature") + "</b>"))
        self.versions = QListWidget()
        self.versions.currentItemChanged.connect(self._show_version)
        v.addWidget(self.versions, 1)
        self.restore_btn = QPushButton(tr("Put this version back"))
        self.restore_btn.setToolTip(tr("As an ordinary edit of the layer: undo it with Ctrl+Z, save it to keep it."))
        self.restore_btn.clicked.connect(self.restore)
        v.addWidget(self.restore_btn)

        v.addWidget(QLabel("<b>" + tr("The layer as it was") + "</b>"))
        row = QHBoxLayout()
        self.when = QDateTimeEdit(QDateTime.currentDateTime())
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("yyyy-MM-dd HH:mm")
        row.addWidget(self.when, 1)
        past = QPushButton(tr("Show"))
        past.clicked.connect(self.show_past)
        row.addWidget(past)
        v.addLayout(row)

        v.addWidget(QLabel("<b>" + tr("Branches") + "</b>"))
        self.branch_state = QLabel()
        self.branch_state.setWordWrap(True)
        v.addWidget(self.branch_state)
        row = QHBoxLayout()
        self.branch_btn = QPushButton(tr("Make a branch…"))
        self.branch_btn.setToolTip(tr("A copy for a field laptop or a colleague. Both sides go on working; one sync "
                                      "brings each side the other's work."))
        self.branch_btn.clicked.connect(self.make_branch)
        self.sync_btn = QPushButton(tr("Sync…"))
        self.sync_btn.clicked.connect(self.sync)
        row.addWidget(self.branch_btn)
        row.addWidget(self.sync_btn)
        v.addLayout(row)
        self.conflict_list = QListWidget()
        self.conflict_list.setMaximumHeight(110)
        v.addWidget(self.conflict_list)
        row = QHBoxLayout()
        self.take_other = QPushButton(tr("Use the branch's value"))
        self.take_other.clicked.connect(lambda: self.resolve(True))
        self.keep_mine = QPushButton(tr("Keep this file's value"))
        self.keep_mine.clicked.connect(lambda: self.resolve(False))
        row.addWidget(self.take_other)
        row.addWidget(self.keep_mine)
        v.addLayout(row)

        v.addWidget(QLabel("<b>" + tr("Other formats") + "</b>"))
        row = QHBoxLayout()
        exp = QPushButton(tr("Export with identities…"))
        exp.setToolTip(tr("Any format; each feature carries its identity, so the edited copy can come back."))
        exp.clicked.connect(self.export)
        back = QPushButton(tr("Bring back an edited copy…"))
        back.clicked.connect(self.bring_back)
        row.addWidget(exp)
        row.addWidget(back)
        v.addLayout(row)
        self.setWidget(w)

        self.band = QgsRubberBand(self.iface.mapCanvas()) if self.iface is not None else None
        if self.band is not None:
            self.band.setStrokeColor(QColor(200, 120, 0))
            self.band.setFillColor(QColor(200, 120, 0, 50))
            self.band.setWidth(3)
        self._layer = None
        if self.iface is not None:
            self.iface.currentLayerChanged.connect(self.refresh)
        self.refresh()

    # -- what is selected --
    def layer(self):
        lyr = self.iface.activeLayer() if self.iface is not None else None
        return lyr if isinstance(lyr, QgsVectorLayer) else None

    def refresh(self, *_):
        lyr = self.layer()
        if self._layer is not None and self._layer is not lyr:
            try:
                self._layer.selectionChanged.disconnect(self._fill_versions)
            except (TypeError, RuntimeError):
                pass
        if lyr is not None and lyr is not self._layer:
            lyr.selectionChanged.connect(self._fill_versions)
            lyr.willBeDeleted.connect(self._forget)   # never touch a deleted layer (closing the project)
        self._layer = lyr
        path, table = live.source_of(lyr) if lyr is not None else (None, None)
        found = live.diwan_of(lyr) if lyr is not None else None
        if lyr is None:
            self.state.setText(tr("Choose a layer in the Layers panel."))
        elif path is None:
            self.state.setText(tr("The Diwan keeps GeoPackage layers. Save “{l}” to a GeoPackage first.").format(
                l=lyr.name()))
        elif found is None:
            self.state.setText(tr("“{f}” is an ordinary GeoPackage.").format(f=os.path.basename(path)))
        else:
            with Diwan(path) as d:
                n = len(d.ids(found[1]))
                last = d.changes(found[1], limit=1)
            text = tr("“{l}” is kept in a Diwan: {n} features with their history.").format(l=lyr.name(), n=n)
            if last:
                text += " " + tr("Last change: {t}, {how}.").format(t=local_time(last[0]["at"]),
                                                                    how=how_words(last[0]["how"]))
            self.state.setText(text)
            live.watch(lyr)
        self.enable_btn.setVisible(path is not None and found is None)
        self._fill_branches(found)
        self._fill_versions()

    def _forget(self):
        if self._layer is not None:
            try:
                self._layer.selectionChanged.disconnect(self._fill_versions)
            except (TypeError, RuntimeError):
                pass
        self._layer = None

    def _fill_versions(self, *_):
        self.versions.clear()
        self.restore_btn.setEnabled(False)
        lyr = self.layer()
        found = live.diwan_of(lyr) if lyr is not None else None
        if found is None or lyr.selectedFeatureCount() != 1:
            if found is not None:
                self.versions.addItem(tr("Select one feature to see its history."))
            return
        fid = lyr.selectedFeatureIds()[0]
        with Diwan(found[0]) as d:
            uid = d.uid_of(found[1], fid)
            hist = d.history(found[1], uid) if uid else []
        if not hist:
            self.versions.addItem(tr("This feature is not saved yet."))
            return
        for h in reversed(hist):
            text = f"{local_time(h['at'])} · {op_words(h['op'])} · {how_words(h['how'])}"
            if h["who"]:
                text += f" · {h['who']}"
            it = QListWidgetItem(text)
            it.setData(ROLE, h)
            self.versions.addItem(it)

    def _show_version(self, item, _prev=None):
        h = item.data(ROLE) if item is not None else None
        self.restore_btn.setEnabled(bool(h) and h["op"] != "delete" and item is not self.versions.item(0))
        if self.band is None:
            return
        self.band.reset()
        if not h or not h.get("geom"):
            return
        g = QgsGeometry()
        g.fromWkb(wkb_of(h["geom"]))
        self.band.setToGeometry(g, self.layer())

    # -- actions --
    def enable(self):
        lyr = self.layer()
        path, _table = live.source_of(lyr)
        if not path:
            return
        r = QMessageBox.question(self, tr("Diwan"), tr("Turn on the Diwan for “{f}”? Every layer of the file gets "
                                                       "its features' identities and keeps their history from "
                                                       "now on.").format(f=os.path.basename(path)))
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            with Diwan(path) as d:
                n = d.enable(who=live.who())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.iface.messageBar().pushSuccess(APP_NAME, tr("The Diwan is on: {n} features registered.").format(n=n))
        self.refresh()

    def restore(self):
        """The chosen version becomes an edit of the layer (undoable, not saved yet)."""
        item = self.versions.currentItem()
        h = item.data(ROLE) if item is not None else None
        lyr = self.layer()
        if not h or lyr is None or lyr.selectedFeatureCount() != 1:
            return
        fid = lyr.selectedFeatureIds()[0]
        if not lyr.isEditable() and not lyr.startEditing():
            QMessageBox.warning(self, APP_NAME, tr("The layer cannot be edited."))
            return
        g = QgsGeometry()
        g.fromWkb(wkb_of(h["geom"]))
        lyr.beginEditCommand(tr("Put back the version of {t}").format(t=local_time(h["at"])))
        lyr.changeGeometry(fid, g)
        fields = lyr.fields()
        for name, value in (h["attrs"] or {}).items():
            i = fields.indexOf(name)
            if i >= 0 and lyr.getFeature(fid).attribute(i) != value:
                lyr.changeAttributeValue(fid, i, value)
        lyr.endEditCommand()
        lyr.triggerRepaint()
        self.iface.messageBar().pushInfo(APP_NAME, tr("The old version is back as an edit: save the layer to keep "
                                                      "it, or undo it."))

    def show_past(self):
        lyr = self.layer()
        found = live.diwan_of(lyr) if lyr is not None else None
        if found is None:
            return
        when = self.when.dateTime().toUTC().toString("yyyy-MM-ddTHH:mm:ss.zzzZ")
        with Diwan(found[0]) as d:
            past = d.state_at(found[1], when)
        label = self.when.dateTime().toString("yyyy-MM-dd HH:mm")
        mem = QgsVectorLayer(f"{QgsWkbTypes.displayString(lyr.wkbType())}?crs={lyr.crs().authid()}",
                             tr("{l} as it was on {t}").format(l=lyr.name(), t=label), "memory")
        mem.dataProvider().addAttributes(list(lyr.fields()))
        mem.updateFields()
        feats = []
        for state in past.values():
            f = QgsFeature(mem.fields())
            g = QgsGeometry()
            g.fromWkb(wkb_of(state["geom"]))
            f.setGeometry(g)
            for name, value in state["attrs"].items():
                i = mem.fields().indexOf(name)
                if i >= 0:
                    f.setAttribute(i, value)
            feats.append(f)
        mem.dataProvider().addFeatures(feats)
        mem.setReadOnly(True)
        QgsProject.instance().addMapLayer(mem)
        self.iface.messageBar().pushInfo(APP_NAME, tr("{n} features as they were on {t} (a read-only layer).").format(
            n=len(feats), t=label))

    def export(self):
        lyr = self.layer()
        found = live.diwan_of(lyr) if lyr is not None else None
        if found is None:
            return
        path, _f = QFileDialog.getSaveFileName(self, tr("Export with identities"), lyr.name(),
                                               ";;".join(tr(f) for f, _e in FORMATS))
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".shp"
        try:
            with Diwan(found[0]) as d:
                n = d.export(found[1], path)
        except (DiwanError, RuntimeError) as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.iface.messageBar().pushSuccess(APP_NAME, tr("{n} features exported with their identities to {f}.").format(
            n=n, f=os.path.basename(path)))

    def bring_back(self):
        lyr = self.layer()
        found = live.diwan_of(lyr) if lyr is not None else None
        if found is None:
            return
        if lyr.isEditable() and lyr.isModified():
            QMessageBox.information(self, APP_NAME, tr("Save or discard the edits of “{l}” first.").format(l=lyr.name()))
            return
        path, _f = QFileDialog.getOpenFileName(self, tr("Bring back an edited copy"), "",
                                               ";;".join(tr(f) for f, _e in FORMATS) + ";;" + tr("All files (*)"))
        if not path:
            return
        name = live.who()

        def work():
            with Diwan(found[0]) as d:
                return d.bring_back(found[1], path, who=name)

        def done(counts, error):
            if error is not None:
                QMessageBox.warning(self, APP_NAME, str(error))
                return
            lyr.reload()
            lyr.triggerRepaint()
            self.iface.messageBar().pushSuccess(APP_NAME, tr(
                "The copy is back: {c} changed, {a} added, {d} deleted, {s} unchanged - each recorded in the "
                "history.").format(c=counts["change"], a=counts["add"], d=counts["delete"], s=counts["same"]))
            self.refresh()

        background.run(work, done)

    # -- branches --
    def _fill_branches(self, found):
        self.conflict_list.clear()
        on = found is not None
        for w in (self.branch_btn, self.sync_btn):
            w.setEnabled(on)
        if not on:
            self.branch_state.setText("")
            for w in (self.conflict_list, self.take_other, self.keep_mine):
                w.setVisible(False)
            return
        with Diwan(found[0]) as d:
            role, name, main_path = d.meta("role"), d.meta("branch_name"), d.meta("main_path")
        if role == "branch":
            self.branch_state.setText(tr("This file is the branch “{b}” of {m}.").format(
                b=name, m=os.path.basename(main_path or "?")))
            self.branch_btn.setEnabled(False)
            conf = []
        else:
            names = [b["name"] for b in branch.branches(found[0])]
            self.branch_state.setText(tr("Branches: {b}.").format(b=", ".join(names)) if names else
                                      tr("No branches yet."))
            conf = branch.conflicts(found[0])
        for c in conf:
            if c["what"] == "deleted":
                text = tr("{b}: deleted there, changed here - kept").format(b=c["branch"])
            elif c["what"] == "geometry":
                text = tr("{b}: the shape changed on both sides - this file's shape kept").format(b=c["branch"])
            else:
                text = tr("{b}: {f} is {k} here, {o} there").format(b=c["branch"], f=c["field"],
                                                                     k=json.loads(c["kept"]), o=json.loads(c["other"]))
            it = QListWidgetItem(text)
            it.setData(ROLE, c)
            self.conflict_list.addItem(it)
        for w in (self.conflict_list, self.take_other, self.keep_mine):
            w.setVisible(bool(conf))

    def make_branch(self):
        lyr = self.layer()
        found = live.diwan_of(lyr) if lyr is not None else None
        if found is None:
            return
        name, ok = QInputDialog.getText(self, tr("Make a branch"), tr("Name of the branch (who takes it, or where):"))
        if not ok or not name.strip():
            return
        base = os.path.splitext(found[0])[0]
        path, _f = QFileDialog.getSaveFileName(self, tr("Make a branch"), f"{base} - {name.strip()}.gpkg",
                                               tr("GeoPackage (*.gpkg)"))
        if not path:
            return
        try:
            branch.make_branch(found[0], path, name.strip())
        except (DiwanError, OSError) as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.iface.messageBar().pushSuccess(APP_NAME, tr("The branch “{b}” is ready: {f}. Give it to whoever works "
                                                         "on it; sync it when it comes back.").format(
            b=name.strip(), f=os.path.basename(path)))
        self.refresh()

    def sync(self):
        lyr = self.layer()
        found = live.diwan_of(lyr) if lyr is not None else None
        if found is None:
            return
        if lyr.isEditable() and lyr.isModified():
            QMessageBox.information(self, APP_NAME, tr("Save or discard the edits of “{l}” first.").format(l=lyr.name()))
            return
        with Diwan(found[0]) as d:
            is_branch, main_path = d.meta("role") == "branch", d.meta("main_path")
        if is_branch:
            other = main_path if main_path and os.path.exists(main_path) else QFileDialog.getOpenFileName(
                self, tr("The main file of this branch"), "", tr("GeoPackage (*.gpkg)"))[0]
            main, br = other, found[0]
        else:
            other = QFileDialog.getOpenFileName(self, tr("Choose the branch to sync"), os.path.dirname(found[0]),
                                                tr("GeoPackage (*.gpkg)"))[0]
            main, br = found[0], other
        if not other:
            return
        name = live.who()

        def done(r, error):
            if error is not None:
                QMessageBox.warning(self, APP_NAME, str(error))
                return
            for layer in QgsProject.instance().mapLayers().values():
                if live.source_of(layer)[0] in (main, br):
                    layer.reload()
                    layer.triggerRepaint()
            text = tr("Synced: {m} changes came to the main file, {b} went to the branch, {j} features changed on "
                      "both sides were joined.").format(m=r["to_main"], b=r["to_branch"], j=r["joined"])
            if r["conflicts"]:
                text += " " + tr("{n} conflicts kept the main file's value: see the list in the Diwan panel.").format(
                    n=len(r["conflicts"]))
            self.iface.messageBar().pushSuccess(APP_NAME, text)
            self.refresh()

        background.run(lambda: branch.sync(main, br, who=name), done)

    def resolve(self, use_other):
        item = self.conflict_list.currentItem()
        found = live.diwan_of(self.layer())
        if item is None or found is None:
            return
        try:
            branch.resolve(found[0], item.data(ROLE)["id"], use_other, who=live.who())
        except DiwanError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        lyr = self.layer()
        lyr.reload()
        lyr.triggerRepaint()
        self.refresh()

    def shutdown(self):
        if self.band is not None:
            self.band.reset()
        if self.iface is not None:
            try:
                self.iface.currentLayerChanged.disconnect(self.refresh)
            except TypeError:
                pass


def open_dock(plugin):
    d = _DOCK.get(id(plugin))
    if d is None:
        d = DiwanDock(plugin)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        _DOCK[id(plugin)] = d
    d.show()
    d.raise_()
    d.refresh()
    return d


def close_dock(plugin):
    d = _DOCK.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()
