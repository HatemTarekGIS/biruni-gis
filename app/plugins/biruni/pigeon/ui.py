"""The carrier pigeon window: make a pack for the others, open a pack, and see what it brought."""
import os

from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QLabel, QMessageBox,
                                 QPushButton, QVBoxLayout)

from ..i18n import tr
from . import core


def _light():
    try:
        from ..lightsync import ui as light   # codes on the screen read by a camera (optional)

        return light
    except ImportError:
        return None


REASONS = {
    "sealed with another key or altered": "sealed with another room's key, or altered on the way",
    "bad signature": "its signature does not match its content",
    "not a pack": "not a pack of a room",
    "expired": "older than 30 days",
    "another room": "made for another room",
    "signed by someone else": "signed with a key that is not its maker's",
    "another host's stream": "made by another host of the room (the room changed host since)",
    "a host pack": "made by this room's host",
    "not a member": "its maker is not a member of the room",
    "not the room's host": "its maker is not the room's host",
    "a change of someone else": "it held a change made by someone other than its maker",
    "not a pigeon pack": "not a carrier pigeon pack",
    "damaged pack": "the pack is damaged",
    "the room is not open on this computer": "the room is not open on this computer",
}


def report_lines(rep):
    """The report of an opened pack, as lines for people."""
    lines = [tr("Packs in it: {n}").format(n=rep["packs"])]
    if rep["in"]:
        lines.append(tr("Changes decided into the room: {n}").format(n=rep["in"]))
    if rep["applied"]:
        lines.append(tr("Changes of the room written into this map: {n}").format(n=rep["applied"]))
    if rep["carried"]:
        lines.append(tr("Packs kept to carry on to the others: {n}").format(n=rep["carried"]))
    if rep["waiting"]:
        lines.append(tr("Packs waiting for earlier ones (a gap in the room's changes): {n}").format(n=rep["waiting"]))
    if rep["conflicts"]:
        lines.append(tr("Changes that met another person's change: {n} (see the Team panel)").format(
            n=rep["conflicts"]))
    if rep["dups"]:
        lines.append(tr("Already here: {n}").format(n=rep["dups"]))
    why = {}
    for r in rep["refused"]:
        why[r] = why.get(r, 0) + 1
    for r, n in why.items():
        lines.append(tr("Refused ({n}): {why}").format(n=n, why=tr(REASONS.get(r, r))))
    return lines


class PigeonDialog(QDialog):
    def __init__(self, plugin, node):
        super().__init__(plugin.iface.mainWindow())
        self.plugin, self.node = plugin, node
        self.setWindowTitle(tr("Carrier Pigeon"))
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Where there is no network: give your pack to anyone of the room (a USB stick, a phone over "
                          "a cable, a Bluetooth file, or codes on the screen read by a camera). Whoever opens it "
                          "takes what is for them and carries the rest on, until each change reaches the host and "
                          "the host's changes reach everyone. Packs are encrypted with the room key and signed."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.state = QLabel()
        self.state.setWordWrap(True)
        v.addWidget(self.state)
        g = QGridLayout()
        light = _light()
        self.save_btn = QPushButton(tr("Save pack to a file…"))
        self.save_btn.clicked.connect(self._save)
        self.show_btn = QPushButton(tr("Show pack as codes on the screen…"))
        self.show_btn.clicked.connect(self._show_codes)
        self.open_btn = QPushButton(tr("Open pack files…"))
        self.open_btn.clicked.connect(self._open)
        self.read_btn = QPushButton(tr("Read a pack with the camera…"))
        self.read_btn.clicked.connect(self._read_codes)
        for b in (self.show_btn, self.read_btn):
            b.setEnabled(light is not None)
            if light is None:
                b.setToolTip(tr("Light sync is not available on this computer."))
        g.addWidget(QLabel(tr("Give")), 0, 0)
        g.addWidget(self.save_btn, 0, 1)
        g.addWidget(self.show_btn, 0, 2)
        g.addWidget(QLabel(tr("Take")), 1, 0)
        g.addWidget(self.open_btn, 1, 1)
        g.addWidget(self.read_btn, 1, 2)
        v.addLayout(g)
        self.result = QLabel()
        self.result.setWordWrap(True)
        v.addWidget(self.result)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._refresh()

    def _refresh(self):
        c = core.counts(self.node)
        if c["role"] == "host":
            lines = [tr("This computer is the room's host: its pack carries the room's changes to the members.")]
        else:
            lines = [tr("Your changes not yet with the host: {n}").format(n=c["outbox"])]
        lines.append(tr("Packs carried for others: {n}").format(n=c["carried"]))
        if c["waiting"]:
            lines.append(tr("Packs waiting for earlier ones: {n}").format(n=c["waiting"]))
        self.state.setText("\n".join(lines))

    def _save(self):
        folder = QFileDialog.getExistingDirectory(self, tr("Save pack to a file…"),
                                                  os.path.expanduser("~"))
        if not folder:
            return
        try:
            paths = core.save(self.node, folder)
        except (OSError, core.PigeonError) as e:
            QMessageBox.warning(self, tr("Carrier Pigeon"), str(e))
            return
        self.result.setText(tr("Saved: {files}").format(files=", ".join(os.path.basename(p) for p in paths)))
        self._refresh()

    def _show_codes(self):
        light = _light()
        if light is None:
            return
        datas = core.make(self.node)
        for i, data in enumerate(datas):
            title = tr("Pigeon pack") + (f" {i + 1}/{len(datas)}" if len(datas) > 1 else "")
            light.send_bytes(self, data, title)
        self._refresh()

    def _take(self, datas):
        total = None
        for data in datas:
            try:
                rep = core.open_data(self.node, data)
            except core.PigeonError as e:
                QMessageBox.warning(self, tr("Carrier Pigeon"), tr(REASONS.get(str(e), str(e))))
                continue
            if total is None:
                total = rep
            else:
                for k in ("packs", "in", "applied", "carried", "dups", "conflicts"):
                    total[k] += rep[k]
                total["refused"] += rep["refused"]
                total["waiting"] = rep["waiting"]
        if total is not None:
            self.result.setText("\n".join(report_lines(total)))
        self._refresh()

    def _open(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Open pack files…"), os.path.expanduser("~"),
                                                tr("Pigeon packs") + f" (*{core.EXT})")
        datas = []
        for p in paths:
            try:
                with open(p, "rb") as f:
                    datas.append(f.read())
            except OSError as e:
                QMessageBox.warning(self, tr("Carrier Pigeon"), str(e))
        if datas:
            self._take(datas)

    def _read_codes(self):
        light = _light()
        if light is None:
            return
        data = light.receive_bytes(self)
        if data:
            self._take([data])


def open_ui(plugin):
    from ..team import current

    node = current()
    if node is None:
        QMessageBox.information(plugin.iface.mainWindow(), tr("Carrier Pigeon"),
                                tr("Open or join a team room first: the pack carries that room's changes."))
        return None
    d = PigeonDialog(plugin, node)
    d.show()
    return d
