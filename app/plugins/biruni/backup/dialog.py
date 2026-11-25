"""The "Backup and sync" window: choose the backup disk, see what ran, run a job now, sync folders between PCs
(Syncthing, 9c), and copy the restic repository off-site (rclone, 9d). Not modal (the window stays usable; jobs run in
a background thread and report back)."""
import json
import os
import time

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
                                 QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget,
                                 QVBoxLayout, QWidget)

from .. import background
from ..i18n import tr
from . import SETTINGS, engine, offsite, schedule, secret, syncthing, tools

KINDS = (("usb", "USB disk"), ("nas", "Network folder (NAS)"), ("folder", "Another folder"))
ROLES = (("office", "Office or branch (send only)"), ("center", "Main centre (receive only, keep versions)"))


def _hint(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color:#6c7680")
    return lab


class BackupDialog(QDialog):
    def __init__(self, plugin=None, parent=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin is not None and hasattr(plugin, "iface") else None))
        self.plugin = plugin
        self.setWindowTitle(tr("Backup and sync"))
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.busy = False
        self.s = QgsSettings()
        self.cert = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
        v = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs)
        self._target_tab()
        self._schedule_tab()
        self._sync_tab()
        self._offsite_tab()
        self._log_tab()
        row = QHBoxLayout()
        self.btns = {}
        for key, label, fn in (("now", tr("Back up now"), self.back_up_now), ("verify", tr("Verify now"), self.verify_now),
                               ("test", tr("Test a restore"), self.test_restore), ("restore", tr("Restore…"), self.restore),
                               ("prune", tr("Free space (administrator)…"), self.free_space)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, f=fn: f())
            row.addWidget(b)
            self.btns[key] = b
        v.addLayout(row)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.resize(640, 480)
        self.load()
        self.refresh()

    # ---- tabs -------------------------------------------------------------------------------------------------------
    def _target_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.enabled = QCheckBox(tr("Back up automatically while Biruni is open"))
        f.addRow(self.enabled)
        self.kind = QComboBox()
        for k, label in KINDS:
            self.kind.addItem(tr(label), k)
        f.addRow(tr("Kind"), self.kind)
        r = QHBoxLayout()
        self.repo = QLineEdit()
        self.repo.setPlaceholderText(tr("A folder on another disk"))
        pick = QPushButton(tr("Choose…"))
        pick.clicked.connect(self.pick_repo)
        r.addWidget(self.repo)
        r.addWidget(pick)
        f.addRow(tr("Backup folder"), r)
        self.extra = QLineEdit()
        self.extra.setPlaceholderText(tr("Other folders to include, separated by ;"))
        f.addRow(tr("Also back up"), self.extra)
        self.auto_intake = QCheckBox(tr("This is a backup computer: read the office's Google Drive folder automatically"))
        f.addRow(self.auto_intake)
        save = QPushButton(tr("Save"))
        save.clicked.connect(self.save)
        f.addRow(save)
        f.addRow(_hint(tr(
            "Biruni never deletes old backups by itself. A program that runs as you could delete them too, so keep a "
            "second disk and swap the two every week.")))
        self.tabs.addTab(w, tr("Target"))

    def _schedule_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.spins = {}
        for key, label, lo, hi in (("backup-hours", tr("Back up every (hours)"), 1, 168),
                                   ("check-days", tr("Check the backup every (days)"), 1, 90),
                                   ("restore-days", tr("Test a restore every (days)"), 1, 365),
                                   ("vault-days", tr("Snapshot the keys every (days)"), 1, 90)):
            sp = QDoubleSpinBox()
            sp.setDecimals(0)
            sp.setRange(lo, hi)
            f.addRow(label, sp)
            self.spins[key] = sp
        f.addRow(_hint(tr("Backups run only while Biruni is open. The health screen shows how old the last one is.")))
        self.tabs.addTab(w, tr("Schedule"))

    def _sync_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.sync_enabled = QCheckBox(tr("Sync the photos folder with another office computer"))
        f.addRow(self.sync_enabled)
        self.sync_role = QComboBox()
        for k, label in ROLES:
            self.sync_role.addItem(tr(label), k)
        f.addRow(tr("This computer's role"), self.sync_role)
        self.sync_lan = QCheckBox(tr("Local network only (no internet discovery or relays)"))
        f.addRow(self.sync_lan)
        self.my_dev_id = QLineEdit()
        self.my_dev_id.setReadOnly(True)
        self.my_dev_id.setPlaceholderText(tr("Install Syncthing to see this computer's ID"))
        f.addRow(tr("This computer's ID"), self.my_dev_id)
        pr = QHBoxLayout()
        self.peer_id = QLineEdit()
        self.peer_id.setPlaceholderText(tr("The other computer's Syncthing ID"))
        self.peer_name = QLineEdit()
        self.peer_name.setPlaceholderText(tr("Label (for example: Main centre)"))
        add_btn = QPushButton(tr("Add computer"))
        add_btn.clicked.connect(self.add_sync_peer)
        pr.addWidget(self.peer_id, 2)
        pr.addWidget(self.peer_name, 1)
        pr.addWidget(add_btn)
        f.addRow(tr("Other computer"), pr)
        sr = QHBoxLayout()
        save_sync = QPushButton(tr("Save"))
        save_sync.clicked.connect(self.save)
        dl_sync = QPushButton(tr("Download Syncthing"))
        dl_sync.clicked.connect(lambda _=False: self.download_tool("syncthing"))
        sr.addWidget(save_sync)
        sr.addWidget(dl_sync)
        f.addRow(sr)
        f.addRow(_hint(tr(
            "Syncing keeps a live copy on another computer, so work continues if this computer breaks. A synced folder "
            "is NOT a backup: damage or a mistake can travel to the other computer before it is noticed, and a synced "
            "peer at 100% never marks photos as backed up.")))
        self.tabs.addTab(w, tr("Sync between PCs"))

    def _offsite_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.offsite_remote = QLineEdit()
        self.offsite_remote.setPlaceholderText(tr("Remote name and folder (for example: office-cloud:biruni-backup)"))
        f.addRow(tr("Cloud remote"), self.offsite_remote)
        br = QHBoxLayout()
        cfg_btn = QPushButton(tr("Configure cloud remote…"))
        cfg_btn.clicked.connect(self.configure_offsite)
        copy_btn = QPushButton(tr("Copy off-site now"))
        copy_btn.clicked.connect(self.copy_offsite_now)
        dl_rc = QPushButton(tr("Download rclone"))
        dl_rc.clicked.connect(lambda _=False: self.download_tool("rclone"))
        br.addWidget(cfg_btn)
        br.addWidget(copy_btn)
        br.addWidget(dl_rc)
        f.addRow(br)
        save_off = QPushButton(tr("Save"))
        save_off.clicked.connect(self.save)
        f.addRow(save_off)
        f.addRow(_hint(tr(
            "Copies the encrypted backup repository to your own cloud account with rclone copy --immutable. Biruni "
            "never runs sync or deletes remote files, and the rclone configuration is locked with a password kept in "
            "your key backup. Make a new key backup after setting this up.")))
        self.tabs.addTab(w, tr("Off-site"))

    def _log_tab(self):
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, tr("Log"))

    # ---- settings -----------------------------------------------------------------------------------------------------
    def load(self):
        cfg = engine.config(lambda k, d=None: self.s.value(k, d))
        self.enabled.setChecked(cfg["enabled"])
        self.repo.setText(cfg["repo"])
        i = self.kind.findData(cfg["kind"])
        self.kind.setCurrentIndex(max(0, i))
        self.extra.setText(";".join(engine.extra_paths(lambda k, d=None: self.s.value(k, d))))
        self.auto_intake.setChecked(cfg["auto-intake"])
        for key, sp in self.spins.items():
            sp.setValue(cfg[key])
        self.sync_enabled.setChecked(cfg["sync-enabled"])
        ri = self.sync_role.findData(cfg["sync-role"])
        self.sync_role.setCurrentIndex(max(0, ri))
        self.sync_lan.setChecked(cfg["sync-lan-only"])
        self.offsite_remote.setText(cfg["offsite-remote"])

    def save(self):
        set_ = lambda k, v: self.s.setValue(SETTINGS + k, v)   # noqa: E731
        repo = self.repo.text().strip()
        first = bool(repo) and not secret.load_password(self.cert)
        set_("enabled", self.enabled.isChecked())
        set_("repo", repo)
        set_("kind", self.kind.currentData())
        set_("extra-paths", self.extra.text().strip())
        set_("auto-intake", self.auto_intake.isChecked())
        for key, sp in self.spins.items():
            set_(key, int(sp.value()))
        set_("sync-enabled", self.sync_enabled.isChecked())
        set_("sync-role", self.sync_role.currentData())
        set_("sync-lan-only", self.sync_lan.isChecked())
        remote = self.offsite_remote.text().strip()
        set_("offsite-remote", remote)
        if remote:
            offsite.ensure_password(self.cert)
        if self.sync_enabled.isChecked():
            syncthing.ensure_gui_lock(syncthing.default_home(self.cert), self.cert, lan_only=self.sync_lan.isChecked())
        if first:
            pw, _created = secret.ensure_password(self.cert)
            box = QMessageBox(QMessageBox.Icon.Information, tr("Backup password"), tr(
                "The backup's password was made and is kept on this computer. It is also saved inside your key backup: "
                "make a new key backup now. Write it down and keep it where the backup disk is not:\n\n{pw}").format(pw=pw),
                QMessageBox.StandardButton.Ok, self)
            box.open()      # shown once, not modal
        self.refresh()

    def pick_repo(self):
        d = QFileDialog.getExistingDirectory(self, tr("Backup folder"), self.repo.text() or os.path.expanduser("~"))
        if d:
            self.repo.setText(d)

    # ---- running jobs --------------------------------------------------------------------------------------------------
    def _run(self, name, fn):
        if self.busy:
            return
        self.busy = True
        self.status.setText(tr("Working: {name}…").format(name=name))
        for b in self.btns.values():
            b.setEnabled(False)

        def done(res, err):
            self.busy = False
            for b in self.btns.values():
                b.setEnabled(True)
            if err is not None:
                self.status.setText(tr("It could not be done: {e}").format(e=err))
            else:
                self.refresh()
            self.fill_log()

        background.run(fn, done)

    def _engine(self):
        return schedule.make_engine(self.plugin)

    def back_up_now(self):
        self.save()
        eng = self._engine()
        self._run(tr("Back up now"), eng.run_backup)

    def verify_now(self):
        eng = self._engine()
        self._run(tr("Verify now"), eng.run_check)

    def test_restore(self):
        eng = self._engine()
        self._run(tr("Test a restore"), eng.run_restore_test)

    def restore(self):
        target = QFileDialog.getExistingDirectory(self, tr("Restore…"), os.path.expanduser("~"))
        if not target:
            return
        eng = self._engine()

        def work():
            with eng.lock:
                r = eng.restic()
                snaps = sorted(r.snapshots(), key=lambda s: str(s.get("time") or ""))
                if not snaps:
                    raise RuntimeError("no snapshot")
                r.restore(str(snaps[-1].get("id")), engine.snapshot_path(eng.photos), target)
            return target

        self._run(tr("Restore…"), work)

    def free_space(self):
        eng = self._engine()
        text, ok = QInputDialog.getText(self, tr("Free space (administrator)…"), tr(
            "This deletes old backups for good. Type the organisation's name to go on:"))
        if not ok:
            return
        if not eng.org_name() or text.strip() != eng.org_name():
            QMessageBox.warning(self, tr("Free space (administrator)…"), tr("The name does not match; nothing was deleted."))
            return
        policy = {"keep-daily": 30, "keep-weekly": 26, "keep-monthly": 24}
        self._run(tr("Free space (administrator)…"), lambda: eng.admin_prune(policy, text))

    def download_tool(self, name):
        self._run(name, lambda: tools.download(name))

    def add_sync_peer(self):
        self.save()
        pid = self.peer_id.text().strip()
        if not pid:
            return
        name = self.peer_name.text().strip()
        role = self.sync_role.currentData() or "office"
        eng = self._engine()
        st = syncthing.Syncthing(cert_folder=self.cert, lan_only=self.sync_lan.isChecked())
        st.ensure_gui_lock()
        st.add_peer(pid, name=name)
        st.share(eng.photos, pid, role=role)
        self.peer_id.clear()
        self.peer_name.clear()
        self.refresh()

    def configure_offsite(self):
        self.save()
        try:
            offsite.ensure_password(self.cert)
            offsite.Rclone(cert_folder=self.cert).open_config_console()
        except Exception as e:  # noqa: BLE001
            self.status.setText(tr("It could not be done: {e}").format(e=e))

    def copy_offsite_now(self):
        self.save()
        eng = self._engine()
        self._run(tr("Copy off-site now"), eng.run_offsite)

    # ---- what is shown --------------------------------------------------------------------------------------------------
    def refresh(self):
        st = engine.load_state(self.cert)
        lb = st.get("last_backup") or {}
        if lb.get("at"):
            self.status.setText(tr("Last backup: {t}.").format(t=time.strftime("%Y-%m-%d %H:%M", time.localtime(lb["at"]))))
        else:
            self.status.setText(tr("No backup has run yet."))
        try:
            if tools.find("syncthing"):
                did = syncthing.device_id(cert_folder=self.cert)
                if did:
                    self.my_dev_id.setText(did)
        except Exception:  # noqa: BLE001
            pass
        self.fill_log()

    def fill_log(self):
        lines = []
        try:
            with open(os.path.join(engine.backup_dir(self.cert), "log.jsonl"), encoding="utf-8") as f:
                for raw in f.readlines()[-60:]:
                    try:
                        r = json.loads(raw)
                        lines.append(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r.get('at', 0)))}  {r.get('kind')}  "
                                     f"{'ok' if r.get('ok') else 'deferred' if r.get('deferred') else 'FAILED'}")
                    except ValueError:
                        continue
        except OSError:
            pass
        self.log.setPlainText("\n".join(reversed(lines)))


def open_dialog(plugin):
    dlg = getattr(plugin, "_backup_dialog", None)
    if dlg is None:
        dlg = BackupDialog(plugin)
        try:
            plugin._backup_dialog = dlg
        except AttributeError:
            pass
    else:
        dlg.load()
        dlg.refresh()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
