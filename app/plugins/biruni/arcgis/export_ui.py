"""Commands to save the project for ArcGIS Pro (.lyrx / .mapx / .aprx) and to
import ArcGIS Pro layout files (.pagx). Each one asks for a file, writes or
reads it, and lists everything that could not be converted."""
import os

from qgis.core import QgsCoordinateTransform, QgsLayerTreeGroup, QgsLayerTreeLayer, QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QLabel, QMessageBox,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from .writer import WriteError, needs_copy, write_aprx, write_lyrx, write_mapx

_LAST_DIR = "biruni/arcgis-export-dir"


def _save_path(plugin, title, default_name, file_filter, ext):
    s = QgsSettings()
    start = os.path.join(s.value(_LAST_DIR, os.path.expanduser("~")), default_name)
    path, _ = QFileDialog.getSaveFileName(plugin.iface.mainWindow(), title, start, file_filter)
    if not path:
        return None
    if not path.lower().endswith(ext):
        path += ext
    s.setValue(_LAST_DIR, os.path.dirname(path))
    return path


def _layers_of(nodes):
    out = []
    for n in nodes:
        if isinstance(n, QgsLayerTreeLayer) and n.layer() is not None:
            out.append(n.layer())
        elif isinstance(n, QgsLayerTreeGroup):
            out += [c.layer() for c in n.findLayers() if c.layer() is not None]
    return out


def _ask_copy(plugin, layers):
    """True / False to copy data ArcGIS Pro cannot open; None when the user cancels."""
    names = needs_copy(layers)
    if not names:
        return False
    shown = "\n".join("• " + n for n in names[:12]) + ("\n…" if len(names) > 12 else "")
    box = QMessageBox(plugin.iface.mainWindow())
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(APP_NAME)
    box.setText(tr("ArcGIS Pro cannot open the data of these layers where it is now:") + "\n\n" + shown)
    box.setInformativeText(tr("Copy their data into a file geodatabase next to the file you save? "
                              "Otherwise they are left out."))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                           | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Yes)
    ans = box.exec()
    if ans == QMessageBox.StandardButton.Cancel:
        return None
    return ans == QMessageBox.StandardButton.Yes


class ExportResultDialog(QDialog):
    """What was written, what was left out and why."""

    def __init__(self, parent, report, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(660, 440)
        lay = QVBoxLayout(self)
        head = QLabel(_summary(report))
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(head)
        tree = QTreeWidget()
        tree.setHeaderLabels([tr("Layer"), tr("Note")])
        for label, rows in ((tr("Not written ({n})"), report.skipped),
                            (tr("Written with changes ({n})"), [w.partition(": ")[::2] for w in report.warnings]),
                            (tr("Data copied into the file geodatabase ({n})"), report.copied)):
            if rows:
                top = QTreeWidgetItem([label.format(n=len(rows)), ""])
                for name, why in rows:
                    QTreeWidgetItem(top, [name, why])
                tree.addTopLevelItem(top)
                top.setExpanded(True)
        tree.resizeColumnToContents(0)
        lay.addWidget(tree)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def _summary(report):
    text = tr("Saved {file}: {n} layers.").format(file=os.path.basename(report.path), n=len(report.written))
    if report.layouts:
        text += " " + tr("Layouts: {names}").format(names=", ".join(report.layouts))
    if report.skipped:
        text += " " + tr("{n} could not be converted.").format(n=len(report.skipped))
    return text


def _run(plugin, title, fn, *args, **kwargs):
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        report = fn(*args, **kwargs)
    except WriteError as e:
        plugin.iface.messageBar().pushWarning(APP_NAME, str(e))
        return None
    except (OSError, ValueError) as e:
        plugin.iface.messageBar().pushWarning(APP_NAME, tr("Could not save the file") + f": {e}")
        return None
    finally:
        QApplication.restoreOverrideCursor()
    if report.skipped or report.warnings or report.copied:
        ExportResultDialog(plugin.iface.mainWindow(), report, title).exec()
    else:
        plugin.iface.messageBar().pushSuccess(APP_NAME, _summary(report))
    return report


def _canvas_extent(plugin, project):
    canvas = plugin.iface.mapCanvas()
    ext = canvas.extent()
    src = canvas.mapSettings().destinationCrs()
    if src.isValid() and project.crs().isValid() and src != project.crs():
        ext = QgsCoordinateTransform(src, project.crs(), project).transformBoundingBox(ext)
    return ext


def export_lyrx(plugin):
    """Save the selected layers / group as an ArcGIS Pro layer file."""
    project = QgsProject.instance()
    view = plugin.iface.layerTreeView()
    nodes = list(view.selectedNodes(True)) if view is not None else []
    if not nodes and plugin.iface.activeLayer() is not None:
        node = project.layerTreeRoot().findLayer(plugin.iface.activeLayer().id())
        nodes = [node] if node is not None else []
    if not nodes:
        plugin.iface.messageBar().pushWarning(APP_NAME, tr("Select a layer or a group first."))
        return None
    title = tr("Save as ArcGIS Pro layer file")
    path = _save_path(plugin, title, nodes[0].name() + ".lyrx", tr("Layer file") + " (*.lyrx)", ".lyrx")
    if not path:
        return None
    copy = _ask_copy(plugin, _layers_of(nodes))
    if copy is None:
        return None
    return _run(plugin, title, write_lyrx, path, nodes, project, copy)


def export_mapx(plugin):
    """Save the whole map as an ArcGIS Pro map file."""
    project = QgsProject.instance()
    if not project.mapLayers():
        plugin.iface.messageBar().pushWarning(APP_NAME, tr("The map has no layers."))
        return None
    title = tr("Save as ArcGIS Pro map file")
    name = project.baseName() or "Map"
    path = _save_path(plugin, title, name + ".mapx", tr("Map file") + " (*.mapx)", ".mapx")
    if not path:
        return None
    copy = _ask_copy(plugin, list(project.mapLayers().values()))
    if copy is None:
        return None
    return _run(plugin, title, write_mapx, path, project, copy, None, _canvas_extent(plugin, project))


def export_aprx(plugin):
    """Save the project (map + print layouts) as an ArcGIS Pro project."""
    project = QgsProject.instance()
    if not project.mapLayers():
        plugin.iface.messageBar().pushWarning(APP_NAME, tr("The map has no layers."))
        return None
    title = tr("Save as ArcGIS Pro project")
    name = project.baseName() or "Project"
    path = _save_path(plugin, title, name + ".aprx", tr("ArcGIS Pro project") + " (*.aprx)", ".aprx")
    if not path:
        return None
    copy = _ask_copy(plugin, list(project.mapLayers().values()))
    if copy is None:
        return None
    return _run(plugin, title, write_aprx, path, project, copy, True, None, _canvas_extent(plugin, project))


def import_pagx(plugin):
    """Open an ArcGIS Pro layout file (.pagx) as a print layout."""
    from .ui import open_path

    s = QgsSettings()
    path, _ = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Open an ArcGIS Pro layout file"),
                                          s.value("biruni/arcgis-last-dir", os.path.expanduser("~")),
                                          tr("Layout file") + " (*.pagx);;" + tr("ArcGIS Pro project") + " (*.aprx)")
    if not path:
        return None
    s.setValue("biruni/arcgis-last-dir", os.path.dirname(path))
    report = open_path(plugin, path, maps="*" if path.lower().endswith(".aprx") else None)
    if report is not None and report.layouts:
        mgr = QgsProject.instance().layoutManager()
        layout = mgr.layoutByName(report.layouts[0])
        if layout is not None:
            plugin.iface.openLayoutDesigner(layout)
    return report
