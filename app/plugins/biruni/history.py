"""Project history ("time machine").

Every save of a project folder keeps a version of the project file and the
project database in <project>/.history/<timestamp>/, so the project can be put
back the way it was an hour, a day or a week ago. Files in data/ are not
versioned (they are inputs and can be large).

Retention: every version of the last 24 hours (at most one per 10 minutes
unless saved by hand), then one per day for 30 days, then one per week.
Restoring first keeps the current state as a version, and never rolls back
the activity log.
"""
import datetime as dt
import json
import os
import shutil
import sqlite3

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import QObject, Qt
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QInputDialog,
                                 QLabel, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .i18n import tr
from .project import project_folder, project_gpkg
from .version import APP_NAME

FOLDER = ".history"
ENABLED_KEY = "biruni/history/enabled"
MIN_GAP = dt.timedelta(minutes=10)
MAX_AUTO_BYTES = 1_500_000_000   # bigger databases are only versioned by hand
STAMP = "%Y%m%d-%H%M%S"
ACTIVITY_TABLES = ("biruni_activity", "biruni_sessions", "biruni_active_time")


def history_dir(folder):
    return os.path.join(folder, FOLDER)


def sqlite_copy(src, dst):
    """Consistent copy of an SQLite/GeoPackage file even while it is open."""
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(dst, timeout=30)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _tables(gpkg):
    try:
        con = sqlite3.connect(gpkg, timeout=10)
        try:
            return [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents ORDER BY table_name")]
        finally:
            con.close()
    except sqlite3.Error:
        return []


def make_version(folder, qgz, gpkg, note="", manual=False, now=None):
    now = now or dt.datetime.now()
    root = history_dir(folder)
    os.makedirs(root, exist_ok=True)
    try:  # hide the folder from casual browsing on Windows
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(root, 0x02)
    except Exception:
        pass
    target = os.path.join(root, now.strftime(STAMP))
    k = 1
    while os.path.exists(target):
        target = os.path.join(root, now.strftime(STAMP) + f"-{k}")
        k += 1
    os.makedirs(target)
    shutil.copy2(qgz, os.path.join(target, os.path.basename(qgz)))
    if gpkg and os.path.exists(gpkg):
        sqlite_copy(gpkg, os.path.join(target, os.path.basename(gpkg)))
    info = {"time": now.isoformat(timespec="seconds"), "note": note, "manual": manual,
            "user": os.environ.get("USERNAME") or "", "tables": _tables(gpkg) if gpkg else []}
    with open(os.path.join(target, "version.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return target


def list_versions(folder):
    root = history_dir(folder)
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root), reverse=True):
        path = os.path.join(root, name)
        meta = os.path.join(path, "version.json")
        if not os.path.exists(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
        info.update(path=path, size=size, when=dt.datetime.fromisoformat(info["time"]))
        out.append(info)
    return out


def prune(folder, now=None):
    """Apply the retention rules; versions saved by hand are kept for 90 days."""
    now = now or dt.datetime.now()
    keep_days, keep_weeks = set(), set()
    removed = 0
    for v in list_versions(folder):  # newest first
        age = now - v["when"]
        if age <= dt.timedelta(hours=24):
            continue
        if v.get("manual") and age <= dt.timedelta(days=90):
            continue
        if age <= dt.timedelta(days=30):
            key = v["when"].date()
            if key not in keep_days:
                keep_days.add(key)
                continue
        else:
            key = v["when"].isocalendar()[:2]
            if key not in keep_weeks:
                keep_weeks.add(key)
                continue
        shutil.rmtree(v["path"], ignore_errors=True)
        removed += 1
    return removed


def restore_files(version_path, qgz, gpkg, keep_activity_from=None):
    """Put a version back in place. The activity tables of ``keep_activity_from``
    (the state before restoring) are copied over the restored database."""
    src_qgz = os.path.join(version_path, os.path.basename(qgz))
    src_gpkg = os.path.join(version_path, os.path.basename(gpkg)) if gpkg else None
    if src_gpkg and os.path.exists(src_gpkg):
        sqlite_copy(src_gpkg, gpkg)
        if keep_activity_from and os.path.exists(keep_activity_from):
            con = sqlite3.connect(gpkg, timeout=30)
            try:
                con.execute("ATTACH DATABASE ? AS cur", (keep_activity_from,))
                have = {r[0] for r in con.execute("SELECT name FROM cur.sqlite_master WHERE type='table'")}
                with con:
                    for t in ACTIVITY_TABLES:
                        if t in have:
                            con.execute(f"DROP TABLE IF EXISTS main.{t}")
                            sql = con.execute("SELECT sql FROM cur.sqlite_master WHERE name=?", (t,)).fetchone()[0]
                            con.execute(sql)
                            con.execute(f"INSERT INTO main.{t} SELECT * FROM cur.{t}")
                con.execute("DETACH DATABASE cur")
            finally:
                con.close()
    shutil.copy2(src_qgz, qgz)


class ProjectHistory(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self._last = None
        self._restoring = False

    def install(self):
        QgsProject.instance().projectSaved.connect(self._on_saved)

    def unload(self):
        try:
            QgsProject.instance().projectSaved.disconnect(self._on_saved)
        except TypeError:
            pass

    @staticmethod
    def enabled():
        return QgsSettings().value(ENABLED_KEY, True, type=bool)

    def _paths(self):
        folder = project_folder()
        if not folder:
            return None, None, None
        return folder, QgsProject.instance().fileName(), project_gpkg()

    def _on_saved(self):
        if self._restoring or not self.enabled():
            return
        folder, qgz, gpkg = self._paths()
        if not folder:
            return
        now = dt.datetime.now()
        if self._last and self._last[0] == folder and now - self._last[1] < MIN_GAP:
            return
        if gpkg and os.path.exists(gpkg) and os.path.getsize(gpkg) > MAX_AUTO_BYTES:
            return
        try:
            make_version(folder, qgz, gpkg, now=now)
            prune(folder, now)
            self._last = (folder, now)
        except (OSError, sqlite3.Error) as e:
            self.iface.messageBar().pushWarning(APP_NAME, tr("Could not keep a version of the project: {e}").format(
                e=e))

    def save_version(self, note=""):
        folder, qgz, gpkg = self._paths()
        if not folder:
            return None
        if QgsProject.instance().isDirty():
            QgsProject.instance().write()
        path = make_version(folder, qgz, gpkg, note=note, manual=True)
        self._last = (folder, dt.datetime.now())
        return path

    def restore(self, version_path):
        folder, qgz, gpkg = self._paths()
        if not folder:
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._restoring = True
        try:
            before = make_version(folder, qgz, gpkg, note=tr("Before restoring"), manual=True)
            self.plugin.activity.flush()
            prj = QgsProject.instance()
            prj.setDirty(False)
            prj.clear()
            QApplication.processEvents()
            restore_files(version_path, qgz, gpkg,
                          keep_activity_from=os.path.join(before, os.path.basename(gpkg)) if gpkg else None)
            self.iface.addProject(qgz)
            self.plugin.activity.log("project", "restored", os.path.basename(version_path))
            self.plugin.activity.flush()
        except (OSError, sqlite3.Error) as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self.iface.mainWindow(), APP_NAME, tr("The version could not be restored: {e}").format(
                e=e))
            return False
        finally:
            self._restoring = False
            QApplication.restoreOverrideCursor()
        self.iface.messageBar().pushSuccess(APP_NAME, tr("The project was restored. The state before restoring "
                                                         "was kept as a version."))
        return True

    def show_dialog(self):
        if not project_folder():
            if not self.plugin.project._require_folder():
                return
        HistoryDialog(self).exec()


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class HistoryDialog(QDialog):
    def __init__(self, history):
        super().__init__(history.iface.mainWindow())
        self.history = history
        self.setWindowTitle(tr("Project History"))
        self.resize(640, 460)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        intro = QLabel(tr("Biruni GIS keeps a version of the project every time it is saved: the map and the "
                          "project database. Pick a version to put the project back the way it was."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Saved"), tr("Note"), tr("Layers"), tr("Size")])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._sel)
        v.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.keep = QPushButton(tr("Save a version now…"))
        self.keep.clicked.connect(self._save_now)
        self.restore_btn = QPushButton(tr("Restore this version"))
        self.restore_btn.clicked.connect(self._restore)
        self.delete_btn = QPushButton(tr("Delete"))
        self.delete_btn.clicked.connect(self._delete)
        row.addWidget(self.keep)
        row.addStretch(1)
        row.addWidget(self.delete_btn)
        row.addWidget(self.restore_btn)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._fill()

    def _fill(self):
        self.tree.clear()
        folder = project_folder()
        today = dt.date.today()
        for info in list_versions(folder) if folder else []:
            w = info["when"]
            when = w.strftime("%H:%M") if w.date() == today else w.strftime("%Y-%m-%d  %H:%M")
            note = info.get("note") or (tr("Saved by hand") if info.get("manual") else tr("Automatic"))
            it = QTreeWidgetItem([when, note, str(len(info.get("tables", []))), _human_size(info["size"])])
            it.setData(0, Qt.ItemDataRole.UserRole, info["path"])
            self.tree.addTopLevelItem(it)
        for c in (0, 2, 3):
            self.tree.resizeColumnToContents(c)
        self._sel()

    def _sel(self):
        has = bool(self.tree.selectedItems())
        self.restore_btn.setEnabled(has)
        self.delete_btn.setEnabled(has)

    def _current(self):
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    def _save_now(self):
        note, ok = QInputDialog.getText(self, tr("Save a version now…"), tr("Note (optional)"))
        if ok:
            self.history.save_version(note.strip())
            self._fill()

    def _restore(self):
        path = self._current()
        if not path:
            return
        r = QMessageBox.question(self, APP_NAME, tr("Put the project back to this version? The current state is "
                                                    "kept as a version first, so this can be undone."))
        if r == QMessageBox.StandardButton.Yes and self.history.restore(path):
            self.accept()

    def _delete(self):
        path = self._current()
        if path and QMessageBox.question(self, APP_NAME, tr("Delete this version?")) == QMessageBox.StandardButton.Yes:
            shutil.rmtree(path, ignore_errors=True)
            self._fill()
