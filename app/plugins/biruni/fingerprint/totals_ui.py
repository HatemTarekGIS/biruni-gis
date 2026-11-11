"""The buttons of the totals proofs: prove how many features an area has, their area (or length) and the totals of
the numeric fields, without the data; and check such a proof from the file alone."""
import datetime as dt
import os

from qgis.core import Qgis, QgsFeature, QgsFillSymbol, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout)

from ..i18n import tr
from . import core, totals
from .proof_ui import _area_of, _why
from .ui import PROOF, _bar, _dir, _remember, _vectors, _zoom

TOTALS = ".biruni-totals.json"


def _num(v):
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.3f}".rstrip("0").rstrip(".")


def _amount(r):
    if r.measure == "area":
        return tr("{a} {u}² in all").format(a=_num(r.amount), u=r.unit)
    if r.measure == "length":
        return tr("{a} {u} long in all").format(a=_num(r.amount), u=r.unit)
    return ""


def summary(r, name):
    """The totals of a checked proof, in words."""
    head = tr("«{l}» has {n} features all inside the area").format(l=name, n=r.count)
    amt = _amount(r)
    text = head + (", " + amt if amt else "") + "."
    if r.fields:
        text += " " + tr("Totals: {t}.").format(t="; ".join(f"{k} = {_num(v)}" for k, v in r.fields.items()))
    if r.crossing:
        text += " " + tr("{c} more cross its edge and are not counted.").format(c=r.crossing)
    return text


class TotalsDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.cert = None
        self.setWindowTitle(tr("Prove the Totals of an Area"))
        self.setMinimumWidth(480)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A proof of how many features an area has, their area or length and the totals of their "
                          "numeric fields, without the features: the parts of the data all inside the area are given "
                          "by their sums only, and only the features on its edge show their shape. Anyone checks it "
                          "from the file alone, against the code of the delivery certificate."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.data, self.area = QComboBox(), QComboBox()
        for lyr in _vectors():
            self.data.addItem(lyr.name(), lyr.id())
            if lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
                self.area.addItem(tr("{n} ({s} selected)").format(n=lyr.name(), s=lyr.selectedFeatureCount()), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None:
            if self.area.findData(act.id()) >= 0 and act.selectedFeatureCount():
                self.area.setCurrentIndex(self.area.findData(act.id()))
            elif self.data.findData(act.id()) >= 0:
                self.data.setCurrentIndex(self.data.findData(act.id()))
        form.addRow(tr("The layer"), self.data)
        form.addRow(tr("The area: the selected features of"), self.area)
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0, 1000)
        self.tol.setDecimals(1)
        self.tol.setValue(5)
        self.tol.setSuffix(" " + tr("cm"))
        self.tol.setToolTip(tr("A feature that comes into the area less than this (a shared boundary drawn a few "
                               "centimetres apart) is not in it."))
        form.addRow(tr("Tolerance"), self.tol)
        row = QHBoxLayout()
        self.cert_lbl = QLabel(tr("(none: the layer as it is now, with a code of its own)"))
        self.cert_lbl.setWordWrap(True)
        pick = QPushButton(tr("Choose…"))
        pick.clicked.connect(lambda: self.pick_cert())
        row.addWidget(self.cert_lbl, 1)
        row.addWidget(pick)
        form.addRow(tr("Delivery certificate"), row)
        v.addLayout(form)
        note = QLabel(tr("The totals of every numeric field are in the proof, square by square (not feature by "
                         "feature)."))
        note.setWordWrap(True)
        v.addWidget(note)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.go = QPushButton(tr("Make the Proof"))
        bb.addButton(self.go, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        self.go.clicked.connect(lambda: self.make())
        v.addWidget(bb)

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
        p = QgsProject.instance()
        data, alyr = p.mapLayer(self.data.currentData() or ""), p.mapLayer(self.area.currentData() or "")
        if data is None or alyr is None:
            self.out.setText(tr("Choose the layer, and a polygon layer with the area selected."))
            return None
        area = _area_of(alyr, data.crs())
        if area is None:
            self.out.setText(tr("Select the features that draw the area first."))
            return None
        tol = self.tol.value() / 100.0
        if data.crs().isGeographic():
            tol /= 111320.0
        index = 0
        if self.cert is not None:
            names = [e["name"] for e in self.cert["layers"]]
            index = names.index(data.name()) if data.name() in names else (0 if len(names) == 1 else -1)
            if index < 0:
                self.out.setText(tr("The certificate has no layer named «{n}».").format(n=data.name()))
                return None
        try:
            pr = totals.prove_totals(data, area, tol, self.cert, index)
        except core.FingerprintError as e:
            msg = str(e)
            if msg == "old certificate":
                msg = tr("This certificate was made by an older version, without the sums a totals proof needs: "
                         "make a new certificate of the layer.")
            self.out.setText(_why(msg))
            return None
        r = totals.verify_totals(pr)
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.windowTitle(),
                                                   os.path.join(_dir(), data.name() + TOTALS),
                                                   tr("Totals proof") + f" (*{TOTALS})")
            if not path:
                return None
        if not path.endswith(".json"):
            path += TOTALS
        _remember(path)
        core.write_json(pr, path)
        text = tr("Proved: {s}").format(s=summary(r, data.name())) + " " + tr("Code {c}.").format(c=r.code)
        if self.cert is None:
            text += " " + tr("This code is new: send it now to whoever must trust the date, since the proof is only "
                             "as old as the first time the code was seen.")
        self.out.setText(text)
        _bar(self.plugin, text, seconds=0)
        return pr


