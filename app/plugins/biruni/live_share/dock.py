"""See my map panel: share the live map with a link and a QR code, see how many watch, stop."""
import os
import time

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QDockWidget, QFrame, QHBoxLayout, QLabel, QPushButton,
                                 QVBoxLayout, QWidget)

from ..i18n import lang, tr

KEY = "biruni/live-share/"


class LiveShareDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("See My Map"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniLiveShare")
        self.share = None
        self.link = ""
        self._since = 0.0
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._read_events)
        self._build()

    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniGps")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)

        card = QFrame()
        card.setObjectName("GpsCard")
        cv = QVBoxLayout(card)
        intro = QLabel(tr("Anyone with the link sees your map as it is on the screen - the same layers and styling - "
                          "and it updates while you work. They can move around, tap a feature to see its details "
                          "and see where they are. Nothing is uploaded: the map goes directly from this computer "
                          "to them, encrypted."))
        intro.setObjectName("GpsHint")
        intro.setWordWrap(True)
        cv.addWidget(intro)
        self.attrs = QCheckBox(tr("Include feature details when tapped"))
        self.attrs.setChecked(QgsSettings().value(KEY + "attributes", True, type=bool))
        self.attrs.toggled.connect(self._attrs_toggled)
        cv.addWidget(self.attrs)
        self.start_btn = QPushButton(tr("Share map live"))
        self.start_btn.setObjectName("GpsPrimary")
        self.start_btn.clicked.connect(self.toggle)
        cv.addWidget(self.start_btn)
        self.state = QLabel(tr("Not shared"))
        self.state.setObjectName("GpsHint")
        self.state.setWordWrap(True)
        cv.addWidget(self.state)
        v.addWidget(card)

        self.qr_card = QFrame()
        self.qr_card.setObjectName("GpsCard")
        qv = QVBoxLayout(self.qr_card)
        self.viewers = QLabel(tr("Watching now: {n}").format(n=0))
        self.viewers.setObjectName("GpsValue")
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.url = QLabel()
        self.url.setObjectName("GpsHint")
        self.url.setWordWrap(True)
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout()
        self.copy_btn = QPushButton(tr("Copy link"))
        self.copy_btn.clicked.connect(self._copy)
        preview = QPushButton(tr("Open"))
        preview.setToolTip(tr("Opens the link in this computer's browser, to see what the others see."))
        preview.clicked.connect(lambda: self.link and QDesktopServices.openUrl(QUrl(self.link)))
        row.addWidget(self.copy_btn, 1)
        row.addWidget(preview)
        hint = QLabel(tr("Only the layers shown on the map are shared. Stopping ends the link for everyone - the "
                         "next share has a new one. A few networks block direct links; a viewer on one of them "
                         "can try another network or Wi-Fi."))
        hint.setObjectName("GpsHint")
        hint.setWordWrap(True)
        qv.addWidget(self.viewers)
        qv.addWidget(self.qr)
        qv.addWidget(self.url)
        qv.addLayout(row)
        qv.addWidget(hint)
        self.qr_card.setVisible(False)
        v.addWidget(self.qr_card)
        v.addStretch(1)
        self.setWidget(body)
        self.setMinimumWidth(300)

    def refresh_theme(self):
        from ..tracking import panel_qss

        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens()))

    def _attrs_toggled(self, on):
        QgsSettings().setValue(KEY + "attributes", bool(on))
        if self.share is not None:
            self.share.set_attributes(on)

    def _copy(self):
        if self.link:
            QApplication.clipboard().setText(self.link)
            self.copy_btn.setText(tr("Copied"))
            QTimer.singleShot(1500, lambda: self.copy_btn.setText(tr("Copy link")))

    # ---- start / stop --------------------------------------------------------
    def toggle(self):
        if self.share is not None:
            self.stop()
        else:
            self.start()

    def start(self):
        from ..gps.browser import qr_png
        from ..vendor import ensure_vendor
        from .server import SHARE_PORT
        from .share import LiveShare

        ensure_vendor()  # certificate and QR code libraries
        cert_folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "live-share")
        share = LiveShare(self.iface.mapCanvas(), cert_folder, self.attrs.isChecked(), SHARE_PORT)
        try:
            share.start()
        except OSError as e:
            self.state.setText(tr("Sharing could not start: {e}").format(e=e))
            return
        link = share.url(lang())
        from ..secure.door import door_ok

        if not door_ok(self, link):
            share.stop()
            self.state.setText(tr("The phone page could not be trusted: no link was shown."))
            return
        self.share = share
        self.link = link
        pm = QPixmap()
        pm.loadFromData(qr_png(self.link, 5))
        self.qr.setPixmap(pm)
        self.url.setText(self.link)
        self.url.setToolTip(self.link)
        self.viewers.setText(tr("Watching now: {n}").format(n=0))
        self.qr_card.setVisible(True)
        self.start_btn.setText(tr("Stop sharing"))
        self.state.setText(tr("Connecting to the relays…"))
        self._since = time.monotonic()
        self._poll.start()
        try:
            self.plugin.activity.log("live_share", "started", "bridge", "bridge", 1)
        except Exception:  # noqa: BLE001 - the activity log is only a record
            pass

    def stop(self, message=None):
        self._poll.stop()
        if self.share is not None:
            self.share.stop()
            self.share = None
        self.link = ""
        self.qr.clear()
        self.url.clear()
        self.qr_card.setVisible(False)
        self.start_btn.setText(tr("Share map live"))
        self.state.setText(message or tr("Stopped. The link no longer works."))

    def shutdown(self):
        if self.share is not None:
            self.stop()

    # ---- events ----------------------------------------------------------------
    def _read_events(self):
        if self.share is None:
            return
        for kind, ev in self.share.poll():
            if kind == "ended":
                self.stop(tr("Sharing stopped unexpectedly ({e}). Press Share map live again.").format(e=ev))
                return
            t = ev.get("type")
            if t == "relays":
                if ev.get("up"):
                    self.state.setText(tr("Ready. Send the link, or let someone scan the code."))
                else:
                    self.state.setText(tr("Connecting to the relays…"))
            elif t == "viewers":
                self.viewers.setText(tr("Watching now: {n}").format(n=self.share.viewers))
            elif t == "viewer":
                self.state.setText(tr("Someone opened the map."))
            elif t == "p2pfail":
                self.state.setText(tr("A viewer's network did not allow a direct link. They can try another network "
                                      "or Wi-Fi."))
            elif t == "full":
                self.state.setText(tr("Too many viewers at once - a new one was turned away."))
        if self.share.relays_up == 0 and time.monotonic() - self._since > 30:
            self.state.setText(tr("No relay can be reached. Check this computer's internet connection."))


_DOCK = {}


def open_dock(plugin):
    d = _DOCK.get(id(plugin))
    if d is None:
        d = LiveShareDock(plugin)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        d.refresh_theme()
        _DOCK[id(plugin)] = d
        QgsApplication.instance().aboutToQuit.connect(d.shutdown)
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
