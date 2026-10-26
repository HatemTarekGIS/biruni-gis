"""Phone GPS panel: connect a phone (or GNSS receiver), see the live position on
the map, and record tracks into the project."""
import datetime as dt
import os
import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY,
                       QgsProject, QgsSettings)
from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDockWidget, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from . import icons
from .gps.recorder import TrackRecorder, default_track_name
from .gps.source import DEFAULT_PORT, NmeaSource, local_addresses, serial_ports
from .i18n import tr

KEY = "biruni/gps/"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MERC = QgsCoordinateReferenceSystem("EPSG:3857")


def panel_qss(t):
    return f"""
#BiruniGps QLabel#GpsValue {{ font-size: 11pt; font-weight: 600; color: {t['text']}; }}
#BiruniGps QLabel#GpsKey {{ color: {t['text_dim']}; }}
#BiruniGps QLabel#GpsHint {{ color: {t['text_dim']}; }}
#BiruniGps QLabel#GpsAddress {{ font-size: 12pt; font-weight: 700; color: {t['accent']}; }}
#BiruniGps QFrame#GpsCard {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 8px; }}
#BiruniGps QPushButton#GpsPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: 600; }}
#BiruniGps QPushButton#GpsPrimary:hover {{ background: {t['accent_hover']}; }}
#BiruniGps QPushButton#GpsRecord {{ background: #D64545; color: white; border: none; border-radius: 6px;
    padding: 7px 14px; font-weight: 600; }}
#BiruniGps QPushButton#GpsRecord:disabled {{ background: {t['mid']}; }}
"""


class TrackingDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Phone GPS"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniGps")
        self.source = NmeaSource(self)
        self.recorder = TrackRecorder()
        self.last_fix = None
        self.marker = None
        self.accuracy_band = None
        self._last_data = None
        self._zoomed = False
        self.source.fixReceived.connect(self._on_fix)
        self.source.status.connect(self._on_status)
        self.source.rawReceived.connect(lambda: setattr(self, "_last_data", dt.datetime.now()))
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._refresh_clock)
        self._build()

    # ---- UI --------------------------------------------------------------
    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniGps")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)
        s = QgsSettings()

        conn = QFrame()
        conn.setObjectName("GpsCard")
        cv = QVBoxLayout(conn)
        self.mode = QComboBox()
        self.mode.addItem(tr("The phone sends to this PC (Wi-Fi)"), "listen")
        self.mode.addItem(tr("Connect to the phone (Wi-Fi)"), "connect")
        self.mode.addItem(tr("Bluetooth / USB (COM port)"), "serial")
        self.mode.addItem(tr("Phone browser - scan a QR code (no app)"), "browser")
        self.mode.setCurrentIndex(max(0, self.mode.findData(s.value(KEY + "mode", "listen"))))
        cv.addWidget(self.mode)
        self.pages = QStackedWidget()
        # listen
        p1 = QWidget()
        f1 = QFormLayout(p1)
        f1.setContentsMargins(0, 0, 0, 0)
        self.listen_port = QSpinBox()
        self.listen_port.setRange(1024, 65535)
        self.listen_port.setValue(int(s.value(KEY + "listen-port", DEFAULT_PORT)))
        f1.addRow(tr("Port"), self.listen_port)
        self.address = QLabel()
        self.address.setObjectName("GpsAddress")
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f1.addRow(tr("Send to"), self.address)
        hint = QLabel(tr("In the phone app, choose to send NMEA to this address (UDP or TCP). Android: "
                         "GPSd Forwarder. iPhone: GPS2IP. The phone and this PC must be on the same Wi-Fi "
                         "or the PC connected to the phone's hotspot."))
        hint.setObjectName("GpsHint")
        hint.setWordWrap(True)
        f1.addRow(hint)
        self.pages.addWidget(p1)
        # connect
        p2 = QWidget()
        f2 = QFormLayout(p2)
        f2.setContentsMargins(0, 0, 0, 0)
        self.phone_ip = QLineEdit(s.value(KEY + "phone-ip", ""))
        self.phone_ip.setPlaceholderText("192.168.1.20")
        self.phone_port = QSpinBox()
        self.phone_port.setRange(1, 65535)
        self.phone_port.setValue(int(s.value(KEY + "phone-port", DEFAULT_PORT)))
        f2.addRow(tr("Phone address"), self.phone_ip)
        f2.addRow(tr("Port"), self.phone_port)
        hint2 = QLabel(tr("Use this when the phone app works as a server and shows its own address and port "
                          "(e.g. GPS2IP in socket mode)."))
        hint2.setObjectName("GpsHint")
        hint2.setWordWrap(True)
        f2.addRow(hint2)
        self.pages.addWidget(p2)
        # serial
        p3 = QWidget()
        f3 = QFormLayout(p3)
        f3.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.com = QComboBox()
        refresh = QPushButton(icons.icon("refresh", self.plugin.theme.tokens()["icon"]), "")
        refresh.setToolTip(tr("Refresh"))
        refresh.clicked.connect(self._fill_ports)
        row.addWidget(self.com, 1)
        row.addWidget(refresh)
        f3.addRow(tr("Port"), row)
        self.baud = QComboBox()
        for b in (4800, 9600, 19200, 38400, 57600, 115200):
            self.baud.addItem(str(b), b)
        self.baud.setCurrentText(str(s.value(KEY + "baud", "9600")))
        f3.addRow(tr("Speed (baud)"), self.baud)
        hint3 = QLabel(tr("For a phone paired over Bluetooth with a GPS-sharing app, or an external GNSS "
                          "receiver connected by Bluetooth or USB."))
        hint3.setObjectName("GpsHint")
        hint3.setWordWrap(True)
        f3.addRow(hint3)
        self.pages.addWidget(p3)
        # browser (QR)
        p4 = QWidget()
        f4 = QVBoxLayout(p4)
        f4.setContentsMargins(0, 0, 0, 0)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_url = QLabel()
        self.qr_url.setObjectName("GpsHint")
        self.qr_url.setWordWrap(True)
        self.qr_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.qr_url.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint4 = QLabel(tr("Press Connect, then scan the code with the phone's camera (same Wi-Fi). The first time "
                          "the browser warns that the connection is not private: choose Advanced, then Continue - "
                          "the connection stays inside your network. Allow the location and keep the page open. "
                          "Experimental."))
        hint4.setObjectName("GpsHint")
        hint4.setWordWrap(True)
        f4.addWidget(self.qr)
        f4.addWidget(self.qr_url)
        f4.addWidget(hint4)
        self.pages.addWidget(p4)
        cv.addWidget(self.pages)
        self.connect_btn = QPushButton(tr("Connect"))
        self.connect_btn.setObjectName("GpsPrimary")
        self.connect_btn.clicked.connect(self.toggle_connection)
        cv.addWidget(self.connect_btn)
        self.state = QLabel(tr("Not connected"))
        self.state.setObjectName("GpsHint")
        cv.addWidget(self.state)
        v.addWidget(conn)

        live = QFrame()
        live.setObjectName("GpsCard")
        g = QGridLayout(live)
        self.values = {}
        items = [("fix", tr("Fix")), ("accuracy", tr("Accuracy")), ("speed", tr("Speed")),
                 ("heading", tr("Heading")), ("sats", tr("Satellites")), ("alt", tr("Altitude")),
                 ("pos", tr("Position")), ("age", tr("Last update"))]
        for i, (k, label) in enumerate(items):
            key = QLabel(label)
            key.setObjectName("GpsKey")
            val = QLabel("—")
            val.setObjectName("GpsValue")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            r, c = divmod(i, 2)
            g.addWidget(key, r * 2, c)
            g.addWidget(val, r * 2 + 1, c)
            self.values[k] = val
        v.addWidget(live)

        rec = QFrame()
        rec.setObjectName("GpsCard")
        rv = QVBoxLayout(rec)
        self.track_name = QLineEdit(default_track_name())
        rv.addWidget(self.track_name)
        self.record_btn = QPushButton(tr("Start recording"))
        self.record_btn.setObjectName("GpsRecord")
        self.record_btn.setEnabled(False)
        self.record_btn.clicked.connect(self.toggle_recording)
        rv.addWidget(self.record_btn)
        self.rec_info = QLabel("")
        self.rec_info.setObjectName("GpsHint")
        rv.addWidget(self.rec_info)
        opts = QHBoxLayout()
        self.follow = QCheckBox(tr("Follow on the map"))
        self.follow.setChecked(s.value(KEY + "follow", True, type=bool))
        opts.addWidget(self.follow)
        center = QPushButton(tr("Center"))
        center.clicked.connect(lambda: self._center(force=True))
        opts.addWidget(center)
        rv.addLayout(opts)
        v.addWidget(rec)

        imp = QPushButton(icons.icon("file-import", self.plugin.theme.tokens()["icon"]),
                          tr("Import a recorded track (GPX)…"))
        imp.clicked.connect(lambda: self.plugin.runner.open_algorithm("biruni:analysetrack"))
        v.addWidget(imp)
        v.addStretch(1)
        self.setWidget(body)
        self.setMinimumWidth(300)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._fill_ports()
        self._mode_changed()

    def refresh_theme(self):
        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens()))

    def _fill_ports(self):
        self.com.clear()
        for name, desc in serial_ports():
            self.com.addItem(f"{name} - {desc}" if desc else name, name)

    def _mode_changed(self):
        self.pages.setCurrentIndex(self.mode.currentIndex())
        ips = local_addresses()
        port = self.listen_port.value()
        self.address.setText(f"{ips[0]} : {port}" if ips else tr("No network found"))
        self.address.setToolTip("\n".join(f"{ip} : {port}" for ip in ips))

    # ---- connection -----------------------------------------------------
    def toggle_connection(self):
        if self.source.is_active():
            self.disconnect_source()
            return
        s = QgsSettings()
        mode = self.mode.currentData()
        s.setValue(KEY + "mode", mode)
        ok = False
        if mode == "listen":
            s.setValue(KEY + "listen-port", self.listen_port.value())
            ok = self.source.listen(self.listen_port.value())
        elif mode == "connect":
            host = self.phone_ip.text().strip()
            if not host:
                self.state.setText(tr("Enter the phone's address."))
                return
            s.setValue(KEY + "phone-ip", host)
            s.setValue(KEY + "phone-port", self.phone_port.value())
            ok = self.source.connect_to(host, self.phone_port.value())
        elif mode == "browser":
            ok = self._serve_browser()
        else:
            if self.com.currentData() is None:
                self.state.setText(tr("No COM port found. Pair the phone or plug in the receiver."))
                return
            s.setValue(KEY + "baud", self.baud.currentData())
            ok = self.source.open_serial(self.com.currentData(), self.baud.currentData())
        if ok:
            self._zoomed = False
            self.connect_btn.setText(tr("Disconnect"))
            self._clock.start()
            self.plugin.activity.log("gps", "connected", self.mode.currentText(), mode, 1)

    def _serve_browser(self):
        from qgis.core import QgsApplication
        from qgis.PyQt.QtGui import QPixmap

        from .i18n import lang
        from .vendor import ensure_vendor

        ensure_vendor()  # certificate and QR code libraries
        from .gps.browser import qr_png

        texts = {"title": tr("Biruni GIS - phone location"),
                 "intro": tr("Keep this page open and the screen on while you walk."),
                 "start": tr("Start sending my location"), "stop": tr("Stop"), "sent": tr("Positions sent:"),
                 "lost": tr("Not reaching the computer - check the Wi-Fi."),
                 "denied": tr("Location not allowed."),
                 "insecure": tr("This browser blocks the location here. Use Chrome or Safari and accept the "
                                "certificate warning.")}
        folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
        urls = self.source.serve_browser(folder, texts, lang())
        if not urls:
            return False
        pm = QPixmap()
        pm.loadFromData(qr_png(urls[0], 5))
        self.qr.setPixmap(pm)
        self.qr_url.setText(urls[0])
        self.qr_url.setToolTip("\n".join(urls))
        return True

    def disconnect_source(self):
        if self.recorder.recording:
            self.toggle_recording()
        self.source.stop()
        if hasattr(self, "qr"):
            self.qr.clear()
            self.qr_url.clear()
        self._clock.stop()
        self.connect_btn.setText(tr("Connect"))
        self.record_btn.setEnabled(False)
        self._clear_marker()

    def _on_status(self, message, connected):
        texts = {"listening": tr("Waiting for the phone…"), "connected": tr("Connected - waiting for a position…"),
                 "phone connected": tr("Phone connected - waiting for a position…"),
                 "disconnected": tr("Connection lost - retrying…"), "unreachable": tr("Phone not reachable - retrying…"),
                 "stopped": tr("Not connected")}
        self.state.setText(texts.get(message, message))

    # ---- positions -------------------------------------------------------
    def _on_fix(self, fix):
        self.last_fix = fix
        t = self.plugin.theme.tokens()
        quality = {1: "GPS", 2: "DGPS", 4: "RTK fixed", 5: "RTK float"}.get(fix.quality, "GPS")
        self.values["fix"].setText(quality if fix.valid else tr("No fix"))
        self.values["accuracy"].setText(f"≈ {fix.accuracy_m:g} m" if fix.accuracy_m else "—")
        self.values["speed"].setText(f"{fix.speed_kmh:.1f} km/h" if fix.speed_kmh is not None else "—")
        self.values["heading"].setText(f"{fix.course:.0f}°" if fix.course is not None else "—")
        self.values["sats"].setText(str(fix.satellites) if fix.satellites is not None else "—")
        self.values["alt"].setText(f"{fix.alt:.1f} m" if fix.alt is not None else "—")
        self.values["pos"].setText(f"{fix.lat:.6f}, {fix.lon:.6f}")
        self.state.setText(tr("Receiving positions"))
        self.record_btn.setEnabled(fix.valid or self.recorder.recording)
        if fix.valid:
            self._draw(fix, t)
            if self.recorder.add(fix):
                self._refresh_record_info()

    def _draw(self, fix, t):
        canvas = self.iface.mapCanvas()
        to_canvas = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        pt = to_canvas.transform(QgsPointXY(fix.lon, fix.lat))
        if self.marker is None:
            self.marker = QgsVertexMarker(canvas)
            self.marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
            self.marker.setIconSize(16)
            self.marker.setPenWidth(4)
            self.accuracy_band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        accent = QColor(t["accent"])
        self.marker.setColor(accent)
        self.marker.setFillColor(QColor("#ffffff"))
        self.marker.setCenter(pt)
        fill = QColor(accent)
        fill.setAlpha(40)
        self.accuracy_band.setFillColor(fill)
        self.accuracy_band.setStrokeColor(accent)
        if fix.accuracy_m:
            to_merc = QgsCoordinateTransform(WGS84, MERC, QgsProject.instance())
            c = to_merc.transform(QgsPointXY(fix.lon, fix.lat))
            r = fix.accuracy_m / max(math.cos(math.radians(fix.lat)), 0.01)  # Mercator scale
            circle = QgsGeometry.fromPointXY(c).buffer(r, 24)
            circle.transform(QgsCoordinateTransform(MERC, canvas.mapSettings().destinationCrs(),
                                                    QgsProject.instance()))
            self.accuracy_band.setToGeometry(circle, None)
        if not self._zoomed:  # first position: zoom to street level around it
            self._zoomed = True
            to_merc = QgsCoordinateTransform(WGS84, MERC, QgsProject.instance())
            c = to_merc.transform(QgsPointXY(fix.lon, fix.lat))
            box = QgsGeometry.fromPointXY(c).buffer(300 / max(math.cos(math.radians(fix.lat)), 0.01), 4)
            box.transform(QgsCoordinateTransform(MERC, canvas.mapSettings().destinationCrs(), QgsProject.instance()))
            canvas.setExtent(box.boundingBox())
            canvas.refresh()
            return
        self._center()

    def _center(self, force=False):
        if self.marker is None or self.last_fix is None:
            return
        canvas = self.iface.mapCanvas()
        pt = self.marker.center()
        ext = canvas.extent()
        inner = ext.buffered(-min(ext.width(), ext.height()) * 0.2)
        if force or (self.follow.isChecked() and not inner.contains(pt)):
            canvas.setCenter(pt)
            canvas.refresh()

    def _clear_marker(self):
        canvas = self.iface.mapCanvas()
        for item in (self.marker, self.accuracy_band):
            if item is not None:
                canvas.scene().removeItem(item)
        self.marker = self.accuracy_band = None

    def _refresh_clock(self):
        if self._last_data is None:
            return
        secs = int((dt.datetime.now() - self._last_data).total_seconds())
        self.values["age"].setText(tr("{s} s ago").format(s=secs) if secs > 1 else tr("now"))
        if secs > 10 and self.source.is_active():
            self.state.setText(tr("No data for {s} s - check the phone app.").format(s=secs))
        if self.recorder.recording:
            self._refresh_record_info()

    # ---- recording -------------------------------------------------------
    def toggle_recording(self):
        if not self.recorder.recording:
            name = self.track_name.text().strip() or default_track_name()
            self.recorder.start(name, self.mode.currentText())
            if self.last_fix is not None and self.last_fix.valid:
                self.recorder.add(self.last_fix)
            self.record_btn.setText(tr("Stop recording"))
            self.plugin.activity.log("gps", "recording_started", name, self.recorder.track_id, 1)
        else:
            name, tid = self.recorder.name, self.recorder.track_id
            st = self.recorder.stop()
            self.record_btn.setText(tr("Start recording"))
            self.track_name.setText(default_track_name())
            if st and st.points >= 2:
                summary = tr("{km} km in {min} min, {stops} stops").format(
                    km=round(st.distance_m / 1000, 2), min=round(st.duration_s / 60, 1), stops=len(st.stops))
                self.rec_info.setText(tr("Saved: {name} - {summary}").format(name=name, summary=summary))
                self.plugin.activity.log("gps", "track_recorded", name, tid, st.points, summary)
                QgsProject.instance().setDirty(True)
            else:
                self.rec_info.setText(tr("Nothing was recorded."))

    def _refresh_record_info(self):
        pts = self.recorder.points
        if len(pts) < 2:
            self.rec_info.setText(tr("Recording… {n} points").format(n=len(pts)))
            return
        self.rec_info.setText(tr("Recording… {n} points · {km} km · {min} min").format(
            n=len(pts), km=round(self.recorder.distance_m / 1000, 2),
            min=round((pts[-1][0] - pts[0][0]).total_seconds() / 60, 1)))

    def shutdown(self):
        self.disconnect_source()
