"""The DWG buttons: open a DWG drawing as GIS layers (through CAD to GIS), and save layers or a DXF as a DWG."""
import os

from qgis.core import Qgis, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QApplication, QFileDialog

from ..i18n import tr
from ..version import APP_NAME
from . import core

DIR_KEY = "biruni/dwg_dir"


def _dir():
    return QgsSettings().value(DIR_KEY, "") or os.path.expanduser("~")


def _remember(path):
    QgsSettings().setValue(DIR_KEY, os.path.dirname(path))


def _bar(plugin, text, level=Qgis.MessageLevel.Success, seconds=10):
    plugin.iface.messageBar().pushMessage(APP_NAME, text, level, seconds)


def _missing(plugin):
    _bar(plugin, tr("LibreDWG, which reads and writes DWG drawings, is not installed with the program."),
         Qgis.MessageLevel.Warning, 10)


def open_ui(plugin, path=None, run=None):
    """Pick a DWG and open CAD to GIS on it (layers, arcs, blocks, Arabic texts as from a DXF)."""
    if not core.available():
        _missing(plugin)
        return None
    if path is None:
        path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Open a DWG Drawing"), _dir(),
                                               "AutoCAD DWG (*.dwg *.DWG)")
        if not path:
            return None
    _remember(path)
    if not core.version_of(path):
        _bar(plugin, tr("This is not a DWG drawing."), Qgis.MessageLevel.Warning, 8)
        return None
    if run is None:
        import processing

        run = processing.execAlgorithmDialog
    return run("biruni:cadtogis", {"INPUT": path})


def _chosen(plugin):
    try:
        sel = [l for l in plugin.iface.layerTreeView().selectedLayers() if isinstance(l, QgsVectorLayer)]
    except AttributeError:
        sel = []
    return sel


def export_layers_dxf(layers, path, crs=None):
    """The layers as an AutoCAD 2000 DXF (Windows-1256, so Arabic attributes and labels keep their letters)."""
    from qgis.core import QgsDxfExport, QgsMapSettings, QgsProject
    from qgis.PyQt.QtCore import QFile, QIODevice

    ex = QgsDxfExport()
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(crs or layers[0].crs())
    ext = None
    for l in layers:
        e = l.extent()
        if crs is not None and l.crs() != crs:
            from qgis.core import QgsCoordinateTransform

            e = QgsCoordinateTransform(l.crs(), crs, QgsProject.instance()).transformBoundingBox(e)
        if ext is None:
            ext = e
        else:
            ext.combineExtentWith(e)
    ms.setExtent(ext)
    ex.setMapSettings(ms)
    ex.setDestinationCrs(crs or layers[0].crs())
    ex.addLayers([QgsDxfExport.DxfLayer(l) for l in layers])
    f = QFile(path)
    if not f.open(QIODevice.OpenModeFlag.WriteOnly | QIODevice.OpenModeFlag.Truncate):
        raise OSError(path)
    try:
        res = ex.writeToFile(f, "cp1256")
    finally:
        f.close()
    if res != QgsDxfExport.ExportResult.Success:
        raise core.DwgError(f"failed: DXF export {res}")
    return path


def save_ui(plugin, out=None, dxf=None):
    """The layers selected in the Layers panel (or a DXF file, when none is) saved as an AutoCAD 2000 DWG."""
    if not core.available():
        _missing(plugin)
        return None
    mw = plugin.iface.mainWindow()
    layers = [] if dxf else _chosen(plugin)
    if not layers and dxf is None:
        dxf, _f = QFileDialog.getOpenFileName(mw, tr("Save as DWG: the DXF drawing"), _dir(), "DXF (*.dxf *.DXF)")
        if not dxf:
            return None
    if out is None:
        base = layers[0].name() if layers else os.path.splitext(os.path.basename(dxf))[0]
        out, _f = QFileDialog.getSaveFileName(mw, tr("Save as DWG"), os.path.join(_dir(), base + ".dwg"),
                                              "AutoCAD 2000 DWG (*.dwg)")
        if not out:
            return None
    if not out.lower().endswith(".dwg"):
        out += ".dwg"
    _remember(out)
    many = sum(max(0, l.featureCount()) for l in layers)
    if many > 10000:     # LibreDWG's writer slows down with the size: 2,000 features take a second, 20,000 a minute
        _bar(plugin, tr("Writing {n} features as a DWG takes a few minutes.").format(n=many), Qgis.MessageLevel.Info, 8)
        QApplication.processEvents()
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        if layers:
            import tempfile

            dxf = export_layers_dxf(layers, os.path.join(tempfile.mkdtemp(prefix="biruni_dwg_"), "layers.dxf"))
        res = core.to_dwg(dxf, out)
    except (core.DwgError, OSError) as e:
        QApplication.restoreOverrideCursor()
        why = str(e)
        if why.startswith("lost"):
            text = tr("LibreDWG could not write the whole drawing ({e} entities would be missing): save it as DXF "
                      "instead.").format(e=why[5:])
        else:
            text = tr("The DWG could not be written: {e}").format(e=why[:300])
        _bar(plugin, text, Qgis.MessageLevel.Critical, 0)
        return None
    QApplication.restoreOverrideCursor()
    text = tr("Saved «{f}» as an AutoCAD 2000 drawing (opens in every AutoCAD since 2000).").format(
        f=os.path.basename(out))
    if res["escaped"]:
        text += " " + tr("{n} characters with no code page were written as \\U+ codes.").format(n=res["escaped"])
    _bar(plugin, text, seconds=12)
    return out
