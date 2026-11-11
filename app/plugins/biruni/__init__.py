"""Biruni GIS - the application shell (ribbon, start page, themes, i18n) that
runs on top of the bundled QGIS engine.

Before anything else: an update may have put a newer copy of this folder in the user's own folder (the program's
folder needs an administrator). Then every module of Biruni comes from that copy (update/boot.py, update/client.py).
Keep this file small: only this, and classFactory - the copy's own __init__.py is never run."""
import os as _os
import sys as _sys


def _use_update():
    try:
        from .update import boot
    except Exception:  # noqa: BLE001 - never stop Biruni from starting
        return None
    try:
        return boot.pick(_os.path.dirname(_os.path.abspath(__file__)))
    except Exception:  # noqa: BLE001
        return None


_copy = _use_update()
if _copy:
    __path__[:] = [_copy]                       # noqa: F821 - the package's own search path
    for _name in [n for n in _sys.modules if n == __name__ + ".update" or n.startswith(__name__ + ".update.")]:
        del _sys.modules[_name]                 # the copy's own update code from now on


def classFactory(iface):  # noqa: N802 (QGIS plugin entry point)
    from .plugin import BiruniPlugin

    return BiruniPlugin(iface)
