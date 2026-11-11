"""The time machine: a shared layer as it was at any moment, and what was there brought back.

The room's record keeps, for every change, the feature as it was before it ("prev"). So the layer at a moment is
the layer now, with every feature changed after that moment put back to how it was before its first change
since: a feature drawn after it is not there, one deleted after it is there again. Nothing is copied or kept for
this; any moment since the room opened can be seen, played forward change by change, and features picked from it
are brought back as a change of their own (which everyone gets, and which can itself be taken back).
"""
import datetime as dt
import time

from qgis.core import Qgis, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
                                 QVBoxLayout)

from ..edit_journal import decode
from ..i18n import tr
from .store import OP_COLS

STEPS = 1000


def moments(store, key):
    """The times of the layer's changes, oldest first."""
    with store._lock:
        rows = store.con.execute("SELECT ts FROM ops WHERE layer=? AND kind!='field' ORDER BY ts", (key,)).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def state_at(store, key, ts):
    """{fid: the feature as it was at ts (None: it did not exist)} for every feature changed after ts."""
    rows = store._rows(f"SELECT {', '.join(OP_COLS)} FROM ops WHERE layer=? AND ts>? AND kind!='field' ORDER BY seq",
                       (key, ts))
    out = {}
    for e in rows:
        if e["fid"] not in out:
            out[e["fid"]] = e["prev"]
    return out


def _feature(fields, keys, fid, state):
    f = QgsFeature(fields)
    g = QgsGeometry()
    if state.get("g"):
        g.fromWkb(bytes.fromhex(state["g"]))
    f.setGeometry(g)
    a = state.get("a") or {}
    for i, fl in enumerate(fields):
        if i in keys:
            f.setAttribute(i, int(fid))
        elif fl.name() in a:
            f.setAttribute(i, decode(a[fl.name()]))
    return f


