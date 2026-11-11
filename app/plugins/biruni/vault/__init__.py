"""The Vault («الخزانة»): everything this project has, in one cheerful pane beside the map - the project's maps,
layouts, Map Books, Map Stops, Data Charters, field photos, indoor maps, thesis figures, models, styles and sealed
files; the project folder's data grouped by kind; databases; favourite folders and recent data.

    kinds.py    what a file is (vector, raster, CAD, table, document …), its colour and icon
    scan.py     reading folders and files (plain Python + GDAL, safe in the worker thread)
    thumbs.py   small pictures of files, layouts and photos, cached on disk by path + date
    worker.py   the background thread
    catalog.py  the entries of the pane (what "This project" holds, the folder, databases, folders)
    usage.py    "where is this used", duplicates, renaming and deleting files
    preview.py  quick look: a small map of a file without adding it to the project; properties card
    dock.py     the pane

Nothing here is imported or done at start-up except, through install(), one signal that remembers the files added
to maps (for "Recent data"). The pane is built the first time it is opened.
"""
import sys

RECENT_KEY = "biruni/vault/recent"
RECENT_MAX = 30


def open_dock(plugin):
    """Shows the Vault (built on first use) as a tab beside the Browser."""
    from .dock import open_dock as _open

    return _open(plugin)


def toggle_dock(plugin):
    from .dock import toggle_dock as _toggle

    return _toggle(plugin)


def close_dock(plugin):
    """Removes the pane when it was ever opened (plugin unload)."""
    dock = sys.modules.get(f"{__package__}.dock")
    if dock is not None:
        dock.close_dock(plugin)


# ---- recent data (the only thing wired at start-up: one cheap signal) ---------------------------------------------
def remember_layers(layers):
    """Puts the files of newly added layers at the top of the recent data list."""
    import os

    from qgis.core import QgsProviderRegistry, QgsSettings

    s = QgsSettings()
    recent = [r for r in (s.value(RECENT_KEY, [], type=list) or []) if isinstance(r, str)]
    for lyr in layers:
        try:
            parts = QgsProviderRegistry.instance().decodeUri(lyr.providerType(), lyr.source())
        except Exception:  # noqa: BLE001 - an odd provider: nothing to remember
            continue
        path = parts.get("path") or ""
        if not path or not os.path.isabs(path):
            continue
        path = os.path.normpath(path)
        recent = [path] + [r for r in recent if os.path.normcase(r) != os.path.normcase(path)]
    s.setValue(RECENT_KEY, recent[:RECENT_MAX])


def install(plugin):
    """Remembers the files added to maps (for Recent data). Returns the undo."""
    from qgis.core import QgsProject

    QgsProject.instance().layersAdded.connect(remember_layers)

    def undo():
        try:
            QgsProject.instance().layersAdded.disconnect(remember_layers)
        except (TypeError, RuntimeError):
            pass
        close_dock(plugin)

    return undo
