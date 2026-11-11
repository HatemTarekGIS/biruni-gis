"""The buttons of the spatial fingerprints: compare two copies and bring one's changes into the other, a
delivery certificate and its check, a package of the changed squares to carry and its applying."""
import os

from qgis.core import (Qgis, QgsFeature, QgsFillSymbol, QgsGeometry, QgsProject, QgsRectangle, QgsSettings,
                       QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
                                 QLabel, QMessageBox, QPushButton, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import core

DIR_KEY = "biruni/fingerprint/dir"
PROOF = ".biruni-proof.json"
CHANGES = ".biruni-changes"


def _bar(plugin, text, level=Qgis.MessageLevel.Success, seconds=14):
    plugin.iface.messageBar().pushMessage(APP_NAME, text, level, seconds)


def _dir():
    return QgsSettings().value(DIR_KEY, "") or QgsProject.instance().homePath() or os.path.expanduser("~")


def _remember(path):
    QgsSettings().setValue(DIR_KEY, os.path.dirname(path))


def _vectors():
    return [l for l in QgsProject.instance().mapLayers().values() if isinstance(l, QgsVectorLayer) and l.isValid()]


def _why(e):
    s = str(e)
    if s == "crs":
        return tr("The two copies are in different coordinate systems: save one in the other's first.")
    if s == "grid":
        return tr("The two were fingerprinted on different grids.")
    if s == "fields":
        return tr("The two copies do not have the same fields.")
    if s.startswith("fields missing"):
        return tr("The layer does not have the fields of the other copy: {f}").format(f=s.split(":", 1)[1].strip())
    if s == "cancelled":
        return tr("Cancelled.")
    if s == "reach":
        return tr("The certificate does not match the data.")
    return s


def cells_layer(cell, crs, cells, name):
    """The squares as a layer: where two copies differ."""
    lyr = QgsVectorLayer(f"Polygon?crs={crs}&field=square:string", name, "memory")
    feats = []
    for ix, iy in cells:
        if (ix, iy) == core.NULL_CELL:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(ix * cell, iy * cell, (ix + 1) * cell, (iy + 1) * cell)))
        f.setAttributes([f"{ix},{iy}"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.renderer().setSymbol(QgsFillSymbol.createSimple(
        {"color": "230,57,70,40", "outline_color": "230,57,70,255", "outline_width": "0.6"}))
    return lyr


def _zoom(plugin, lyr):
    if lyr.featureCount():
        ext = lyr.extent()
        ext.scale(1.4)
        canvas = plugin.iface.mapCanvas()
        try:
            from qgis.core import QgsCoordinateTransform

            tr_ = QgsCoordinateTransform(lyr.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance())
            ext = tr_.transformBoundingBox(ext)
        except Exception:  # noqa: BLE001
            pass
        canvas.setExtent(ext)
        canvas.refresh()


# ---- compare two copies -------------------------------------------------------------------------------------
class CompareDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.diff = None
        self.setWindowTitle(tr("Compare Two Copies"))
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Two copies of the same layer are compared square by square, by their fingerprints: "
                          "only the places that differ are opened. Then the changes of one can be brought into the other."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.a, self.b, self.key = QComboBox(), QComboBox(), QComboBox()
        for lyr in _vectors():
            self.a.addItem(lyr.name(), lyr.id())
            self.b.addItem(lyr.name(), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None and self.a.findData(act.id()) >= 0:
            self.a.setCurrentIndex(self.a.findData(act.id()))
            self.b.setCurrentIndex(1 if self.a.currentIndex() == 0 and self.b.count() > 1 else 0)
        form.addRow(tr("The newer copy"), self.a)
        form.addRow(tr("The copy to compare"), self.b)
        form.addRow(tr("The field that names a feature"), self.key)
        v.addLayout(form)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.go = QPushButton(tr("Compare"))
        self.bring_btn = QPushButton(tr("Bring the changes into the second copy"))
        self.bring_btn.setEnabled(False)
        bb.addButton(self.go, QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton(self.bring_btn, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.go.clicked.connect(self.compare)
        self.bring_btn.clicked.connect(self.bring)
        self.a.currentIndexChanged.connect(self._keys)
        self.b.currentIndexChanged.connect(self._keys)
        self._keys()

    def layers(self):
        p = QgsProject.instance()
        return p.mapLayer(self.a.currentData() or ""), p.mapLayer(self.b.currentData() or "")

    def _keys(self):
        self.key.clear()
        self.bring_btn.setEnabled(False)
        a, b = self.layers()
        self.key.addItem(tr("(none: a changed feature is one gone and one new)"), "")
        if a is None or b is None:
            return
        real = {f.name().lower(): f.name() for f in a.fields()}
        both = [n for n in real if n in {f.name().lower() for f in b.fields()}]
        for n in both:     # OBJECTID too: not data, but it can name a feature
            self.key.addItem(real[n], n)
        guess = core.guess_identity(both)
        if guess and self.key.findData(guess) >= 0:
            self.key.setCurrentIndex(self.key.findData(guess))

    def compare(self):
        a, b = self.layers()
        self.diff = None
        self.bring_btn.setEnabled(False)
        if a is None or b is None or a.id() == b.id():
            self.out.setText(tr("Choose two different layers."))
            return
        fields = core.common_fields(a, b)
        key = self.key.currentData() or ""
        try:
            ta = core.build(a, fields, identity=key)
            tb = core.build(b, fields, identity=key, cell=ta.cell, step=ta.step)
            d = core.compare(ta, tb)
        except core.FingerprintError as e:
            self.out.setText(_why(e))
            return
        if d.same:
            self.out.setText(tr("The two copies are the same: {n} features, one fingerprint compared.").format(n=ta.count))
            return
        self.diff, self.trees = d, (ta, tb)
        text = tr("They differ in {c} squares, found by {k} fingerprints compared instead of reading {n} features.").format(
            c=len(d.cells), k=d.compared, n=ta.count + tb.count)
        text += " " + tr("Only in the newer copy: {a}. Only in the other: {b}. Changed: {m}.").format(
            a=len(d.only_a), b=len(d.only_b), m=len(d.changed))
        self.out.setText(text)
        self._show(a, b, ta, d)
        self.bring_btn.setEnabled(True)

    def _show(self, a, b, ta, d):
        p = QgsProject.instance()
        sq = cells_layer(ta.cell, ta.crs, d.cells, tr("Where {a} and {b} differ").format(a=a.name(), b=b.name()))
        kind = QgsWkbTypes.displayString(a.wkbType())
        lyr = QgsVectorLayer(f"{kind}?crs={ta.crs}&field=change:string", tr("Differences: {a} / {b}").format(
            a=a.name(), b=b.name()), "memory")
        feats = []
        groups = ((a, d.only_a, tr("only in {x}").format(x=a.name())), (b, d.only_b, tr("only in {x}").format(x=b.name())),
                  (a, [fa for fa, _fb in d.changed], tr("changed")))
        for src, fids, label in groups:
            if not fids:
                continue
            from qgis.core import QgsFeatureRequest

            for f in src.getFeatures(QgsFeatureRequest().setFilterFids(list(fids))):
                nf = QgsFeature(lyr.fields())
                nf.setGeometry(QgsGeometry(f.geometry()))
                nf.setAttributes([label])
                feats.append(nf)
        lyr.dataProvider().addFeatures(feats)
        p.addMapLayer(sq)
        p.addMapLayer(lyr)
        _zoom(self.plugin, sq)

    def bring(self):
        a, b = self.layers()
        if self.diff is None or a is None or b is None:
            return
        if QMessageBox.question(self, self.windowTitle(), tr(
                "Make {b} like {a} in the {c} squares that differ? The changes go into {b}'s edits: "
                "undo them or save them as any edit.").format(a=a.name(), b=b.name(), c=len(self.diff.cells))) \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            r = core.bring(self.diff, a, b)
        except core.FingerprintError as e:
            self.out.setText(_why(e))
            return
        ta, _tb = self.trees
        same = core.build(b, ta.fields, identity=ta.identity, cell=ta.cell, step=ta.step).root == ta.root
        self.out.setText(tr("{x} added, {y} removed, {z} changed in {b}.").format(
            x=r["added"], y=r["removed"], z=r["changed"], b=b.name()) + " " +
            (tr("The two copies are now the same.") if same else
             tr("Some differences remain (a field the other copy does not have, or a shape it cannot hold).")))
        self.bring_btn.setEnabled(False)
        self.diff = None


def compare_ui(plugin):
    if len(_vectors()) < 2:
        _bar(plugin, tr("Add the two copies of the layer to the map first."), Qgis.MessageLevel.Warning, 8)
        return None
    dlg = CompareDialog(plugin)
    dlg.show()
    plugin._fingerprint_dialog = dlg
    return dlg


# ---- the certificate ----------------------------------------------------------------------------------------
def _chosen(plugin):
    sel = [l for l in plugin.iface.layerTreeView().selectedLayers() if isinstance(l, QgsVectorLayer)]
    if not sel and isinstance(plugin.iface.activeLayer(), QgsVectorLayer):
        sel = [plugin.iface.activeLayer()]
    return sel


def certificate_ui(plugin, path=None):
    layers = _chosen(plugin)
    if not layers:
        _bar(plugin, tr("Select the layers of the delivery in the Layers panel first."), Qgis.MessageLevel.Warning, 8)
        return None
    if path is None:
        path, _f = QFileDialog.getSaveFileName(plugin.iface.mainWindow(), tr("Delivery Certificate"),
                                               os.path.join(_dir(), "delivery" + PROOF), tr("Certificate") + f" (*{PROOF})")
        if not path:
            return None
    if not path.endswith(".json"):
        path += PROOF
    _remember(path)
    from . import passport
    cert = core.certificate([(l.name(), core.build(l, commit=True, sums=True)) for l in layers])
    cert = passport.attach(cert, layers)
    core.write_json(cert, path)
    text = tr("Certificate of {n} layers written. Its code is {c}: whoever receives the data checks it "
              "against the certificate, and if anything changed it shows where.").format(n=len(layers), c=cert["code"])
    lines = passport_lines(cert)
    if lines:
        text += " " + tr("With its quality passport: {p}").format(p="; ".join(lines))
    _bar(plugin, text, seconds=0)
    return cert


def _rule_text(r):
    from ..georules.guard import describe
    return describe({"kind": r["kind"], "params": r.get("params", {})})


def passport_lines(cert, now=None):
    """The passport of a certificate in sentences; with ``now`` ({layer name: layer}) checked again on the data."""
    from . import passport
    out = []
    for e in cert.get("layers", []):
        rules = (e.get("passport") or {}).get("rules") or []
        if not rules:
            continue
        again = dict((id(r), n) for r, n in passport.again(now[e["name"]], e)) if now and now.get(e["name"]) else {}
        for r in rules:
            b = r.get("broken")
            line = (tr("{l}: {r}: not checked at delivery (a layer it needs was not there)") if b is None else
                    tr("{l}: {r}: broken by {b} of {n} at delivery")).format(
                l=e["name"], r=_rule_text(r), b=b, n=r.get("of", "?"))
            if id(r) in again and again[id(r)] is not None and again[id(r)] != b:
                line += " " + tr("(now {k})").format(k=again[id(r)])
            out.append(line)
    return out


def _layer_for(plugin, name, title):
    found = [l for l in QgsProject.instance().mapLayersByName(name) if isinstance(l, QgsVectorLayer)]
    if found:
        return found[0]
    vs = _vectors()
    if not vs:
        return None
    item, ok = QInputDialog.getItem(plugin.iface.mainWindow(), title,
                                    tr("Which layer is «{n}»?").format(n=name), [l.name() for l in vs], 0, False)
    return vs[[l.name() for l in vs].index(item)] if ok else None


def check_ui(plugin, path=None):
    if path is None:
        path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Check Against a Certificate"), _dir(),
                                               tr("Certificate") + f" (*{PROOF} *.json)")
        if not path:
            return None
    _remember(path)
    try:
        cert = core.read_json(path)
        assert cert.get("kind") == "biruni-fingerprint"
    except Exception:  # noqa: BLE001
        _bar(plugin, tr("This is not a delivery certificate."), Qgis.MessageLevel.Warning, 8)
        return None
    from . import passport
    lines, bad = [], 0
    forged = passport.verify(cert)
    if forged:
        bad += 1
        lines.append(tr("The certificate itself was altered: its code is not the one its contents make.") if forged == "code"
                     else tr("The certificate's quality passport was altered after it was made."))
    found = {}
    for entry in cert["layers"]:
        lyr = _layer_for(plugin, entry["name"], tr("Check Against a Certificate"))
        if lyr is None:
            lines.append(tr("{n}: not on the map.").format(n=entry["name"]))
            bad += 1
            continue
        found[entry["name"]] = lyr
        res = core.check(lyr, entry)
        if res["same"]:
            lines.append(tr("{n}: the same as delivered ({f} features).").format(n=entry["name"], f=res["features"]))
            continue
        bad += 1
        if res.get("fields"):
            lines.append(tr("{n}: fields missing: {f}.").format(n=entry["name"], f=", ".join(res["fields"])))
        elif res.get("crs"):
            lines.append(tr("{n}: in another coordinate system.").format(n=entry["name"]))
        elif res.get("reach"):
            lines.append(tr("{n}: the data is as delivered, but the certificate's boxes are not the data's: "
                            "the certificate was altered.").format(n=entry["name"]))
        else:
            lines.append(tr("{n}: differs in {c} squares, shown on the map.").format(n=entry["name"], c=len(res["cells"])))
            sq = cells_layer(entry["cell"], entry["crs"], res["cells"],
                             tr("{n}: not as delivered").format(n=entry["name"]))
            QgsProject.instance().addMapLayer(sq)
            _zoom(plugin, sq)
    pl = passport_lines(cert, found)
    if pl:
        lines.append("")
        lines.append(tr("Quality passport:"))
        lines += pl
    head = (tr("The data is what the certificate {c} says.") if not bad else
            tr("The data is not all what the certificate {c} says.")).format(c=cert["code"])
    QMessageBox.information(plugin.iface.mainWindow(), tr("Check Against a Certificate"), head + "\n\n" + "\n".join(lines))
    return bad == 0


# ---- a package of changes --------------------------------------------------------------------------------------
def package_ui(plugin):
    lyr = plugin.iface.activeLayer()
    if not isinstance(lyr, QgsVectorLayer):
        _bar(plugin, tr("Choose the newer copy of the layer first."), Qgis.MessageLevel.Warning, 8)
        return None
    mw = plugin.iface.mainWindow()
    cpath, _f = QFileDialog.getOpenFileName(mw, tr("The certificate of the other copy"), _dir(),
                                            tr("Certificate") + f" (*{PROOF} *.json)")
    if not cpath:
        return None
    _remember(cpath)
    cert = core.read_json(cpath)
    entries = cert.get("layers") or []
    entry = next((e for e in entries if e["name"] == lyr.name()), entries[0] if len(entries) == 1 else None)
    if entry is None:
        names = [e["name"] for e in entries]
        item, ok = QInputDialog.getItem(mw, tr("Changes Package"), tr("Which layer of the certificate is this one?"),
                                        names, 0, False)
        if not ok:
            return None
        entry = entries[names.index(item)]
    try:
        pkg = core.make_package(lyr, entry)
    except core.FingerprintError as e:
        _bar(plugin, _why(e), Qgis.MessageLevel.Warning, 10)
        return None
    if pkg is None:
        _bar(plugin, tr("The other copy is already the same as this one: nothing to carry."))
        return None
    path, _f = QFileDialog.getSaveFileName(mw, tr("Changes Package"), os.path.join(_dir(), lyr.name() + CHANGES),
                                           tr("Changes package") + f" (*{CHANGES})")
    if not path:
        return None
    if not path.endswith(CHANGES):
        path += CHANGES
    core.write_json(pkg, path)
    n = sum(len(c["features"]) for c in pkg["cells"].values())
    _bar(plugin, tr("{c} changed squares with {n} features written ({kb} KB). Carry the file to the other copy "
                    "and apply it there.").format(c=len(pkg["cells"]), n=n, kb=max(1, round(os.path.getsize(path) / 1024))))
    return path


def apply_ui(plugin):
    lyr = plugin.iface.activeLayer()
    if not isinstance(lyr, QgsVectorLayer):
        _bar(plugin, tr("Choose the layer to bring the changes into first."), Qgis.MessageLevel.Warning, 8)
        return None
    path, _f = QFileDialog.getOpenFileName(plugin.iface.mainWindow(), tr("Apply a Changes Package"), _dir(),
                                           tr("Changes package") + f" (*{CHANGES})")
    if not path:
        return None
    _remember(path)
    try:
        pkg = core.read_json(path)
        assert pkg.get("kind") == "biruni-changes"
        r = core.apply_package(lyr, pkg)
    except core.FingerprintError as e:
        _bar(plugin, _why(e), Qgis.MessageLevel.Warning, 10)
        return None
    except Exception:  # noqa: BLE001
        _bar(plugin, tr("This is not a changes package."), Qgis.MessageLevel.Warning, 8)
        return None
    text = tr("{x} features added and {y} removed, in the layer's edits: save them to keep them.").format(
        x=r["added"], y=r["removed"])
    level = Qgis.MessageLevel.Success
    if r["left"]:
        text += " " + tr("{n} squares changed here too since the certificate: they are left as they are, shown on the map.").format(
            n=len(r["left"]))
        sq = cells_layer(pkg["settings"]["cell"], pkg["settings"]["crs"], r["left"], tr("Changed on both sides"))
        QgsProject.instance().addMapLayer(sq)
        level = Qgis.MessageLevel.Warning
    _bar(plugin, text, level, 0)
    return r
