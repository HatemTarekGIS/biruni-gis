"""Moving from ArcGIS: a readiness scan of whole folders (projects, maps, layer files, arcpy scripts, toolboxes and
data: what opens 100%, what changes, what is missing) and the import of ModelBuilder models as Biruni models.

    scanner.py      walk folders and check every item with the real readers (nothing is added to the project)
    modelbuilder.py read .atbx / .tbx toolboxes, convert their models to .model3
    report.py       HTML (English / Arabic, right to left) and Excel reports
    dialog.py       the window: pick folders, scan in the background, sortable results"""


def open_dialog(plugin):
    """Command: the readiness scan window."""
    from .dialog import open_dialog as _open

    return _open(plugin)


def import_models_dialog(plugin):
    """Command: pick an ArcGIS toolbox and import its models."""
    from .dialog import import_models_dialog as _imp

    return _imp(plugin)
