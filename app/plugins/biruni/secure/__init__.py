"""The locked safe: password-locked projects (*.bproj) and databases (*.bgpkg) with offline users and
rights. Importing this package costs nothing: the crypto library, GDAL and the dialogs load on first use.
install(plugin) only registers the handlers that open these files (drag and drop, Open Project)."""
DB_EXT = ".bgpkg"
PROJECT_EXT = ".bproj"


def is_locked_path(path):
    p = str(path or "").lower()
    return p.endswith(DB_EXT) or p.endswith(PROJECT_EXT)


def install(plugin):
    """Registers the file-open handlers; keep the result and pass it to uninstall()."""
    from .handlers import install as _install

    return _install(plugin)


def uninstall(plugin, handlers):
    if handlers:
        from .handlers import uninstall as _uninstall

        _uninstall(plugin, handlers)
