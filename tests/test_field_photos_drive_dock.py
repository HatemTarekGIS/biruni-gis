r"""Field photos through Google Drive in the Field Photos dock, off screen.

Run:  engine\bin\python-qgis.bat tests\test_field_photos_drive_dock.py
"""
import os
import shutil
import socket as _sock
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication, QgsProject, QgsSettings  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from biruni.field_photos import bridge, dock as fd, drive, handover, server as _fs  # noqa: E402

with _sock.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    _fs.PHOTO_PORT = PHOTO_PORT = _s.getsockname()[1]

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


class Bar:
    notices = []

    def pushWarning(self, *a):
        pass

    def pushSuccess(self, title, text):
        Bar.notices.append(text)

    def createMessage(self, title, text):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

        w = QWidget()
        QHBoxLayout(w)
        w.text = text
        return w

    def pushWidget(self, item, level, duration=0):
        Bar.notices.append(item.text)


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return Bar()


class Theme:
    def tokens(self):
        return {k: "#888888" for k in ("text", "text_dim", "accent", "panel", "border", "accent_text",
                                       "accent_hover", "mid", "icon")}


class Activity:
    def __init__(self):
        self.logged = []

    def log(self, *a):
        self.logged.append(a)


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.activity = Activity()
        self.toolbox = None


drive.suggest_roots = lambda *a, **kw: []

work = tempfile.mkdtemp()
folder = os.path.join(work, "photos")
QgsSettings().setValue(fd.KEY + "folder", folder)
QgsSettings().setValue(fd.KEY + "mode", "local")
plugin = Plugin()

_img = QImage(32, 24, QImage.Format.Format_RGB32)
_img.fill(QColor("#2CC5B4"))
_ba = QByteArray()
_buf = QBuffer(_ba)
_buf.open(QIODevice.OpenModeFlag.WriteOnly)
_img.save(_buf, "JPG")
JPEG = bytes(_ba)

_phone_sk = ec.generate_private_key(ec.SECP256R1())
_phone_pub = drive._b64u(
    _phone_sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
)


def put(root, k, pc_pub, pid, n, dev="DEV1", meta_over=None):
    meta = {"id": pid, "t": int(time.time() * 1000), "lat": 24.7, "lon": 46.7, "surveyor": "Sara"}
    meta.update(meta_over or {})
    body = handover.seal(pc_pub, meta, JPEG)
    h = {"room": drive.room_tag(k), "dev": dev, "kid": drive.key_id(pc_pub), "id": pid, "n": n,
         "t": meta["t"], "pub": _phone_pub, "cert": drive.cert(drive.auth_key(k, ""), _phone_pub, dev)}
    blob, fname = drive.pack(h, body, _phone_sk)
    d_dir = os.path.join(root, drive.folder_name(k), "p-" + dev)
    os.makedirs(d_dir, exist_ok=True)
    path = os.path.join(d_dir, fname)
    with open(path, "wb") as f:
        f.write(blob)
    return path


# 1. New project: box off, frame hidden, no project entry
d = fd.open_dock(plugin)
check(not d.drive_box.isChecked() and d.drive_frame.isHidden() and not d.drive_enabled()
      and not QgsProject.instance().readBoolEntry(fd.P_SCOPE, fd.P_DRIVE, False)[1],
      "1. new project: box off, frame hidden, no project entry")

# 2. Notice declined: box off again, no entry, project not dirty
confirm_calls = [0]
d._confirm_drive = lambda: (confirm_calls.__setitem__(0, confirm_calls[0] + 1), False)[1]
d._nudge_backup = lambda: None
QgsProject.instance().setDirty(False)
d.drive_box.setChecked(True)
check(not d.drive_box.isChecked() and d.drive_frame.isHidden() and not d.drive_enabled()
      and not QgsProject.instance().isDirty() and confirm_calls[0] == 1,
      "2. notice declined: box off again, no entry, project not dirty")

# 3. Notice accepted: entry true, dirty, frame shown; untick then tick: notice not asked again
confirm_calls[0] = 0
d._confirm_drive = lambda: (confirm_calls.__setitem__(0, confirm_calls[0] + 1), True)[1]
d.drive_box.setChecked(True)
check(d.drive_enabled() and QgsProject.instance().isDirty() and not d.drive_frame.isHidden()
      and confirm_calls[0] == 1,
      "3a. notice accepted: entry true, dirty, frame shown")
d.drive_box.setChecked(False)
d.drive_box.setChecked(True)
check(confirm_calls[0] == 1 and d.drive_enabled(),
      "3b. untick then tick: notice not asked again")

# 4. Project cleared then on_project_changed: box off
QgsProject.instance().clear()
fd.on_project_changed(plugin)
check(not d.drive_box.isChecked() and d.drive_frame.isHidden() and not d.drive_enabled(),
      "4. project cleared then on_project_changed: box off")

# 5. Roots persist in QgsSettings across a new dock; Remove works; duplicate Add ignored
d.drive_box.setChecked(True)
r1 = os.path.join(work, "root1")
r2 = os.path.join(work, "root2")
os.makedirs(r1)
os.makedirs(r2)
picks = iter([r1, r1, r2])
orig_get_dir = fd.QFileDialog.getExistingDirectory
fd.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **kw: next(picks, ""))
try:
    d.drive_add_btn.click()
    d.drive_add_btn.click()
    d.drive_add_btn.click()
finally:
    fd.QFileDialog.getExistingDirectory = orig_get_dir
check(d.drive_list.count() == 2 and d._drive_roots() == [r1, r2], "5a. duplicate Add ignored")

