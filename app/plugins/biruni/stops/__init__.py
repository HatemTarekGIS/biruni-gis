"""Map Stops («محطات»): named places on the map that remember the view (extent, rotation, scale), optionally the
layers shown (a map theme), the time range, a note, a colour, a group and a small picture of the map.

Every stop is also a real QGIS bookmark (same id, same name and group), so QGIS's own tools keep seeing them; the
extra data is kept beside it, in the project (or, for "all my projects", in the user profile).

Nothing here does work at import time. install() only adds a few keyboard actions and one debounced signal on the
map canvas (the view history); everything else is imported when it is used.
"""
import importlib

_STATE = {}  # id(plugin) -> {"history": ViewHistory, "actions": [...], "backup": {qaction: shortcut}}


def _sub(name):
    return importlib.import_module(f"{__package__}.{name}")


# ---- start-up / unload -------------------------------------------------------------------------------------------
def install(plugin):
    """Cheap: the view history (one debounced signal on the canvas) and the keyboard shortcuts."""
    from .history import ViewHistory
    from .keys import install_keys

    st = _STATE.setdefault(id(plugin), {})
    if "history" not in st:
        st["history"] = ViewHistory(plugin.iface.mapCanvas())
    install_keys(plugin, st)
    return lambda: uninstall(plugin)


def uninstall(plugin):
    st = _STATE.pop(id(plugin), None)
    if st is None:
        return
    from .keys import uninstall_keys

    uninstall_keys(plugin, st)
    h = st.get("history")
    if h is not None:
        h.detach()
    import sys

    for mod in ("pane", "tour", "fly"):
        m = sys.modules.get(f"{__package__}.{mod}")
        if m is not None and hasattr(m, "shutdown"):
            m.shutdown(plugin)


def view_history(plugin):
    st = _STATE.setdefault(id(plugin), {})
    if "history" not in st:
        from .history import ViewHistory

        st["history"] = ViewHistory(plugin.iface.mapCanvas())
    return st["history"]


# ---- commands (each imports its part when used) ------------------------------------------------------------------
def fill_menu(plugin, menu):
    """The ribbon dropdown: filter, gallery of pictures, actions. Built every time the menu opens."""
    _sub("gallery").fill_menu(plugin, menu)


def new_stop(plugin):
    return _sub("actions").new_stop(plugin)


def open_manager(plugin):
    return _sub("pane").open_pane(plugin)


def play_tour(plugin):
    return _sub("actions").play_tour(plugin)


def export_stops(plugin):
    return _sub("actions").export_dialog(plugin)


def import_stops(plugin):
    return _sub("actions").import_dialog(plugin)


def options(plugin):
    return _sub("actions").options_dialog(plugin)


def go_back(plugin):
    return _sub("actions").go_history(plugin, -1)


def go_forward(plugin):
    return _sub("actions").go_history(plugin, 1)


def jump_to_number(plugin, n):
    return _sub("actions").jump_to_number(plugin, n)


def send_to_phone(plugin, stop_id=None):
    return _sub("actions").send_to_phone(plugin, stop_id)
