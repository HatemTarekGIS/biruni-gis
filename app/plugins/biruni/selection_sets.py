"""Saved selections and selection history.

* Save the selection of a layer under a name (kept in the project with the
  layer) and bring it back later: replace, add to, remove from or keep only
  what is common with the current selection. A set can also be an expression,
  evaluated again when it is restored.
* Every layer remembers its last 20 selections: Select previous / Select next.
"""
import json
import time

from qgis.core import QgsFeatureRequest, QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox

from .i18n import tr
from .version import APP_NAME

SETS_PROP = "biruni/selection_sets"
HISTORY_SIZE = 20
OPS = ("replace", "add", "remove", "intersect")

_history = {}        # layer id -> {"stack": [frozenset], "pos": int}
_connected = {}      # layer id -> slot
_busy = [False]      # True while this module changes a selection itself


# ---------------------------------------------------------------- saved sets
def sets(layer):
    try:
        items = json.loads(layer.customProperty(SETS_PROP, "[]") or "[]")
    except (TypeError, ValueError):
        items = []
    return [s for s in items if isinstance(s, dict) and s.get("name")]


def _store(layer, items):
    layer.setCustomProperty(SETS_PROP, json.dumps(items, ensure_ascii=False))
    QgsProject.instance().setDirty(True)


def save_set(layer, name, expression=None):
    """Saves the current selection (or an expression) as `name`, replacing a set of that name."""
    name = name.strip()
    if not name:
        raise ValueError(tr("Give the selection a name."))
    entry = {"name": name, "created": time.strftime("%Y-%m-%d %H:%M")}
    if expression:
        entry["expression"] = expression
    else:
        entry["fids"] = sorted(int(f) for f in layer.selectedFeatureIds())
    items = [s for s in sets(layer) if s["name"] != name] + [entry]
    _store(layer, items)
    return entry


def delete_set(layer, name):
    items = sets(layer)
    keep = [s for s in items if s["name"] != name]
    _store(layer, keep)
    return len(items) - len(keep)


def set_ids(layer, entry):
    if entry.get("expression"):
        req = QgsFeatureRequest().setFilterExpression(entry["expression"]).setNoAttributes()
        req.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        return {f.id() for f in layer.getFeatures(req)}
    return {int(f) for f in entry.get("fids", [])}


def restore_set(layer, name, op="replace"):
    """Applies a saved set to the selection; returns the number of selected features."""
    entry = next((s for s in sets(layer) if s["name"] == name), None)
    if entry is None:
        raise KeyError(name)
    ids = set_ids(layer, entry)
    current = set(layer.selectedFeatureIds())
    if op == "add":
        ids = current | ids
    elif op == "remove":
        ids = current - ids
    elif op == "intersect":
        ids = current & ids
    layer.selectByIds(list(ids))
    return len(ids)


# ---------------------------------------------------------------- history
def _record(layer):
    if _busy[0]:
        return
    h = _history.setdefault(layer.id(), {"stack": [frozenset()], "pos": 0})
    now = frozenset(layer.selectedFeatureIds())
    if h["stack"][h["pos"]] == now:
        return
    del h["stack"][h["pos"] + 1:]
    h["stack"].append(now)
    if len(h["stack"]) > HISTORY_SIZE:
        del h["stack"][0]
    h["pos"] = len(h["stack"]) - 1


def _apply(layer, ids):
    _busy[0] = True
    try:
        layer.selectByIds(list(ids))
    finally:
        _busy[0] = False


def can_go(layer, step):
    h = _history.get(layer.id()) if layer else None
    return bool(h) and 0 <= h["pos"] + step < len(h["stack"])


def go(layer, step):
    """step -1 = previous selection, +1 = next. Returns False when there is none."""
    if not can_go(layer, step):
        return False
    h = _history[layer.id()]
    h["pos"] += step
    _apply(layer, h["stack"][h["pos"]])
    return True


