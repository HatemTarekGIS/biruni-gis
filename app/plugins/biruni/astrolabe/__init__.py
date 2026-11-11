"""The astrolabe: a disc of three rings over the map - on what, what to do, how much or with what. Turning
them writes a command as a sentence and draws its result on the map before it happens; the hub carries it out.

It opens from the ribbon, from the map's right-click menu ("Astrolabe here") and with F9 at the cursor.
"""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QKeySequence

from ..i18n import tr

KEY = "F9"


def get(plugin):
    a = getattr(plugin, "_astrolabe", None)
    if a is None:
        from .controller import Astrolabe

        a = plugin._astrolabe = Astrolabe(plugin)
    return a


def toggle(plugin):
    get(plugin).toggle()


def repeat(plugin):
    get(plugin).repeat()


def install(plugin):
    """The map's right-click menu and the key; returns what undoes it."""
    canvas, mw = plugin.iface.mapCanvas(), plugin.iface.mainWindow()

    def on_menu(menu, event):
        a = QAction(tr("Astrolabe here"), menu)
        at = event.pos()
        a.triggered.connect(lambda _=False: get(plugin).open(at))
        first = menu.actions()[0] if menu.actions() else None
        menu.insertAction(first, a)
        if first is not None:
            menu.insertSeparator(first)

    canvas.contextMenuAboutToShow.connect(on_menu)
    key = None
    seq = QKeySequence(KEY)
    if not any(a.shortcut() == seq for a in mw.findChildren(QAction)):   # the key is free
        key = QAction(mw)
        key.setShortcut(seq)
        key.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        key.triggered.connect(lambda _=False: toggle(plugin))
        mw.addAction(key)

    def undo():
        try:
            canvas.contextMenuAboutToShow.disconnect(on_menu)
        except (TypeError, RuntimeError):
            pass
        if key is not None:
            mw.removeAction(key)
        a = getattr(plugin, "_astrolabe", None)
        if a is not None:
            a.unload()
            plugin._astrolabe = None
    return undo
