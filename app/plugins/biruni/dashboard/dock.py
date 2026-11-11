"""The dashboards panel: the project's dashboards (new, edit, delete), sharing them live from this PC (on this
network and/or from anywhere), the cards that say who sees what (issued with their QR code and link, revoked
in one click), the viewers open now, the big-screen view on this PC, and "Check a seal".
"""
import time

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
                       QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QDateTime, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices, QGuiApplication, QImage, QPixmap
from qgis.PyQt.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog,
                                 QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QTabWidget,
                                 QVBoxLayout, QWidget)

from ..i18n import tr
from . import model

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MAX_ZONE_POINTS = 400        # a zone is simplified to keep the link (and its QR code) short


# ---- cards --------------------------------------------------------------------------------------------------------
def card_text(payload):
    """The card text of an issued card's payload (the signature is deterministic: the same card again)."""
    from ..secure import crypto
    from . import cards

    body = cards._body(payload)
    return f"{cards.PREFIX}.{cards.b64u(body)}.{cards.b64u(crypto.sign(cards._seed(), body))}"


def zone_wkt(geom, crs):
    """A zone geometry (in ``crs``) as short WGS84 WKT for a card."""
    g = QgsGeometry(geom)
    if crs is not None and crs.isValid() and crs != WGS84:
        g.transform(QgsCoordinateTransform(crs, WGS84, QgsProject.instance()))
    tol = 0.000005
    while _points(g) > MAX_ZONE_POINTS and tol < 0.05:
        g = g.simplify(tol)
        tol *= 2
    g = g.makeValid() if not g.isGeosValid() else g
    return g.asWkt(6)


def _points(g):
    n = 0
    for _ in g.vertices():
        n += 1
    return n


def issue_card(d, name, widgets="*", hide=(), zone=None, filters=None, days=30, project=None):
    """A card for dashboard ``d``; zone is (geometry, crs) or None. -> card text."""
    from . import cards

    wkt = zone_wkt(*zone) if zone else None
    return cards.issue(d["id"], name, widgets=widgets, hide=tuple(hide), zone_wkt=wkt, filters=filters or None,
                       days=days, project=project or QgsProject.instance())


def all_fields(d):
    out = []
    for s in (d.get("sources") or {}).values():
        lyr = QgsProject.instance().mapLayer(s.get("layer") or "")
        if lyr is not None:
            for f in lyr.fields():
                if f.name() not in out:
                    out.append(f.name())
    return out


def qr_pixmap(text, scale=5):
    import io

    import segno

    buf = io.BytesIO()
    err = "m" if len(text) < 1200 else "l"
    segno.make(text, error=err).save(buf, kind="png", scale=scale, border=2, dark="#111", light="#fff")
    img = QImage.fromData(buf.getvalue())
    return QPixmap.fromImage(img)


