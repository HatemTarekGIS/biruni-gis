"""Add Data («إضافة بيانات»): a simpler window than the classic Data Source Manager. Drop or choose files and
folders (or paste a path / web address) and each one is recognised by itself - points, lines, areas, tables,
images, elevation, CAD drawings, GPS tracks, point clouds, archives, web maps - shown as a card with its quick facts,
then added with one button.

    detect.py   what a file / folder / address holds (runs in the background thread)
    kinds.py    the friendly name, icon and colour of each kind
    dialog.py   the window

Nothing is imported or built until open_dialog() is called.
"""
_OPEN = {}  # id(plugin) -> the open window


def open_dialog(plugin, show=True):
    """Opens the Add Data window centred over the main window (the one already open is brought forward)."""
    dlg = _OPEN.get(id(plugin))
    if dlg is not None:
        try:
            if dlg.isVisible():
                dlg.raise_()
                dlg.activateWindow()
                return dlg
        except RuntimeError:  # deleted
            pass
        _OPEN.pop(id(plugin), None)
    from qgis.PyQt.QtCore import Qt

    from .dialog import AddDataDialog

    mw = plugin.iface.mainWindow()
    dlg = AddDataDialog(plugin, mw)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    _OPEN[id(plugin)] = dlg
    dlg.finished.connect(lambda *_: _OPEN.pop(id(plugin), None) if _OPEN.get(id(plugin)) is dlg else None)
    if show:
        if mw is not None:
            geo = dlg.frameGeometry()
            geo.moveCenter(mw.frameGeometry().center())
            dlg.move(geo.topLeft())
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
    return dlg
