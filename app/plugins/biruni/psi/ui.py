"""The private match dialog: three steps, by files (e-mail, a flash drive), no server."""
import os

from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFileDialog, QFormLayout, QLabel, QListWidget, QListWidgetItem, QProgressDialog,
                                 QPushButton, QTabWidget, QVBoxLayout, QWidget)

from ..fingerprint.ui import _bar, _dir, _remember, _vectors
from ..i18n import tr
from . import core


def _why(e):
    s = str(e)
    return {
        "fields": tr("Choose as many fields as the other side's key has, in the same order."),
        "empty": tr("No feature has a key: the fields are empty, or there are no shapes."),
        "format": tr("This is not a file of this step."),
        "altered": tr("This file was changed after it was made: ask for it again."),
        "session": tr("This answer is to a match not started on this computer."),
        "not ours": tr("This file answers another file, not the one made here."),
        "finished": tr("This match is already finished."),
        "cancelled": tr("Cancelled."),
    }.get(s, s)


def _key_text(desc):
    if desc.get("mode") == "fields":
        return tr("the values of {f}").format(f=", ".join(f"«{n}»" for n in desc["fields"]))
    return tr("the place: the centre of each shape, within {t}").format(t=_dist(desc))


def _dist(desc):
    from qgis.core import QgsCoordinateReferenceSystem
    t = float(desc.get("tolerance", 0))
    if QgsCoordinateReferenceSystem(desc.get("crs", "")).isGeographic():
        return tr("{m} m").format(m=round(t * 111320.0, 2))
    return tr("{m} m").format(m=round(t, 2))


class _Progress:
    def __init__(self, parent, text):
        self.dlg = QProgressDialog(text, tr("Cancel"), 0, 100, parent)
        self.dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.dlg.setMinimumDuration(400)

    def __call__(self, pct):
        self.dlg.setValue(int(pct))
        QApplication.processEvents()
        return not self.dlg.wasCanceled()

    def close(self):
        self.dlg.reset()


def _layer_combo():
    c = QComboBox()
    for lyr in _vectors():
        c.addItem(lyr.name(), lyr.id())
    return c


def _layer(combo):
    return QgsProject.instance().mapLayer(combo.currentData() or "")


def _save(parent, title, name, step):
    path, _f = QFileDialog.getSaveFileName(parent, title, os.path.join(_dir(), name + core.SUFFIX[step]),
                                           tr("Private match") + f" (*{core.SUFFIX[step]})")
    if path and not path.endswith(core.SUFFIX[step]):
        path += core.SUFFIX[step]
    return path


class PrivateMatchDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.file1 = None
        self.setWindowTitle(tr("Private Match"))
        self.setMinimumWidth(500)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Find the features you and another party both have (the same parcel number, the same "
                          "place) without either of you seeing the other's list. You trade small files: you start, "
                          "they answer, you finish. Only the features in common are found; nothing else is learned "
                          "but how many each side has."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._start_tab(), tr("1. Start"))
        self.tabs.addTab(self._answer_tab(), tr("2. Answer"))
        self.tabs.addTab(self._finish_tab(), tr("3. Finish"))
        v.addWidget(self.tabs)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        self.out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # ---- 1 --------------------------------------------------------------------------------------------
    def _start_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.s_layer = _layer_combo()
        act = self.plugin.iface.activeLayer()
        if act is not None and self.s_layer.findData(act.id()) >= 0:
            self.s_layer.setCurrentIndex(self.s_layer.findData(act.id()))
        f.addRow(tr("Your layer"), self.s_layer)
        self.s_mode = QComboBox()
        self.s_mode.addItem(tr("The values of fields"), "fields")
        self.s_mode.addItem(tr("The place of the shapes"), "shape")
        f.addRow(tr("Match by"), self.s_mode)
        self.s_fields = QListWidget()
        self.s_fields.setMinimumHeight(110)
        f.addRow(tr("Fields"), self.s_fields)
        self.s_cell = QDoubleSpinBox()
        self.s_cell.setRange(0.1, 1000)
        self.s_cell.setDecimals(1)
        self.s_cell.setValue(1.0)
        self.s_cell.setSuffix(" " + tr("m"))
        self.s_cell.setToolTip(tr("Two centres closer than half of this meet; farther than this they never do."))
        f.addRow(tr("Grid"), self.s_cell)
        self.s_only = QCheckBox(tr("Only the selected features"))
        f.addRow("", self.s_only)
        go = QPushButton(tr("Make File 1…"))
        go.clicked.connect(lambda: self.start())
        f.addRow("", go)
        self.s_layer.currentIndexChanged.connect(lambda _i: self._fill_start())
        self.s_mode.currentIndexChanged.connect(lambda _i: self._mode())
        self._fill_start()
        self._mode()
        return w

    def _fill_start(self):
        self.s_fields.clear()
        lyr = _layer(self.s_layer)
        if lyr is None:
            return
        for fl in lyr.fields():
            it = QListWidgetItem(fl.name())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.s_fields.addItem(it)
        self.s_only.setEnabled(lyr.selectedFeatureCount() > 0)

    def _mode(self):
        fields = self.s_mode.currentData() == "fields"
        self.s_fields.setEnabled(fields)
        self.s_cell.setEnabled(not fields)

    def set_start_fields(self, names):
        for i in range(self.s_fields.count()):
            it = self.s_fields.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.text() in names else Qt.CheckState.Unchecked)

    def start(self, path=None):
        lyr = _layer(self.s_layer)
        if lyr is None:
            self.out.setText(tr("Choose the layer."))
            return None
        if self.s_mode.currentData() == "fields":
            names = [self.s_fields.item(i).text() for i in range(self.s_fields.count())
                     if self.s_fields.item(i).checkState() == Qt.CheckState.Checked]
            if not names:
                self.out.setText(tr("Tick the fields that make the key."))
                return None
            spec = {"mode": "fields", "fields": names}
        else:
            spec = core.shape_spec(lyr, self.s_cell.value())
        fids = lyr.selectedFeatureIds() if self.s_only.isChecked() and self.s_only.isEnabled() else None
        path = path or _save(self, tr("File 1"), lyr.name(), 1)
        if not path:
            return None
        _remember(path)
        prog = _Progress(self, tr("Preparing the keys…"))
        try:
            r = core.start(lyr, spec, path, fids, label=lyr.name(), feedback=prog)
        except core.PsiError as e:
            self.out.setText(_why(e))
            return None
        finally:
            prog.close()
        text = tr("File 1 made: {n} keys of «{l}», none readable. Send it to the other side; when their answer "
                  "(file 2) comes, open it in «3. Finish».").format(n=r.mine, l=lyr.name())
        self.out.setText(text + " " + _speed(r))
        return r

    # ---- 2 --------------------------------------------------------------------------------------------
    def _answer_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        pick = QPushButton(tr("Open File 1…"))
        pick.clicked.connect(lambda: self.open_file1())
        self.a_what = QLabel(tr("(no file yet)"))
        self.a_what.setWordWrap(True)
        f.addRow(pick, self.a_what)
        self.a_layer = _layer_combo()
        f.addRow(tr("Your layer"), self.a_layer)
        self.a_box = QWidget()
        self.a_form = QFormLayout(self.a_box)
        self.a_form.setContentsMargins(0, 0, 0, 0)
        f.addRow(self.a_box)
        self.a_only = QCheckBox(tr("Only the selected features"))
        f.addRow("", self.a_only)
        go = QPushButton(tr("Make File 2…"))
        go.clicked.connect(lambda: self.answer())
        f.addRow("", go)
        self.a_combos = []
        self.a_layer.currentIndexChanged.connect(lambda _i: self._fill_answer())
        return w

    def open_file1(self, path=None):
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, tr("File 1"), _dir(),
                                                   tr("Private match") + f" (*{core.SUFFIX[1]})")
            if not path:
                return None
        _remember(path)
        try:
            d = core.describe(path)
        except core.PsiError as e:
            self.a_what.setText(_why(e))
            self.file1 = None
            return None
        self.file1 = (path, d)
        self.a_what.setText(tr("{l}: {n} keys, made of {k}.").format(
            l=d["label"] or tr("The other side"), n=d["count"], k=_key_text(d["key"])))
        self._fill_answer()
        return d

    def _fill_answer(self):
        while self.a_form.rowCount():
            self.a_form.removeRow(0)
        self.a_combos = []
        lyr = _layer(self.a_layer)
        if self.file1 is None or lyr is None or self.file1[1]["key"]["mode"] != "fields":
            return
        names = [fl.name() for fl in lyr.fields()]
        for theirs in self.file1[1]["key"]["fields"]:
            c = QComboBox()
            c.addItems(names)
            low = [n.lower() for n in names]
            if theirs.lower() in low:
                c.setCurrentIndex(low.index(theirs.lower()))
            self.a_form.addRow(tr("Yours for «{f}»").format(f=theirs), c)
            self.a_combos.append(c)
        self.a_only.setEnabled(lyr.selectedFeatureCount() > 0)

    def set_answer_fields(self, names):
        for c, n in zip(self.a_combos, names):
            c.setCurrentIndex(c.findText(n))

    def answer(self, path=None):
        lyr = _layer(self.a_layer)
        if self.file1 is None or lyr is None:
            self.out.setText(tr("Open file 1 and choose your layer."))
            return None
        fields = [c.currentText() for c in self.a_combos]
        fids = lyr.selectedFeatureIds() if self.a_only.isChecked() and self.a_only.isEnabled() else None
        path = path or _save(self, tr("File 2"), lyr.name(), 2)
        if not path:
            return None
        _remember(path)
        prog = _Progress(self, tr("Answering…"))
        try:
            r = core.answer(self.file1[0], lyr, path, fields, fids, feedback=prog)
        except core.PsiError as e:
            self.out.setText(_why(e))
            return None
        finally:
            prog.close()
        text = tr("File 2 made from {n} keys of «{l}». Send it back. You learned only that they have {t} keys; "
                  "if they send file 3, open it in «3. Finish» to find yours in common.").format(
            n=r.mine, l=lyr.name(), t=r.theirs)
        self.out.setText(text + " " + _speed(r))
        return r

    # ---- 3 --------------------------------------------------------------------------------------------
    def _finish_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.f_tell = QCheckBox(tr("Make file 3 too, so they find theirs in common"))
        f.addRow("", self.f_tell)
        go = QPushButton(tr("Open File 2 or 3…"))
        go.clicked.connect(lambda: self.finish())
        f.addRow("", go)
        return w

    def finish(self, path=None, path3=None):
        if path is None:
            path, _f = QFileDialog.getOpenFileName(
                self, tr("Finish"), _dir(), tr("Private match") + f" (*{core.SUFFIX[2]} *{core.SUFFIX[3]})")
            if not path:
                return None
        _remember(path)
        if path.endswith(core.SUFFIX[3]):
            try:
                r = core.finish_answer(path)
                st = core.load_session(r.session, "answer")
            except core.PsiError as e:
                self.out.setText(_why(e))
                return None
            self._select(st, r.fids)
            self.out.setText(tr("{n} of your features are in common with theirs: selected on the map.").format(
                n=len(r.fids)))
            return r
        if self.f_tell.isChecked() and path3 is None:
            path3 = path[:-len(core.SUFFIX[2])] + core.SUFFIX[3] if path.endswith(core.SUFFIX[2]) else \
                path + core.SUFFIX[3]
        prog = _Progress(self, tr("Finding the common keys…"))
        try:
            r = core.finish(path, path3=path3, feedback=prog)
            st = core.load_session(r.session, "start")
        except core.PsiError as e:
            self.out.setText(_why(e))
            return None
        finally:
            prog.close()
        lyr = self._select(st, r.fids)
        text = tr("{n} of your features are in common with theirs (they have {t} keys): selected on the map.").format(
            n=len(r.fids), t=r.theirs)
        if lyr is None:
            text += " " + tr("Add «{l}» to the map to see them.").format(l=st.get("layer", ""))
        if r.path:
            text += " " + tr("File 3 made: send it to them.") + f" ({os.path.basename(r.path)})"
        self.out.setText(text + " " + _speed(r))
        _bar(self.plugin, text, seconds=0)
        return r

    @staticmethod
    def _select(st, fids):
        p = QgsProject.instance()
        lyr = p.mapLayer(st.get("layer_id", "")) or next(iter(p.mapLayersByName(st.get("layer", ""))), None)
        if lyr is not None:
            lyr.selectByIds(list(fids))
        return lyr


def _speed(r):
    if r.seconds < 1 or not r.rate:
        return ""
    return tr("({s} s, {r} keys a second)").format(s=round(r.seconds, 1), r=int(r.rate))


def open_ui(plugin):
    if not _vectors():
        _bar(plugin, tr("Add the layer to the map first."), Qgis.MessageLevel.Warning, 8)
        return None
    dlg = PrivateMatchDialog(plugin)
    dlg.show()
    plugin._psi_dialog = dlg
    return dlg
