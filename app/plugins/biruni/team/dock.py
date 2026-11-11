"""The Team Room panel: open a room or join one; then the team, the host's control panel, conflicts and
the activity of the room."""
import csv
import datetime as dt
import html
import json
import os
import threading
import time

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsHeatmapRenderer, QgsPointXY,
                       QgsMapLayer, QgsProject, QgsRectangle, QgsStyle, QgsVectorLayer)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                                 QDockWidget, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
                                 QScrollArea, QShortcut, QStackedWidget, QTabWidget, QTreeWidget, QTreeWidgetItem,
                                 QVBoxLayout, QWidget)

from ..i18n import tr
from . import area, current, provenance, rules_ui, session, wire
from .overlay import Overlay, color_of

ROOMS_KEY = "biruni/team/rooms-folder"


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def _fmt_age(seconds):
    s = int(seconds)
    if s < 60:
        return tr("{n} s").format(n=s)
    if s < 3600:
        return tr("{n} min").format(n=s // 60)
    if s < 86400:
        return tr("{h} h {m} min").format(h=s // 3600, m=(s % 3600) // 60)
    return tr("{n} days").format(n=s // 86400)


def _when(ts):
    return dt.datetime.fromtimestamp(ts or 0).strftime("%H:%M:%S")


def _dot(color, size=10):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size)
    p.end()
    return pm


# ---- small charts ---------------------------------------------------------------------------------------
class Spark(QWidget):
    """Changes per minute over the last half hour."""

    def __init__(self):
        super().__init__()
        self.values = [0] * 30
        self.setMinimumHeight(58)

    def set_values(self, v):
        self.values = list(v)
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 6, -2, -14)
        top = max(self.values + [1])
        accent = self.palette().highlight().color()
        n = len(self.values)
        path = QPainterPath(QPointF(r.left(), r.bottom()))
        for i, v in enumerate(self.values):
            path.lineTo(r.left() + r.width() * i / (n - 1), r.bottom() - r.height() * v / top)
        line = QPainterPath(path)
        path.lineTo(r.right(), r.bottom())
        path.closeSubpath()
        fill = QColor(accent)
        fill.setAlpha(50)
        p.fillPath(path, fill)
        p.setPen(QPen(accent, 1.8))
        p.drawPath(line)
        p.setPen(self.palette().mid().color())
        f = p.font()
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.drawText(QRectF(r.left(), r.bottom() + 1, r.width(), 12), Qt.AlignmentFlag.AlignLeft, tr("30 min ago"))
        p.drawText(QRectF(r.left(), r.bottom() + 1, r.width(), 12), Qt.AlignmentFlag.AlignRight, tr("now"))
        p.drawText(QRectF(r.left(), 0, r.width(), 12), Qt.AlignmentFlag.AlignRight, str(top))


class Bars(QWidget):
    """Changes per member."""

    def __init__(self):
        super().__init__()
        self.rows = []

    def set_rows(self, rows):
        self.rows = rows[:12]
        self.setMinimumHeight(18 * len(self.rows) + 4)
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        top = max([n for _, n, _ in self.rows] + [1])
        w = self.width()
        for i, (name, n, mid) in enumerate(self.rows):
            y = 2 + i * 18
            p.setPen(self.palette().text().color())
            p.drawText(QRectF(0, y, w * 0.32, 16), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       name)
            bw = (w * 0.55) * n / top
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color_of(mid))
            p.drawRoundedRect(QRectF(w * 0.33, y + 3, max(bw, 2), 10), 4, 4)
            p.setPen(self.palette().text().color())
            p.drawText(QRectF(w * 0.33 + bw + 6, y, 60, 16), Qt.AlignmentFlag.AlignVCenter, str(n))


