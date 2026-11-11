"""The buttons of the marked copies: give a copy marked with who it is for, and find whose copy a file is."""
import datetime as dt
import os

from qgis.core import Qgis, QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

from ..fingerprint.ui import _bar, _dir, _remember, _vectors
from ..i18n import tr
from . import core


def _when(text):
    try:
        return dt.datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return text or ""


class MarkDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(tr("Marked Copy"))
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A copy marked with who it is for, hidden in its coordinates (a fortieth of a millimetre): "
                          "it is still the certified data, but if it turns up where it should not, «Whose Copy?» "
                          "tells whom it was given to. The secret of the mark stays on this computer."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.layer = QComboBox()
        for lyr in _vectors():
            self.layer.addItem(lyr.name(), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None and self.layer.findData(act.id()) >= 0:
            self.layer.setCurrentIndex(self.layer.findData(act.id()))
        form.addRow(tr("The layer"), self.layer)
        self.who = QLineEdit()
        self.who.setPlaceholderText(tr("The office, the company or the person"))
        form.addRow(tr("Given to"), self.who)
        self.note = QLineEdit()
        form.addRow(tr("Note"), self.note)
        self.only = QCheckBox(tr("Only the selected features"))
        form.addRow("", self.only)
        v.addLayout(form)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.go = QPushButton(tr("Make the Copy"))
        bb.addButton(self.go, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        self.go.clicked.connect(lambda: self.make())
        v.addWidget(bb)

    def make(self, path=None, reg=None):
        lyr = QgsProject.instance().mapLayer(self.layer.currentData() or "")
        if lyr is None:
            self.out.setText(tr("Choose the layer."))
            return None
        if not self.who.text().strip():
            self.out.setText(tr("Write whom the copy is for."))
            return None
        fids = lyr.selectedFeatureIds() if self.only.isChecked() else None
        if self.only.isChecked() and not fids:
            self.out.setText(tr("Select the features first."))
            return None
        if path is None:
            safe = "".join(c for c in self.who.text().strip() if c.isalnum() or c in " -_")[:40].strip() or "copy"
            path, _f = QFileDialog.getSaveFileName(self, self.windowTitle(),
                                                   os.path.join(_dir(), f"{lyr.name()} - {safe}.gpkg"),
                                                   "GeoPackage (*.gpkg)")
            if not path:
                return None
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        _remember(path)
        try:
            e = core.mark(lyr, self.who.text(), path, self.note.text().strip(), fids, reg)
        except (core.WatermarkError, OSError) as ex:
            self.out.setText(tr("The copy could not be written: {e}").format(e=ex))
            return None
        text = tr("Saved: {n} features of «{l}» marked for «{w}». The same delivery certificate checks it.").format(
            n=e["features"], l=lyr.name(), w=e["recipient"])
        self.out.setText(text)
        _bar(self.plugin, text)
        return e


def mark_ui(plugin):
    if not _vectors():
        _bar(plugin, tr("Add the layer to the map first."), Qgis.MessageLevel.Warning, 8)
        return None
    dlg = MarkDialog(plugin)
    dlg.show()
    plugin._mark_dialog = dlg
    return dlg


def describe(layer, reg=None):
    """What a layer's coordinates say of whose copy it is, as a sentence."""
    found, n = core.find(layer, reg)
    best = core.who(layer, reg)
    if best is not None:
        e = best.entry
        text = tr("This is the copy given to «{w}» on {t} ({l}, {f} features).").format(
            w=e["recipient"], t=_when(e.get("made")), l=e.get("layer", ""), f=e.get("features", "?"))
        if e.get("note"):
            text += " " + tr("Note: {n}").format(n=e["note"])
        text += " " + tr("{p}% of {c} marks agree.").format(p=round(best.rate * 100, 1), c=best.total)
        return text, best
    if n < 20 or not found:
        return tr("No mark of a copy given from this computer: it is not a marked copy, or its coordinates were "
                  "rounded or moved to another coordinate system."), None
    return tr("None of the {k} copies given from this computer: the closest agrees on {p}% of {c} coordinates, as "
              "chance would. A copy whose coordinates were rounded, or moved to another coordinate system, loses its "
              "mark.").format(k=len(found), p=round(found[0].rate * 100, 1), c=found[0].total), None


class WhoDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.setWindowTitle(tr("Whose Copy?"))
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self.layer = QComboBox()
        for lyr in _vectors():
            self.layer.addItem(lyr.name(), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None and self.layer.findData(act.id()) >= 0:
            self.layer.setCurrentIndex(self.layer.findData(act.id()))
        row.addWidget(self.layer, 1)
        openb = QPushButton(tr("Open a File…"))
        openb.clicked.connect(lambda: self.open_file())
        row.addWidget(openb)
        form = QFormLayout()
        form.addRow(tr("The copy"), row)
        v.addLayout(form)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        go = QPushButton(tr("Find Whose It Is"))
        bb.addButton(go, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        go.clicked.connect(lambda: self.find())
        v.addWidget(bb)
        self.file_layer = None

    def open_file(self, path=None):
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, self.windowTitle(), _dir(),
                                                   tr("Vector files") + " (*.gpkg *.shp *.geojson *.json *.kml *.dxf *.csv)")
            if not path:
                return None
        _remember(path)
        lyr = QgsVectorLayer(path, os.path.basename(path), "ogr")
        if not lyr.isValid():
            self.out.setText(tr("This file cannot be read."))
            return None
        self.file_layer = lyr
        self.layer.insertItem(0, os.path.basename(path), "")
        self.layer.setCurrentIndex(0)
        return lyr

    def find(self, reg=None):
        lyr = QgsProject.instance().mapLayer(self.layer.currentData() or "") if self.layer.currentData() else self.file_layer
        if lyr is None:
            self.out.setText(tr("Choose the layer."))
            return None
        text, best = describe(lyr, reg)
        self.out.setText(text)
        return best


def who_ui(plugin):
    dlg = WhoDialog(plugin)
    dlg.show()
    plugin._who_dialog = dlg
    return dlg
