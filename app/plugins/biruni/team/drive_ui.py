"""Google Drive wizard and warning helpers for team rooms."""
import os
import subprocess

from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout)

from ..field_photos import drive
from ..i18n import tr
from ..project import safe_name
from . import folder as rf
from . import session

def member_steps_text():
    return tr(
        "1. drive.google.com → Shared with me. 2. Right-click «Biruni Team - …» → Organize → Add shortcut → "
        "My Drive. 3. In File Explorer, right-click the folder → Offline access → Available offline. (Required.)"
    )


def offline_warning_text():
    return tr(
        "This folder is streamed from Google Drive. Make it available offline, or changes may arrive late "
        "and Biruni may wait for downloads."
    )


def needs_offline_warning(status, store):
    """True when streamed without offline confirmation, or placeholders/slow/unreadable files were seen."""
    if not status:
        return False
    if status.get("streamed") and not (store.get("folder_offline_ok") if store else False):
        return True
    if int(status.get("unreadable") or 0) > 0 or bool(status.get("slow")):
        return True
    if int(status.get("placeholders") or 0) > 0:
        return True
    return False


def show_qr(node, parent):
    """Show the room code as a QR code dialog."""
    from ..gps.browser import qr_png
    from ..vendor import ensure_vendor

    ensure_vendor()
    code = session.room_code(node)
    pm = QPixmap()
    pm.loadFromData(qr_png(code, 8))
    d = QDialog(parent)
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


def _open_in_explorer(path):
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _left_out_lines(items):
    why_map = {"raster": tr("raster"), "too large": tr("too large")}
    lines = []
    for item in items or ():
        why = why_map.get(item.get("why"), item.get("why") or "")
        lines.append(tr("{layer}: stays on this computer ({why}). Hand it over separately.").format(
            layer=item.get("name", ""), why=why))
    return "\n".join(lines)


