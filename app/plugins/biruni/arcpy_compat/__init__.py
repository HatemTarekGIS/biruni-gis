"""arcpy compatibility layer: lets existing arcpy scripts run in Biruni on the GDAL/OGR engine.

    from biruni import arcpy_compat
    arcpy_compat.install()          # `import arcpy` now gives the shim
    arcpy_compat.run_script(path, ["arg1", "arg2"], on_message=print)

    arcpy_compat.analyse.analyse_script(text) -> coverage report of a script (see analyse.py)
    arcpy_compat.registry.as_list()           -> what is supported

Unsupported calls raise arcpy.ExecuteError("... not available in Biruni yet")."""
import sys

_saved = {}


def _module():
    from . import _arcpy
    return _arcpy


def install():
    """Put the shim on sys.modules as `arcpy` (and arcpy.da, arcpy.management, ...). Returns the module."""
    mod = _module()
    names = {"arcpy": mod}
    for sub, m in mod.submodules().items():
        names[f"arcpy.{sub}"] = m
    for key, m in names.items():
        if key not in _saved:
            _saved[key] = sys.modules.get(key)
        sys.modules[key] = m
    return mod


def uninstall():
    """Restore whatever `arcpy` was before install()."""
    for key, old in list(_saved.items()):
        if old is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = old
    _saved.clear()


def is_installed():
    m = sys.modules.get("arcpy")
    return getattr(m, "__biruni_shim__", False)


def run_script(path, args=None, on_message=None, on_progress=None, workspace=None, cancel_check=None):
    from .runner import run_script as _run
    return _run(path, args, on_message, on_progress, workspace, cancel_check)


def __getattr__(name):
    if name in ("analyse", "registry", "py2fix", "runner"):
        import importlib
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)
