"""The buttons of the area proofs: prove what a layer has in an area (nothing, or exactly these features), and
check such a proof with nothing but the proof itself."""
import datetime as dt
import os

from qgis.core import (Qgis, QgsCoordinateTransform, QgsFeature, QgsFillSymbol, QgsGeometry, QgsProject,
                       QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout)

from ..i18n import tr
from . import core, proof
from .ui import PROOF, _bar, _dir, _remember, _vectors, _zoom

AREA_PROOF = ".biruni-area.json"


def _why(e):
    s = str(e)
    return {
        "area": tr("The area must be polygons: select the features that draw it."),
        "small": tr("The area is smaller than the tolerance."),
        "changed": tr("The layer is no longer what the certificate says: a proof against it would not hold. "
                      "Make it without the certificate, or bring the layer back as delivered."),
        "old certificate": tr("This certificate was made by an older version, without the boxes a proof needs: "
                              "make a new certificate of the layer."),
        "reach": tr("The certificate does not match the data."),
    }.get(s, s)


def _area_of(lyr, crs):
    """The selected features of a polygon layer, as one shape in ``crs``."""
    geoms = [QgsGeometry(f.geometry()) for f in lyr.selectedFeatures() if f.hasGeometry()]
    if not geoms:
        return None
    g = QgsGeometry.unaryUnion(geoms)
    if lyr.crs() != crs:
        g.transform(QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance()))
    return g


class AreaProofDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.cert = None
        self.setWindowTitle(tr("Prove What Is in an Area"))
        self.setMinimumWidth(480)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A proof that the layer has nothing in the area, or exactly these features and no others. "
                          "Anyone checks it from the file alone, against the code of the delivery certificate; "
                          "the values of the features around the area stay hidden."))
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
        pick.clicked.connect(self.pick_cert)
        row.addWidget(self.cert_lbl, 1)
        row.addWidget(pick)
        form.addRow(tr("Delivery certificate"), row)
        v.addLayout(form)
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
            pr = proof.prove(data, area, tol, self.cert, index)
        except core.FingerprintError as e:
            self.out.setText(_why(e))
            return None
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.windowTitle(),
                                                   os.path.join(_dir(), data.name() + AREA_PROOF),
                                                   tr("Area proof") + f" (*{AREA_PROOF})")
            if not path:
                return None
        if not path.endswith(".json"):
            path += AREA_PROOF
        _remember(path)
        core.write_json(pr, path)
        code = pr["certificate"]["code"]
        text = (tr("Proved: «{l}» has nothing in the area.") if not pr["inside"] else
                tr("Proved: «{l}» has exactly {n} features in the area, and no others.")).format(l=data.name(), n=pr["inside"])
        text += " " + tr("Code {c}.").format(c=code)
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
    dlg = AreaProofDialog(plugin)
    dlg.show()
    plugin._area_proof_dialog = dlg
    return dlg


def _why_not(why):
    return {
        "code": tr("its code is not the one its certificate's fingerprints make."),
        "open": tr("a part of the data that reaches the area was kept closed."),
        "hidden": tr("a feature in the area was given without its values."),
        "box": tr("a feature's box is not its shape's."),
        "reach": tr("what it holds is not the data the certificate fingerprinted: something was taken out, added or changed."),
        "area": tr("its area is not a polygon."),
        "small": tr("its area is smaller than its tolerance."),
    }.get(why, tr("it is not a proof file."))


def verify_ui(plugin, path=None, quiet=False):
    if path is None:
        path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Check an Area Proof"), _dir(),
                                               tr("Area proof") + f" (*{AREA_PROOF} *.json)")
        if not path:
            return None
    _remember(path)
    try:
        pr = core.read_json(path)
    except Exception:  # noqa: BLE001
        pr = {}
    r = proof.verify(pr)
    title = tr("Check an Area Proof")
    if not r.ok:
        text = tr("Not proved: {w}").format(w=_why_not(r.why))
        if not quiet:
            QMessageBox.warning(plugin.iface.mainWindow(), title, text)
        return r
    crs = pr["settings"]["crs"]
    name = pr.get("layer", "")
    alyr = QgsVectorLayer(f"Polygon?crs={crs}", tr("The area of the proof: {l}").format(l=name), "memory")
    af = QgsFeature()
    af.setGeometry(r.area)
    alyr.dataProvider().addFeatures([af])
    alyr.renderer().setSymbol(QgsFillSymbol.createSimple(
        {"color": "0,0,0,0", "outline_color": "42,157,143,255", "outline_width": "0.8", "outline_style": "dash"}))
    QgsProject.instance().addMapLayer(alyr)
    if r.inside:
        fields = [kv[0] for kv in r.inside[0][1]]
        kind = QgsWkbTypes.displayString(r.inside[0][0].wkbType())
        spec = "&".join(f"field={n}:string" for n in fields)
        ilyr = QgsVectorLayer(f"{kind}?crs={crs}&{spec}", tr("In the area: {l}").format(l=name), "memory")
        feats = []
        for g, vals in r.inside:
            f = QgsFeature(ilyr.fields())
            f.setGeometry(g)
            f.setAttributes(["" if v is None else str(v) for _n, v in vals])
            feats.append(f)
        ilyr.dataProvider().addFeatures(feats)
        QgsProject.instance().addMapLayer(ilyr)
    _zoom(plugin, alyr)
    made = pr.get("made", "")
    try:
        made = dt.datetime.fromisoformat(made).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    head = (tr("Proved: «{l}» has nothing in this area.") if not r.inside else
            tr("Proved: «{l}» has exactly {n} features in this area, and no others; they are on the map.")).format(
        l=name, n=len(r.inside))
    text = head + "\n\n" + tr("The data is the one whose certificate has the code {c} ({f} features). Compare it with "
                              "the code on the certificate you were given, or the one sent when it was made: the proof "
                              "is only as old as that code.").format(c=r.code, f=pr.get("features", "?"))
    text += "\n\n" + tr("Made {t}. {o} squares opened, {k} kept closed: the values around the area are not in the proof.").format(
        t=made, o=r.opened, k=r.closed)
    if not quiet:
        QMessageBox.information(plugin.iface.mainWindow(), title, text)
    r.text = text
    return r
