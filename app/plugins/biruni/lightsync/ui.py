"""Light Sync windows: one computer shows an endless animated QR code, the other reads it with its camera.

No cable, no network, no memory stick: what crosses is light. ``send_bytes`` and ``receive_bytes`` carry any
bytes (a pigeon pack of a team room seals its own); ``send_file_ui`` and ``receive_file_ui`` carry one file,
sealed with a passphrase if the user wants.
"""
import time

import numpy as np
from qgis.core import Qgis, QgsSettings
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QImage, QPixmap
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                                 QSizePolicy, QVBoxLayout)

from ..i18n import tr
from . import files, frames

PRESET_KEY = "biruni/lightsync/preset"


def _preset():
    p = QgsSettings().value(PRESET_KEY, "normal")
    return p if p in frames.PRESETS else "normal"


def _preset_names():
    return [("weak", tr("Weak camera (slow, large squares)")), ("normal", tr("Normal")),
            ("strong", tr("Sharp camera, big screen (fast)"))]


def _size(n):
    if n < 1024:
        return tr("{n} bytes").format(n=n)
    if n < 1024 * 1024:
        return tr("{n} KB").format(n=f"{n / 1024:.1f}")
    return tr("{n} MB").format(n=f"{n / 1048576:.2f}")


def _duration(s):
    s = int(round(s))
    if s < 60:
        return tr("{n} s").format(n=max(1, s))
    return tr("{m} min {s} s").format(m=s // 60, s=s % 60)


def _qimage(gray):
    h, w = gray.shape
    g = np.ascontiguousarray(gray)
    return QImage(g.data, w, h, w, QImage.Format.Format_Grayscale8).copy()


# ---- sending -------------------------------------------------------------------------------------------
class SendDialog(QDialog):
    """Shows the frames of ``data`` one after another until it is closed."""

    def __init__(self, parent, data, title=""):
        super().__init__(parent)
        self.data = bytes(data)
        self.setWindowTitle(tr("Light Sync - Sending"))
        self.setMinimumSize(560, 640)
        self.setStyleSheet("QDialog { background: #ffffff; } QLabel { color: #111111; }")
        v = QVBoxLayout(self)
        self.title = QLabel(title or tr("Sending by light"))
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)
        self.code = QLabel()
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.code.setMinimumSize(320, 320)
        self.code.setStyleSheet("background: #ffffff;")
        v.addWidget(self.code, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Speed:")))
        self.speed = QComboBox()
        for key, name in _preset_names():
            self.speed.addItem(name, key)
        self.speed.setCurrentIndex(max(0, self.speed.findData(_preset())))
        self.speed.currentIndexChanged.connect(self._restart)
        row.addWidget(self.speed)
        row.addStretch(1)
        self.counter = QLabel()
        row.addWidget(self.counter)
        self.full = QPushButton(tr("Full screen"))
        self.full.clicked.connect(self._toggle_full)
        row.addWidget(self.full)
        stop = QPushButton(tr("Stop"))
        stop.clicked.connect(self.reject)
        row.addWidget(stop)
        v.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self._restart()

    def _restart(self, *_):
        key = self.speed.currentData() or "normal"
        QgsSettings().setValue(PRESET_KEY, key)
        block, fps = frames.PRESETS[key]
        self.sender_ = frames.Sender(self.data, block)
        self.fps = fps
        self.timer.start(int(1000 / fps))
        self.hint.setText(tr("Point the other computer's camera at this code (Light Sync - Receive there) and hold "
                             "it still until it says it has everything. {size} - about {time}; nothing goes "
                             "back, so if frames are missed it just takes a little longer. A changed speed "
                             "makes the receiver start again.").format(
            size=_size(len(self.data)), time=_duration(frames.seconds_for(len(self.data), block, fps))))
        self.tick()

    def frame_image(self):
        """The next frame as a QImage (one square per module, scaled when shown)."""
        return _qimage(frames.qr_gray(self.sender_.frame(), scale=1, border=4))

    def tick(self):
        img = self.frame_image()
        side = max(64, min(self.code.width(), self.code.height()))
        n = max(1, side // img.width())
        pm = QPixmap.fromImage(img.scaled(img.width() * n, img.height() * n, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.FastTransformation))
        self.code.setPixmap(pm)
        k = self.sender_.k
        self.counter.setText(tr("frame {i} · {k} blocks").format(i=self.sender_.index, k=k))

    def _toggle_full(self):
        if self.isFullScreen():
            self.showNormal()
            self.full.setText(tr("Full screen"))
        else:
            self.showFullScreen()
            self.full.setText(tr("Leave full screen"))

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self._toggle_full()
            return
        super().keyPressEvent(e)

    def done(self, r):
        self.timer.stop()
        super().done(r)


def send_bytes(parent, data, title=""):
    """Show ``data`` as an animated QR code until the user stops it."""
    if len(data) > frames.MAX_PAYLOAD:
        QMessageBox.warning(parent, tr("Light Sync"), tr("Light Sync carries up to {size}; this is {n}.").format(
            size=_size(frames.MAX_PAYLOAD), n=_size(len(data))))
        return
    d = SendDialog(parent, data, title)
    d.exec()


# ---- receiving -----------------------------------------------------------------------------------------
def _multimedia():
    try:
        from qgis.PyQt import QtMultimedia
    except ImportError:
        try:
            from PyQt6 import QtMultimedia
        except ImportError:
            return None
    return QtMultimedia


def _best_format(device):
    """The camera format to read with: the largest picture up to 1920 wide, then the most frames a second."""
    best, score = None, None
    for f in device.videoFormats():
        r = f.resolution()
        if r.width() > 1920:
            continue
        s = (r.width() * r.height(), f.maxFrameRate())
        if score is None or s > score:
            best, score = f, s
    return best


class ReceiveDialog(QDialog):
    """Reads frames with a camera (or from ``feed_image`` - the tests) until the payload is whole."""

    def __init__(self, parent, title="", camera=True):
        super().__init__(parent)
        self.setWindowTitle(tr("Light Sync - Receiving"))
        self.setMinimumSize(560, 560)
        self.rx = frames.Receiver()
        self.result = None
        self.started = None
        self._busy = False
        self._shown = 0.0
        self.camera = self.session = self.sink = None
        v = QVBoxLayout(self)
        lead = QLabel(title or tr("Point this computer's camera at the moving code on the other screen (Light Sync - "
                                  "Send there). It may start at any moment and miss frames: it only needs enough."))
        lead.setWordWrap(True)
        v.addWidget(lead)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Camera:")))
        self.cams = QComboBox()
        row.addWidget(self.cams, 1)
        v.addLayout(row)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(320, 240)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setStyleSheet("background: #202020; color: #dddddd;")
        v.addWidget(self.preview, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        v.addWidget(self.bar)
        self.state = QLabel(tr("Waiting for the first frame…"))
        self.state.setWordWrap(True)
        v.addWidget(self.state)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.mm = _multimedia() if camera else None
        self.devices = []
        if camera:
            self._list_cameras()

    # -- camera
    def _list_cameras(self):
        if self.mm is None:
            self._no_camera(tr("This computer cannot use a camera here (Qt Multimedia is missing)."))
            return
        self.devices = list(self.mm.QMediaDevices.videoInputs())
        if not self.devices:
            self._no_camera(tr("No camera was found. Connect a webcam (or use a laptop's) and open this again."))
            return
        default = self.mm.QMediaDevices.defaultVideoInput()
        for d in self.devices:
            self.cams.addItem(d.description())
        idx = next((i for i, d in enumerate(self.devices) if d.id() == default.id()), 0)
        self.cams.setCurrentIndex(idx)
        self.cams.currentIndexChanged.connect(self._open_camera)
        self._open_camera(idx)

    def _no_camera(self, text):
        self.cams.setEnabled(False)
        self.preview.setText(text)
        self.state.setText(text)

    def _open_camera(self, idx):
        self._close_camera()
        mm = self.mm
        dev = self.devices[idx]
        self.camera = mm.QCamera(dev, self)
        f = _best_format(dev)
        if f is not None:
            self.camera.setCameraFormat(f)
        self.session = mm.QMediaCaptureSession(self)
        self.sink = mm.QVideoSink(self)
        self.session.setCamera(self.camera)
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.camera.errorOccurred.connect(lambda _e, s="": self.state.setText(
            tr("The camera could not be used: {why}").format(why=s or self.camera.errorString())))
        self.camera.start()

    def _close_camera(self):
        if self.camera is not None:
            try:
                self.sink.videoFrameChanged.disconnect(self._on_frame)
            except (TypeError, RuntimeError):
                pass
            self.camera.stop()
            self.camera.deleteLater()
        self.camera = self.session = self.sink = None

    def _on_frame(self, vf):
        if self._busy or self.result is not None:
            return
        self._busy = True
        try:
            img = vf.toImage()
            if img.isNull():
                return
            self.feed_image(frames.gray_of_qimage(img), preview=img)
        finally:
            self._busy = False

    # -- frames
    def feed_image(self, gray, preview=None):
        """Read every code in one picture. -> True when the payload is whole."""
        done = False
        for raw in frames.read_codes(gray):
            if self.started is None and frames.unpack(raw) is not None:
                self.started = time.time()
            if self.rx.feed(raw):
                done = True
        now = time.time()
        if preview is not None and now - self._shown > 0.08:   # the picture at about 12 a second is enough
            self._shown = now
            self.preview.setPixmap(QPixmap.fromImage(preview).scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        self._show_progress()
        if done:
            self.result = self.rx.result
            self._close_camera()
            self.accept()
        return done

    def _show_progress(self):
        frac, known, kept, k = self.rx.progress()
        self.bar.setValue(int(frac * 1000))
        if not k:
            if self.rx.foreign:
                self.state.setText(tr("Codes are seen, but they are not Light Sync frames."))
            return
        took = max(0.001, time.time() - (self.started or time.time()))
        rate = self.rx.frames / took
        text = tr("{known} of {k} blocks · {n} frames read ({rate:.1f} a second)").format(
            known=known, k=k, n=self.rx.frames, rate=rate)
        if self.rx.error == "checksum":
            text += " · " + tr("a damaged frame got through - starting again")
        if self.rx.foreign:
            text += " · " + tr("{n} codes of another transfer ignored").format(n=self.rx.foreign)
        self.state.setText(text)

    def done(self, r):
        self._close_camera()
        super().done(r)


def receive_bytes(parent, title=""):
    """Read one payload with the camera. -> the bytes, or None if cancelled."""
    d = ReceiveDialog(parent, title)
    if d.exec() == QDialog.DialogCode.Accepted:
        return d.result
    return None


# ---- one file ------------------------------------------------------------------------------------------
class _Passphrase(QDialog):
    def __init__(self, parent, confirm):
        super().__init__(parent)
        self.setWindowTitle(tr("Light Sync"))
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow(tr("Passphrase:"), self.pw)
        self.pw2 = None
        if confirm:
            self.pw2 = QLineEdit()
            self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
            f.addRow(tr("Again:"), self.pw2)
        v.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _ok(self):
        if not self.pw.text():
            return
        if self.pw2 is not None and self.pw2.text() != self.pw.text():
            QMessageBox.warning(self, tr("Light Sync"), tr("The two passphrases are not the same."))
            return
        self.accept()


class SendFileDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(tr("Send a File by Light"))
        self.setMinimumWidth(480)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A file goes to a computer with no network and no USB port allowed: this screen shows it "
                          "as a moving code, the other computer's camera reads it. Up to {size}.").format(
            size=_size(frames.MAX_PAYLOAD)))
        intro.setWordWrap(True)
        v.addWidget(intro)
        row = QHBoxLayout()
        self.path = QLineEdit()
        row.addWidget(self.path, 1)
        pick = QPushButton(tr("Browse…"))
        pick.clicked.connect(self._pick)
        row.addWidget(pick)
        v.addLayout(row)
        self.seal = QCheckBox(tr("Seal it with a passphrase (anyone who films the screen sees only unreadable "
                                 "bytes)"))
        self.seal.setChecked(True)
        v.addWidget(self.seal)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Start"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _pick(self):
        p, _ = QFileDialog.getOpenFileName(self, tr("File to send"), self.path.text())
        if p:
            self.path.setText(p)


def send_file_ui(plugin):
    parent = plugin.iface.mainWindow()
    d = SendFileDialog(parent)
    if d.exec() != QDialog.DialogCode.Accepted or not d.path.text():
        return
    path = d.path.text()
    try:
        with open(path, "rb") as f:
            content = f.read(frames.MAX_PAYLOAD + 1)
    except OSError as e:
        QMessageBox.warning(parent, tr("Light Sync"), str(e))
        return
    pw = None
    if d.seal.isChecked():
        p = _Passphrase(parent, confirm=True)
        if p.exec() != QDialog.DialogCode.Accepted:
            return
        pw = p.pw.text()
    import os

    data = files.pack(os.path.basename(path), content, pw)
    send_bytes(parent, data, tr("Sending «{name}» ({size})").format(name=os.path.basename(path),
                                                                    size=_size(len(content))))


def open_received(parent, data):
    """(name, content) of received file bytes, asking for the passphrase when sealed; None when given up."""
    try:
        if not files.sealed(data):
            return files.unpack(data)
    except files.FileError:
        QMessageBox.warning(parent, tr("Light Sync"), tr("What was received is not a file sent by light."))
        return None
    for _ in range(3):
        p = _Passphrase(parent, confirm=False)
        if p.exec() != QDialog.DialogCode.Accepted:
            return None
        try:
            return files.unpack(data, p.pw.text())
        except files.FileError as e:
            if str(e) != "passphrase":
                QMessageBox.warning(parent, tr("Light Sync"), tr("The file arrived damaged: send it again."))
                return None
            QMessageBox.warning(parent, tr("Light Sync"), tr("Wrong passphrase."))
    return None


def receive_file_ui(plugin):
    parent = plugin.iface.mainWindow()
    data = receive_bytes(parent, tr("Point this computer's camera at the moving code of «Send a File by Light» on "
                                    "the other screen."))
    if data is None:
        return
    got = open_received(parent, data)
    if got is None:
        return
    name, content = got
    from ..fingerprint.ui import _dir, _remember
    import os

    dest, _ = QFileDialog.getSaveFileName(parent, tr("Save the received file"), os.path.join(_dir(), name))
    if not dest:
        return
    with open(dest, "wb") as f:
        f.write(content)
    _remember(dest)
    from ..version import APP_NAME

    plugin.iface.messageBar().pushMessage(APP_NAME, tr("Received «{name}» ({size}) by light.").format(
        name=os.path.basename(dest), size=_size(len(content))), Qgis.MessageLevel.Success, 12)
