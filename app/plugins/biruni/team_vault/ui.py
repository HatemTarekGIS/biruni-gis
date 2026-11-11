"""The team vault window: snapshots and their health, this computer as a keeper, the recovery kit."""
import os
import threading
import time

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
                                 QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
                                 QVBoxLayout, QWidget)

from ..i18n import tr
from . import core, net


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


STATE = {"safe": "Safe", "at risk": "At risk", "lost": "Not enough shards", "waiting": "Waiting for members",
         "not shared": "Not shared yet"}


class _Job:
    """Runs fn in a thread; the window polls ``done``."""

    def __init__(self, fn):
        self.result = self.error = None
        self.done = False
        self.progress = (0, 0)

        def run():
            try:
                self.result = fn(self)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.error = e
            self.done = True
        threading.Thread(target=run, daemon=True).start()


class SnapshotDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New Vault Snapshot"))
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Files and folders to keep:")))
        self.paths = QListWidget()
        lay.addWidget(self.paths)
        row = QHBoxLayout()
        for text, fn in ((tr("Add Files…"), self._files), (tr("Add Folder…"), self._folder),
                         (tr("Project Folder"), self._project), (tr("Remove"), self._remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        form = QFormLayout()
        self.label = QLineEdit()
        self.label.setPlaceholderText(tr("e.g. Before the municipality review"))
        form.addRow(tr("Name"), self.label)
        self.k = QSpinBox()
        self.k.setRange(1, 50)
        self.k.setValue(core.DEFAULT_K)
        self.n = QSpinBox()
        self.n.setRange(1, 100)
        self.n.setValue(core.DEFAULT_N)
        kn = QHBoxLayout()
        kn.addWidget(self.k)
        kn.addWidget(QLabel(tr("of")))
        kn.addWidget(self.n)
        kn.addStretch(1)
        form.addRow(tr("Any … of … shards restore it"), kn)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText(tr("Optional - recommended"))
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(tr("Vault password"), self.pw)
        form.addRow(tr("Repeat the password"), self.pw2)
        lay.addLayout(form)
        note = QLabel(tr("Without a password, k team members together could open the snapshot. With one, nobody "
                         "can without it - and nobody can recover it if it is forgotten."))
        note.setWordWrap(True)
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Take Snapshot"))
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._project()

    def _files(self):
        fs, _ = QFileDialog.getOpenFileNames(self, tr("Files to keep"))
        for f in fs:
            self.paths.addItem(f)

    def _folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("Folder to keep"))
        if d:
            self.paths.addItem(d)

    def _project(self):
        p = QgsProject.instance().fileName()
        if p:
            d = os.path.dirname(p)
            if d and not self.paths.findItems(d, Qt.MatchFlag.MatchExactly):
                self.paths.addItem(d)

    def _remove(self):
        for it in self.paths.selectedItems():
            self.paths.takeItem(self.paths.row(it))

    def values(self):
        return {"paths": [self.paths.item(i).text() for i in range(self.paths.count())],
                "label": self.label.text().strip(), "k": self.k.value(), "n": self.n.value(),
                "password": self.pw.text() or None}

    def _ok(self):
        v = self.values()
        if not v["paths"]:
            QMessageBox.warning(self, self.windowTitle(), tr("Add at least one file or folder."))
            return
        if v["k"] > v["n"]:
            QMessageBox.warning(self, self.windowTitle(), tr("The number needed cannot be more than the shards."))
            return
        if self.pw.text() != self.pw2.text():
            QMessageBox.warning(self, self.windowTitle(), tr("The two passwords differ."))
            return
        self.accept()


class VaultDialog(QDialog):
    def __init__(self, plugin=None, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.job = None
        self.setWindowTitle(tr("Team Vault"))
        self.resize(820, 520)
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Snapshots of your files kept on the computers of the team room: encrypted, cut into "
                          "shards, any few of them give it back. No computer can read what it keeps."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)
        self.tabs.addTab(self._snap_tab(), tr("Snapshots"))
        self.tabs.addTab(self._keep_tab(), tr("This Computer"))
        self.tabs.addTab(self._kit_tab(), tr("Recovery"))
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._poll)
        self.refresh()

    # ---- tabs ---------------------------------------------------------------------------------------------------
    def _snap_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([tr("Snapshot"), tr("Date"), tr("Files"), tr("Size"), tr("New data"),
                                              tr("Shards alive"), tr("Can lose"), tr("State")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        self.b_new = QPushButton(tr("New Snapshot…"))
        self.b_restore = QPushButton(tr("Restore…"))
        self.b_repair = QPushButton(tr("Repair"))
        self.b_export = QPushButton(tr("Save Shards to Folders…"))
        self.b_remove = QPushButton(tr("Remove…"))
        self.b_refresh = QPushButton(tr("Refresh"))
        self.b_keep = QPushButton(tr("Cancel Deletion"))
        self.b_keep.setToolTip(tr("The members were asked to delete some of your shards. They keep them 30 days "
                                  "first: if you did not remove those snapshots yourself, cancel here."))
        self.b_keep.hide()
        for b, fn in ((self.b_new, self.new_snapshot), (self.b_restore, self.restore), (self.b_repair, self.repair),
                      (self.b_export, self.export), (self.b_remove, self.remove), (self.b_refresh, self.refresh),
                      (self.b_keep, self.cancel_deletion)):
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _keep_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        hold, quota = net.load_settings()
        self.hold = QCheckBox(tr("Keep shards for the team on this computer"))
        self.hold.setChecked(hold)
        self.quota = QSpinBox()
        self.quota.setRange(1, 1024)
        self.quota.setSuffix(" GB")
        self.quota.setValue(max(1, quota // 1024))
        form = QFormLayout()
        form.addRow(self.hold)
        form.addRow(tr("Space given to the team"), self.quota)
        lay.addLayout(form)
        self.hold.toggled.connect(self._save_keep)
        self.quota.valueChanged.connect(self._save_keep)
        note = QLabel(tr("These shards belong to others. Each is one part of an encrypted whole: this computer "
                         "cannot open it, and the owner can always ask for it back."))
        note.setWordWrap(True)
        lay.addWidget(note)
        self.held = QTableWidget(0, 3)
        self.held.setHorizontalHeaderLabels([tr("Owner"), tr("Shards"), tr("Size")])
        self.held.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.held.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.held)
        return w

    def _kit_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.kit_fp = QLabel()
        self.kit_fp.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.kit_fp)
        note = QLabel(tr("If this computer is lost, the recovery kit lets you ask the team for your shards from "
                         "another computer. Keep it on paper or a USB stick, away from this computer."))
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QHBoxLayout()
        for text, fn in ((tr("Save Recovery Kit…"), self.save_kit), (tr("Use a Recovery Kit…"), self.use_kit),
                         (tr("Restore from Folders…"), self.restore_folders)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    # ---- state --------------------------------------------------------------------------------------------------
    def vault(self):
        k = net.current()
        if k is not None:
            return k.vault
        if getattr(self, "_own_vault", None) is None:
            self._own_vault = core.Vault()
        return self._own_vault

    def refresh(self):
        k = net.current()
        try:
            rows = k.health() if k is not None else [dict(s, state="not shared", alive=0, can_lose=None)
                                                    for s in self.vault().snapshots()]
        except Exception as e:  # noqa: BLE001
            rows = [dict(s, state="not shared", alive=0, can_lose=None) for s in self.vault().snapshots()]
            self.status.setText(tr("The team room did not answer: {}").format(e))
        self.rows = rows
        self.table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            cells = [s.get("label") or s["id"][:8], time.strftime("%Y-%m-%d %H:%M", time.localtime(s["ts"])),
                     str(s.get("files", "")), human(s.get("total")), human(s.get("new")),
                     f"{s.get('alive', 0)} / {s.get('n') or '-'}",
                     "-" if s.get("can_lose") is None or s["can_lose"] < 0 else str(s["can_lose"]),
                     tr(STATE.get(s.get("state"), s.get("state", "")))]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setData(Qt.ItemDataRole.UserRole, s["id"])
                self.table.setItem(r, c, it)
        held = self.vault().held()
        by_owner = {}
        for p, v in held.items():
            e = by_owner.setdefault(v["owner"], [0, 0])
            e[0] += len(v["idx"])
            e[1] += v["bytes"]
        self.held.setRowCount(len(by_owner))
        for r, (o, (n, b)) in enumerate(sorted(by_owner.items())):
            for c, text in enumerate((core.fingerprint(o), str(n), human(b))):
                self.held.setItem(r, c, QTableWidgetItem(text))
        self.kit_fp.setText(tr("Your vault key: {}").format(core.fingerprint(self.vault().pub)))
        if k is None:
            self.status.setText(tr("No team room is open: snapshots are kept on this computer and wait to be "
                                   "shared. Open or join a room, or save the shards to folders."))
        elif not self.status.text():
            self.status.setText(tr("{} computers in the room keep shards.").format(
                sum(1 for v in k.live_members().values() if v.get("hold"))))
        dropping = {}
        try:
            dropping = k.dropping() if k is not None else {}
        except Exception:  # noqa: BLE001 - only a warning
            pass
        self.b_keep.setVisible(bool(dropping))
        if dropping:
            self.status.setText(tr("{} of your packs will be deleted by the members on {}. If you did not remove "
                                   "those snapshots, press Cancel Deletion.").format(
                len(dropping), time.strftime("%Y-%m-%d", time.localtime(min(dropping.values())))))

    def cancel_deletion(self):
        k = net.current()
        if k is None:
            return
        packs = list(k.dropping())
        if packs:
            k.keep(packs)
            self.status.setText(tr("The members were asked to keep {} packs.").format(len(packs)))

    def _save_keep(self):
        net.save_settings(self.hold.isChecked(), self.quota.value() * 1024)

    def selected(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self.rows):
            return None
        return self.rows[r]

    # ---- work in a thread ---------------------------------------------------------------------------------------
    def _run(self, fn, done):
        if self.job is not None and not self.job.done:
            return
        self.job = _Job(fn)
        self._done = done
        self.bar.setRange(0, 0)
        self.bar.show()
        for b in (self.b_new, self.b_restore, self.b_repair, self.b_export):
            b.setEnabled(False)
        self.timer.start()

    def _poll(self):
        j = self.job
        if j is None:
            return
        a, b = j.progress
        if b:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * a / b))
        if not j.done:
            return
        self.timer.stop()
        self.bar.hide()
        for btn in (self.b_new, self.b_restore, self.b_repair, self.b_export):
            btn.setEnabled(True)
        if j.error is not None:
            self.status.setText(tr("Failed: {}").format(j.error))
        else:
            self.status.setText(self._done(j.result))
        self.refresh()

    # ---- actions ------------------------------------------------------------------------------------------------
    def new_snapshot(self):
        d = SnapshotDialog(self)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        v = d.values()
        from . import snapshot

        def fn(job):
            return snapshot(v["paths"], v["label"], v["k"], v["n"], v["password"],
                            progress=lambda a, b: setattr(job, "progress", (a, b)))

        def done(r):
            ratio = "∞" if r["dedup"] == float("inf") else f"{r['dedup']:.1f}×"
            text = tr("Snapshot taken: {} files, {}; new data {} (deduplication {}). {} shards of {}, any {} "
                      "restore it.").format(r["files"], human(r["total"]), human(r["new"]), ratio, r["n"],
                                            human(r["shard"]), r["k"])
            p = r.get("placement")
            if p and p.get("waiting"):
                text += " " + tr("{} shards wait: {}").format(p["waiting"], tr(p.get("why") or ""))
            return text
        self._run(fn, done)

    def restore(self):
        s = self.selected()
        dest = QFileDialog.getExistingDirectory(self, tr("Restore into folder"))
        if not dest:
            return
        pw, ok = QInputDialog.getText(self, tr("Restore"), tr("Vault password (leave empty if none):"),
                                      QLineEdit.EchoMode.Password)
        if not ok:
            return
        from . import restore

        sid = s["id"] if s else None
        self._run(lambda job: restore(dest, sid, pw or None),
                  lambda r: tr("Restored {} files ({}) from {}.").format(r["files"], human(r["bytes"]),
                                                                        tr(r.get("from", ""))))

    def restore_folders(self):
        folders = []
        while True:
            d = QFileDialog.getExistingDirectory(self, tr("A folder with shards ({} chosen)").format(len(folders)))
            if not d:
                break
            folders.append(d)
        if not folders:
            return
        dest = QFileDialog.getExistingDirectory(self, tr("Restore into folder"))
        if not dest:
            return
        pw, ok = QInputDialog.getText(self, tr("Restore"), tr("Vault password (leave empty if none):"),
                                      QLineEdit.EchoMode.Password)
        if not ok:
            return
        from . import restore

        self._run(lambda job: restore(dest, None, pw or None, folders=folders),
                  lambda r: tr("Restored {} files ({}) from {}.").format(r["files"], human(r["bytes"]),
                                                                        tr(r.get("from", ""))))

    def repair(self):
        from . import repair

        def done(r):
            if r["short"]:
                return tr("Repaired {} shards; {} snapshots have fewer shards than needed in the room right now.").format(
                    r["repaired"], len(r["short"]))
            return tr("Repaired {} shards.").format(r["repaired"]) if r["repaired"] else tr("Nothing to repair.")
        self._run(lambda job: repair(), done)

    def export(self):
        s = self.selected()
        if s is None:
            QMessageBox.information(self, self.windowTitle(), tr("Choose a snapshot first."))
            return
        v = self.vault()
        packs = v.packs_of(s["id"])
        n = (v.pack_info(packs[0]) or {}).get("n", core.DEFAULT_N)
        folders = []
        while len(folders) < n:
            d = QFileDialog.getExistingDirectory(self, tr("Folder {} of {} (a USB stick each is best)").format(
                len(folders) + 1, n))
            if not d:
                break
            folders.append(d)
        if not folders:
            return
        k = net.current()

        def fn(job):
            if k is not None:
                found = k.gather(packs, need={p: (v.pack_info(p) or {}).get("k", 1) for p in packs}, timeout=60)
            else:
                found, _ = core.scan_folders([os.path.join(v.root, "own", "out")])
            files = {}
            for p in packs:
                got = found.get(p, {})
                info = v.pack_info(p)
                if not info or len(got) < info["k"]:
                    raise core.VaultError(tr("not enough shards of this snapshot are at hand"))
                lost = [i for i in range(info["n"]) if i not in got]
                blobs = {i: core.pack_shard(e[0], e[3], i, e[1], e[2]) for i, e in got.items()}
                blobs.update(core.rebuild_shards(got, lost) if lost else {})
                files[p] = blobs
            return v.export_folders(s["id"], folders, files)
        self._run(fn, lambda c: tr("{} shard files written to {} folders.").format(c, len(folders)))

    def remove(self):
        s = self.selected()
        if s is None:
            QMessageBox.information(self, self.windowTitle(), tr("Choose a snapshot first."))
            return
        if QMessageBox.question(self, tr("Remove Snapshot"), tr("Remove the snapshot «{}»? Its shards are deleted from "
                                                                "the team unless a later snapshot still needs them.")
                                .format(s.get("label") or s["id"][:8])) != QMessageBox.StandardButton.Yes:
            return
        from . import remove

        try:
            gone = remove(s["id"])
        except core.VaultError as e:
            self.status.setText(tr("Failed: {}").format(e))
            return
        self.status.setText(tr("Snapshot removed; {} of its packs withdrawn from the team.").format(len(gone)))
        self.refresh()

    def save_kit(self):
        if QMessageBox.question(self, tr("Recovery Kit"), tr("Anyone with this kit can ask the team for your shards "
                                                             "(and, without a vault password, open them). Save it?")) \
                != QMessageBox.StandardButton.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save Recovery Kit"), "biruni-vault-kit.txt", "*.txt")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(tr("Biruni team vault - recovery kit") + "\n" + core.fingerprint(self.vault().pub) + "\n\n"
                    + core.kit_text(self.vault().seed) + "\n")
        self.status.setText(tr("Recovery kit saved."))

    def use_kit(self):
        text, ok = QInputDialog.getMultiLineText(self, tr("Use a Recovery Kit"), tr("Paste the kit (BVK1…):"))
        if not ok or not text.strip():
            return
        line = next((x.strip() for x in text.splitlines() if x.strip().startswith(core.KIT)), text.strip())
        try:
            seed = core.read_kit(line)
        except core.VaultError as e:
            QMessageBox.warning(self, tr("Use a Recovery Kit"), tr(str(e)))
            return
        from qgis.core import QgsSettings

        from ..field_photos.tunnel import protect

        QgsSettings().setValue("biruni/team_vault/kit_seed", protect(seed.hex()))
        k = net.current()
        if k is not None:
            k.vault.seed = seed
            k.vault.pub = core._crypto().public_key(seed).hex()
        self.status.setText(tr("This computer now uses the vault key {}. Restore asks the team for its "
                               "shards.").format(core.fingerprint(core._crypto().public_key(seed).hex())))
        self.refresh()


def open_dialog(plugin=None, show=True):
    parent = plugin.iface.mainWindow() if plugin is not None and getattr(plugin, "iface", None) else None
    d = VaultDialog(plugin, parent)
    if show:
        d.exec()
    return d
