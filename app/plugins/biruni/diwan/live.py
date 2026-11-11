"""The Diwan while Biruni runs: every save of a layer kept in a Diwan file is logged (who, when, "biruni"), and
when a project is opened the edits other programs made to its Diwan files are found and logged ("external").
The file work runs in the background (background.py); the window never waits for it."""
from qgis.core import QgsProject, QgsProviderRegistry, QgsVectorLayer

from .. import background
from ..i18n import tr
from ..version import APP_NAME
from .store import Diwan

_STATE = {"layers": set(), "plugin": None}


def source_of(layer):
    """(GeoPackage path, table) of a layer, or (None, None)."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return None, None
    parts = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    path = parts.get("path") or ""
    if not path.lower().endswith(".gpkg"):
        return None, None
    return path, parts.get("layerName") or None


def diwan_of(layer):
    """(path, table) when the layer is kept in a Diwan file and its table is under the Diwan, else None."""
    path, table = source_of(layer)
    if not path or not Diwan.is_diwan(path):
        return None
    with Diwan(path) as d:
        if table is None:
            tables = d.layer_tables()
            table = tables[0] if len(tables) == 1 else None
        if table not in d.tables():
            return None
    return path, table


def who():
    from ..team.session import my_name

    return my_name()


def _on_saved(layer):
    found = diwan_of(layer)
    if found is None:
        return
    path, table = found
    name = who()

    def work():
        with Diwan(path) as d:
            return d.reconcile(table, name, "biruni")

    background.run(work)


def watch(layer):
    """Log the saves of ``layer`` (no effect when it is not in a Diwan file)."""
    if layer.id() in _STATE["layers"] or diwan_of(layer) is None:
        return
    _STATE["layers"].add(layer.id())
    layer.afterCommitChanges.connect(lambda lyr=layer: _on_saved(lyr))
    layer.willBeDeleted.connect(lambda lid=layer.id(): _STATE["layers"].discard(lid))


def on_project_read(*_):
    """Watch the project's Diwan layers and log what other programs changed in their files meanwhile."""
    paths = set()
    for lyr in QgsProject.instance().mapLayers().values():
        found = diwan_of(lyr)
        if found:
            watch(lyr)
            paths.add(found[0])
    if not paths:
        return

    def work():
        total = 0
        for p in paths:
            with Diwan(p) as d:
                total += sum(d.reconcile(who="", how="external").values())
        return total

    def done(n, error):
        plugin = _STATE["plugin"]
        if error is None and n and plugin is not None and getattr(plugin, "iface", None) is not None:
            plugin.iface.messageBar().pushInfo(APP_NAME, tr("{n} changes made outside Biruni were found and "
                                                            "recorded in the Diwan.").format(n=n))

    background.run(work, done)


def install(plugin):
    _STATE["plugin"] = plugin
    QgsProject.instance().readProject.connect(on_project_read)
    QgsProject.instance().layersAdded.connect(_on_added)
    on_project_read()

    def undo():
        for sig, slot in ((QgsProject.instance().readProject, on_project_read),
                          (QgsProject.instance().layersAdded, _on_added)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        _STATE["plugin"] = None
    return undo


def _on_added(layers):
    for lyr in layers:
        watch(lyr)
