"""Keyboard shortcuts of Map Stops (window shortcuts of the main window; cheap - no stops are read here):

* Ctrl+Shift+B   new stop. QGIS uses it for "Show Spatial Bookmarks"; that action's shortcut is taken while
                 Biruni runs and given back on unload (the QGIS manager stays in Map Stops > Options).
                 Ctrl+B ("New Spatial Bookmark" of QGIS) is left alone: a bookmark made there is a stop too.
* Alt+1 … Alt+9  go to stop 1…9 (QGIS uses Ctrl+1…Ctrl+9 for its panels). Options: Ctrl+Alt+1…9, or none.
* Alt+← / Alt+→  back / forward through the views visited.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QKeySequence

NEW_STOP_KEY = "Ctrl+Shift+B"
QGIS_BOOKMARKS_ACTION = "mActionShowBookmarks"
JUMP_PREFIX = {"alt": "Alt+", "ctrl_alt": "Ctrl+Alt+", "off": None}


def _qaction(plugin, name):
    runner = getattr(plugin, "runner", None)
    if runner is not None:
        return runner.qaction(name)
    from qgis.PyQt.QtWidgets import QWidget

    mw = plugin.iface.mainWindow()
    return mw.findChild(QAction, name) if isinstance(mw, QWidget) else None


def jump_sequence(n, mode=None):
    from .model import setting

    prefix = JUMP_PREFIX.get(mode or setting("jump_keys"), "Alt+")
    return QKeySequence(prefix + str(n)) if prefix else QKeySequence()


def install_keys(plugin, st):
    uninstall_keys(plugin, st)
    mw = plugin.iface.mainWindow()
    from . import go_back, go_forward, jump_to_number, new_stop

    backup = {}
    qa = _qaction(plugin, QGIS_BOOKMARKS_ACTION)
    if qa is not None and qa.shortcut() == QKeySequence(NEW_STOP_KEY):
        backup[qa] = qa.shortcut()
        qa.setShortcut(QKeySequence())
    actions = []

    def add(seq, fn, name):
        if seq.isEmpty():
            return
        a = QAction(mw)
        a.setObjectName(name)
        a.setShortcut(seq)
        a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        a.triggered.connect(lambda _=False, f=fn: f(plugin))
        mw.addAction(a)
        actions.append(a)

    add(QKeySequence(NEW_STOP_KEY), new_stop, "BiruniNewStop")
    add(QKeySequence("Alt+Left"), go_back, "BiruniViewBack")
    add(QKeySequence("Alt+Right"), go_forward, "BiruniViewForward")
    for n in range(1, 10):
        add(jump_sequence(n), lambda p, n=n: jump_to_number(p, n), f"BiruniStop{n}")
    st["actions"] = actions
    st["backup"] = backup


def uninstall_keys(plugin, st):
    mw = plugin.iface.mainWindow()
    for a in st.pop("actions", []):
        try:
            mw.removeAction(a)
            a.deleteLater()
        except RuntimeError:
            pass
    for qa, seq in st.pop("backup", {}).items():
        try:
            qa.setShortcut(seq)
        except RuntimeError:
            pass


def reinstall(plugin):
    """After the jump keys were changed in Options."""
    from . import _STATE

    st = _STATE.get(id(plugin))
    if st is not None:
        install_keys(plugin, st)


def shortcut_text(seq):
    return seq.toString(QKeySequence.SequenceFormat.NativeText)