fd.close_dock(plugin)
d = fd.open_dock(plugin)
d._confirm_drive = lambda: True
d._nudge_backup = lambda: None
check(d._drive_roots() == [r1, r2] and d.drive_list.count() == 2,
      "5b. roots persist in QgsSettings across a new dock")
d.drive_list.setCurrentRow(1)
d.drive_remove_btn.click()
check(d._drive_roots() == [r1] and d.drive_list.count() == 1, "5c. Remove works")

# 6. Flag off: after start(), d.drive is None
QgsProject.instance().clear()
fd.on_project_changed(plugin)
d.start()
check(d.server is not None and d.drive is None, "6. flag off: after start(), d.drive is None")
d.stop()

# 7. Flag on with a root: after start() intake thread is alive and shares server.store and server.handover
d.drive_box.setChecked(True)
drive.INTERVAL = 0.2
d.start()
check(d.drive is not None and d.drive.thread is not None and d.drive.thread.is_alive()
      and d.drive.store is d.server.store and d.drive.handover is d.server.handover,
      "7. flag on with a root: intake thread alive, shares store and handover")

# 8. .bfp dropped -> record exists with route "drive" and status shows "Received: 1"
k = bridge.load_key(QgsSettings())
put(r1, k, d.server.handover.public, "drive-photo-1", 1)
deadline = time.time() + 10
while time.time() < deadline and "drive-photo-1" not in d.server.store.records:
    time.sleep(0.1)
d._read_events()
rec = d.server.store.records.get("drive-photo-1") or {}
check(rec.get("route") == "drive" and "Received: 1" in d.drive_state.text(),
      f"8. .bfp taken in with route drive and Received: 1 ({d.drive_state.text()!r})")

# 9. ("drive", {"error": "x", "at": 0}) and {"folders": 0, ...} give the right status texts
d.server.events.put(("drive", {"error": "x", "at": 0}))
d._read_events()
check("could not be checked: x" in d.drive_state.text(),
      f"9a. drive error status text ({d.drive_state.text()!r})")
d.server.events.put(("drive", {"folders": 0, "seen": 0, "new": 0, "refused": [], "at": 0}))
d._read_events()
check(drive.folder_name(k) in d.drive_state.text() and "No Biruni folder" in d.drive_state.text(),
      f"9b. folders == 0 status text ({d.drive_state.text()!r})")

# 10. A blocked device's file is refused; d.unblock_device(...) then brings it in
d.server.store.block_device("phone-blk-1", reason="test")
put(r1, k, d.server.handover.public, "drive-photo-2", 2, dev="DEV2", meta_over={"device_id": "phone-blk-1"})
deadline = time.time() + 10
while time.time() < deadline:
    with d.drive._lock:
        seen_now = dict(d.drive.seen)
    if any(key.startswith("p-DEV2/") for key in seen_now):
        break
    time.sleep(0.1)
d._read_events()
check("drive-photo-2" not in d.server.store.records, "10a. blocked device's file is refused")
d.unblock_device("phone-blk-1")
deadline = time.time() + 10
while time.time() < deadline and "drive-photo-2" not in d.server.store.records:
    time.sleep(0.1)
d._read_events()
check("drive-photo-2" in d.server.store.records, "10b. unblock_device brings it in")

# 11. Untick while running: intake stops, server stays; tick again: restarts
d.drive_box.setChecked(False)
check(d.drive is None and d.server is not None, "11a. untick while running: intake stops, server stays")
d.drive_box.setChecked(True)
check(d.drive is not None and d.drive.thread is not None and d.drive.thread.is_alive(),
      "11b. tick again while running: intake restarts")

# 12. stop(): intake thread ended, d.drive is None
t = d.drive.thread
d.stop()
check(d.drive is None and not t.is_alive(), "12. stop(): intake thread ended, d.drive is None")

# 13. Key hint warns before backup; after handover.backup + KEY_BACKUP set via dock method it shows the date
d.drive_box.setChecked(True)
d._show_key_state()
check("not backed up" in d.key_hint.text().lower(), "13a. key hint warns before backup")
bpath = os.path.join(work, "pc.biruni-key")
pub = handover.backup(d._cert_folder(), bpath, "StrongPass#2026")
d._record_key_backup(drive.key_id(pub), at="2026-11-17")
d._show_key_state()
check("2026-11-17" in d.key_hint.text(), f"13b. key hint shows backup date ({d.key_hint.text()!r})")

# 14. Restore is refused while started
warns = []
orig_warn = fd.QMessageBox.warning
fd.QMessageBox.warning = staticmethod(lambda parent, title, text, *a: warns.append(text))
try:
    d.start()
    d._restore_key()
finally:
    fd.QMessageBox.warning = orig_warn
    d.stop()
check(any("Stop Field Photos first." in w for w in warns), "14. restore refused while started")

# 15. The backup password dialog builds (closed at once), and a cancelled backup writes nothing
orig_exec = fd.QDialog.exec
fd.QDialog.exec = lambda self: 0
try:
    built = d._ask_backup_password()
    d._backup_key()
    ok15 = built is None
except Exception as e:  # noqa: BLE001
    ok15 = False
    print("dialog error:", e)
finally:
    fd.QDialog.exec = orig_exec
check(ok15 and "2026-11-17" in d.key_hint.text(), "15. the password dialog builds; cancelling changes nothing")

fd.close_dock(plugin)
QgsProject.instance().clear()
shutil.rmtree(work, ignore_errors=True)
shutil.rmtree(SETTINGS_DIR, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
