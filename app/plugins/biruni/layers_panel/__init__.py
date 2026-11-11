"""A friendlier Layers panel («الطبقات»): our own clear icons and tooltips on the panel's buttons, plus

    search.py       a live search box over the layer tree (Arabic diacritics and letter forms ignored)
    empty_state.py  a cheerful "your map is empty" card with Add data / Open the Vault
    health.py       «نبض الطبقات» - small badges on layers with a problem (missing source, empty, another coordinate
                    system, unsaved edits, slow to draw) and a one-click fix for each
    solo.py         Solo peek: only the selected layers on the map, then exactly the old visibility back
    arrange.py      Smart arrange: groups by kind, points above lines above areas above images; preview and undo
    origin.py       "where did it come from": layers made by a tool remember the tool and the date
    toolbar.py      recognising the panel's own buttons and giving them our icons
    panel.py        puts it all into the panel

install(plugin) only schedules the work for after start-up (one zero-delay timer); nothing is imported or built
at import time.
"""
_STATE = {}  # id(plugin) -> {"panel": LayersPanel | None}


def install(plugin, defer=True):
    """Builds the panel additions on the next turn of the event loop (or at once with defer=False). Returns the undo."""
    if id(plugin) in _STATE:
        return lambda: uninstall(plugin)
    _STATE[id(plugin)] = {"panel": None}
    if defer:
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, lambda: _build(plugin))
    else:
        _build(plugin)
    return lambda: uninstall(plugin)


def _build(plugin):
    st = _STATE.get(id(plugin))
    if st is None or st["panel"] is not None:  # unloaded before the timer fired, or already built
        return
    from .panel import LayersPanel

    try:
        st["panel"] = LayersPanel(plugin)
    except Exception as e:  # noqa: BLE001 - the stock panel keeps working whatever happens here
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(f"Layers panel additions not installed: {e!r}", "Biruni", Qgis.MessageLevel.Warning)


def live_panel(plugin):
    """The live LayersPanel of this plugin (None before it is built)."""
    st = _STATE.get(id(plugin))
    return st["panel"] if st else None


def uninstall(plugin):
    st = _STATE.pop(id(plugin), None)
    if st and st["panel"] is not None:
        st["panel"].teardown()


# ---- commands (for the ribbon / command palette, if wanted) -------------------------------------------------------
def solo_peek(plugin):
    p = live_panel(plugin)
    if p is not None:
        p.toggle_solo()


def smart_arrange(plugin):
    p = live_panel(plugin)
    if p is not None:
        p.smart_arrange()


def undo_arrange(plugin):
    p = live_panel(plugin)
    if p is not None:
        p.undo_arrange()


def focus_search(plugin):
    p = live_panel(plugin)
    if p is not None:
        p.focus_search()
