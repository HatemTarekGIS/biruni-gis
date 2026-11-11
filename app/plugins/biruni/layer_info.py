"""Catalog-style facts in the Layers panel: an indicator on every vector layer whose tooltip shows the
feature count, file size, last change, path, coordinate system and encoding. Kept cheap: nothing is
counted on remote databases or services unless the provider already knows the count.

install(iface) / uninstall() wire it to the layer tree; layer_facts() and facts_text() are plain functions."""
import datetime
import os

from qgis.core import QgsProject, QgsProviderRegistry, QgsVectorLayer

from .i18n import tr

REMOTE = {"postgres", "oracle", "mssql", "hana", "db2", "wfs", "oapif", "arcgisfeatureserver", "arcgismapserver",
          "virtual", "sensorthings", "WFS"}
ICON_COLOR = "#8a94a6"  # readable on light and dark themes
_state = {"iface": None, "indicators": {}, "conns": [], "pending": set(), "timer": None}


def _sidecars(path):
    """A shapefile is several files: count them all in the size."""
    base, ext = os.path.splitext(path)
    if ext.lower() != ".shp":
        return [path]
    folder = os.path.dirname(path) or "."
    stem = os.path.basename(base).lower()
    try:
        return [os.path.join(folder, f) for f in os.listdir(folder) if os.path.splitext(f)[0].lower() == stem]
    except OSError:
        return [path]


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def layer_facts(layer):
    """Facts about a layer, without reading its features where that could be slow."""
    facts = {"name": layer.name(), "provider": layer.providerType(), "crs": "", "count": None, "path": "",
             "size": None, "modified": None, "encoding": "", "geometry": "", "subset": ""}
    crs = layer.crs()
    if crs.isValid():
        facts["crs"] = f"{crs.authid()} - {crs.description()}" if crs.authid() else crs.description()
    if isinstance(layer, QgsVectorLayer) and layer.isValid():
        provider = layer.dataProvider()
        if layer.providerType() not in REMOTE:  # counting a remote table or service can take minutes
            n = layer.featureCount()
            facts["count"] = n if n is not None and n >= 0 else None
        facts["encoding"] = provider.encoding() if layer.providerType() == "ogr" else ""
        from qgis.core import QgsWkbTypes

        facts["geometry"] = QgsWkbTypes.displayString(layer.wkbType())
        facts["subset"] = layer.subsetString()
    parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    path = parts.get("path") or ""
    if path and os.path.exists(path):
        facts["path"] = os.path.normpath(path)
        try:
            if os.path.isdir(path):  # File Geodatabase
                facts["size"] = _dir_size(path)
                facts["modified"] = max((os.path.getmtime(os.path.join(r, f))
                                         for r, _d, fs in os.walk(path) for f in fs), default=os.path.getmtime(path))
            else:
                files = _sidecars(path)
                facts["size"] = sum(os.path.getsize(f) for f in files if os.path.exists(f))
                facts["modified"] = max(os.path.getmtime(f) for f in files if os.path.exists(f))
        except OSError:
            pass
        if parts.get("layerName"):
            facts["table"] = parts["layerName"]
    elif layer.providerType() == "memory":
        facts["path"] = tr("Temporary layer (in memory only)")
    return facts


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def facts_text(facts):
    """Tooltip text (HTML) for the facts of a layer."""
    rows = []

    def row(label, value):
        if value not in (None, ""):
            rows.append(f"<tr><td><b>{label}</b>&nbsp;&nbsp;</td><td>{value}</td></tr>")

    if facts.get("count") is not None:
        row(tr("Features"), f"{facts['count']:,}")
    elif facts.get("provider") in REMOTE:
        row(tr("Features"), tr("not counted (database or service)"))
    row(tr("Geometry"), facts.get("geometry"))
    row(tr("Size"), human_size(facts.get("size")))
    if facts.get("modified"):
        row(tr("Last changed"), datetime.datetime.fromtimestamp(facts["modified"]).strftime("%Y-%m-%d %H:%M"))
    row(tr("Coordinate system"), facts.get("crs"))
    row(tr("Encoding"), facts.get("encoding"))
    row(tr("Table"), facts.get("table"))
    row(tr("File"), facts.get("path"))
    if facts.get("subset"):
        row(tr("Filter"), facts["subset"][:200])
    return "<table>" + "".join(rows) + "</table>"