class Tile(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName("GpsCard")
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(0)
        self.value = QLabel("–")
        self.value.setObjectName("GpsValue")
        cap = QLabel(title)
        cap.setObjectName("GpsHint")
        cap.setWordWrap(True)
        v.addWidget(self.value)
        v.addWidget(cap)

    def set(self, text, tip=""):
        self.value.setText(str(text))
        self.setToolTip(tip)


# ---- dialogs --------------------------------------------------------------------------------------------
class OpenDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(tr("Open a team room"))
        v = QVBoxLayout(self)
        intro = QLabel(tr("The layers you tick are edited by the whole team at once. This computer is the room's "
                          "server: keep it on while the team works. Layers in another format are copied into a "
                          "GeoPackage first."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.list = QListWidget()
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.type() != QgsMapLayer.VectorLayer:
                continue
            why = session.shareable(lyr)
            if why not in ("", "convert"):
                continue
            it = QListWidgetItem(lyr.name() + (("  · " + tr("will be copied into a GeoPackage")) if why else ""))
            it.setData(Qt.ItemDataRole.UserRole, lyr.id())
            it.setCheckState(Qt.CheckState.Checked if lyr.isEditable() or not why else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        v.addWidget(self.list, 1)
        form = QFormLayout()
        stem = os.path.splitext(os.path.basename(QgsProject.instance().fileName()))[0]
        self.name = QLineEdit(QgsProject.instance().title() or stem)
        form.addRow(tr("Room name"), self.name)
        self.me = QLineEdit(session.my_name())
        form.addRow(tr("Your name"), self.me)
        self.rights = QComboBox()
        self.rights.addItem(tr("Can edit"), "edit")
        self.rights.addItem(tr("Can edit, not delete"), "nodelete")
        self.rights.addItem(tr("Can only view"), "view")
        form.addRow(tr("New members"), self.rights)
        v.addLayout(form)
        self.approve = QCheckBox(tr("I approve each new member"))
        self.approve.setChecked(True)
        v.addWidget(self.approve)
        self.outside = QCheckBox(tr("Members outside this network can join too (a direct encrypted link, nothing to download)"))
        v.addWidget(self.outside)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Open the room"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(460, 480)

    def layers(self):
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(QgsProject.instance().mapLayer(it.data(Qt.ItemDataRole.UserRole)))
        return [lyr for lyr in out if lyr is not None]


class _Bridge(QObject):
    step = pyqtSignal(str, int, int)
    done = pyqtSignal(object, str)


class JoinDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(tr("Join a team room"))
        self.result_data = None
        self._cancel = False
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText("XXXX-XXXX-XXXX-XXXX")
        f = self.code.font()
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setFamily("Consolas")
        self.code.setFont(f)
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        form.addRow(tr("Room code"), self.code)
        self.me = QLineEdit(session.my_name())
        form.addRow(tr("Your name"), self.me)
        from qgis.core import QgsSettings
        self.folder = QLineEdit(QgsSettings().value(ROOMS_KEY, os.path.join(os.path.expanduser("~"), "Documents",
                                                                           "Biruni Rooms")))
        pick = QPushButton("…")
        pick.clicked.connect(self._pick)
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(pick)
        form.addRow(tr("Keep the room's map in"), row)
        v.addLayout(form)
        self.state = QLabel(tr("Type the code the room's host gave you. The room is found on this network by "
                               "itself."))
        self.state.setWordWrap(True)
        v.addWidget(self.state)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Join"))
        self.bb.accepted.connect(self._go)
        self.bb.rejected.connect(self._stop)
        v.addWidget(self.bb)
        self.bridge = _Bridge()
        self.bridge.step.connect(self._step)
        self.bridge.done.connect(self._done)
        self.resize(460, 240)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, tr("Keep the room's map in"), self.folder.text())
        if d:
            self.folder.setText(d)

    def _go(self):
        if not session.codes.normalize(self.code.text()):
            self.state.setText(tr("That is not a room code: it has 16 letters and digits, like K7QM-2D9X-HR4P-8WZT."))
            return
        if QgsProject.instance().isDirty() and QMessageBox.question(
                self, tr("Join a team room"), tr("The current map has unsaved changes. Close it without saving?")) \
                != QMessageBox.StandardButton.Yes:
            return
        session.set_my_name(self.me.text())
        from qgis.core import QgsSettings
        QgsSettings().setValue(ROOMS_KEY, self.folder.text())
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        code, folder = self.code.text(), self.folder.text()

        def run():
            try:
                res = session.join_steps(code, folder, lambda s, a, b: self.bridge.step.emit(s, a, b),
                                         lambda: self._cancel)
                self.bridge.done.emit(res, "")
            except Exception as e:  # noqa: BLE001
                self.bridge.done.emit(None, str(e))

        threading.Thread(target=run, daemon=True).start()

    def _step(self, step, a, b):
        if step == "finding":
            self.state.setText(tr("Looking for the room on this network and outside…"))
        elif step == "waiting":
            self.state.setText(tr("Found. Waiting for the host to let you in…"))
        elif step == "downloading":
            self.state.setText(tr("Copying the room's map to this computer: {a} of {b}").format(
                a=_fmt_bytes(a), b=_fmt_bytes(b)))

    def _done(self, res, err):
        if res is None:
            self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            if "not-found" in err:
                self.state.setText(tr("The room was not found. Check the code, and that the host's computer is on "
                                      "and in the same network (or that the host allowed joining from outside)."))
            elif "refused" in err:
                self.state.setText(tr("The host did not let you in."))
            elif "cancelled" in err:
                self.reject()
            else:
                self.state.setText(tr("Joining failed: {e}").format(e=err))
            return
        self.result_data = res
        self.accept()

    def _stop(self):
        self._cancel = True
        self.reject()


class HistoryDialog(QDialog):
    def __init__(self, parent, node, key, fid, canvas):
        super().__init__(parent)
        self.node, self.key, self.fid, self.canvas = node, key, fid, canvas
        self.setWindowTitle(tr("History of feature {fid}").format(fid=fid))
        v = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Time"), tr("Who"), tr("What"), tr("Drawn on")])
        self.entries = node.store.feature_history(key, fid)
        for e in reversed(self.entries):
            what = {"put": tr("changed"), "del": tr("deleted")}.get(e["kind"], e["kind"])
            if e["prev"] is None and e["kind"] == "put":
                what = tr("added")
            if e.get("note") == "merged":
                what += " · " + tr("joined with another edit")
            imgs, scale = provenance.source_text(e)
            drawn = " · ".join(x for x in (imgs, f"1:{scale:,}" if scale else "") if x)
            it = QTreeWidgetItem([dt.datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S"), e["name"], what,
                                  drawn])
            it.setData(0, Qt.ItemDataRole.UserRole, e)
            self.tree.addTopLevelItem(it)
        self.tree.currentItemChanged.connect(self._preview)
        v.addWidget(self.tree, 1)
        self.markers = []
        self._origin(v)
        row = QHBoxLayout()
        restore = QPushButton(tr("Bring back this version"))
        restore.clicked.connect(self._restore)
        row.addStretch(1)
        row.addWidget(restore)
        v.addLayout(row)
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setStrokeColor(QColor("#E8590C"))
        self.band.setFillColor(QColor(232, 89, 12, 50))
        self.band.setWidth(2)
        self.resize(520, 420)

    def _preview(self, it, _=None):
        e = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        self.band.reset(Qgis.GeometryType.Polygon)
        st = (e or {}).get("state") or {}
        lyr = self.node.layers.get(self.key)
        if st.get("g") and lyr is not None:
            g = QgsGeometry()
            g.fromWkb(bytes.fromhex(st["g"]))
            self.band.setToGeometry(g, lyr)

    def _restore(self):
        it = self.tree.currentItem()
        if it is None:
            return
        e = it.data(0, Qt.ItemDataRole.UserRole)
        self.node.submit(self.key, self.fid, e["state"], note="restore")
        self.accept()

    def _origin(self, v):
        """Where it came from: the points of the shape by who placed them (on the map in their colour), who gave
        each value, and the field photos taken near it before."""
        lyr = self.node.layers.get(self.key)
        pts = provenance.vertices(self.entries)
        who = provenance.authors(self.entries)
        self.origin = QLabel("")
        self.origin.setWordWrap(True)
        parts = []
        if who:
            parts.append(tr("Points of the shape: {w}.").format(w=", ".join(
                f"{n or tr('from before the room')} {c}" for n, c in who)))
        vals = provenance.fields(self.entries)
        if vals:
            by = {}
            for k, e in vals.items():
                by.setdefault(e.get("name") or "?", []).append(k)
            parts.append(tr("Values: {w}.").format(w="; ".join(f"{n}: {', '.join(ks)}" for n, ks in by.items())))
        self.origin.setText(" ".join(parts))
        v.addWidget(self.origin)
        if lyr is not None:
            for _p, _r, i, (x, y), e in pts:
                if e is None:          # from before the room: no marker
                    continue
                m = QgsVertexMarker(self.canvas)
                pt = QgsPointXY(x, y)
                if lyr.crs() != self.canvas.mapSettings().destinationCrs():
                    pt = QgsCoordinateTransform(lyr.crs(), self.canvas.mapSettings().destinationCrs(),
                                                QgsProject.instance()).transform(pt)
                m.setCenter(pt)
                m.setColor(color_of(e.get("author")))
                m.setFillColor(color_of(e.get("author")))
                m.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
                m.setIconSize(9)
                m.setPenWidth(2)
                when = dt.datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M")
                m.setToolTip(f"{e.get('name', '')} · {when}")
                self.markers.append(m)
        self.photos = QListWidget()
        last = self.entries[-1] if self.entries else None
        g = None
        if last and (last.get("state") or {}).get("g"):
            g = QgsGeometry()
            g.fromWkb(bytes.fromhex(last["state"]["g"]))
        found = provenance.photos_near(g, lyr.crs(), last["ts"] if last else None) if lyr is not None else []
        for taken, path, _pl, _fid in found[:20]:
            it = QListWidgetItem(tr("Photo of {t}").format(t=str(taken).replace("T", " ")[:16]))
            it.setData(Qt.ItemDataRole.UserRole, path)
            self.photos.addItem(it)
        if found:
            v.addWidget(QLabel(tr("Field photos near it, taken before the change (double-click to open):")))
            self.photos.setMaximumHeight(90)
            self.photos.itemDoubleClicked.connect(self._open_photo)
            v.addWidget(self.photos)

    def _open_photo(self, it):
        path = it.data(Qt.ItemDataRole.UserRole)
        if path and os.path.isfile(str(path)):
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def done(self, r):
        self.canvas.scene().removeItem(self.band)
        for m in self.markers:
            self.canvas.scene().removeItem(m)
        self.markers = []
        super().done(r)


# ---- the panel ------------------------------------------------------------------------------------------
class TeamDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Team Room"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.canvas = plugin.iface.mapCanvas()
        self.setObjectName("BiruniTeam")
        self.node = None
        self.overlay = Overlay(self.canvas)
        self.activity = []
        self._bands = []
        self._area_bands = []
        self._areas_key = None
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(400)
        self._refresh.timeout.connect(self.refresh)
        self._live = QTimer(self)
        self._live.setInterval(1000)
        self._live.timeout.connect(self._tick)
        self._backup_at = time.time()
        self._build()
        self.canvas.xyCoordinates.connect(self._cursor)
        self.undo_key = QShortcut(QKeySequence("Ctrl+Alt+Z"), self.iface.mainWindow())
        self.undo_key.activated.connect(self.undo)
        self.attach(current())

    # ---- building -------------------------------------------------------------------------------------
    def _build(self):
        self.pages = QStackedWidget()
        self.pages.setObjectName("BiruniGps")
        # start
        start = QWidget()
        sv = QVBoxLayout(start)
        sv.setContentsMargins(10, 8, 10, 8)
        card = QFrame()
        card.setObjectName("GpsCard")
        cv = QVBoxLayout(card)
        intro = QLabel(tr("Work on the same data with your team at the same time. One of your computers is the "
                          "server - no server to buy, no account, no settings. Everyone sees the others' edits "
                          "within seconds and where they are working; two edits of the same feature are joined "
                          "vertex by vertex; if the host's computer stops, another member takes over."))
        intro.setObjectName("GpsHint")
        intro.setWordWrap(True)
        cv.addWidget(intro)
        b1 = QPushButton(tr("Open a room for this map"))
        b1.setObjectName("GpsPrimary")
        b1.clicked.connect(self.open_room)
        b2 = QPushButton(tr("Join a room with a code"))
        b2.clicked.connect(self.join_room)
        cv.addWidget(b1)
        cv.addWidget(b2)
        sv.addWidget(card)
        sv.addStretch(1)
        self.pages.addWidget(start)
        # room
        room = QWidget()
        rv = QVBoxLayout(room)
        rv.setContentsMargins(10, 8, 10, 8)
        rv.setSpacing(6)
        head = QFrame()
        head.setObjectName("GpsCard")
        hv = QVBoxLayout(head)
        top = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("GpsValue")
        self.badge = QLabel()
        top.addWidget(self.title, 1)
        top.addWidget(self.badge)
        hv.addLayout(top)
        self.state = QLabel()
        self.state.setObjectName("GpsHint")
        self.state.setWordWrap(True)
        hv.addWidget(self.state)
        coderow = QHBoxLayout()
        self.code = QLabel()
        f = self.code.font()
        f.setFamily("Consolas")
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.code.setFont(f)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        copy = QPushButton(tr("Copy code"))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(session.room_code(self.node)))
        qr = QPushButton(tr("QR"))
        qr.clicked.connect(self._show_qr)
        coderow.addWidget(self.code, 1)
        coderow.addWidget(copy)
        coderow.addWidget(qr)
        hv.addLayout(coderow)
        rv.addWidget(head)
        self.tabs = QTabWidget()
        rv.addWidget(self.tabs, 1)
        # team
        team = QWidget()
        tv = QVBoxLayout(team)
        self.members = QTreeWidget()
        self.members.setHeaderLabels([tr("Member"), tr("Status"), tr("Edits")])
        self.members.setRootIsDecorated(False)
        self.members.itemDoubleClicked.connect(self._go_to_member)
        self.members.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.members.customContextMenuRequested.connect(self._member_menu)
        tv.addWidget(self.members, 1)
        hint = QLabel(tr("Double-click a member to go to where they are. Right-click for rights."))
        hint.setObjectName("GpsHint")
        hint.setWordWrap(True)
        tv.addWidget(hint)
        self.tabs.addTab(team, tr("Team"))
        # control panel (host)
        self.panel = self._build_panel()
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setWidget(self.panel)
        self.tabs.addTab(self.panel_scroll, tr("Control panel"))
        # conflicts
        conf = QWidget()
        cfv = QVBoxLayout(conf)
        self.conflicts = QListWidget()
        self.conflicts.setWordWrap(True)
        cfv.addWidget(self.conflicts, 1)
        crow = QHBoxLayout()
        show = QPushButton(tr("Show"))
        show.clicked.connect(self._show_conflict)
        mine = QPushButton(tr("Keep mine"))
        mine.clicked.connect(lambda: self._resolve(True))
        theirs = QPushButton(tr("Keep theirs"))
        theirs.clicked.connect(lambda: self._resolve(False))
        for b in (show, mine, theirs):
            crow.addWidget(b)
        cfv.addLayout(crow)
        self.tabs.addTab(conf, tr("Conflicts"))
        # activity
        act = QWidget()
        av = QVBoxLayout(act)
        self.feed = QListWidget()
        self.feed.itemDoubleClicked.connect(self._go_to_entry)
        av.addWidget(self.feed, 1)
        self.tabs.addTab(act, tr("Activity"))
        # actions
        row = QHBoxLayout()
        undo = QPushButton(tr("Undo my last change"))
        undo.setToolTip(tr("Ctrl+Alt+Z - also when the change was saved and sent"))
        undo.clicked.connect(self.undo)
        hist = QPushButton(tr("History of the selected feature"))
        hist.clicked.connect(self.history)
        row.addWidget(undo)
        row.addWidget(hist)
        rv.addLayout(row)
        tm = QPushButton(tr("Time Machine"))
        tm.setToolTip(tr("The shared layers as they were at any moment since the room opened: played forward, "
                         "and what was there brought back."))
        tm.clicked.connect(self.time_machine)
        rv.addWidget(tm)
        # [I06] the room's merge rules and number ranges (team/rules.py, rules_ui.py)
        rrow = QHBoxLayout()
        mr = QPushButton(tr("Merge Rules…"))
        mr.setToolTip(tr("What happens when two people change the same field at the same time, signed by the "
                         "room's admin."))
        mr.clicked.connect(lambda: rules_ui.open_rules(self.node, self))
        nr = QPushButton(tr("Number Ranges…"))
        nr.setToolTip(tr("Parcel or point numbers granted to each member, so numbers never repeat, even offline."))
        nr.clicked.connect(lambda: rules_ui.open_numbers(self.node, self))
        rrow.addWidget(mr)
        rrow.addWidget(nr)
        rv.addLayout(rrow)
        # [room folder] the room's changes through a folder the office already shares (team/folder.py)
        shf = QPushButton(tr("Use a Shared Folder…"))
        shf.setToolTip(tr("A OneDrive, Google Drive or Dropbox folder, a network share or a USB stick: the room's "
                          "changes travel through it, sealed and signed, so the host and the members never need to be "
                          "online at the same time."))
        shf.clicked.connect(self.shared_folder)
        rv.addWidget(shf)
        self.folder_state = QLabel()
        self.folder_state.setWordWrap(True)
        rv.addWidget(self.folder_state)
        leave = QPushButton(tr("Leave the room"))
        leave.clicked.connect(self.leave)
        rv.addWidget(leave)
        self.pages.addWidget(room)
        self.setWidget(self.pages)

    def _build_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 4, 2, 4)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles = {}
        specs = (("online", tr("online now")), ("per_min", tr("changes this minute")),
                 ("today", tr("changes today")), ("merged", tr("edits joined automatically")),
                 ("conflicts", tr("need a decision")), ("apply", tr("average write time")),
                 ("queue", tr("waiting to be written")), ("traffic", tr("traffic in / out")),
                 ("uptime", tr("server running for")), ("size", tr("shared data")),
                 ("epoch", tr("host changes")), ("next", tr("takes over if this computer stops")))
        for i, (k, title) in enumerate(specs):
            t = Tile(title)
            self.tiles[k] = t
            grid.addWidget(t, i // 3, i % 3)
        v.addLayout(grid)
        lab = QLabel(tr("Changes per minute"))
        lab.setObjectName("GpsHint")
        v.addWidget(lab)
        self.spark = Spark()
        v.addWidget(self.spark)
        lab = QLabel(tr("Changes by member"))
        lab.setObjectName("GpsHint")
        v.addWidget(lab)
        self.bars = Bars()
        v.addWidget(self.bars)
        # waiting members
        self.waiting = QListWidget()
        self.waiting.setMaximumHeight(80)
        wrow = QHBoxLayout()
        let_in = QPushButton(tr("Let in"))
        let_in.clicked.connect(lambda: self._decide(True))
        refuse = QPushButton(tr("Refuse"))
        refuse.clicked.connect(lambda: self._decide(False))
        wrow.addWidget(let_in)
        wrow.addWidget(refuse)
        self.waiting_box = QFrame()
        self.waiting_box.setObjectName("GpsCard")
        wb = QVBoxLayout(self.waiting_box)
        wl = QLabel(tr("Asking to join (check the fingerprint with the person)"))
        wl.setObjectName("GpsHint")
        wb.addWidget(wl)
        wb.addWidget(self.waiting)
        wb.addLayout(wrow)
        v.addWidget(self.waiting_box)
        # server controls
        box = QFrame()
        box.setObjectName("GpsCard")
        bv = QVBoxLayout(box)
        self.addresses = QLabel()
        self.addresses.setObjectName("GpsHint")
        self.addresses.setWordWrap(True)
        self.addresses.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bv.addWidget(self.addresses)
        self.door = QCheckBox(tr("New members can join"))
        self.door.toggled.connect(lambda on: self.node and self.node.hub and setattr(self.node.hub, "open_door", on))
        self.approve = QCheckBox(tr("I approve each new member"))
        self.approve.toggled.connect(self._approve_toggled)
        self.outside = QCheckBox(tr("Members outside this network can join"))
        self.outside.toggled.connect(self._outside_toggled)
        self.freeze = QCheckBox(tr("Freeze editing (maintenance): only this computer edits"))
        self.freeze.toggled.connect(self._freeze_toggled)
        self.autobackup = QCheckBox(tr("Back up the shared data every hour"))
        self.autobackup.setChecked(True)
        for c in (self.door, self.approve, self.outside, self.freeze, self.autobackup):
            bv.addWidget(c)
        msg = QHBoxLayout()
        self.notice = QLineEdit()
        self.notice.setPlaceholderText(tr("A message to everyone in the room"))
        send = QPushButton(tr("Send"))
        send.clicked.connect(self._send_notice)
        msg.addWidget(self.notice, 1)
        msg.addWidget(send)
        bv.addLayout(msg)
        tools = QGridLayout()
        for i, (text, fn) in enumerate(((tr("Back up now"), self._backup), (tr("Export the change log"), self._export),
                                        (tr("Map of today's work"), self._heat), (tr("Test the network"), self._probe),
                                        (tr("Change the room code"), self._change_code))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tools.addWidget(b, i // 2, i % 2)
        bv.addLayout(tools)
        v.addWidget(box)
        v.addStretch(1)
        return w

    def refresh_theme(self):
        from ..tracking import panel_qss

        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens()))

    # ---- following a room -------------------------------------------------------------------------------
    def attach(self, node):
        if self.node is not None:
            for sig, slot in ((self.node.changed, self._refresh.start), (self.node.status, self._status),
                              (self.node.entry, self._entry), (self.node.joinRequested, self._join_request)):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
        self.node = node
        self.overlay.clear()
        if node is not None:
            node.source_of_change = self._screen_source
        if node is None:
            self._live.stop()
            self.pages.setCurrentIndex(0)
            return
        node.changed.connect(self._refresh.start)
        node.status.connect(self._status)
        node.entry.connect(self._entry)
        node.joinRequested.connect(self._join_request)
        node.deletesHeld.connect(self._held_deletes)
        self.activity = []
        for e in node.store.last(60):
            self._entry(e, quiet=True)
        self.pages.setCurrentIndex(1)
        self._live.start()
        self.refresh()

    def refresh(self):
        n = self.node
        if n is None:
            return
        host = n.role == "host"
        self.title.setText(n.store.get("room_name", "") or tr("Team room"))
        self.badge.setText(tr("Host (server)") if host else tr("Member"))
        states = {"host": tr("This computer is the room's server. Keep it on while the team works."),
                  "connected": tr("Connected - edits are shared as you work."),
                  "reconnecting": tr("The host cannot be reached. Keep working: your edits wait and are sent "
                                     "later. If the host does not come back, a member takes over."),
                  "rejoining": tr("Joining the room's new host…"), "idle": ""}
        self.state.setText(states.get(n.state, ""))
        self.refresh_folder()
        self.code.setText(session.room_code(n))
        self.tabs.setTabVisible(1, host)
        # members
        self.members.clear()
        counts = n.store.counts()
        pres = n.presence
        for m in n.roster:
            if m.get("state") == "out":
                continue
            if m["id"] == n.me_id:
                status = tr("you")
            elif m.get("host") and not host:
                status = tr("host") + (" · " + tr("online") if n.state == "connected" else "")
            elif m["id"] in pres:
                ed = pres[m["id"]].get("edit") or []
                status = tr("editing") if ed else tr("online")
            else:
                status = tr("away")
            it = QTreeWidgetItem([m.get("name", ""), status, str(counts.get(m["id"], 0))])
            it.setIcon(0, QIcon(_dot(color_of(m["id"]))))
            it.setData(0, Qt.ItemDataRole.UserRole, m)
            self.members.addTopLevelItem(it)
        for i in range(3):
            self.members.resizeColumnToContents(i)
        # conflicts
        self.conflicts.clear()
        cs = n.store.open_conflicts()
        for c in cs:
            lyr = n.layers.get(c["layer"])
            parts = []
            for item in c.get("items", []):
                if item["what"] == "field":
                    parts.append(tr("{f}: yours «{a}», {o}'s «{b}»").format(f=item["field"], a=item["mine"],
                                                                          b=item["theirs"], o=c.get("other") or "?"))
                elif item["what"] == "geometry":
                    parts.append(tr("the shape: you and {o} moved the same vertices").format(o=c.get("other") or "?"))
                elif item["what"] == "deleted":
                    parts.append(tr("{o} deleted it while you edited it").format(o=c.get("other") or "?"))
                elif item["what"] == "delete":
                    parts.append(tr("you deleted it while {o} edited it").format(o=c.get("other") or "?"))
            it = QListWidgetItem(f"{lyr.name() if lyr else c['layer']} · #{c['fid']}\n" + "\n".join(parts))
            it.setData(Qt.ItemDataRole.UserRole, c)
            self.conflicts.addItem(it)
        self.tabs.setTabText(2, tr("Conflicts") + (f" ({len(cs)})" if cs else ""))
        if host and n.hub is not None:
            self.door.blockSignals(True)
            self.door.setChecked(n.hub.open_door)
            self.door.blockSignals(False)
            self.approve.blockSignals(True)
            self.approve.setChecked(not n.hub.auto_accept)
            self.approve.blockSignals(False)
            self.outside.blockSignals(True)
            self.outside.setChecked(bool(n.store.get("external")))
            self.outside.blockSignals(False)
            self.freeze.blockSignals(True)
            self.freeze.setChecked(n.hub.frozen)
            self.freeze.blockSignals(False)
            self._tick()

    def _tick(self):
        n = self.node
        if n is None:
            return
        presence = n.presence if n.role != "host" or n.hub is None else {
            k: v for k, v in n.hub.presence.items() if k != n.me_id and time.time() - v["ts"] < 20}
        self.overlay.update(presence, n.zones(), n.me_id)
        self._show_areas()
        if n.role == "host" and n.hub is not None and self.tabs.currentIndex() == 1:
            self._panel_numbers()
        if n.role == "host" and n.hub is not None and self.autobackup.isChecked() and time.time() - self._backup_at > 3600:
            self._backup(quiet=True)

    def _panel_numbers(self):
        n = self.node
        x = n.hub.numbers()
        members = [m for m in n.roster if m.get("state") != "out"]
        self.tiles["online"].set(f"{len(x['online'])} / {max(len(members) - 1, 0)}")
        self.tiles["per_min"].set(x["last_min"])
        self.tiles["today"].set(x["today"])
        total = max(x["today"], 1)
        self.tiles["merged"].set(x["merged"], tr("{p}% of today's changes").format(p=round(100 * x["merged"] / total)))
        self.tiles["conflicts"].set(x["conflicts"])
        self.tiles["apply"].set(tr("{ms} ms").format(ms=round(x["apply_ms"], 1)),
                                tr("slowest: {ms} ms").format(ms=round(x["apply_max"], 1)))
        self.tiles["queue"].set(x["queue"])
        self.tiles["traffic"].set(f"{_fmt_bytes(x['bytes_in'])} / {_fmt_bytes(x['bytes_out'])}",
                                  tr("{n} requests").format(n=x["requests"]))
        self.tiles["uptime"].set(_fmt_age(x["uptime"]))
        self.tiles["size"].set(_fmt_bytes(x["size"]))
        self.tiles["epoch"].set(max(x["epoch"] - 1, 0))
        nxt = sorted([m for m in members if not m.get("host")], key=lambda m: m.get("rank", 99))
        self.tiles["next"].set(nxt[0]["name"] if nxt else "–")
        self.spark.set_values(x["per_min"])
        names = {m["id"]: m.get("name", "") for m in n.roster}
        self.bars.set_rows(sorted(((names.get(k, k[:6]), v, k) for k, v in x["by"].items()), key=lambda r: -r[1]))
        self.waiting.clear()
        for r in x["waiting"]:
            it = QListWidgetItem(f"{r['name']}   ·   {r['fingerprint']}")
            it.setData(Qt.ItemDataRole.UserRole, r["id"])
            self.waiting.addItem(it)
        self.waiting_box.setVisible(bool(x["waiting"]))
        lines = [f"http://{a}:{n.hub.port}" for a in wire.local_addresses()]
        if n.external_url == "link:":
            lines.append(tr("Outside this network: a direct encrypted link ({n} connected through it)").format(
                n=n.link.members() if n.link is not None else 0))
        elif n.external_url:
            lines.append(n.external_url + "  (" + tr("outside") + ")")
        self.addresses.setText(tr("The room's addresses (the code finds them by itself):") + "\n" + "\n".join(lines))

    # ---- events ---------------------------------------------------------------------------------------
    def _cursor(self, pt):
        if self.node is not None:
            self.node.set_cursor(pt)

    def _bar(self, text, level=0, secs=6):
        from qgis.core import Qgis
        lv = {0: Qgis.MessageLevel.Info, 1: Qgis.MessageLevel.Warning, 2: Qgis.MessageLevel.Success}[level]
        # plain text: names and notices come from other members, and the message bar reads HTML (links)
        self.iface.messageBar().pushMessage(tr("Team room"), html.escape(text), lv, secs)

    def _status(self, level, text):
        msgs = {"lost": tr("The room's host cannot be reached. Keep working - your edits will be sent later."),
                "back": tr("Connected to the room again."),
                "new-code": tr("The room's code changed; this computer switched to the new one."),
                "deletes-held": tr("You are deleting many features at once: the host is asked to agree first."),
                "deletes-refused": tr("The host did not agree to your deletions: the features are back."),
                "undo-changed": tr("Someone changed this feature after you: bring back the version you want from "
                                   "its history."),
                "took-over": tr("The host stopped. This computer is now the room's server."),
                "rejoined": tr("Joined the room's new host. Your copy is up to date."),
                "handover": tr("Another computer holds the room for more members. Joining it…"),
                "fields": tr("Only the host can add columns to a shared layer."),
                "frozen": tr("The host froze editing for maintenance. You can look, not edit."),
                "unfrozen": tr("Editing is open again."),
                "external-download": tr("Downloading the tunnel program from Cloudflare (once, about 60 MB)…")}
        if level == "zone":
            self._bar(tr("{name} is working in this area right now.").format(name=text), 1, 5)
        elif level == "conflict":
            self._bar(tr("You and {name} changed the same thing. See Conflicts in the Team Room.").format(
                name=text or "?"), 1, 8)
            self._refresh.start()
        elif level == "notice":
            self._bar(text, 0, 15)
        elif text in msgs:
            self._bar(msgs[text], 2 if level == "ok" else 1)
        elif text.startswith("refused:"):
            why = text.split(":", 1)[1]
            self._bar({"view only": tr("You can only view this layer; your change was not kept."),
                       "cannot delete": tr("You can edit this layer but not delete from it; the feature is back."),
                       "frozen": tr("Editing is frozen by the host; your change was not kept."),
                       "outside your area": tr("That feature is outside your area of work; your change was not kept.")}.get(
                why, tr("The host did not accept a change: {w}").format(w=why)), 1)
        elif text.startswith("external:"):
            self._bar(tr("Joining from outside could not start: {e}").format(e=text.split(":", 1)[1]), 1)
        elif level == "warn" and text:
            self._bar(text, 1)
        self._refresh.start()

    def _entry(self, e, quiet=False):
        n = self.node
        if n is None:
            return
        lyr = n.layers.get(e["layer"])
        what = tr("added a feature to {l}") if e["kind"] == "put" and not e.get("prev") else (
            tr("deleted a feature from {l}") if e["kind"] == "del" else tr("edited a feature in {l}"))
        if e["kind"] == "field":
            what = tr("added a column to {l}")
        text = f"{_when(e['ts'])}  {e.get('name') or '?'} " + what.format(l=lyr.name() if lyr else e["layer"])
        if e.get("note") == "merged":
            text += " · " + tr("joined with another edit")
        it = QListWidgetItem(text)
        it.setIcon(QIcon(_dot(color_of(e["author"]))))
        it.setData(Qt.ItemDataRole.UserRole, e)
        self.feed.insertItem(0, it)
        while self.feed.count() > 300:
            self.feed.takeItem(self.feed.count() - 1)
        if not quiet:
            self._refresh.start()

    def _join_request(self, req):
        self._bar(tr("{name} asks to join the room. Let them in from the Control panel.").format(name=req["name"]), 0, 10)
        self.tabs.setCurrentIndex(1)
        self.show()
        self.raise_()

    # ---- actions -------------------------------------------------------------------------------------------
    def open_room(self):
        if not QgsProject.instance().fileName():
            self._bar(tr("Save the map first: the room keeps its record next to it."), 1)
            return
        d = OpenDialog(self.iface.mainWindow())
        if d.exec() != QDialog.DialogCode.Accepted or not d.layers():
            return
        session.set_my_name(d.me.text())
        try:
            node = session.open_room(d.layers(), d.name.text().strip() or tr("Team room"), d.approve.isChecked(),
                                     d.outside.isChecked(), d.rights.currentData())
        except Exception as e:  # noqa: BLE001
            self._bar(tr("The room could not be opened: {e}").format(e=e), 1, 10)
            return
        self.attach(node)
        self._bar(tr("The room is open. Give your team the code: {code}").format(code=session.room_code(node)), 2, 15)

    def join_room(self):
        d = JoinDialog(self.iface.mainWindow())
        if d.exec() != QDialog.DialogCode.Accepted or not d.result_data:
            return
        if current() is not None:
            session.close_room(current())
        node, gone = session.finish_join(d.result_data)
        self.attach(node)
        text = tr("You are in the room. Edit as usual: your work is shared as you go.")
        if gone:
            text += " " + tr("Some layers are files only the host has: {names}").format(names=", ".join(gone[:5]))
        self._bar(text, 2, 12)

    def leave(self):
        if self.node is None:
            return
        host = self.node.role == "host"
        q = tr("Close the room? Members keep their copies and can no longer share edits until you open it again.") \
            if host else tr("Leave the room? Your copy stays on this computer.")
        if QMessageBox.question(self, tr("Team room"), q) != QMessageBox.StandardButton.Yes:
            return
        node = self.node
        self.attach(None)
        session.close_room(node, forget=not host)

    def undo(self):
        if self.node is None:
            return
        e = self.node.undo_last()
        if e is None:
            self._bar(tr("Nothing of yours to undo in this room."), 0, 4)

    def shared_folder(self):
        from . import folder as rf

        if self.node is None:
            return
        cur = self.node.store.get("folder_carrier") or ""
        if cur:
            box = QMessageBox(QMessageBox.Icon.Question, tr("Shared folder"),
                              tr("This room travels through:\n{0}").format(cur), parent=self)
            change = box.addButton(tr("Choose Another Folder…"), QMessageBox.ButtonRole.AcceptRole)
            stop = box.addButton(tr("Stop Using It"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is stop:
                rf.use_folder(self.node, None)
                self.refresh_folder()
                return
            if box.clickedButton() is not change:
                return
        d = QFileDialog.getExistingDirectory(self, tr("A folder the whole team syncs"), cur)
        if d:
            rf.use_folder(self.node, d)
            self.refresh_folder()

    def refresh_folder(self):
        """The shared folder's line: when it last synced, how many computers write in it, what waits."""
        from . import folder as rf

        lab = getattr(self, "folder_state", None)
        if lab is None:
            return
        c = rf.current()
        if c is None or self.node is None or c.node is not self.node:
            lab.setText("")
            return
        s = c.status
        if s.get("error"):
            lab.setText(tr("Shared folder: cannot be reached ({0})").format(s["error"][:120]))
            return
        when = time.strftime("%H:%M", time.localtime(s["last"])) if s.get("last") else "–"
        text = tr("Shared folder: synced at {0} · {1} other computers").format(when, s.get("devices", 0))
        if s.get("pending"):
            text += " · " + tr("{0} files still arriving").format(s["pending"])
        lab.setText(text)

    def time_machine(self):
        if self.node is None:
            return None
        from .timemachine import TimeMachine

        old = getattr(self, "_time_machine", None)
        if old is not None:
            try:
                old.close()
            except RuntimeError:
                pass
        self._time_machine = TimeMachine(self.iface.mainWindow(), self.node, self.iface)
        self._time_machine.show()
        return self._time_machine

    def history(self):
        n = self.node
        lyr = self.iface.activeLayer()
        key = n.key_of(lyr) if n and lyr else None
        if key is None or lyr.selectedFeatureCount() != 1:
            self._bar(tr("Select one feature of a shared layer first."), 1, 5)
            return
        HistoryDialog(self.iface.mainWindow(), n, key, lyr.selectedFeatureIds()[0], self.canvas).exec()

    def _show_qr(self):
        from ..gps.browser import qr_png
        from ..vendor import ensure_vendor

        ensure_vendor()
        code = session.room_code(self.node)
        pm = QPixmap()
        pm.loadFromData(qr_png(code, 8))
        d = QDialog(self)
        d.setWindowTitle(tr("Room code"))
        v = QVBoxLayout(d)
        img = QLabel()
        img.setPixmap(pm)
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(img)
        lab = QLabel(code)
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(lab)
        d.exec()

    def _screen_source(self):
        """What the map shows while a change is made: the images (top first) and the scale."""
        names = []
        try:
            for lyr in QgsProject.instance().layerTreeRoot().checkedLayers():
                if lyr.type() == QgsMapLayer.LayerType.RasterLayer:
                    names.append(lyr.name())
        except RuntimeError:
            pass
        return {"img": names[:3], "scale": round(self.canvas.scale())}

    def _set_area(self, m):
        """The member may change features only inside the selected features of the current layer."""
        lyr = self.iface.activeLayer()
        sel = list(lyr.getSelectedFeatures()) if lyr is not None and hasattr(lyr, "getSelectedFeatures") else []
        a = area.make([f.geometry() for f in sel], lyr.crs()) if sel else None
        if a is None:
            self._bar(tr("Select the features that make the area first (a part of the work, a district…), "
                         "on any layer."), 1)
            return
        self.node.hub.set_area(m["id"], a)
        self._show_areas()
        self._bar(tr("{n} may now change features only inside the area shown on the map.").format(
            n=m.get("name", "")), 0)

    def _clear_areas(self):
        for b in self._area_bands:
            try:
                self.canvas.scene().removeItem(b)
            except RuntimeError:
                pass
        self._area_bands = []
        self._areas_key = None

    def _show_areas(self):
        """The areas of work on the map: every member's on the host, your own on a member's computer."""
        n = self.node
        want = {}
        if n is not None and n.role == "host" and n.hub is not None:
            for m in n.hub.store.members():
                a = area.of(m.get("rights"))
                if a and m.get("state") != "out":
                    want[m["id"]] = a
        elif n is not None:
            a = area.of((n.member or {}).get("rights"))
            if a:
                want[n.me_id or ""] = a
        key = json.dumps(want, sort_keys=True)
        if key == self._areas_key:
            return
        self._clear_areas()
        self._areas_key = key
        dest = self.canvas.mapSettings().destinationCrs()
        for mid, a in want.items():
            g = QgsGeometry.fromWkt(a["wkt"])
            src = QgsCoordinateReferenceSystem(a["crs"])
            if src.isValid() and dest.isValid() and src != dest:
                g.transform(QgsCoordinateTransform(src, dest, QgsProject.instance()))
            b = QgsRubberBand(self.canvas, Qgis.GeometryType.Polygon)
            b.setToGeometry(g, None)
            c = color_of(mid)
            b.setStrokeColor(c)
            fill = QColor(c)
            fill.setAlpha(18)
            b.setFillColor(fill)
            b.setWidth(2)
            b.setLineStyle(Qt.PenStyle.DashLine)
            self._area_bands.append(b)

    def _member_menu(self, pos):
        n = self.node
        it = self.members.itemAt(pos)
        if n is None or it is None or n.role != "host":
            return
        m = it.data(0, Qt.ItemDataRole.UserRole)
        if m.get("host"):
            return
        menu = QMenu(self)
        for key, lyr in n.layers.items():
            sub = menu.addMenu(lyr.name())
            for right, label in (("edit", tr("Can edit")), ("nodelete", tr("Can edit, not delete")),
                                 ("view", tr("Can only view"))):
                a = sub.addAction(label)
                a.setCheckable(True)
                a.setChecked((m.get("rights") or {}).get(key, "edit") == right)
                a.triggered.connect(lambda _=False, k=key, r=right: n.hub.set_rights(m["id"], k, r))
        zone = menu.addMenu(tr("Area of work"))
        a = zone.addAction(tr("The selected features"))
        a.triggered.connect(lambda: self._set_area(m))
        a = zone.addAction(tr("Anywhere"))
        a.setCheckable(True)
        a.setChecked(area.of(m.get("rights")) is None)
        a.triggered.connect(lambda: (n.hub.set_area(m["id"], None), self._show_areas()))
        menu.addSeparator()
        back = menu.addMenu(tr("Take back this member's work"))
        for label, secs in ((tr("of the last hour"), 3600), (tr("of today"), None), (tr("all of it"), 0)):
            a = back.addAction(label)
            a.triggered.connect(lambda _=False, s=secs: self._revert_member(m, s))
        menu.addSeparator()
        rm = menu.addAction(tr("Remove from the room"))
        rm.triggered.connect(lambda: n.hub.remove_member(m["id"]))
        menu.exec(self.members.viewport().mapToGlobal(pos))

    def _revert_member(self, m, secs):
        n = self.node
        if n is None or n.role != "host":
            return
        if secs is None:
            since = time.mktime(dt.date.today().timetuple())
        else:
            since = time.time() - secs if secs else 0
        count = len({(e["layer"], e["fid"]) for e in n.store.by_author_since(m["id"], since)})
        if not count:
            self._bar(tr("{name} changed nothing in that time.").format(name=m.get("name", "")), 0)
            return
        r = QMessageBox.question(self, tr("Take back this member's work"), tr(
            "Put the {n} features {name} changed back as they were before? What others changed in them afterwards "
            "is kept. Everyone gets the result; each feature can still be brought back from its history.").format(
            n=count, name=m.get("name", "")))
        if r != QMessageBox.StandardButton.Yes:
            return
        done, left = n.revert_member(m["id"], since)
        text = tr("{d} features taken back.").format(d=done)
        if left:
            text += " " + tr("{k} were left as they are: others changed them too much since.").format(k=left)
        self._bar(text, 2)

    def _held_deletes(self, member_id):
        """The guard stopped a member deleting a lot at once: the host says yes or no."""
        n = self.node
        if n is None or n.hub is None:
            return
        name = next((m.get("name", "") for m in n.hub.store.members() if m["id"] == member_id), "?")
        bar = self.iface.messageBar()
        item = bar.createMessage(tr("Team room"), html.escape(tr(
            "{name} is deleting many features at once. Their deletions wait for you.").format(name=name)))
        yes, no = QPushButton(tr("Allow")), QPushButton(tr("Refuse"))

        def answer(allow):
            k = n.hub.release(member_id, allow) if n.hub is not None else 0
            bar.popWidget(item)
            self._bar((tr("{k} deletions carried out.") if allow else tr("{k} deletions refused; the features stay."))
                      .format(k=k), 2)
        yes.clicked.connect(lambda: answer(True))
        no.clicked.connect(lambda: answer(False))
        item.layout().addWidget(yes)
        item.layout().addWidget(no)
        from qgis.core import Qgis

        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)

    def _go_to_member(self, it, _col):
        p = (self.node.presence if self.node else {}).get(it.data(0, Qt.ItemDataRole.UserRole)["id"])
        if p and p.get("e"):
            self.canvas.setExtent(QgsRectangle(*p["e"]))
            self.canvas.refresh()

    def _go_to_entry(self, it):
        e = it.data(Qt.ItemDataRole.UserRole)
        lyr = self.node.layers.get(e["layer"]) if self.node else None
        if lyr is None:
            return
        f = lyr.getFeature(int(e["fid"]))
        if f.isValid() and f.hasGeometry():
            lyr.selectByIds([f.id()])
            self.canvas.zoomToSelected(lyr)

    def _current_conflict(self):
        it = self.conflicts.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _show_conflict(self):
        c = self._current_conflict()
        if c is None:
            return
        for b in self._bands:
            self.canvas.scene().removeItem(b)
        self._bands = []
        lyr = self.node.layers.get(c["layer"])
        if lyr is None:
            return
        for state, color in (((c.get("mine") or {}).get("g"), "#D6336C"), ((c.get("theirs") or {}).get("g"), "#1C7ED6")):
            if not state:
                continue
            g = QgsGeometry()
            g.fromWkb(bytes.fromhex(state))
            b = QgsRubberBand(self.canvas, g.type())
            b.setStrokeColor(QColor(color))
            fill = QColor(color)
            fill.setAlpha(40)
            b.setFillColor(fill)
            b.setWidth(2)
            b.setToGeometry(g, lyr)
            self._bands.append(b)
        f = lyr.getFeature(int(c["fid"]))
        if f.isValid():
            lyr.selectByIds([f.id()])
            self.canvas.zoomToSelected(lyr)
        self._bar(tr("Pink: your version. Blue: the other version."), 0, 6)

    def _resolve(self, mine):
        c = self._current_conflict()
        if c is None:
            return
        for b in self._bands:
            self.canvas.scene().removeItem(b)
        self._bands = []
        self.node.resolve(c, mine)

    def _decide(self, accept):
        it = self.waiting.currentItem() or (self.waiting.item(0) if self.waiting.count() else None)
        if it is not None and self.node and self.node.hub:
            self.node.hub.decide(it.data(Qt.ItemDataRole.UserRole), accept)
            self._panel_numbers()

    def _approve_toggled(self, on):
        if self.node and self.node.hub:
            self.node.hub.auto_accept = not on
            self.node.store.put("approve", bool(on))

    def _outside_toggled(self, on):
        if not self.node:
            return
        if on:
            self.node.start_external()
            self._bar(tr("Opening an encrypted tunnel for members outside this network…"), 0, 6)
        else:
            self.node.stop_external()

    def _change_code(self):
        """A new code for the room: after removing a member (who still knows the old one) or if the code leaked."""
        if not (self.node and self.node.hub):
            return
        r = QMessageBox.question(self, tr("Change the room code"), tr(
            "Make a new code for this room? The members in the room now get it sealed for them and switch by "
            "themselves in a few seconds. Removed members, and anyone else who knows the old code, can no longer "
            "find, read or join the room. Members who are not connected now will need the new code from you."))
        if r != QMessageBox.StandardButton.Yes:
            return
        new = self.node.change_code()
        if new:
            QMessageBox.information(self, tr("Change the room code"), tr(
                "The new code is:\n\n{c}\n\nThe room switches to it in 20 seconds. Give it to members who were "
                "not connected.").format(c=new))

    def _freeze_toggled(self, on):
        if self.node and self.node.hub:
            self.node.hub.frozen = on
            self.node.hub._bump()

    def _send_notice(self):
        text = self.notice.text().strip()
        if text and self.node and self.node.hub:
            self.node.hub.notice = {"text": f"{self.node.ident['name']}: {text}", "ts": time.time()}
            self.node.hub._bump()
            self.notice.clear()
            self._bar(tr("Sent to everyone in the room."), 2, 4)

    def _backup(self, quiet=False):
        if not (self.node and self.node.hub):
            return
        self._backup_at = time.time()
        folder = os.path.join(os.path.dirname(self.node.store.path), "backups")
        try:
            dest = self.node.hub.backup(folder)
            if not quiet:
                self._bar(tr("Backed up to {p}").format(p=dest), 2, 8)
        except Exception as e:  # noqa: BLE001
            self._bar(tr("The backup failed: {e}").format(e=e), 1)

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Export the change log"), "room-changes.csv", "CSV (*.csv)")
        if not path:
            return
        n = self.node
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["seq", "time", "member", "layer", "feature", "change", "note", "signature"])
            seq = 0
            while True:
                rows = n.store.since(seq, 2000)
                if not rows:
                    break
                for e in rows:
                    lyr = n.layers.get(e["layer"])
                    w.writerow([e["seq"], dt.datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds"), e["name"],
                                lyr.name() if lyr else e["layer"], e["fid"], e["kind"], e["note"] or "", e["sig"] or ""])
                seq = rows[-1]["seq"]
        self._bar(tr("Saved {p}").format(p=path), 2, 6)

    def _heat(self):
        n = self.node
        crs = QgsProject.instance().crs()
        out = QgsVectorLayer(f"Point?crs={crs.authid() or 'EPSG:4326'}&field=member:string&field=time:string",
                             tr("Today's team work"), "memory")
        feats = []
        since = time.time() - 86400
        seq = 0
        while True:
            rows = n.store.since(seq, 5000)
            if not rows:
                break
            seq = rows[-1]["seq"]
            for e in rows:
                if e["ts"] < since:
                    continue
                st = e.get("state") or e.get("prev") or {}
                lyr = n.layers.get(e["layer"])
                if not st.get("g") or lyr is None:
                    continue
                g = QgsGeometry()
                g.fromWkb(bytes.fromhex(st["g"]))
                c = g.centroid()
                if lyr.crs() != crs:
                    try:
                        c.transform(QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance()))
                    except Exception:  # noqa: BLE001
                        continue
                f = QgsFeature(out.fields())
                f.setAttributes([e["name"], _when(e["ts"])])
                f.setGeometry(c)
                feats.append(f)
        if not feats:
            self._bar(tr("No changes today yet."), 0, 4)
            return
        out.dataProvider().addFeatures(feats)
        r = QgsHeatmapRenderer()
        r.setRadius(18)
        ramp = QgsStyle.defaultStyle().colorRamp("Inferno") or QgsStyle.defaultStyle().colorRamp("Reds")
        if ramp:
            r.setColorRamp(ramp)
        out.setRenderer(r)
        out.setOpacity(0.75)
        QgsProject.instance().addMapLayer(out)

    def _probe(self):
        n = self.node
        if not (n and n.hub):
            return
        t0 = time.perf_counter()
        found = wire.find_on_network(n.keys, wire.Sealer(n.keys.key), wait=1.2)
        ms = (time.perf_counter() - t0) * 1000
        mine = [f for f in found if f.get("hid") == n.me_id]
        if mine:
            self._bar(tr("The room answers on this network ({ms} ms). Members here can find it by the code.").format(
                ms=round(ms)), 2, 8)
        else:
            self._bar(tr("The room does not answer on this network: Windows Firewall may be blocking Biruni GIS. "
                         "Allow it on private networks."), 1, 12)

    def shutdown(self):
        self._live.stop()
        self.overlay.clear()
        self._clear_areas()
        try:
            self.canvas.xyCoordinates.disconnect(self._cursor)
        except (TypeError, RuntimeError):
            pass
        if self.node is not None:
            node = self.node
            self.attach(None)
            session.close_room(node)


_DOCK = {}


def open_dock(plugin):
    d = _DOCK.get(id(plugin))
    if d is None:
        d = TeamDock(plugin)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        d.refresh_theme()
        _DOCK[id(plugin)] = d
        QgsApplication.instance().aboutToQuit.connect(d.shutdown)
    elif current() is not d.node:
        d.attach(current())
    d.show()
    d.raise_()
    return d


def close_dock(plugin):
    d = _DOCK.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()


def on_project_read(plugin):
    """A map that belongs to a room was opened: follow the room again and show the panel."""
    if current() is not None:
        d = _DOCK.get(id(plugin))
        if d is not None:
            d.attach(None)
        session.close_room(current())
    node = session.reopen()
    if node is not None:
        open_dock(plugin).attach(node)
