"""Run by the launcher via --code.

Guarantees the Biruni shell is active even if its plugin was disabled in the
user's profile (e.g. from the plugin manager). QGIS 4 runs --code before it
loads the enabled plugins, so the check waits for initialisation to finish."""
import qgis.utils
from qgis.PyQt.QtCore import QTimer

_done = False


def _ensure():
    global _done
    if _done:
        return
    _done = True
    if "biruni" not in qgis.utils.active_plugins:
        if qgis.utils.loadPlugin("biruni"):
            qgis.utils.startPlugin("biruni")


qgis.utils.iface.initializationCompleted.connect(lambda: QTimer.singleShot(0, _ensure))
QTimer.singleShot(30000, _ensure)  # fallback if initialisation had already completed