def layer_at(layer, store, key, ts, name):
    """(memory layer of ``layer`` at ts, {its fid: (the shared layer's fid, state)}, counts)."""
    past = state_at(store, key, ts)
    crs = layer.crs().authid() or layer.crs().toWkt()
    out = QgsVectorLayer(f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={crs}", name, "memory")
    out.dataProvider().addAttributes(list(layer.fields()))
    out.updateFields()
    fields = out.fields()
    keys = set(layer.primaryKeyAttributes())
    feats, origin = [], []
    now_ids = set()
    for f in layer.dataProvider().getFeatures():
        now_ids.add(f.id())
        if f.id() in past:
            continue
        nf = QgsFeature(fields)
        nf.setGeometry(QgsGeometry(f.geometry()))
        nf.setAttributes(f.attributes())
        feats.append(nf)
        origin.append((f.id(), None))
    back = changed = 0
    for fid, st in past.items():
        if st is None:
            continue
        feats.append(_feature(fields, keys, fid, st))
        origin.append((fid, st))
        if fid in now_ids:
            changed += 1
        else:
            back += 1
    ok, added = out.dataProvider().addFeatures(feats)
    mapping = {a.id(): o for a, o in zip(added, origin)} if ok else {}
    new = sum(1 for fid, st in past.items() if st is None and fid in now_ids)
    return out, mapping, {"changed": changed, "deleted_since": back, "added_since": new}


class TimeMachine(QDialog):
    """A slider from the room's first change to now: the layer as it was, played forward, brought back."""

    def __init__(self, parent, node, iface):
        super().__init__(parent)
        self.node, self.iface = node, iface
        self.setWindowTitle(tr("Time Machine"))
        self.setMinimumWidth(460)
        self.ghost, self.mapping, self.hidden = None, {}, None
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.layer = QComboBox()
        for key, lyr in node.layers.items():
            self.layer.addItem(lyr.name(), key)
        form.addRow(tr("Layer"), self.layer)
        v.addLayout(form)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, STEPS)
        self.slider.setValue(STEPS)
        v.addWidget(self.slider)
        self.when = QLabel("")
        self.when.setWordWrap(True)
        v.addWidget(self.when)
        row = QHBoxLayout()
        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setToolTip(tr("The change before"))
        self.play_btn = QPushButton(tr("Play"))
        self.next_btn = QPushButton("⏭")
        self.next_btn.setToolTip(tr("The change after"))
        self.bring_btn = QPushButton(tr("Bring back the selected features"))
        self.now_btn = QPushButton(tr("Now"))
        for b in (self.prev_btn, self.play_btn, self.next_btn, self.now_btn):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.bring_btn)
        v.addLayout(row)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self.show_moment)
        self._player = QTimer(self)
        self._player.setInterval(350)
        self._player.timeout.connect(self._play_step)
        self.slider.valueChanged.connect(lambda _v: self._debounce.start())
        self.layer.currentIndexChanged.connect(self._layer_changed)
        self.play_btn.clicked.connect(self.play)
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        self.now_btn.clicked.connect(lambda: self.slider.setValue(STEPS))
        self.bring_btn.clicked.connect(self.bring_back)
        self._layer_changed()

    # ---- time -----------------------------------------------------------------------------------------------
    def key(self):
        return self.layer.currentData()

    def _layer_changed(self):
        self._player.stop()
        self.times = moments(self.node.store, self.key()) if self.key() else []
        self.t0 = (self.times[0] - 1.0) if self.times else time.time() - 60
        self.slider.setValue(STEPS)
        self.show_moment()

    def ts(self):
        v = self.slider.value()
        if v >= STEPS:
            return None
        return self.t0 + (time.time() - self.t0) * v / STEPS

    def set_ts(self, ts):
        span = max(1e-6, time.time() - self.t0)
        self.slider.blockSignals(True)
        self.slider.setValue(max(0, min(STEPS - 1, int(round((ts - self.t0) / span * STEPS)))))
        self.slider.blockSignals(False)
        self._at = ts
        self.show_moment(ts)

    def step(self, d):
        cur = self._at if getattr(self, "_at", None) is not None else time.time()
        if d < 0:
            before = [t for t in self.times if t < cur - 1e-6]
            if before:
                self.set_ts(before[-1] - 1e-3)
        else:
            after = [t for t in self.times if t > cur + 1e-6]
            if after:
                self.set_ts(after[0] + 1e-3)
            else:
                self.slider.setValue(STEPS)

    def play(self):
        if self._player.isActive():
            self._player.stop()
            self.play_btn.setText(tr("Play"))
            return
        if self.ts() is None and self.times:
            self.set_ts(self.t0)
        self.play_btn.setText(tr("Stop"))
        self._player.start()

    def _play_step(self):
        cur = self._at if getattr(self, "_at", None) is not None else time.time()
        if not [t for t in self.times if t > cur + 1e-6]:
            self._player.stop()
            self.play_btn.setText(tr("Play"))
            self.slider.setValue(STEPS)
            return
        self.step(1)

    # ---- the map ------------------------------------------------------------------------------------------------
    def _drop_ghost(self):
        if self.ghost is not None:
            try:
                QgsProject.instance().removeMapLayer(self.ghost.id())
            except RuntimeError:
                pass
        self.ghost, self.mapping = None, {}

    def _show_original(self, on):
        lyr = self.node.layers.get(self.key()) if self.key() else None
        root = QgsProject.instance().layerTreeRoot()
        if self.hidden is not None and (on or (lyr is not None and self.hidden.id() != lyr.id())):
            node = root.findLayer(self.hidden.id())
            if node is not None:
                node.setItemVisibilityChecked(True)
            self.hidden = None
        if not on and lyr is not None:
            node = root.findLayer(lyr.id())
            if node is not None and node.isVisible():
                node.setItemVisibilityChecked(False)
                self.hidden = lyr

    def show_moment(self, ts=None):
        ts = self.ts() if ts is None else ts
        self._at = ts
        key = self.key()
        lyr = self.node.layers.get(key) if key else None
        self._drop_ghost()
        if lyr is None:
            return
        if ts is None:
            self._show_original(True)
            self.when.setText(tr("Now. {n} changes recorded since the room opened.").format(n=len(self.times)))
            self.bring_btn.setEnabled(False)
            return
        stamp = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        ghost, self.mapping, c = layer_at(lyr, self.node.store, key, ts, tr("{l} at {t}").format(l=lyr.name(), t=stamp))
        try:
            ghost.setRenderer(lyr.renderer().clone())
            if lyr.labelsEnabled() and lyr.labeling() is not None:
                ghost.setLabeling(lyr.labeling().clone())
                ghost.setLabelsEnabled(True)
        except Exception:  # noqa: BLE001 - a style that does not fit: the default one
            pass
        ghost.setReadOnly(True)
        QgsProject.instance().addMapLayer(ghost)
        self.ghost = ghost
        self._show_original(False)
        after = sum(1 for t in self.times if t > ts)
        self.when.setText(tr("{t}: {a} changes after this moment. Compared with now: {c} were different, {d} were "
                             "deleted since, {n} did not exist yet.").format(
            t=stamp, a=after, c=c["changed"], d=c["deleted_since"], n=c["added_since"]))
        self.bring_btn.setEnabled(True)
        self.iface.mapCanvas().refresh()

    def bring_back(self):
        """The features selected on the past layer become what they were: a change like any other."""
        if self.ghost is None:
            return 0
        sel = self.ghost.selectedFeatureIds()
        if not sel:
            self.when.setText(self.when.text().split(" — ")[0] + " — " +
                              tr("select features on the past layer first."))
            return 0
        n = 0
        for mid in sel:
            fid, st = self.mapping.get(mid, (None, None))
            if fid is not None and st is not None and self.node.submit(self.key(), fid, st, note="restore"):
                n += 1
        self.iface.messageBar().pushMessage(tr("Time Machine"), tr(
            "{n} features brought back as they were. Everyone gets them; they can be taken back like any change.").format(n=n),
            Qgis.MessageLevel.Success, 10)
        self.show_moment(self._at)
        return n

    def done(self, r):
        self._player.stop()
        self._drop_ghost()
        self._show_original(True)
        super().done(r)
