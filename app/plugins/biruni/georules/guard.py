"""The guard: a save that breaks a spatial rule of the layer does not happen.

Every vector layer with rules (in the project, in its GeoPackage, beside its shapefile) is watched: just before
its edits are written, the features added or changed are checked. A broken rule of the "error" level stops the
save (the edits stay, nothing is lost), says which features and why, and offers to show them or to save anyway;
a "warning" says so and lets the save go on.
"""
from qgis.core import Qgis, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtWidgets import QPushButton

from ..i18n import tr
from ..version import APP_NAME
from . import core

SKIP = "biruni/georules_skip_once"


def describe(r):
    p = r.get("params", {})
    k = r["kind"]
    if k == "valid":
        return tr("Shapes are valid (no ring crossing itself)")
    if k == "no_overlap":
        return tr("Features do not overlap (more than {a} m²)").format(a=p.get("min_area", 0.01))
    if k == "min_area":
        return tr("A feature is at least {a} m²").format(a=p["min"])
    if k == "measure_field":
        if p.get("what", "area") == "area":
            return tr("«{f}» is the feature's area (±{r}%)").format(f=p["field"], r=round(float(p.get("rel", 0.01)) * 100, 2))
        return tr("«{f}» is the feature's length (±{r}%)").format(f=p["field"], r=round(float(p.get("rel", 0.01)) * 100, 2))
    if k == "inside":
        return tr("Every feature is inside a feature of «{l}»").format(l=p["layer"])
    if k == "ends_meet":
        return tr("Line ends meet another line")
    return k


def explain(v):
    x = v.values
    if v.key == "invalid":
        return tr("feature {id}: its shape is not valid").format(id=v.fid)
    if v.key == "overlap":
        return tr("feature {id} overlaps feature {o} by {a} m²").format(id=v.fid, o=x["other"], a=x["area"])
    if v.key == "small":
        return tr("feature {id} is {a} m², under {m} m²").format(id=v.fid, a=x["area"], m=x["min"])
    if v.key == "measure":
        return tr("feature {id}: «{f}» is {v} but the shape measures {m}").format(
            id=v.fid, f=x["field"], v=x["value"], m=x["measured"])
    if v.key == "outside":
        return tr("feature {id} is not inside any feature of «{l}»").format(id=v.fid, l=x["layer"])
    if v.key == "dangle":
        return tr("feature {id}: an end meets no other line").format(id=v.fid)
    return f"{v.fid}: {v.key}"


def edited_fids(layer):
    buf = layer.editBuffer()
    if buf is None:
        return []
    fids = set(buf.addedFeatures()) | set(buf.changedGeometries()) | set(buf.changedAttributeValues())
    return sorted(fids - set(buf.deletedFeatureIds()))