class DriveRoomDialog(QDialog):
    """Wizard for setting up and managing a team room over Google Drive."""

    def __init__(self, node, parent=None):
        super().__init__(parent)
        self.node = node
        self.setWindowTitle(tr("Team over Google Drive"))
        self._lay = QVBoxLayout(self)
        self.warning_label = QLabel(offline_warning_text())
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #B45309; font-weight: bold;")
        self.left_out_label = QLabel()
        self.left_out_label.setWordWrap(True)

        if node.role == "host":
            self._build_host()
        else:
            self._build_member()

        self._lay.addWidget(self.left_out_label)
        self._lay.addWidget(self.warning_label)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        self._lay.addWidget(bb)
        self.refresh()
        self.resize(520, 360)

    def _build_host(self):
        self.create_btn = QPushButton(tr("Create the Folder"))
        self.create_btn.clicked.connect(self.create_folder)
        self._lay.addWidget(self.create_btn)

        self.no_drive_label = QLabel(tr("Google Drive for desktop is not running on this computer."))
        self.no_drive_label.setWordWrap(True)
        self.no_drive_label.setVisible(False)
        self._lay.addWidget(self.no_drive_label)

        self.choose_btn = QPushButton(tr("Choose a Folder…"))
        self.choose_btn.clicked.connect(self._choose_host_folder)
        self.choose_btn.setVisible(False)
        self._lay.addWidget(self.choose_btn)

        self.host_steps = QLabel()
        self.host_steps.setWordWrap(True)
        self._lay.addWidget(self.host_steps)

        row = QHBoxLayout()
        self.explorer_btn = QPushButton(tr("Show in Explorer"))
        self.explorer_btn.clicked.connect(self._show_explorer)
        self.copy_name_btn = QPushButton(tr("Copy the Folder's Name"))
        self.copy_name_btn.clicked.connect(self._copy_name)
        self.qr_btn = QPushButton(tr("Room Code"))
        self.qr_btn.clicked.connect(lambda: show_qr(self.node, self))
        for b in (self.explorer_btn, self.copy_name_btn, self.qr_btn):
            row.addWidget(b)
        self._lay.addLayout(row)

    def _build_member(self):
        self.member_steps = QLabel(member_steps_text())
        self.member_steps.setWordWrap(True)
        self._lay.addWidget(self.member_steps)

        self.offline_cb = QCheckBox(tr("I made the folder available offline"))
        self.offline_cb.setChecked(bool(self.node.store.get("folder_offline_ok")))
        self.offline_cb.toggled.connect(self._offline_toggled)
        self._lay.addWidget(self.offline_cb)

        self.choose_member_btn = QPushButton(tr("Choose a Folder…"))
        self.choose_member_btn.clicked.connect(self._choose_member_folder)
        self._lay.addWidget(self.choose_member_btn)

    def _offline_toggled(self, on):
        self.node.store.put("folder_offline_ok", bool(on))
        self.node.changed.emit()
        self.refresh()

    def _folder_name(self):
        room_name = self.node.store.get("room_name", "") or "Room"
        clean = safe_name(room_name) or "Room"
        return f"Biruni Team - {clean}"

    def _setup_host_parent(self, root):
        parent = os.path.join(root, self._folder_name())
        if not os.path.isdir(parent):
            os.mkdir(parent)
        rf.create_room_folder(self.node, parent)
        rf.use_folder(self.node, parent)
        self.refresh()
        return parent

    def create_folder(self):
        """Host: create 'Biruni Team - <name>' under the first suggested Drive root."""
        roots = drive.suggest_roots()
        if not roots:
            self.no_drive_label.setVisible(True)
            self.choose_btn.setVisible(True)
            return None
        self.no_drive_label.setVisible(False)
        self.choose_btn.setVisible(False)
        try:
            return self._setup_host_parent(roots[0])
        except (OSError, rf.FolderError) as e:
            QMessageBox.warning(self, tr("Team over Google Drive"), str(e))
            return None

    def _choose_host_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("Choose a Folder…"))
        if not d:
            return
        try:
            if os.path.basename(os.path.normpath(d)) == self._folder_name():
                rf.create_room_folder(self.node, d)
                rf.use_folder(self.node, d)
                self.refresh()
            else:
                self._setup_host_parent(d)
        except (OSError, rf.FolderError) as e:
            QMessageBox.warning(self, tr("Team over Google Drive"), str(e))

    def _choose_member_folder(self):
        cur = self.node.store.get("folder_carrier") or ""
        d = QFileDialog.getExistingDirectory(self, tr("Choose a Folder…"), cur)
        if not d:
            return
        try:
            rf.use_folder(self.node, d)
        except rf.FolderError as e:
            why = e.args[0] if e.args else ""
            msg = tr("No team room was found in that folder.") if why == "no-room-here" else tr(
                "Could not use that folder.")
            QMessageBox.warning(self, tr("Team over Google Drive"), msg)
        self.refresh()

    def _show_explorer(self):
        folder = self.node.store.get("folder_carrier") or ""
        if folder and os.path.isdir(folder):
            _open_in_explorer(folder)

    def _copy_name(self):
        folder = self.node.store.get("folder_carrier") or ""
        name = os.path.basename(os.path.normpath(folder)) if folder else self._folder_name()
        cb = QApplication.clipboard()
        if cb is not None and name:
            cb.setText(name)

    def refresh(self):
        """Update visible instructions, buttons, left-out layers, and offline warning."""
        folder = self.node.store.get("folder_carrier") or ""
        if self.node.role == "host":
            has_folder = bool(folder)
            self.host_steps.setVisible(has_folder)
            self.explorer_btn.setVisible(has_folder)
            self.copy_name_btn.setVisible(has_folder)
            self.qr_btn.setVisible(has_folder)
            if has_folder:
                name = os.path.basename(os.path.normpath(folder))
                self.host_steps.setText(tr(
                    "1. Open drive.google.com. 2. Right-click «{name}» → Share. 3. Add each member's Google "
                    "address, role Editor, Send. 4. Give each member the room code by QR, by phone or in person - "
                    "never inside Google Drive."
                ).format(name=name))
            left_out = self.node.hub.base_left_out() if self.node.hub is not None else []
        else:
            left_out = self.node.store.get("folder_left_out") or []

        lo_text = _left_out_lines(left_out)
        self.left_out_label.setText(lo_text)
        self.left_out_label.setVisible(bool(lo_text))

        c = rf.carrier_for(self.node)
        status = c.status if c is not None else {}
        self.warning_label.setVisible(needs_offline_warning(status, self.node.store))
