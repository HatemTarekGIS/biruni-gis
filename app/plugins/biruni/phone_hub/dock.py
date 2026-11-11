"""Phone panel: one code for the phone, and every phone tool with its state and a Start / Stop button.

The phone scans the code once and gets a page (server.py) that opens the tools started here. Starting and
stopping happen on this PC only; each tool keeps its own panel for its settings.
"""
import os

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (QDockWidget, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                                 QWidget)

from ..i18n import lang, tr


class PhoneHubDock(QDockWidget):
    def __init__(self, plugin, features=None, port=None, bind="0.0.0.0", addresses=None):
        from .features import all_features

        super().__init__(tr("Phone"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniPhoneHub")
        self.features = list(features) if features is not None else all_features()
        self.port, self.bind, self.addresses = port, bind, addresses
        self.server = None
        self.rows = {}
        self.phones = 0
        self._poll = QTimer(self)
        self._poll.setInterval(1500)
        self._poll.timeout.connect(self.refresh)
        self._build()
        self.refresh()
        self._poll.start()

    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniGps")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)

        card = QFrame()
        card.setObjectName("GpsCard")
        cv = QVBoxLayout(card)
        intro = QLabel(tr("Scan this code once with the phone's camera (same Wi-Fi or the phone's hotspot). The "
                          "phone opens one page with the phone tools; it opens the ones you start below."))
        intro.setObjectName("GpsHint")
        intro.setWordWrap(True)
        cv.addWidget(intro)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cv.addWidget(self.qr)
        self.url = QLabel()
        self.url.setObjectName("GpsHint")
        self.url.setWordWrap(True)
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cv.addWidget(self.url)
        self.start_btn = QPushButton(tr("Show the phone code"))
        self.start_btn.setObjectName("GpsPrimary")
        self.start_btn.clicked.connect(self.toggle)
        cv.addWidget(self.start_btn)
        self.state = QLabel(tr("Not started"))
        self.state.setObjectName("GpsHint")
        self.state.setWordWrap(True)
        cv.addWidget(self.state)
        v.addWidget(card)

        tools = QFrame()
        tools.setObjectName("GpsCard")
        tv = QVBoxLayout(tools)
        head = QLabel(tr("Phone tools"))
        head.setObjectName("GpsValue")
        tv.addWidget(head)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        for i, f in enumerate(self.features):
            icon = QLabel()
            name = QLabel(f.title())
            name.setObjectName("GpsValue")
            on = QLabel()
            on.setObjectName("GpsHint")
            btn = QPushButton(tr("Start"))
            btn.clicked.connect(lambda _=False, f=f: self.toggle_feature(f))
            panel = QPushButton(tr("Panel"))
            panel.setToolTip(tr("Opens this tool's own panel, with its settings."))
            panel.clicked.connect(lambda _=False, f=f: self.show_feature(f))
            names = QVBoxLayout()
            names.setSpacing(0)
            names.addWidget(name)
            names.addWidget(on)
            grid.addWidget(icon, i, 0)
            grid.addLayout(names, i, 1)
            grid.addWidget(btn, i, 2)
            grid.addWidget(panel, i, 3)
            self.rows[f.id] = (icon, on, btn, panel)
        tv.addLayout(grid)
        note = QLabel(tr("The phone cannot start a tool: a tool that is off shows \"ask the person at the "
                         "computer\" on the phone. The page shows only which tools are on - nothing of the project."))
        note.setObjectName("GpsHint")
        note.setWordWrap(True)
        tv.addWidget(note)
        v.addWidget(tools)

        more = QFrame()
        more.setObjectName("GpsCard")
        mv = QVBoxLayout(more)
        head = QLabel(tr("More for the field"))
        head.setObjectName("GpsValue")
        mv.addWidget(head)
        from .features import OTHER_TOOLS

        labels = {"qfield_send": tr("Send to Field"), "qfield_back": tr("Bring Back from Field"),
                  "live_qr": tr("Live QR Code"), "team_room": tr("Team Room")}
        self.more = {}
        row = None
        for i, (cid, icon) in enumerate(OTHER_TOOLS):
            if i % 2 == 0:
                row = QHBoxLayout()
                mv.addLayout(row)
            b = QPushButton(labels.get(cid, cid))
            b.setProperty("biruni_icon", icon)
            b.clicked.connect(lambda _=False, c=cid: self._run(c))
            row.addWidget(b, 1)
            self.more[cid] = b
        v.addWidget(more)
        v.addStretch(1)
        self.setWidget(body)
        self.setMinimumWidth(300)

    def refresh_theme(self):
        from .. import icons
        from ..tracking import panel_qss

        tokens = self.plugin.theme.tokens()
        self.widget().setStyleSheet(panel_qss(tokens))
        for f in self.features:
            self.rows[f.id][0].setPixmap(icons.icon(f.icon, tokens["icon"]).pixmap(24, 24))
        for b in self.more.values():
            b.setIcon(icons.icon(b.property("biruni_icon"), tokens["icon"]))

    # ---- the hub server --------------------------------------------------------
    def toggle(self):
        if self.server is not None:
            self.stop()
        else:
            self.start()

    def start(self):
        from ..gps.browser import qr_png
        from ..gps.source import local_addresses
        from ..vendor import ensure_vendor
        from .server import HUB_PORT, PORT_TRIES, HubServer, all_phone_texts

        ensure_vendor()  # certificate and QR code libraries
        ips = list(self.addresses) if self.addresses is not None else local_addresses()
        cert_folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")  # the same as the phone tools
        port = self.port or HUB_PORT
        try:
            self.server = HubServer(cert_folder, all_phone_texts(), lang(), port, ips, bind=self.bind,
                                    tries=1 if self.port else PORT_TRIES)
        except OSError as e:
            self.state.setText(tr("Port {p} is busy ({e}).").format(p=port, e=e))
            return
        self.phones = 0
        self.start_btn.setText(tr("Stop the phone code"))
        if not ips:
            self.state.setText(tr("No network found. Connect this computer to the Wi-Fi or the phone's hotspot."))
        else:
            url = self.server.url(ips[0])
            pm = QPixmap()
            pm.loadFromData(qr_png(url, 5))
            self.qr.setPixmap(pm)
            self.url.setText(url)
            self.url.setToolTip("\n".join(self.server.url(ip) for ip in ips))
            self.state.setText(tr("Scan the code with the phone's camera. The first time the phone warns that the "
                                  "connection is not private: choose Advanced, then Continue."))
        self.refresh()
        try:
            self.plugin.activity.log("phone_hub", "started", "local", "local", 1)
        except Exception:  # noqa: BLE001 - the activity log is only a record
            pass

    def stop(self):
        if self.server is not None:
            try:
                self.server.stop()
            except OSError:
                pass
            self.server = None
        self.qr.clear()
        self.url.clear()
        self.url.setToolTip("")
        self.start_btn.setText(tr("Show the phone code"))
        self.state.setText(tr("Stopped. The code no longer works; the tools keep running until you stop them."))

    def shutdown(self):
        self._poll.stop()
        if self.server is not None:
            self.stop()

    # ---- the tools -------------------------------------------------------------
    def _state(self, f):
        try:
            if not f.available(self.plugin):
                return None, None
            return f.state(self.plugin)
        except (AttributeError, RuntimeError):  # a panel being closed
            return False, None

    def refresh(self):
        items = []
        for f in self.features:
            on, link = self._state(f)
            _icon, label, btn, panel = self.rows[f.id]
            btn.setEnabled(on is not None)
            panel.setEnabled(on is not None)
            if on is None:
                label.setText(tr("Not available"))
            elif not on:
                label.setText(tr("Off"))
            elif link is None:
                label.setText(tr("On - not on the phone page"))
            else:
                label.setText(tr("On - on the phone page"))
            btn.setText(tr("Stop") if on else tr("Start"))
            items.append({"id": f.id, "on": bool(on), "link": link})
        if self.server is not None:
            self.server.set_features(items)
            while not self.server.events.empty():
                kind, _value = self.server.events.get_nowait()
                if kind == "phone":
                    self.phones += 1
                    self.state.setText(tr("A phone opened the page ({n}).").format(n=self.phones))
        return items

    def toggle_feature(self, f):
        on, _link = self._state(f)
        try:
            if on:
                f.stop(self.plugin)
            else:
                f.start(self.plugin)
        except Exception as e:  # noqa: BLE001 - shown to the user; the tool's own panel tells more
            self.state.setText(tr("It could not be done: {e}").format(e=e))
        self.show()
        self.raise_()  # the tool's panel may have opened over this one
        self.refresh()

    def show_feature(self, f):
        try:
            f.show(self.plugin)
        except Exception as e:  # noqa: BLE001
            self.state.setText(tr("It could not be done: {e}").format(e=e))

    def _run(self, cid):
        from ..commands import CATALOG

        runner = getattr(self.plugin, "runner", None)
        if runner is not None and cid in CATALOG:
            runner.execute(CATALOG[cid])


_DOCK = {}


def open_dock(plugin, **options):
    """options (tests): features, port, bind, addresses."""
    d = _DOCK.get(id(plugin))
    if d is None:
        d = PhoneHubDock(plugin, **options)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        d.refresh_theme()
        _DOCK[id(plugin)] = d
        QgsApplication.instance().aboutToQuit.connect(d.shutdown)
        d.start()  # opening the panel shows the code at once
    if d.isFloating():
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
    d.show()
    d.raise_()
    return d


def close_dock(plugin):
    d = _DOCK.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()