def prove_ui(plugin):
    if not [l for l in _vectors() if l.geometryType() == QgsWkbTypes.PolygonGeometry]:
        _bar(plugin, tr("Add the layer, and a polygon layer that draws the area, to the map first."),
             Qgis.MessageLevel.Warning, 8)
        return None
    dlg = TotalsDialog(plugin)
    dlg.show()
    plugin._totals_dialog = dlg
    return dlg


def _why_not(why):
    return {
        "code": tr("its code is not the one its certificate's fingerprints make."),
        "open": tr("a part of the data on the edge of the area was kept closed."),
        "hidden": tr("a feature in the area or on its edge was given without its shape or its numbers."),
        "shape": tr("a feature's shape is not the one fingerprinted."),
        "box": tr("a feature's box is not its shape's."),
        "amount": tr("a feature's area or length is not its shape's."),
        "sums": tr("its sums are not the ones the certificate sealed: something was taken out, added or changed."),
        "area": tr("its area is not a polygon."),
        "small": tr("its area is smaller than its tolerance."),
    }.get(why, tr("it is not a totals proof."))


def verify_ui(plugin, path=None, quiet=False):
    if path is None:
        path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Check a Totals Proof"), _dir(),
                                               tr("Totals proof") + f" (*{TOTALS} *.json)")
        if not path:
            return None
    _remember(path)
    try:
        pr = core.read_json(path)
    except Exception:  # noqa: BLE001
        pr = {}
    r = totals.verify_totals(pr)
    title = tr("Check a Totals Proof")
    if not r.ok:
        r.text = tr("Not proved: {w}").format(w=_why_not(r.why))
        if not quiet:
            QMessageBox.warning(plugin.iface.mainWindow(), title, r.text)
        return r
    crs = pr["settings"]["crs"]
    name = pr.get("layer", "")
    alyr = QgsVectorLayer(f"Polygon?crs={crs}", tr("The area of the totals: {l}").format(l=name), "memory")
    af = QgsFeature()
    af.setGeometry(r.area)
    alyr.dataProvider().addFeatures([af])
    alyr.renderer().setSymbol(QgsFillSymbol.createSimple(
        {"color": "0,0,0,0", "outline_color": "42,157,143,255", "outline_width": "0.8", "outline_style": "dash"}))
    QgsProject.instance().addMapLayer(alyr)
    _zoom(plugin, alyr)
    made = pr.get("made", "")
    try:
        made = dt.datetime.fromisoformat(made).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    text = tr("Proved: {s}").format(s=summary(r, name))
    text += "\n\n" + tr("The data is the one whose certificate has the code {c} ({f} features). Compare it with the "
                        "code on the certificate you were given, or the one sent when it was made.").format(
        c=r.code, f=pr.get("features", "?"))
    text += "\n\n" + tr("Made {t}. {o} squares opened, {k} kept closed, {g} shapes shown.").format(
        t=made, o=r.opened, k=r.closed, g=r.shown)
    if not quiet:
        QMessageBox.information(plugin.iface.mainWindow(), title, text)
    r.text = text
    return r
