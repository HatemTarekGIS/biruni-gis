"""Opening locked files from outside: drag and drop on the window, File > Open Project, and a file given on
the command line (Windows "Open with" / double-click). Registering them costs almost nothing at start-up:
the crypto and the dialogs load when a locked file is actually opened."""
import os

from qgis.gui import QgsCustomDropHandler, QgsCustomProjectOpenHandler

from ..i18n import tr
from . import DB_EXT, PROJECT_EXT

SCHEME = "biruni-locked:"


def _ours(path):
    p = str(path or "")
    return not p.startswith(SCHEME) and os.path.splitext(p)[1].lower() in (DB_EXT, PROJECT_EXT)


class LockedDropHandler(QgsCustomDropHandler):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def customUriProviderKey(self):  # noqa: N802
        return "biruni_locked"

    def handleFileDrop(self, file):  # noqa: N802
        if not _ours(file):
            return False
        from .runtime import open_locked

        open_locked(self.plugin, file)
        return True


class LockedProjectOpenHandler(QgsCustomProjectOpenHandler):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def filters(self):
        return [tr("Locked project") + " (*.bproj *.BPROJ)", tr("Locked database") + " (*.bgpkg *.BGPKG)"]

    def handleProjectOpen(self, file):  # noqa: N802
        if not _ours(file):
            return False
        from .runtime import open_locked

        open_locked(self.plugin, file)
        return True

    def createDocumentThumbnailAfterOpen(self):  # noqa: N802
        return False


def install(plugin):
    handlers = (LockedDropHandler(plugin), LockedProjectOpenHandler(plugin))
    plugin.iface.registerCustomDropHandler(handlers[0])
    plugin.iface.registerCustomProjectOpenHandler(handlers[1])
    return handlers


def uninstall(plugin, handlers):
    plugin.iface.unregisterCustomDropHandler(handlers[0])
    plugin.iface.unregisterCustomProjectOpenHandler(handlers[1])
    try:
        from .runtime import close_all

        close_all()
    except Exception:  # noqa: BLE001
        pass
