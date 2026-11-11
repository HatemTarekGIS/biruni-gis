"""Map Studio: say what the map is for, get a finished layout.

The purpose (a deed sketch, a report figure, a wall map, a slide, a social post…) decides the page, the type
sizes, the elements and the outputs; the elements are then placed where the map has no data, and one map is
exported in every size it is needed. Optional parts written separately: ``themes`` (colour themes) and
``doctor`` (checks with fixes) - the studio works without them."""


def optional(name):
    """A sibling module of the studio, or None when it is not there (or fails to import)."""
    import importlib

    try:
        return importlib.import_module(f"{__name__}.{name}")
    except Exception:  # noqa: BLE001 - optional parts never take the studio down
        return None


def open_studio(plugin):
    from .studio_dialog import open_studio as _open

    return _open(plugin)
