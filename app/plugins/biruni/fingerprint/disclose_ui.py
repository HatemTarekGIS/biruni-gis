"""The buttons of the shared copies: a copy with some fields (or features) left out that proves itself, and its
check by whoever receives it."""
import os

from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                                 QVBoxLayout)

from ..i18n import tr
from . import core, disclose
from .proof_ui import _why
from .ui import PROOF, _bar, _dir, _remember, _vectors, _zoom

SHARED = "-shared.gpkg"


class ShareDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.cert = None
        self.setWindowTitle(tr("Share with Fields Hidden"))
        self.setMinimumWidth(480)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A GeoPackage without the fields you tick (owners' names, prices…) that still proves every "
                          "shape and every value it shows is what the delivery certificate sealed, none added and none "
                          "taken out. The values left out cannot be found from it, not even a yes or no."))
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
        self.fields = QListWidget()
        self.fields.setMinimumHeight(140)
        form.addRow(tr("Fields to hide"), self.fields)
        self.only = QCheckBox(tr("Only the selected features (the others are counted, not shown)"))
        form.addRow("", self.only)
        row = QHBoxLayout()
        self.cert_lbl = QLabel(tr("(none: the layer as it is now, with a code of its own)"))
        self.cert_lbl.setWordWrap(True)
        pick = QPushButton(tr("Choose…"))
        pick.clicked.connect(lambda: self.pick_cert())
        row.addWidget(self.cert_lbl, 1)
        row.addWidget(pick)
        form.addRow(tr("Delivery certificate"), row)
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
        self.layer.currentIndexChanged.connect(lambda _i: self.fill())
        self.fill()

    def current(self):
        return QgsProject.instance().mapLayer(self.layer.currentData() or "")

    def fill(self):
        self.fields.clear()
        lyr = self.current()
        if lyr is None:
            return
        for n in core.field_names(lyr):
            it = QListWidgetItem(n)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.fields.addItem(it)
        self.only.setChecked(False)
        self.only.setEnabled(lyr.selectedFeatureCount() > 0)

    def hidden(self):
        return [self.fields.item(i).text() for i in range(self.fields.count())
                if self.fields.item(i).checkState() == Qt.CheckState.Checked]

    def set_hidden(self, names):
        names = {n.lower() for n in names}
        for i in range(self.fields.count()):
            it = self.fields.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.text() in names else Qt.CheckState.Unchecked)

    def pick_cert(self, path=None):
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, tr("Delivery certificate"), _dir(),
                                                   tr("Certificate") + f" (*{PROOF} *.json)")
            if not path:
                return
        try:
            cert = core.read_json(path)
            assert cert.get("kind") == "biruni-fingerprint"
        except Exception:  # noqa: BLE001
            self.out.setText(tr("This is not a delivery certificate."))
            return
        _remember(path)
        self.cert = cert
        self.cert_lbl.setText(f"{os.path.basename(path)} — {cert['code']}")

    def make(self, path=None):
        lyr = self.current()
        if lyr is None:
            self.out.setText(tr("Choose the layer."))
            return None
        hide = self.hidden()
        only = lyr.selectedFeatureIds() if self.only.isChecked() and self.only.isEnabled() else None
        if not hide and only is None:
            self.out.setText(tr("Tick the fields to hide, or share only the selected features."))
            return None
        index = 0
        if self.cert is not None:
            names = [e["name"] for e in self.cert["layers"]]
            index = names.index(lyr.name()) if lyr.name() in names else (0 if len(names) == 1 else -1)
            if index < 0:
                self.out.setText(tr("The certificate has no layer named «{n}».").format(n=lyr.name()))
                return None
        try:
            doc, keep, shown = disclose.share(lyr, hide, self.cert, index, only)
        except core.FingerprintError as e:
            msg = str(e)
            if msg == "old certificate":
                msg = tr("This certificate was made by an older version, without the seals a shared copy needs: "
                         "make a new certificate of the layer.")
            self.out.setText(_why(msg))
            return None
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.windowTitle(), os.path.join(_dir(), lyr.name() + SHARED),
                                                   "GeoPackage (*.gpkg)")
            if not path:
                return None
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        _remember(path)
        try:
            disclose.write(lyr, doc, keep, shown, path)
        except (disclose.ShareError, OSError) as e:
            self.out.setText(tr("The copy could not be written: {e}").format(e=e))
            return None
        code = doc["certificate"]["code"]
        text = tr("Saved: {n} features of «{l}», without {h}.").format(
            n=len(keep), l=lyr.name(), h=", ".join(doc["hidden"]) or tr("no field"))
        if doc["withheld"]:
            text += " " + tr("{w} features held back, counted.").format(w=doc["withheld"])
        text += " " + tr("Code {c}.").format(c=code)
        if self.cert is None:
            text += " " + tr("This code is new: send it now to whoever must trust the date, since the proof is only "
                             "as old as the first time the code was seen.")
        self.out.setText(text)
        _bar(self.plugin, text, seconds=0)
        return doc


def share_ui(plugin):
    if not _vectors():
        _bar(plugin, tr("Add the layer to the map first."), Qgis.MessageLevel.Warning, 8)
        return None
    dlg = ShareDialog(plugin)
    dlg.show()
    plugin._share_dialog = dlg
    return dlg


def _why_not(why):
    return {
        "code": tr("its code is not the one its certificate's fingerprints make."),
        "fields": tr("its fields are not the ones it says it shows."),
        "crs": tr("its coordinate system is not the certified one."),
        "changed": tr("some features are not as certified, or certified ones were taken out."),
        "seals": tr("its seals are not the certified ones: something was taken out, added or changed."),
    }.get(why, tr("it is not a shared copy."))


def verify_ui(plugin, path=None, quiet=False):
    if path is None:
        path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Check a Shared Copy"), _dir(),
                                               "GeoPackage (*.gpkg)")
        if not path:
            return None
    _remember(path)
    r = disclose.verify(path)
    title = tr("Check a Shared Copy")
    if r.layer is not None and r.layer.isValid():
        QgsProject.instance().addMapLayer(r.layer)
        if r.bad:
            r.layer.selectByIds(r.bad)
        _zoom(plugin, r.layer)
    if not r.ok:
        text = tr("Not proved: {w}").format(w=_why_not(r.why))
        if r.bad or r.missing:
            text += " " + tr("{b} features not as certified (selected on the map), {m} certified features missing.").format(
                b=len(r.bad), m=r.missing)
        if not quiet:
            QMessageBox.warning(plugin.iface.mainWindow(), title, text)
        r.text = text
        return r
    d = r.doc
    text = tr("Proved: the {n} features of «{l}» in this copy, their shapes and the values shown, are what the "
              "certificate sealed, and none is missing.").format(n=r.shown, l=d.get("layer", ""))
    if d.get("hidden"):
        text += " " + tr("Left out: {h}.").format(h=", ".join(d["hidden"]))
    if r.withheld:
        text += " " + tr("{w} more features were held back.").format(w=r.withheld)
    text += "\n\n" + tr("The data is the one whose certificate has the code {c} ({f} features). Compare it with the "
                        "code on the certificate you were given, or the one sent when it was made.").format(
        c=r.code, f=d.get("features", "?"))
    if not quiet:
        QMessageBox.information(plugin.iface.mainWindow(), title, text)
    r.text = text
    return r