def watch(layer):
    if not isinstance(layer, QgsVectorLayer) or layer.id() in _connected:
        return

    def slot(*_args, lyr=layer):
        _record(lyr)

    layer.selectionChanged.connect(slot)
    _connected[layer.id()] = (layer, slot)
    _history.setdefault(layer.id(), {"stack": [frozenset(layer.selectedFeatureIds())], "pos": 0})


def _on_added(layers):
    for lyr in layers:
        watch(lyr)


def _on_removed(ids):
    for lid in ids:
        _history.pop(lid, None)
        _connected.pop(lid, None)


def install(iface=None):
    proj = QgsProject.instance()
    for lyr in proj.mapLayers().values():
        watch(lyr)
    proj.layersAdded.connect(_on_added)
    proj.layersWillBeRemoved.connect(_on_removed)


def uninstall():
    proj = QgsProject.instance()
    for sig, slot in ((proj.layersAdded, _on_added), (proj.layersWillBeRemoved, _on_removed)):
        try:
            sig.disconnect(slot)
        except TypeError:
            pass
    for layer, slot in list(_connected.values()):
        try:
            layer.selectionChanged.disconnect(slot)
        except (TypeError, RuntimeError):
            pass
    _connected.clear()
    _history.clear()


# ---------------------------------------------------------------- commands
def _active(plugin):
    layer = plugin.iface.activeLayer()
    if not isinstance(layer, QgsVectorLayer):
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("Select a vector layer in the Layers panel first."))
        return None
    return layer


def select_previous(plugin):
    layer = _active(plugin)
    if layer is not None and not go(layer, -1):
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("There is no earlier selection on this layer."))


def select_next(plugin):
    layer = _active(plugin)
    if layer is not None and not go(layer, 1):
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("There is no later selection on this layer."))


def save_selection(plugin):
    layer = _active(plugin)
    if layer is None:
        return
    if not layer.selectedFeatureCount():
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("Select some features first."))
        return
    name, ok = QInputDialog.getText(plugin.iface.mainWindow(), tr("Save the selection"),
                                    tr("Name of this selection ({n} features):").format(n=layer.selectedFeatureCount()))
    if not ok or not name.strip():
        return
    if any(s["name"] == name.strip() for s in sets(layer)):
        if QMessageBox.question(plugin.iface.mainWindow(), APP_NAME,
                                tr("A selection called {s} exists. Replace it?").format(s=name.strip())) \
                != QMessageBox.StandardButton.Yes:
            return
    save_set(layer, name)
    plugin.iface.messageBar().pushSuccess(APP_NAME, tr("Selection saved: {s}").format(s=name.strip()))


def fill_menu(plugin, menu):
    """The Saved selections menu of the ribbon."""
    menu.clear()
    layer = plugin.iface.activeLayer()
    menu.addAction(tr("Save the current selection…"), lambda: save_selection(plugin))
    prev = menu.addAction(tr("Select previous"), lambda: select_previous(plugin))
    nxt = menu.addAction(tr("Select next"), lambda: select_next(plugin))
    prev.setEnabled(isinstance(layer, QgsVectorLayer) and can_go(layer, -1))
    nxt.setEnabled(isinstance(layer, QgsVectorLayer) and can_go(layer, 1))
    if not isinstance(layer, QgsVectorLayer):
        return
    items = sets(layer)
    if not items:
        return
    menu.addSeparator()
    labels = {"replace": tr("Select these"), "add": tr("Add to the selection"),
              "remove": tr("Remove from the selection"), "intersect": tr("Keep only the common ones")}
    for s in items:
        n = len(s.get("fids", [])) if not s.get("expression") else None
        title = s["name"] + (f"  ({n})" if n is not None else "  (" + tr("expression") + ")")
        sub = menu.addMenu(title)
        for op in OPS:
            sub.addAction(labels[op], lambda op=op, name=s["name"]: _restore(plugin, layer, name, op))
        sub.addSeparator()
        sub.addAction(tr("Delete this saved selection"), lambda name=s["name"]: delete_set(layer, name))


def _restore(plugin, layer, name, op):
    n = restore_set(layer, name, op)
    plugin.iface.messageBar().pushInfo(APP_NAME, tr("{n} features selected.").format(n=n))