class Guard(QObject):
    def __init__(self, iface=None, project=None):
        super().__init__()
        self.iface = iface
        self.project = project or QgsProject.instance()
        self.watched = {}        # layer id -> layer
        self.last = {}           # layer id -> [Violation] of the last save stopped or warned
        self.stopped = {}        # layer id -> True while its edits are set aside, until they are back
        self._item = None

    def install(self):
        self.project.layersAdded.connect(self._added)
        self.project.layersRemoved.connect(self._gone)
        self._added(list(self.project.mapLayers().values()))

    def uninstall(self):
        for sig, fn in ((self.project.layersAdded, self._added), (self.project.layersRemoved, self._gone)):
            try:
                sig.disconnect(fn)
            except TypeError:
                pass
        self.watched.clear()

    def _added(self, layers):
        for lyr in layers:
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.id() not in self.watched:
                try:
                    doc = core.load(lyr)
                except Exception:  # noqa: BLE001 - a file that cannot be read has no rules here
                    doc = None
                if doc and doc.get("rules"):
                    if not lyr.customProperty(core.PROP, ""):
                        import json

                        lyr.setCustomProperty(core.PROP, json.dumps(doc, ensure_ascii=False))
                    self.watch(lyr)

    def watch(self, lyr):
        if lyr.id() in self.watched:
            return
        self.watched[lyr.id()] = lyr
        lyr.beforeCommitChanges.connect(lambda *_a, l=lyr: self.before(l))

    def unwatch(self, lyr):
        self.watched.pop(lyr.id(), None)

    def _gone(self, ids):
        for i in ids:
            self.watched.pop(i, None)

    def before(self, lyr):
        """Never lets an error out: a slot that raises takes the whole program down (PyQt aborts)."""
        try:
            return self._before(lyr)
        except Exception:  # noqa: BLE001
            import traceback

            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(traceback.format_exc(), APP_NAME, Qgis.MessageLevel.Critical)
            return True

    def _before(self, lyr):
        if lyr.id() not in self.watched:
            return True
        if lyr.customProperty(SKIP, False):
            lyr.removeCustomProperty(SKIP)
            return True
        doc = core.load(lyr) or {}
        rules = doc.get("rules") or []
        fids = edited_fids(lyr)
        if not rules or not fids:
            return True
        found = core.Checker(lyr, rules, self.project).check(fids)
        self.last[lyr.id()] = found
        errors = [v for v in found if v.rule.get("level", "error") == "error"]
        buf = lyr.editBuffer()
        schema = buf.addedAttributes() or buf.deletedAttributeIds() or \
            (buf.renamedAttributes() if hasattr(buf, "renamedAttributes") else {})
        if errors and not schema:
            # the edits are set aside (taken back one by one, as Undo does), so the save writes nothing, and put
            # back in the edit session after it
            kept = self._stash(lyr)
            stack = lyr.undoStack()
            while stack.canUndo():
                stack.undo()
            if not lyr.editBuffer().isModified():
                self.stopped[lyr.id()] = True
                QTimer.singleShot(0, lambda: self._restore(lyr, kept))
                self._say(lyr, errors, stopped=True)
                return False
            self._restore(lyr, kept, clear=True)    # edits made without Undo: they cannot be held back
            found = errors
        if found:
            self._say(lyr, found, stopped=False)
        return True

    @staticmethod
    def _stash(lyr):
        buf = lyr.editBuffer()
        added = [QgsFeature(f) for f in buf.addedFeatures().values()]
        geoms = {fid: QgsGeometry(g) for fid, g in buf.changedGeometries().items() if fid >= 0}
        attrs = {fid: dict(a) for fid, a in buf.changedAttributeValues().items() if fid >= 0}
        return added, geoms, attrs, list(buf.deletedFeatureIds())

    def _restore(self, lyr, kept, clear=False):
        """The edits set aside, back in the layer's edit session (after the save that wrote nothing)."""
        added, geoms, attrs, deleted = kept
        try:
            if not lyr.isEditable() and not lyr.startEditing():
                return
        except RuntimeError:
            return
        if clear:     # what Undo could not take back is still in the buffer: only what it took back is put again
            buf = lyr.editBuffer()
            added = [f for f in added if f.id() not in buf.addedFeatures()]
            geoms = {k: v for k, v in geoms.items() if k not in buf.changedGeometries()}
            attrs = {k: v for k, v in attrs.items() if k not in buf.changedAttributeValues()}
            deleted = [d for d in deleted if d not in buf.deletedFeatureIds()]
        lyr.beginEditCommand(tr("Edits kept back by the spatial rules"))
        for fid, g in geoms.items():
            lyr.changeGeometry(fid, g)
        for fid, a in attrs.items():
            lyr.changeAttributeValues(fid, a)
        if added:
            lyr.addFeatures(added)
        if deleted:
            lyr.deleteFeatures(deleted)
        lyr.endEditCommand()
        self.stopped.pop(lyr.id(), None)
        doc = core.load(lyr) or {}
        self.last[lyr.id()] = core.Checker(lyr, doc.get("rules") or [], self.project).check(edited_fids(lyr))
        lyr.triggerRepaint()

    def _say(self, lyr, found, stopped):
        if self.iface is None:
            return
        lines = [explain(v) for v in found[:4]] + (["…"] if len(found) > 4 else [])
        head = (tr("«{l}» was not saved: {n} of its edits break its rules.") if stopped else
                tr("«{l}» was saved, with {n} warnings of its rules.")).format(l=lyr.name(), n=len(found))
        bar = self.iface.messageBar()
        item = bar.createMessage(APP_NAME, head + " " + "; ".join(lines))
        show = QPushButton(tr("Show them"))
        show.clicked.connect(lambda: self.show(lyr, found))
        item.layout().addWidget(show)
        if stopped:
            anyway = QPushButton(tr("Save anyway"))
            anyway.clicked.connect(lambda: self.save_anyway(lyr))
            item.layout().addWidget(anyway)
        bar.pushWidget(item, Qgis.MessageLevel.Critical if stopped else Qgis.MessageLevel.Warning, 0)
        self._item = item

    def show(self, lyr, found):
        lyr.selectByIds(sorted({v.fid for v in found}))
        if self.iface is not None:
            self.iface.setActiveLayer(lyr)
            self.iface.mapCanvas().zoomToSelected(lyr)

    def save_anyway(self, lyr):
        lyr.setCustomProperty(SKIP, True)
        ok = lyr.commitChanges(False)
        if self.iface is not None:
            self.iface.messageBar().clearWidgets()
            if ok:
                self.iface.messageBar().pushMessage(APP_NAME, tr("«{l}» saved, its rules broken on purpose.").format(
                    l=lyr.name()), Qgis.MessageLevel.Warning, 8)
        return ok


_instance = None


def instance(iface=None):
    """The guard (made and installed the first time it is asked for)."""
    global _instance
    if _instance is None:
        _instance = Guard(iface)
        _instance.install()
    elif iface is not None and _instance.iface is None:
        _instance.iface = iface
    return _instance


def install(plugin):
    instance(plugin.iface)


def uninstall():
    global _instance
    if _instance is not None:
        _instance.uninstall()
        _instance = None
