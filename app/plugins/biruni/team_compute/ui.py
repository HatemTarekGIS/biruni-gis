"""The windows of team computing: run a heavy job on the room's computers, and share this computer with the room."""
import os
import tempfile
import zlib

from qgis.core import (Qgis, QgsFeature, QgsField, QgsGeometry, QgsProject, QgsRasterLayer, QgsVectorLayer,
                       QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QPainter
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QListWidget, QMessageBox, QProgressBar,
                                 QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .. import team
from ..i18n import tr
from . import jobs, owner, worker

LABELS = {
    "MIN_AREA": "Ignore overlaps and gaps smaller than (layer units²)",
    "MAX_GAP": "Ignore gaps larger than (layer units², 0 = no limit)",
    "DISTANCE": "Distance (layer units)",
    "SEGMENTS": "Segments",
    "END_CAP_STYLE": "End cap style",
    "JOIN_STYLE": "Join style",
    "MITER_LIMIT": "Miter limit",
    "Z_FACTOR": "Z factor",
    "AZIMUTH": "Azimuth (°)",
    "V_ANGLE": "Vertical angle (°)",
}
CHOICES = {"END_CAP_STYLE": ("Round", "Flat", "Square"), "JOIN_STYLE": ("Round", "Miter", "Bevel")}
LIMIT_TEXT = {"light": "Light: one tile at a time, lowest priority",
              "half": "Half: half of the processor",
              "full": "Full: all the processor but one core"}
LIMIT_SHORT = {"light": "Light", "half": "Half", "full": "Full"}
STATE_COLOR = {"waiting": "#9aa3ad", "working": "#3b82c4", "checking": "#d99a1e", "done": "#3d9a5b"}


def _worker_color(who):
    if who == "here":
        return QColor("#2f6f4f")
    h = zlib.crc32(who.encode("utf-8")) % 360
    return QColor.fromHsv(h, 150, 200)


class LocalLink:
    """No room: a board of this computer's own (every tile is computed here)."""

    def __init__(self):
        self.board = jobs.Board()
        self.me = {"id": "local", "name": ""}

    def call(self, path, obj):
        status, out = self.board.route(path, obj, self.me)
        if status != 200:
            raise jobs.JobError(out.get("error", "refused"))
        return out


class TileMap(QWidget):
    """The tiles of a job, coloured by state; a tile being computed has the colour of its computer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 180)
        self.rects = {}
        self.states = {}

    def set_tiles(self, tiles):
        rects = {}
        for t in tiles:
            d = t["data"]
            if "core" in d:
                x0, y0, x1, y1 = d["core"]
                rects[t["tid"]] = (x0, -y1, x1 - x0, y1 - y0)
            else:
                x, y, w, h = t["pos"]
                rects[t["tid"]] = (x, y, w, h)
        self.rects = rects
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        if not self.rects:
            return
        p = QPainter(self)
        xs = [r[0] for r in self.rects.values()] + [r[0] + r[2] for r in self.rects.values()]
        ys = [r[1] for r in self.rects.values()] + [r[1] + r[3] for r in self.rects.values()]
        x0, y0, w, h = min(xs), min(ys), max(max(xs) - min(xs), 1e-9), max(max(ys) - min(ys), 1e-9)
        s = min((self.width() - 8) / w, (self.height() - 8) / h)
        for tid, (x, y, rw, rh) in self.rects.items():
            state, who = self.states.get(tid, ("waiting", ""))
            col = _worker_color(who) if state == "working" and who else QColor(STATE_COLOR.get(state, "#9aa3ad"))
            p.fillRect(QRectF(4 + (x - x0) * s, 4 + (y - y0) * s, max(rw * s - 1, 1), max(rh * s - 1, 1)), col)
        p.end()


class RunDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.node = team.current()
        self.job = None
        self.meta = None
        self.tiles = []
        self.out_path = ""
        self.setWindowTitle(tr("Team Computing"))
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Runs a heavy tool on the computers of the team room: the work is cut into tiles, the "
                          "members who share their computer take tiles while they are not editing, and this computer "
                          "works too. Every tile of a new member, and a share of the others, is computed twice and "
                          "compared; the answer is the same as on one computer."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.task = QComboBox()
        for k, spec in jobs.TASKS.items():
            self.task.addItem(tr(spec["name"]), k)
        form.addRow(tr("Tool"), self.task)
        self.source = QComboBox()
        form.addRow(tr("Input"), self.source)
        v.addLayout(form)
        self.params_form = QFormLayout()
        v.addLayout(self.params_form)
        self.widgets = {}
        row = QHBoxLayout()
        self.map = TileMap()
        row.addWidget(self.map, 2)
        side = QVBoxLayout()
        side.addWidget(QLabel(tr("Computers working")))
        self.who = QListWidget()
        side.addWidget(self.who)
        row.addLayout(side, 1)
        v.addLayout(row)
        self.bar = QProgressBar()
        v.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        bb = QDialogButtonBox()
        self.go = bb.addButton(tr("Run"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.stop = bb.addButton(tr("Stop"), QDialogButtonBox.ButtonRole.ActionRole)
        self.stop.setEnabled(False)
        close = bb.addButton(QDialogButtonBox.StandardButton.Close)
        self.go.clicked.connect(self._start)
        self.stop.clicked.connect(self._cancel)
        close.clicked.connect(self.reject)
        v.addWidget(bb)
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self._tick)
        self.task.currentIndexChanged.connect(self._task_changed)
        self._task_changed()
        if self.node is None:
            self.status.setText(tr("No team room is open: the tool runs on this computer alone, in tiles."))
        else:
            self._show_workers(self._info())

    def _info(self):
        link = worker.link_of(self.node)
        try:
            return link.call("work_info", {}).get("workers", []) if link else []
        except Exception:  # noqa: BLE001
            return []

    def _show_workers(self, workers, by=None):
        self.who.clear()
        by = by or {}
        self.who.addItem(tr("This computer") + (f" — {by.get('here', 0)}" if by else ""))
        for w in workers:
            self.who.addItem(f"{w.get('name') or w['id'][:6]} — {tr(LIMIT_SHORT.get(w.get('limit'), 'Light'))}"
                             + (f" — {by.get(w['id'], 0)}" if by else ""))

    def _task_changed(self):
        key = self.task.currentData()
        spec = jobs.TASKS[key]
        self.source.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            if spec["kind"] == "raster":
                if isinstance(lyr, QgsRasterLayer) and lyr.isValid() and lyr.providerType() == "gdal" \
                        and os.path.isfile(lyr.source().split("|")[0]):
                    self.source.addItem(lyr.name(), lyr.id())
            elif isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial():
                if spec.get("geom") == "polygon" and lyr.geometryType() != QgsWkbTypes.GeometryType.PolygonGeometry:
                    continue
                self.source.addItem(lyr.name(), lyr.id())
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self.widgets = {}
        for name, (typ, lo, hi, default) in spec["params"].items():
            if name in CHOICES:
                w = QComboBox()
                for i, c in enumerate(CHOICES[name]):
                    w.addItem(tr(c), i)
                w.setCurrentIndex(int(default))
            elif typ is int:
                w = QSpinBox()
                w.setRange(int(lo), int(hi))
                w.setValue(int(default))
            else:
                w = QDoubleSpinBox()
                w.setDecimals(4)
                w.setRange(lo, min(hi, 1e12))
                w.setValue(default)
            self.widgets[name] = w
            self.params_form.addRow(tr(LABELS.get(name, name)), w)

    def _params(self):
        out = {}
        for name, w in self.widgets.items():
            out[name] = w.currentData() if isinstance(w, QComboBox) else w.value()
        return out

    def _start(self):
        key = self.task.currentData()
        spec = jobs.TASKS[key]
        lyr = QgsProject.instance().mapLayer(self.source.currentData() or "")
        if lyr is None:
            QMessageBox.information(self, tr("Team Computing"), tr("Choose an input layer."))
            return
        try:
            params = jobs.validate(key, self._params())
            if spec["kind"] == "raster":
                path, _f = QFileDialog.getSaveFileName(self, tr("Save the result as"),
                                                       os.path.join(tempfile.gettempdir(), f"{lyr.name()}_{key}.tif"),
                                                       "GeoTIFF (*.tif)")
                if not path:
                    return
                self.out_path = path
                self.meta, self.tiles = owner.make_tiles(key, raster_path=lyr.source().split("|")[0])
            else:
                self.meta, self.tiles = owner.make_tiles(key, lyr)
        except jobs.JobError as e:
            QMessageBox.warning(self, tr("Team Computing"), str(e))
            return
        if not self.tiles:
            QMessageBox.information(self, tr("Team Computing"), tr("The layer has nothing to compute."))
            return
        self.layer = lyr
        n = os.cpu_count() or 2
        if self.node is not None and worker.link_of(self.node) is not None:
            room = worker.room_id(self.node)
            link, me = worker.link_of(self.node), self.node.me_id
            pubs = owner.pubs_of(self.node)
            trust = owner.load_trust(room)
            self.job = owner.Owner(link, key, params, self.tiles, pubs, trust=trust,
                                   on_trust=lambda t, room=room: owner.save_trust(room, t),
                                   local_slots=max(1, n // 2), me=me)
        else:
            self.job = owner.Owner(LocalLink(), key, params, self.tiles, {}, local_slots=max(1, n - 1), me="local")
        self.map.set_tiles(self.tiles)
        self.bar.setRange(0, len(self.tiles))
        self.bar.setValue(0)
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self.job.start()
        self.timer.start()

    def _cancel(self):
        if self.job is not None:
            self.job.cancel()

    def _tick(self):
        j = self.job
        if j is None:
            return
        self.map.states = dict(j.tiles_state)
        self.map.update()
        self.bar.setValue(len(j.accepted))
        self._show_workers(j.workers, j.by_worker)
        bad = [w for w in j.bad if w != "here"]
        text = tr("{done} of {n} tiles").format(done=len(j.accepted), n=len(j.tiles))
        if bad:
            text += " — " + tr("{n} computers gave wrong answers: their tiles were computed again").format(n=len(bad))
        self.status.setText(text)
        if not j.done.is_set():
            return
        self.timer.stop()
        self.go.setEnabled(True)
        self.stop.setEnabled(False)
        if j.error:
            self.status.setText(tr("Stopped: {why}").format(why=j.error))
            return
        try:
            self._finish(j)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, tr("Team Computing"), str(e))

    def _finish(self, j):
        from . import split

        key = j.task
        if jobs.TASKS[key]["kind"] == "raster":
            split.stitch_raster(self.meta, self.tiles, j.result, self.out_path)
            r = QgsRasterLayer(self.out_path, os.path.splitext(os.path.basename(self.out_path))[0])
            QgsProject.instance().addMapLayer(r)
            self.status.setText(tr("Done: {path}").format(path=self.out_path))
            return
        st = split.stitch_vector(key, j.params, j.result)
        crs = self.layer.crs().authid() or "EPSG:4326"
        if key == "overlapsgaps":
            ov = QgsVectorLayer(f"MultiPolygon?crs={crs}", tr("Overlaps") + " — " + self.layer.name(), "memory")
            ov.dataProvider().addAttributes([QgsField("feature_a", QMetaType.Type.LongLong),
                                             QgsField("feature_b", QMetaType.Type.LongLong),
                                             QgsField("area", QMetaType.Type.Double)])
            ov.updateFields()
            gp = QgsVectorLayer(f"MultiPolygon?crs={crs}", tr("Gaps") + " — " + self.layer.name(), "memory")
            gp.dataProvider().addAttributes([QgsField("gap_id", QMetaType.Type.Int),
                                             QgsField("area", QMetaType.Type.Double)])
            gp.updateFields()
            ov.dataProvider().addFeatures([self._feat(ov, h, [a, b, area]) for h, a, b, area in st["overlaps"]])
            gp.dataProvider().addFeatures([self._feat(gp, h, [i + 1, area]) for i, (h, area) in enumerate(st["gaps"])])
            QgsProject.instance().addMapLayers([ov, gp])
            self.status.setText(tr("Done: {o} overlaps and {g} gaps.").format(o=len(st["overlaps"]), g=len(st["gaps"])))
        else:
            out = QgsVectorLayer(f"MultiPolygon?crs={crs}", tr("Buffer") + " — " + self.layer.name(), "memory")
            if st["geom"]:
                out.dataProvider().addFeatures([self._feat(out, st["geom"], [])])
            QgsProject.instance().addMapLayer(out)
            self.status.setText(tr("Done."))
        self.plugin.iface.messageBar().pushMessage(tr("Team Computing"), self.status.text(), Qgis.MessageLevel.Success,
                                                   6)

    @staticmethod
    def _feat(layer, wkb, attrs):
        g = QgsGeometry()
        g.fromWkb(bytes.fromhex(wkb))
        g.convertToMultiType()
        f = QgsFeature(layer.fields())
        f.setGeometry(g)
        f.setAttributes(attrs)
        return f

    def reject(self):
        if self.job is not None and not self.job.done.is_set():
            self.job.cancel()
        self.timer.stop()
        super().reject()


def run_ui(plugin):
    RunDialog(plugin).exec()


# ---- sharing this computer ---------------------------------------------------------------------------------------
class _IdleWatch:
    """Tells the room's worker whether the user is editing (checked in the main thread every two seconds)."""

    timer = None

    @classmethod
    def ensure(cls):
        if cls.timer is None:
            cls.timer = QTimer()
            cls.timer.setInterval(2000)
            cls.timer.timeout.connect(cls.tick)
            cls.timer.start()

    @staticmethod
    def editing():
        return any(isinstance(l, QgsVectorLayer) and l.isEditable() and l.isModified()
                   for l in QgsProject.instance().mapLayers().values())

    @classmethod
    def tick(cls):
        idle = not cls.editing()
        for w in worker._SHARED.values():
            w.idle = idle


class ShareDialog(QDialog):
    def __init__(self, plugin, node):
        super().__init__(plugin.iface.mainWindow())
        self.node = node
        self.setWindowTitle(tr("Share My Computer"))
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        intro = QLabel(tr("The members of this room can run heavy tools on your computer while you are not editing: "
                          "only the tools of team computing, on data sent with the tile, in a separate process at low "
                          "priority. Nothing on your disk is read or shared. Off until you turn it on; remembered for "
                          "this room."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        on, limit = worker.load_share(worker.room_id(node))
        self.on = QCheckBox(tr("Share my computer with this room"))
        self.on.setChecked(on and worker.sharing(node) is not None)
        v.addWidget(self.on)
        form = QFormLayout()
        self.limit = QComboBox()
        for k in worker.LIMITS:
            self.limit.addItem(tr(LIMIT_TEXT[k]), k)
        self.limit.setCurrentIndex(worker.LIMITS.index(limit))
        form.addRow(tr("How much"), self.limit)
        v.addLayout(form)
        self.status = QLabel("")
        v.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._refresh()

    def _refresh(self):
        w = worker.sharing(self.node)
        if w is None:
            self.status.setText(tr("Not shared now."))
        else:
            self.status.setText(tr("Shared: {done} tiles computed for the room, {failed} failed.")
                                .format(done=w.done, failed=w.failed))

    def accept(self):
        limit = self.limit.currentData()
        if self.on.isChecked():
            worker.start_sharing(self.node, limit)
            _IdleWatch.ensure()
            _IdleWatch.tick()
        else:
            worker.stop_sharing(self.node)
        super().accept()


def share_ui(plugin):
    node = team.current()
    if node is None:
        QMessageBox.information(plugin.iface.mainWindow(), tr("Share My Computer"),
                                tr("Open or join a team room first: your computer is shared with one room."))
        return
    ShareDialog(plugin, node).exec()