def set_feature_counts(on, root=None):
    """Show or hide QGIS's feature count after every vector layer name. Returns how many layers changed."""
    root = root or QgsProject.instance().layerTreeRoot()
    n = 0
    for node in root.findLayers():
        if isinstance(node.layer(), QgsVectorLayer):
            node.setCustomProperty("showFeatureCount", bool(on))
            n += 1
    return n


def feature_counts_shown(root=None):
    root = root or QgsProject.instance().layerTreeRoot()
    nodes = [n for n in root.findLayers() if isinstance(n.layer(), QgsVectorLayer)]
    return bool(nodes) and all(bool(n.customProperty("showFeatureCount", False)) for n in nodes)


# ---- the indicators -------------------------------------------------------------------------------------------

def _view():
    iface = _state["iface"]
    return iface.layerTreeView() if iface is not None else None


def _indicator(layer_id):
    from qgis.gui import QgsLayerTreeViewIndicator

    from . import icons

    ind = _state["indicators"].get(layer_id)
    if ind is None:
        ind = QgsLayerTreeViewIndicator()
        if icons.exists("info-circle"):
            ind.setIcon(icons.icon("info-circle", ICON_COLOR))
        _state["indicators"][layer_id] = ind
    return ind


def refresh_layer(layer):
    view = _view()
    if view is None or not isinstance(layer, QgsVectorLayer):
        return
    node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    if node is None:
        return
    ind = _indicator(layer.id())
    ind.setToolTip(facts_text(layer_facts(layer)))
    if ind not in view.indicators(node):
        view.addIndicator(node, ind)


def _schedule(layer_id):
    """Refresh after edits settle (one refresh per burst of changes)."""
    from qgis.PyQt.QtCore import QTimer

    _state["pending"].add(layer_id)
    timer = _state["timer"]
    if timer is None:
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setInterval(400)
        timer.timeout.connect(_flush)
        _state["timer"] = timer
    timer.start()


def _flush():
    project = QgsProject.instance()
    for lid in list(_state["pending"]):
        layer = project.mapLayer(lid)
        if layer is not None:
            refresh_layer(layer)
    _state["pending"].clear()


def _watch(layer):
    if not isinstance(layer, QgsVectorLayer):
        return
    lid = layer.id()
    if layer.property("biruni_layer_info"):  # already connected
        _schedule(lid)
        return
    layer.setProperty("biruni_layer_info", True)
    for sig in (layer.afterCommitChanges, layer.dataSourceChanged, layer.subsetStringChanged, layer.crsChanged,
                layer.nameChanged):
        sig.connect(lambda *_a, lid=lid: _schedule(lid))
    _schedule(lid)


def _layers_added(layers):
    for layer in layers:
        _watch(layer)


def _layers_removed(ids):
    for lid in ids:
        _state["indicators"].pop(lid, None)


def refresh_all():
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer):
            refresh_layer(layer)


def _project_read(*_args):
    # the layer tree is rebuilt on reading a project: indicators of the old nodes are gone
    _state["indicators"].clear()
    for lid, layer in QgsProject.instance().mapLayers().items():
        if isinstance(layer, QgsVectorLayer):
            _schedule(lid)


def install(iface):
    if _state["iface"] is not None:
        return
    _state["iface"] = iface
    project = QgsProject.instance()
    _state["conns"] = [(project.layersAdded, _layers_added), (project.layersWillBeRemoved, _layers_removed),
                       (project.readProject, _project_read)]
    for sig, slot in _state["conns"]:
        sig.connect(slot)
    _layers_added(list(project.mapLayers().values()))


def uninstall():
    view = _view()
    project = QgsProject.instance()
    if view is not None:
        for lid, ind in _state["indicators"].items():
            node = project.layerTreeRoot().findLayer(lid)
            if node is not None:
                view.removeIndicator(node, ind)
    for sig, slot in _state["conns"]:
        try:
            sig.disconnect(slot)
        except (TypeError, RuntimeError):
            pass
    _state.update(iface=None, indicators={}, conns=[], pending=set())
