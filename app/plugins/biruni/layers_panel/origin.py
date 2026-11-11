"""Where did it come from? A layer made by a tool (any tool run from its dialog - Biruni's or the engine's) remembers
the tool and the date in the project, and shows them as a small wand badge with a tooltip.

How: a tool's dialog writes its results into the processing history just before its output layers are added to the
map. The results (layer ids of temporary outputs, paths of saved ones) are kept for two minutes; a layer added in that
time whose id or file matches gets the custom property ``biruni/origin``."""
import json
import os
import time

from qgis.core import QgsApplication, QgsLayerTree, QgsProject
from qgis.PyQt.QtCore import QDateTime, QObject, QTimer

from ..i18n import tr

KEY = "biruni/origin"
_KEEP_SECONDS = 120
ICON_COLOR = "#8A94A6"


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _strings(value, out):
    if isinstance(value, str):
        out.add(value)
        out.add(_norm(value.split("|")[0]))
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _strings(v, out)
    elif isinstance(value, dict):
        for v in value.values():
            _strings(v, out)
    return out


def origin_of(layer):
    """{"tool": name, "at": ISO date} or None."""
    raw = layer.customProperty(KEY, "") if layer is not None else ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("tool") else None


def origin_text(data):
    when = QDateTime.fromString(data.get("at", ""), "yyyy-MM-ddTHH:mm:ss")
    date = when.toString("yyyy-MM-dd HH:mm") if when.isValid() else data.get("at", "")
    return f"<b>{tr('Made with a tool')}</b><br>" + tr("«{tool}», {date}").format(tool=data["tool"], date=date)


class Origin(QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.recent = []  # (monotonic time, tool name, {result strings})
        self.indicators = {}  # layer id -> indicator
        self.pending = set()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.flush)
        project = QgsProject.instance()
        self.conns = [(project.layersAdded, self.layers_added), (project.readProject, self._project_read),
                      (project.layersWillBeRemoved, self._layers_removed),
                      (project.layerTreeRoot().addedChildren, self._tree_added)]
        try:
            from qgis.gui import QgsGui

            self.conns.append((QgsGui.historyProviderRegistry().entryUpdated, self.history_updated))
        except Exception:  # noqa: BLE001 - no history: only already-marked layers show their origin
            pass
        for sig, slot in self.conns:
            sig.connect(slot)
        self._project_read()

    def history_updated(self, *args):
        data = next((a for a in args if isinstance(a, dict)), None)
        if not data or "results" not in data or not data.get("algorithm_id"):
            return
        alg = QgsApplication.processingRegistry().algorithmById(data["algorithm_id"])
        name = alg.displayName() if alg is not None else data["algorithm_id"]
        values = _strings(data.get("results"), set())
        values.discard("")
        now = time.monotonic()
        self.recent = [r for r in self.recent if now - r[0] < _KEEP_SECONDS] + [(now, name, values)]

    def layers_added(self, layers):
        now = time.monotonic()
        recent = [r for r in self.recent if now - r[0] < _KEEP_SECONDS]
        for layer in layers:
            if not origin_of(layer) and recent:
                path = _norm(layer.source().split("|")[0])
                for _t, name, values in reversed(recent):
                    if layer.id() in values or (path and path in values):
                        stamp = QDateTime.currentDateTime().toString("yyyy-MM-ddTHH:mm:ss")
                        layer.setCustomProperty(KEY, json.dumps({"tool": name, "at": stamp}, ensure_ascii=False))
                        break
            if origin_of(layer):
                self.schedule(layer.id())

    def _layers_removed(self, ids):
        for lid in ids:
            ind = self.indicators.pop(lid, None)
            if ind is not None:
                ind.deleteLater()

    def _tree_added(self, node, first, last):
        children = node.children()
        for i in range(first, min(last, len(children) - 1) + 1):
            child = children[i]
            nodes = [child] if QgsLayerTree.isLayer(child) else (child.findLayers() if QgsLayerTree.isGroup(child)
                                                                 else [])
            for n in nodes:
                if n.layerId() in self.indicators or origin_of(n.layer()):
                    self.schedule(n.layerId())

    def _project_read(self, *_args):
        for ind in self.indicators.values():
            ind.deleteLater()
        self.indicators.clear()
        for lid, layer in QgsProject.instance().mapLayers().items():
            if origin_of(layer):
                self.schedule(lid)

    def schedule(self, lid):
        self.pending.add(lid)
        self.timer.start()

    def flush(self):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        for lid in list(self.pending):
            self.pending.discard(lid)
            layer = project.mapLayer(lid)
            data = origin_of(layer)
            node = root.findLayer(lid)
            if data is None or node is None:
                continue
            ind = self.indicators.get(lid)
            if ind is None:
                from qgis.gui import QgsLayerTreeViewIndicator

                from .. import icons

                ind = QgsLayerTreeViewIndicator(self.view)
                ind.setObjectName("biruniLayerOrigin")
                ind.setIcon(icons.icon("layers/origin", ICON_COLOR))
                self.indicators[lid] = ind
            ind.setToolTip(origin_text(data))
            if ind not in self.view.indicators(node):
                self.view.addIndicator(node, ind)

    def teardown(self):
        self.timer.stop()
        for sig, slot in self.conns:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        root = QgsProject.instance().layerTreeRoot()
        for lid, ind in self.indicators.items():
            node = root.findLayer(lid)
            try:
                if node is not None:
                    self.view.removeIndicator(node, ind)
                ind.deleteLater()
            except RuntimeError:
                pass
        self.indicators.clear()
