"""The "Backup and sync" window: choose the backup disk, see what ran, run a job now. Not modal (the window stays
usable; jobs run in a background thread and report back). 9c (sync between PCs) and 9d (off-site) are marked tabs."""
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
from . import SETTINGS, engine, schedule, secret

KINDS = (("usb", "USB disk"), ("nas", "Network folder (NAS)"), ("folder", "Another folder"))


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
        self._placeholder_tab(tr("Sync between PCs"), tr(
            "Syncing folders between your computers is not available yet. A synced copy never counts as a backup."))
        self._placeholder_tab(tr("Off-site"), tr("Copying the backup off-site is not available yet."))
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

    def _placeholder_tab(self, title, text):
        w = QWidget()
        QVBoxLayout(w).addWidget(_hint(text))
        self.tabs.addTab(w, title)

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

    # ---- what is shown --------------------------------------------------------------------------------------------------
    def refresh(self):
        st = engine.load_state(self.cert)
        lb = st.get("last_backup") or {}
        if lb.get("at"):
            self.status.setText(tr("Last backup: {t}.").format(t=time.strftime("%Y-%m-%d %H:%M", time.localtime(lb["at"]))))
        else:
            self.status.setText(tr("No backup has run yet."))
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