# ---- drawing a zone on the map ----------------------------------------------------------------------------------
def _zone_tool_class():
    from qgis.gui import QgsMapTool, QgsRubberBand

    class ZoneTool(QgsMapTool):
        """Click the corners of a zone; right-click (or Enter) to finish, Esc to give up."""
        done = pyqtSignal(object)

        def __init__(self, canvas):
            super().__init__(canvas)
            self.points = []
            self.band = QgsRubberBand(canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
            self.band.setColor(QColor(21, 101, 192, 90))
            self.band.setStrokeColor(QColor(21, 101, 192))
            self.band.setWidth(2)

        def canvasReleaseEvent(self, e):  # noqa: N802
            if e.button() == Qt.MouseButton.RightButton:
                self._finish()
                return
            self.points.append(QgsPointXY(e.mapPoint()))
            self.band.setToGeometry(QgsGeometry.fromPolygonXY([self.points]), None)

        def keyPressEvent(self, e):  # noqa: N802
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish()
            elif e.key() == Qt.Key.Key_Escape:
                self.points = []
                self._finish()

        def _finish(self):
            g = QgsGeometry.fromPolygonXY([self.points]) if len(self.points) >= 3 else None
            self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
            self.canvas().unsetMapTool(self)
            self.done.emit(g)

        def deactivate(self):
            self.band.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
            super().deactivate()

    return ZoneTool


# ---- issuing a card ----------------------------------------------------------------------------------------------
class CardDialog(QDialog):
    """Who sees what: the card's name, its widgets, the fields kept back, its zone, forced filters, its life."""

    def __init__(self, d, plugin=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Share a dashboard"))
        self.d = d
        self.plugin = plugin
        self.zone = None           # (geometry, crs)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("who gets it, e.g. Supervisor North"))
        f.addRow(tr("Name"), self.name)
        self.days = QSpinBox()
        self.days.setRange(1, 3650)
        self.days.setValue(30)
        self.days.setSuffix(" " + tr("days"))
        f.addRow(tr("Valid for"), self.days)
        lay.addLayout(f)

        g = QGroupBox(tr("Widgets they see"))
        gl = QVBoxLayout(g)
        self.widgets = QListWidget()
        for w in d.get("widgets") or []:
            it = QListWidgetItem(w.get("title") or w["type"])
            it.setData(Qt.ItemDataRole.UserRole, w["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.widgets.addItem(it)
        gl.addWidget(self.widgets)
        lay.addWidget(g)

        g = QGroupBox(tr("Fields kept back (never leave this PC)"))
        gl = QVBoxLayout(g)
        self.hide = QListWidget()
        for name in all_fields(d):
            it = QListWidgetItem(name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.hide.addItem(it)
        self.hide.setMaximumHeight(120)
        gl.addWidget(self.hide)
        lay.addWidget(g)

        g = QGroupBox(tr("Zone"))
        gl = QVBoxLayout(g)
        self.z_all = QRadioButton(tr("The whole data"))
        self.z_all.setChecked(True)
        self.z_sel = QRadioButton(tr("Only inside the selected polygon(s)"))
        self.z_draw = QRadioButton(tr("Only inside a zone I draw on the map"))
        for b in (self.z_all, self.z_sel, self.z_draw):
            gl.addWidget(b)
        self.z_info = QLabel(tr("They see only the features inside the zone; every number is computed on those."))
        self.z_info.setWordWrap(True)
        gl.addWidget(self.z_info)
        self.z_sel.toggled.connect(lambda on: on and self._selected_zone())
        self.z_draw.toggled.connect(lambda on: on and self._draw_zone())
        lay.addWidget(g)

        self.forced = {}
        if d.get("filters"):
            g = QGroupBox(tr("Filters fixed for them"))
            fl = QFormLayout(g)
            for flt in d["filters"]:
                e = QLineEdit()
                e.setPlaceholderText(tr("free (they choose); or values separated by commas"))
                fl.addRow(flt.get("label") or flt.get("field"), e)
                self.forced[flt["id"]] = e
            lay.addWidget(g)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Make the card"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.card = None

    def _selected_zone(self):
        lyr = self.plugin.iface.activeLayer() if self.plugin is not None else None
        geoms = []
        if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry:
            geoms = [f.geometry() for f in lyr.selectedFeatures() if f.hasGeometry()]
        if not geoms:
            QMessageBox.information(self, tr("Zone"), tr("Select one or more polygons in the active layer first."))
            self.z_all.setChecked(True)
            return
        self.zone = (QgsGeometry.unaryUnion(geoms), lyr.crs())
        self.z_info.setText(tr("Zone: {n} selected polygon(s) of {layer}.").format(n=len(geoms), layer=lyr.name()))

    def _draw_zone(self):
        if self.plugin is None:
            return
        canvas = self.plugin.iface.mapCanvas()
        self.tool = _zone_tool_class()(canvas)

        def got(g):
            self.show()
            self.raise_()
            if g is None:
                self.z_all.setChecked(True)
                return
            self.zone = (g, canvas.mapSettings().destinationCrs())
            self.z_info.setText(tr("Zone drawn on the map."))

        self.tool.done.connect(got)
        canvas.setMapTool(self.tool)
        self.hide_for_drawing()

    def hide_for_drawing(self):
        self.setModal(False)
        self.hide()
        if self.plugin is not None:
            self.plugin.iface.messageBar().pushMessage(
                tr("Share a dashboard"), tr("Click the corners of the zone; right-click to finish."), duration=8)

    def choices(self):
        ws = [self.widgets.item(i) for i in range(self.widgets.count())]
        picked = [it.data(Qt.ItemDataRole.UserRole) for it in ws if it.checkState() == Qt.CheckState.Checked]
        widgets = "*" if len(picked) == len(ws) else picked
        hide = [self.hide.item(i).text() for i in range(self.hide.count())
                if self.hide.item(i).checkState() == Qt.CheckState.Checked]
        filters = {}
        for fid, e in self.forced.items():
            vals = [v.strip() for v in e.text().split(",") if v.strip()]
            if vals:
                filters[fid] = vals
        zone = self.zone if not self.z_all.isChecked() else None
        return {"name": self.name.text().strip() or tr("Viewer"), "widgets": widgets, "hide": hide,
                "zone": zone, "filters": filters or None, "days": self.days.value()}

    def _ok(self):
        c = self.choices()
        if c["widgets"] != "*" and not c["widgets"]:
            QMessageBox.information(self, tr("Share a dashboard"), tr("Choose at least one widget."))
            return
        try:
            self.card = issue_card(self.d, c["name"], c["widgets"], c["hide"], c["zone"], c["filters"], c["days"])
        except ValueError as e:
            QMessageBox.warning(self, tr("Share a dashboard"), str(e))
            return
        self.accept()


class LinksDialog(QDialog):
    """The card's links and QR codes: on this network, and from anywhere."""

    def __init__(self, links, name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Links for {name}").format(name=name))
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        self.texts = []
        items = []
        if links.get("lan"):
            items.append((tr("On this network"), links["lan"][0],
                          tr("For phones on the same Wi-Fi as this PC. Nothing goes out to the internet.")))
        if links.get("anywhere"):
            items.append((tr("From anywhere"), links["anywhere"],
                          tr("For phones anywhere: a direct encrypted link to this PC, through public relays that "
                             "see only unreadable bytes.")))
        for title, url, note in items:
            w = QWidget()
            wl = QVBoxLayout(w)
            q = QLabel()
            q.setAlignment(Qt.AlignmentFlag.AlignCenter)
            try:
                q.setPixmap(qr_pixmap(url))
            except Exception:  # noqa: BLE001 - too long for a QR code: the link still works
                q.setText(tr("The link is too long for a QR code; send it as text."))
            wl.addWidget(q)
            n = QLabel(note)
            n.setWordWrap(True)
            wl.addWidget(n)
            e = QLineEdit(url)
            e.setReadOnly(True)
            e.setCursorPosition(0)
            wl.addWidget(e)
            b = QPushButton(tr("Copy the link"))
            b.clicked.connect(lambda _c=False, u=url: QGuiApplication.clipboard().setText(u))
            wl.addWidget(b)
            tabs.addTab(w, title)
            self.texts.append(url)
        if not items:
            lay.addWidget(QLabel(tr("Turn sharing on first.")))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


# ---- checking a seal ----------------------------------------------------------------------------------------------
def check_seal_text(text, pub=None):
    """{"ok": bool, "statement": …, "why": …} for a seal code (of this PC) or a pasted BDS1 text."""
    from . import seals

    text = (text or "").strip()
    if not text:
        return {"ok": False, "why": tr("Paste a seal code or a seal text.")}
    if text.startswith("BDS1."):
        got = seals.from_text(text)
        if got is None:
            return {"ok": False, "why": tr("This is not a seal text.")}
        statement, sig = got
    else:
        found = seals.lookup(text)
        if not found:
            return {"ok": False, "why": tr("This seal code was not made on this computer. Ask for the seal text "
                                           "(tap the number on the phone) and paste it here.")}
        statement, sig = found["statement"], found["sig"]
    ok = seals.verify(statement, sig, pub)
    return {"ok": ok, "statement": statement,
            "why": "" if ok else tr("The signature does not match: the number or its details were changed.")}


def describe_statement(st):
    lines = [tr("Dashboard: {d}").format(d=st.get("dash", "")),
             tr("Widget: {w}").format(w=st.get("title") or st.get("widget", "")),
             tr("Value: {v}").format(v=_short(st.get("value"))),
             tr("When: {t}").format(t=_when(st.get("ts")))]
    try:
        import datetime

        from .. import calendars

        day = datetime.date.fromtimestamp(float(st.get("ts")))
        ymd, _exact = calendars.from_gregorian(day, "hijri")
        lines[-1] += "  (" + calendars.format_date("hijri", ymd) + ")"
    except Exception:  # noqa: BLE001 - the Gregorian time is enough
        pass
    card = st.get("card") or {}
    if card:
        lines.append(tr("Seen through the card: {name}").format(name=card.get("name") or card.get("id", "")))
    if st.get("filters"):
        lines.append(tr("Filters: {f}").format(f=", ".join(f"{k}={v}" for k, v in st["filters"].items())))
    return lines


def _short(v):
    s = str(v)
    return s if len(s) < 300 else s[:300] + "…"


def _when(ts):
    try:
        return QDateTime.fromSecsSinceEpoch(int(ts)).toString("yyyy-MM-dd hh:mm:ss")
    except (TypeError, ValueError):
        return "?"


class SealDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Check a seal"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Every number of a Biruni dashboard carries a seal. Paste its code (as on the "
                                "screenshot) or its seal text to see whether the number is genuine.")))
        self.input = QPlainTextEdit()
        self.input.setMaximumHeight(90)
        self.input.setPlaceholderText("K7QM-2D9X  /  BDS1.…")
        lay.addWidget(self.input)
        row = QHBoxLayout()
        chk = QPushButton(tr("Check"))
        chk.clicked.connect(self.check)
        img = QPushButton(tr("From a picture of the QR code…"))
        img.clicked.connect(self._from_image)
        self.again = QPushButton(tr("Compute it again from the data"))
        self.again.clicked.connect(self._recheck)
        self.again.setEnabled(False)
        row.addWidget(chk)
        row.addWidget(img)
        row.addWidget(self.again)
        row.addStretch(1)
        lay.addLayout(row)
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.result, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.statement = None
        self.resize(560, 420)

    def check(self):
        r = check_seal_text(self.input.toPlainText())
        self.statement = r.get("statement") if r["ok"] else None
        self.again.setEnabled(self.statement is not None)
        if r["ok"]:
            self.result.setText("✔ " + tr("Genuine: signed by the computer that showed it.") + "\n\n"
                                + "\n".join(describe_statement(r["statement"])))
            self.result.setStyleSheet("color:#2e7d32")
        else:
            self.result.setText("✘ " + r["why"])
            self.result.setStyleSheet("color:#c62828")
        return r

    def _from_image(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("A picture of the QR code"), "",
                                              tr("Images") + " (*.png *.jpg *.jpeg *.bmp)")
        if not path:
            return
        text = read_qr(path)
        if not text:
            self.result.setText(tr("No QR code was found in the picture."))
            return
        self.input.setPlainText(text)
        self.check()

    def _recheck(self):
        from . import seals

        r = seals.recheck(self.statement)
        if r.get("error"):
            self.result.setText(self.result.text() + "\n\n" + tr("Cannot compute it again: {why}").format(why=r["error"]))
        elif r.get("same"):
            self.result.setText(self.result.text() + "\n\n" + tr("The data on this computer gives the same number."))
        else:
            self.result.setText(self.result.text() + "\n\n" + tr("The data has changed since: the number now is {v}.")
                                .format(v=_short(r.get("value"))))


def read_qr(path):
    """The text of the first QR code in an image file, or None."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    try:
        import numpy as np
        import zxingcpp
    except ImportError:
        return None
    img = QImage(path)
    if img.isNull():
        return None
    img = img.convertToFormat(QImage.Format.Format_Grayscale8)
    a = np.frombuffer(img.constBits().asstring(img.sizeInBytes()), np.uint8).reshape(img.height(), img.bytesPerLine())
    got = zxingcpp.read_barcodes(np.ascontiguousarray(a[:, :img.width()]))
    return got[0].text if got else None


# ---- the panel ----------------------------------------------------------------------------------------------------
class DashboardDock(QDockWidget):
    def __init__(self, plugin, service=None, sharing_factory=None):
        super().__init__(tr("Live dashboards"), plugin.iface.mainWindow() if plugin is not None else None)
        self.setObjectName("BiruniDashboards")
        self.plugin = plugin
        self._service = service
        self._sharing_factory = sharing_factory
        self.sharing = None
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)

        lay.addWidget(QLabel(tr("Dashboards of this project")))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._selected)
        self.list.itemDoubleClicked.connect(lambda _i: self.edit())
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in ((tr("New"), self.new), (tr("Edit"), self.edit), (tr("Delete"), self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

        g = QGroupBox(tr("Share live from this PC"))
        gl = QVBoxLayout(g)
        self.on_lan = QCheckBox(tr("On this network (Wi-Fi of the office)"))
        self.on_lan.toggled.connect(self._toggle_lan)
        self.on_any = QCheckBox(tr("From anywhere (direct encrypted link, no server)"))
        self.on_any.toggled.connect(self._toggle_any)
        gl.addWidget(self.on_lan)
        gl.addWidget(self.on_any)
        self.state = QLabel(tr("Not shared."))
        self.state.setWordWrap(True)
        gl.addWidget(self.state)
        row = QHBoxLayout()
        self.share_b = QPushButton(tr("Give someone a card…"))
        self.share_b.clicked.connect(self.share)
        self.tv_b = QPushButton(tr("Big screen on this PC"))
        self.tv_b.clicked.connect(self.tv)
        row.addWidget(self.share_b)
        row.addWidget(self.tv_b)
        gl.addLayout(row)
        lay.addWidget(g)

        g = QGroupBox(tr("Cards (who sees what)"))
        gl = QVBoxLayout(g)
        self.cards = QListWidget()
        gl.addWidget(self.cards)
        row = QHBoxLayout()
        self.links_b = QPushButton(tr("Links and QR"))
        self.links_b.clicked.connect(self.show_links)
        self.revoke_b = QPushButton(tr("Revoke"))
        self.revoke_b.clicked.connect(self.revoke)
        row.addWidget(self.links_b)
        row.addWidget(self.revoke_b)
        gl.addLayout(row)
        lay.addWidget(g)

        row = QHBoxLayout()
        seal = QPushButton(tr("Check a seal…"))
        seal.clicked.connect(lambda: SealDialog(self).exec())
        row.addWidget(seal)
        more = self._more_button()
        if more is not None:
            row.addWidget(more)
        lay.addLayout(row)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.refresh()

    def _more_button(self):
        """"More": the actions of the dashboard extensions (ext_*.py ACTIONS), or None when there are none."""
        from qgis.PyQt.QtWidgets import QMenu, QToolButton

        from .page import extensions

        acts = []
        for mod in extensions():
            acts.extend(getattr(mod, "ACTIONS", None) or [])
        if not acts:
            return None
        b = QToolButton()
        b.setText(tr("More"))
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(b)
        for act in acts:
            try:
                _aid, label, icon_name, fn = act[:4]
            except ValueError:
                continue
            a = menu.addAction(tr(label))
            try:
                from .. import icons

                a.setIcon(icons.icon(icon_name, self.palette().text().color().name()))
            except Exception:  # noqa: BLE001 - an icon never stops the menu
                pass
            a.triggered.connect(lambda _c=False, f=fn: self._run_action(f))
        b.setMenu(menu)
        return b

    def _run_action(self, fn):
        try:
            fn(self.plugin, self)
        except Exception as e:  # noqa: BLE001 - an extension's fault is shown, never raised into Qt
            from qgis.PyQt.QtWidgets import QMessageBox

            QMessageBox.warning(self, tr("Live dashboards"), str(e)[:400])

    # ---- the service and the share ------------------------------------------------------------------------------
    def service(self):
        if self._service is None:
            from .service import DashboardService

            self._service = DashboardService(QgsProject.instance())
        return self._service

    def _make_sharing(self):
        if self._sharing_factory is not None:
            return self._sharing_factory(self.service())
        from ..i18n import lang
        from .sharing import Sharing

        return Sharing(self.service(), lang=lang() or "en")

    def _toggle_lan(self, on):
        if on:
            try:
                if self.sharing is None:
                    self.sharing = self._make_sharing()
                self.sharing.start()
            except OSError as e:
                QMessageBox.warning(self, tr("Live dashboards"), tr("Sharing could not start: {why}").format(why=e))
                self.on_lan.blockSignals(True)
                self.on_lan.setChecked(False)
                self.on_lan.blockSignals(False)
        elif not self.on_any.isChecked():
            self.stop_sharing()
        self._tick()

    def _toggle_any(self, on):
        if on:
            try:
                if self.sharing is None:
                    self.sharing = self._make_sharing()
                self.sharing.start_anywhere()
            except OSError as e:
                QMessageBox.warning(self, tr("Live dashboards"), tr("Sharing could not start: {why}").format(why=e))
                self.on_any.blockSignals(True)
                self.on_any.setChecked(False)
                self.on_any.blockSignals(False)
        elif self.sharing is not None:
            self.sharing.stop_anywhere()
            if not self.on_lan.isChecked():
                self.stop_sharing()
        self._tick()

    def stop_sharing(self):
        if self.sharing is not None:
            self.sharing.stop()
            self.sharing = None

    def _tick(self):
        if self.sharing is None or not self.sharing.running():
            self.state.setText(tr("Not shared."))
            return
        viewers = self.sharing.viewers()
        where = []
        if self.on_lan.isChecked():
            where.append(tr("on this network"))
        if self.sharing.anywhere is not None and self.sharing.anywhere.running():
            where.append(tr("from anywhere"))
        self.state.setText(tr("Shared {where}. Viewers now: {n}.").format(where=" + ".join(where) or "—",
                                                                         n=len(viewers)))
        self._fill_cards(viewers)

    # ---- dashboards -----------------------------------------------------------------------------------------------
    def dashboards(self):
        return model.load_all(QgsProject.instance())

    def current(self):
        it = self.list.currentItem()
        if it is None:
            return None
        return model.get(QgsProject.instance(), it.data(Qt.ItemDataRole.UserRole))

    def refresh(self, select=None):
        keep = select or (self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None)
        self.list.blockSignals(True)
        self.list.clear()
        for d in self.dashboards():
            it = QListWidgetItem(d.get("title") or d["id"])
            it.setData(Qt.ItemDataRole.UserRole, d["id"])
            it.setToolTip(tr("{n} widgets").format(n=len(d.get("widgets") or [])))
            self.list.addItem(it)
            if d["id"] == keep:
                self.list.setCurrentItem(it)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._selected()

    def _selected(self, *_a):
        self._fill_cards(self.sharing.viewers() if self.sharing is not None else [])
        has = self.current() is not None
        for b in (self.share_b, self.tv_b):
            b.setEnabled(has)

    def _fill_cards(self, viewers=()):
        d = self.current()
        keep = self.cards.currentItem().data(Qt.ItemDataRole.UserRole) if self.cards.currentItem() else None
        self.cards.clear()
        if d is None:
            return
        from . import cards

        revoked = cards.revoked(QgsProject.instance())
        live = {}
        for v in viewers or []:
            live[v.get("card")] = live.get(v.get("card"), 0) + 1
        now = time.time()
        for p in cards.issued(d["id"], QgsProject.instance()):
            state = (tr("revoked") if p["id"] in revoked else tr("expired") if float(p.get("exp") or 0) < now
                     else tr("{n} open now").format(n=live[p["id"]]) if live.get(p["id"]) else tr("valid"))
            extra = []
            if p.get("zone"):
                extra.append(tr("zone"))
            if p.get("hide"):
                extra.append(tr("{n} fields kept back").format(n=len(p["hide"])))
            if p.get("widgets") != "*":
                extra.append(tr("{n} widgets").format(n=len(p.get("widgets") or [])))
            it = QListWidgetItem(f"{p.get('name') or p['id']}  ·  {state}" + (f"  ·  {', '.join(extra)}" if extra else ""))
            it.setData(Qt.ItemDataRole.UserRole, p["id"])
            it.setData(Qt.ItemDataRole.UserRole + 1, p)
            self.cards.addItem(it)
            if p["id"] == keep:
                self.cards.setCurrentItem(it)

    def new(self):
        from .builder import BuilderDialog

        dlg = BuilderDialog(None, self, lang=_lang())
        dlg.saved.connect(lambda i: self._saved(i))
        dlg.exec()

    def edit(self):
        d = self.current()
        if d is None:
            return self.new()
        from .builder import BuilderDialog

        dlg = BuilderDialog(d, self, lang=_lang())
        dlg.saved.connect(lambda i: self._saved(i))
        dlg.exec()

    def _saved(self, dash_id):
        try:
            self.service().reload()
        except Exception:  # noqa: BLE001
            pass
        self.refresh(select=dash_id)

    def delete(self, ask=True):
        d = self.current()
        if d is None:
            return
        if ask and QMessageBox.question(self, tr("Live dashboards"),
                                        tr("Delete the dashboard «{t}»? Its cards stop working.").format(
                                            t=d.get("title") or d["id"])) != QMessageBox.StandardButton.Yes:
            return
        from . import cards

        for p in cards.issued(d["id"], QgsProject.instance()):
            cards.revoke(p["id"], d["id"], QgsProject.instance())
        model.delete(QgsProject.instance(), d["id"])
        self._saved(None)

    # ---- cards ------------------------------------------------------------------------------------------------------
    def share(self):
        d = self.current()
        if d is None:
            return None
        if self.sharing is None or not self.sharing.running():
            self.on_lan.setChecked(True)
        dlg = CardDialog(d, self.plugin, self)
        if not dlg.exec() or not dlg.card:
            return None
        self._fill_cards()
        LinksDialog(self.sharing.links(dlg.card) if self.sharing else {}, dlg.choices()["name"], self).exec()
        return dlg.card

    def selected_card(self):
        it = self.cards.currentItem()
        return it.data(Qt.ItemDataRole.UserRole + 1) if it is not None else None

    def show_links(self):
        p = self.selected_card()
        if p is None:
            return
        if self.sharing is None or not self.sharing.running():
            self.on_lan.setChecked(True)
        links = self.sharing.links(card_text(p))
        if links.get("anywhere"):
            from ..secure.door import door_ok

            if not door_ok(self, links["anywhere"]):
                links["anywhere"] = None
        LinksDialog(links, p.get("name") or p["id"], self).exec()

    def revoke(self, ask=True):
        p = self.selected_card()
        if p is None:
            return
        if ask and QMessageBox.question(self, tr("Live dashboards"),
                                        tr("Revoke the card «{n}»? Its link stops working at once.").format(
                                            n=p.get("name") or p["id"])) != QMessageBox.StandardButton.Yes:
            return
        from . import cards

        cards.revoke(p["id"], p.get("dash", ""), QgsProject.instance())
        try:
            self.service().reload()      # open sessions of the card are told it ended
        except Exception:  # noqa: BLE001
            pass
        self._fill_cards()

    def tv(self, open_browser=True):
        """This dashboard on this PC's browser, full screen: a card of every widget, valid for one day."""
        d = self.current()
        if d is None:
            return None
        if self.sharing is None or not self.sharing.running():
            self.on_lan.setChecked(True)
        if self.sharing is None:
            return None
        card = issue_card(d, tr("Big screen on this PC"), days=1)
        url = self.sharing.local_link(card, tv=True)
        if open_browser:
            QDesktopServices.openUrl(QUrl(url))
        return url

    def closeEvent(self, e):  # noqa: N802
        self.timer.stop()
        self.stop_sharing()
        if self._service is not None:
            try:
                self._service.stop()
            except Exception:  # noqa: BLE001
                pass
            self._service = None
        super().closeEvent(e)


def _lang():
    from ..i18n import lang

    return lang() or "en"


_DOCK = [None]


def open_ui(plugin):
    if _DOCK[0] is None:
        _DOCK[0] = DashboardDock(plugin)
        plugin.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, _DOCK[0])
    _DOCK[0].refresh()
    _DOCK[0].show()
    _DOCK[0].raise_()
    return _DOCK[0]


def builder_ui(plugin):
    from .builder import builder_ui as run

    return run(plugin)


def seal_check_ui(plugin):
    SealDialog(plugin.iface.mainWindow()).exec()
