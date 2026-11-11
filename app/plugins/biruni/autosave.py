"""Automatic recovery copies of the project.

Every few minutes (setting, default 5; 0 = off), when the project has unsaved
changes, a *recovery copy* is written - never the project file itself:

* a project folder gets ``<Name>.autosave.qgz`` next to ``<Name>.qgz`` (same
  folder, so relative paths to the data stay valid), hidden on Windows;
* a temporary map (not saved yet) goes to ``%APPDATA%/Biruni GIS/autosave``.

Temporary (memory) layers are not stored in a project file, so their features
are copied into ``<Name>.autosave.memory.gpkg`` and put back when the copy is
restored.

The copy is written with the project's signals blocked: no "project saved"
reaction runs, so no history version is made, the activity log and the project
folder settings are untouched, and the project stays marked as unsaved.

Edits that are not committed live in the layers' edit buffers, not in the
project file. They are never committed silently: after a while a message
reminds that edits are waiting, with a button to save them. A copy of those
buffers is kept on disk by the edit journal (``edit_journal.py``), so after a
crash or a power cut one message offers both the project copy and the unsaved
edits.

A normal save, or closing the project the normal way (QGIS asks to save
first), removes the copies written in this session; one that is still there -
and newer than the project - was left by a crash or a power cut, and is
offered back when the project is opened (or at start-up for a temporary map).
Restoring keeps the project file first in ``.biruni/before-restore`` (the last
10) and, when the folder has its ``<Name>.gpkg``, a history version too; when
the file cannot be kept, nothing is restored.

The copies are hidden files inside the project folder; packaging a project can
skip ``*.autosave.*``.
"""
import datetime as dt
import json
import os
import shutil

from qgis.core import (Qgis, QgsMessageLog, QgsProject, QgsSettings, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtWidgets import QPushButton

from .i18n import tr
from .version import APP_NAME

MINUTES_KEY = "biruni/autosave/minutes"
REMINDER_KEY = "biruni/autosave/edit-reminder-minutes"
DEFAULT_MINUTES = 5
DEFAULT_REMINDER = 15
SUFFIX = ".autosave.qgz"
TICK_MS = 30_000
NEWER_BY = 2.0  # seconds: a copy must be newer than the project to be offered


def minutes():
    return max(0, QgsSettings().value(MINUTES_KEY, DEFAULT_MINUTES, type=int))


def set_minutes(n):
    QgsSettings().setValue(MINUTES_KEY, max(0, int(n)))


def reminder_minutes():
    return max(0, QgsSettings().value(REMINDER_KEY, DEFAULT_REMINDER, type=int))


def set_reminder_minutes(n):
    QgsSettings().setValue(REMINDER_KEY, max(0, int(n)))


CHOICES = [0, 2, 5, 10, 15, 30]


def fill_menu(menu):
    """Menu entries to choose how often the recovery copy is written."""
    from qgis.PyQt.QtGui import QActionGroup

    group = QActionGroup(menu)
    current = minutes()
    for n in CHOICES:
        act = menu.addAction(tr("Off") if n == 0 else tr("Every {n} minutes").format(n=n))
        act.setCheckable(True)
        act.setChecked(n == current)
        act.triggered.connect(lambda _=False, n=n: set_minutes(n))
        group.addAction(act)


def temporary_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Biruni GIS", "autosave")


def recovery_path(project_file, temp_dir=None):
    """Where the recovery copy of a project goes (a temporary map when ``project_file`` is empty)."""
    if project_file:
        folder = os.path.dirname(project_file)
        base = os.path.splitext(os.path.basename(project_file))[0]
        return os.path.join(folder, base + SUFFIX)
    return os.path.join(temp_dir or temporary_dir(), "temporary" + SUFFIX)


def _sidecars(path):
    # not "<stem>.gpkg": that name would make the copy look like a project folder of its own
    stem = path[:-len(".qgz")]
    return stem + ".memory.gpkg", stem + ".json"


def check_recovery(project_file, temp_dir=None):
    """The recovery copy left for this project by a crash, or None.

    For a project file the copy must be newer than the file; for a temporary map
    (``project_file`` empty) any copy counts."""
    rec = recovery_path(project_file, temp_dir)
    if not os.path.exists(rec):
        return None
    if project_file and os.path.exists(project_file) and \
            os.path.getmtime(rec) <= os.path.getmtime(project_file) + NEWER_BY:
        return None
    return rec


def discard(rec):
    for p in (rec,) + _sidecars(rec):
        try:
            os.remove(p)
        except OSError:
            pass


KEEP_BEFORE_RESTORE = 10   # copies of the project file kept from before a restore


def keep_before_restore(project_file):
    """Copy the project file to ``<folder>/.biruni/before-restore/<Name>-<time>.qgz`` before a restore
    replaces it (the last 10 are kept). Returns the copy's path; raises OSError when it cannot be made."""
    folder = os.path.join(os.path.dirname(project_file), ".biruni", "before-restore")
    os.makedirs(folder, exist_ok=True)
    name, ext = os.path.splitext(os.path.basename(project_file))
    dest = os.path.join(folder, f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}{ext}")
    shutil.copy2(project_file, dest)
    old = sorted((p for p in os.listdir(folder) if p.startswith(name + "-") and p.endswith(ext)), reverse=True)
    for p in old[KEEP_BEFORE_RESTORE:]:
        try:
            os.remove(os.path.join(folder, p))
        except OSError:
            pass
    return dest


def _hide(path):
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)
    except Exception:
        pass


def _memory_layers(prj):
    return [lyr for lyr in prj.mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.providerType() == "memory"]


def write_recovery(prj=None, temp_dir=None):
    """Write the recovery copy of ``prj`` without touching the project file, its history or its unsaved state.

    Returns the path written, or None."""
    prj = prj or QgsProject.instance()
    original = prj.fileName()
    rec = recovery_path(original, temp_dir)
    os.makedirs(os.path.dirname(rec), exist_ok=True)
    tmp = rec[:-len(".qgz")] + ".writing.qgz"
    was_dirty = prj.isDirty()
    blocked = prj.blockSignals(True)
    try:
        prj.setFileName(tmp)
        ok = prj.write()
    finally:
        prj.setFileName(original)
        prj.blockSignals(blocked)
    # writing cleared the "unsaved" mark and changing the file name may set it: put it back as it was, since
    # the real project file has not been saved
    prj.setDirty(was_dirty)
    if not ok:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    gpkg, meta = _sidecars(rec)
    tmp_gpkg, tmp_meta = _sidecars(tmp)
    mapping = _copy_memory_layers(prj, tmp_gpkg)
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump({"time": dt.datetime.now().isoformat(timespec="seconds"), "project": original,
                   "memory_layers": mapping}, f, ensure_ascii=False, indent=1)
    for src, dst in ((tmp, rec), (tmp_gpkg, gpkg), (tmp_meta, meta)):
        try:
            if os.path.exists(src):
                os.replace(src, dst)
            elif os.path.exists(dst):
                os.remove(dst)
        except OSError:  # e.g. the previous copy is open somewhere: keep what is there, drop the new one
            if dst == rec:
                raise
            try:
                os.remove(src)
            except OSError:
                pass
    for p in (rec, gpkg, meta):
        if os.path.exists(p):
            _hide(p)
    return rec


def _copy_memory_layers(prj, gpkg):
    """Copy the features of temporary layers into ``gpkg``; layer id -> table."""
    mapping = {}
    if os.path.exists(gpkg):
        os.remove(gpkg)
    for k, lyr in enumerate(_memory_layers(prj)):
        table = f"layer_{k + 1}"
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = table
        opts.fileEncoding = "UTF-8"
        opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                     if os.path.exists(gpkg)
                                     else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
        err, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, gpkg, prj.transformContext(), opts)
        if err == QgsVectorFileWriter.WriterError.NoError:
            mapping[lyr.id()] = table
    return mapping


def restore_memory_layers(rec, prj=None):
    """After a recovery copy was opened: point its temporary layers at the features kept beside it."""
    prj = prj or QgsProject.instance()
    gpkg, meta = _sidecars(rec)
    if not os.path.exists(meta) or not os.path.exists(gpkg):
        return 0
    with open(meta, encoding="utf-8") as f:
        mapping = json.load(f).get("memory_layers", {})
    n = 0
    for lid, table in mapping.items():
        lyr = prj.mapLayer(lid)
        if not isinstance(lyr, QgsVectorLayer):
            continue
        src = QgsVectorLayer(f"{gpkg}|layername={table}", "recovered", "ogr")
        if not src.isValid():
            continue
        lyr.dataProvider().truncate()
        lyr.dataProvider().addFeatures(list(src.getFeatures()))
        lyr.updateExtents()
        lyr.triggerRepaint()
        n += 1
    return n


class AutoSaver(QObject):
    """Timer-driven recovery copies and the reminder for uncommitted edits.

    ``clock`` returns the current ``datetime``; tests pass a fake one and call ``tick``."""

    def __init__(self, plugin=None, clock=None, temp_dir=None):
        super().__init__()
        self.plugin = plugin
        self.iface = getattr(plugin, "iface", None)
        self.clock = clock or dt.datetime.now
        self.temp_dir = temp_dir
        self.last_save = None
        self.last_signature = None
        self._last_error = None  # the last reason a copy could not be written (logged once)
        self.edit_since = {}     # layer id -> when its edits started waiting
        self.reminded = {}       # layer id -> when we last reminded
        self.copies = set()      # recovery copies written in this session
        self._restoring = False
        self.journal = None      # the edit journal (unsaved layer edits), started by install()
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        self._installed = False

    # ---- lifecycle --------------------------------------------------------
    def install(self):
        prj = QgsProject.instance()
        prj.projectSaved.connect(self._on_saved)
        prj.readProject.connect(self._on_read)
        prj.cleared.connect(self._on_cleared)
        self.timer.start()
        self._installed = True
        from .edit_journal import EditJournal

        self.journal = EditJournal(self.temp_dir).install()
        # a temporary map lost in a crash is offered at start-up
        QTimer.singleShot(4000, lambda: self.offer_recovery(""))

    def uninstall(self):
        self.timer.stop()
        if self.journal is not None:
            self.journal.uninstall()
            self.journal = None
        prj = QgsProject.instance()
        for sig, slot in ((prj.projectSaved, self._on_saved), (prj.readProject, self._on_read),
                          (prj.cleared, self._on_cleared)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        self._installed = False

    # ---- the clock ----------------------------------------------------------
    def _signature(self, prj):
        """Changes that do not mark the project dirty: features of temporary layers."""
        return tuple((lyr.id(), lyr.featureCount(), lyr.extent().toString(3)) for lyr in _memory_layers(prj))

    def _busy(self):
        hist = getattr(self.plugin, "history", None)
        return bool(getattr(hist, "_restoring", False))

    def tick(self):
        now = self.clock()
        prj = QgsProject.instance()
        written = None
        every = minutes()
        if every and not self._busy():
            sig = self._signature(prj)
            changed = prj.isDirty() or (self.last_signature is not None and sig != self.last_signature)
            due = self.last_save is None or now - self.last_save >= dt.timedelta(minutes=every)
            if self.last_save is None and self.last_signature is None:
                self.last_save = now  # start counting from the first tick
                due = False
            if changed and due and (prj.count() or prj.fileName()):
                try:
                    written = write_recovery(prj, self.temp_dir)
                except Exception as e:  # noqa: BLE001 - e.g. the last copy is locked: try again next time
                    written = None
                    if str(e) != self._last_error:
                        self._last_error = str(e)
                        QgsMessageLog.logMessage(f"automatic copy not written: {e!r}", APP_NAME, Qgis.MessageLevel.Warning)
                else:
                    self._last_error = None
                self.last_save = now
                if written:
                    self.copies.add(written)
            self.last_signature = sig
        self._remind_edits(now, prj)
        return written

    def _remind_edits(self, now, prj):
        wait = reminder_minutes()
        editing = {lyr.id(): lyr for lyr in prj.mapLayers().values()
                   if isinstance(lyr, QgsVectorLayer) and lyr.isEditable() and lyr.isModified()}
        for lid in list(self.edit_since):
            if lid not in editing:
                self.edit_since.pop(lid, None)
                self.reminded.pop(lid, None)
        due = []
        for lid, lyr in editing.items():
            since = self.edit_since.setdefault(lid, now)
            last = self.reminded.get(lid, since)
            if wait and now - last >= dt.timedelta(minutes=wait):
                due.append(lyr)
                self.reminded[lid] = now
        if due:
            self.remind(due)
        return due

    def remind(self, layers):
        if self.iface is None:
            return
        bar = self.iface.messageBar()
        names = ", ".join(lyr.name() for lyr in layers[:3]) + ("…" if len(layers) > 3 else "")
        item = bar.createMessage(APP_NAME, tr("Edits in {names} have not been saved for {n} minutes.").format(
            names=names, n=reminder_minutes()))
        btn = QPushButton(tr("Save edits"))

        def save():
            for lyr in layers:
                if lyr.isEditable() and lyr.isModified():
                    lyr.commitChanges(stopEditing=False)
            bar.popWidget(item)

        btn.clicked.connect(save)
        item.layout().addWidget(btn)
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)

    # ---- project events -------------------------------------------------------
    def _forget_copies(self):
        """Remove the copies this session wrote (copies left by an earlier crash are kept until answered)."""
        for rec in self.copies:
            discard(rec)
        self.copies.clear()

    def _on_saved(self):
        """A real save: the copies of this session are out of date (that includes the temporary map's copy
        when a temporary map has just been saved as a project folder)."""
        self._forget_copies()
        self.last_save = self.clock()

    def _on_cleared(self):
        """The project was closed the normal way - QGIS asked to save it first - so nothing is left to recover.
        (A crash or a power cut never gets here, and the copy stays.)"""
        if self._restoring:
            return
        self._forget_copies()
        self.last_save = None
        self.last_signature = None
        self.edit_since.clear()
        self.reminded.clear()

    def _on_read(self, *args):
        path = QgsProject.instance().fileName()
        if path and not path.endswith(SUFFIX):
            QTimer.singleShot(500, lambda: self.offer_recovery(path))

    # ---- recovery -------------------------------------------------------------
    def offer_recovery(self, project_file):
        """One message for everything a crash or a power cut left: the automatic project copy and/or the
        unsaved layer edits of the edit journal."""
        from . import edit_journal as ej

        rec = check_recovery(project_file, self.temp_dir)
        jpath = ej.check_journal(project_file, self.temp_dir) if ej.enabled() else None
        if (rec is None and jpath is None) or self.iface is None:
            return rec or jpath
        entries = ej.read_journal(jpath) if jpath else []
        n, added, changed, deleted = ej.summary(entries)
        parts = []
        if rec is not None:
            when = dt.datetime.fromtimestamp(os.path.getmtime(rec)).strftime("%Y-%m-%d %H:%M")
            parts.append(tr("Restore the copy that was saved automatically at {t}? The project was not closed "
                            "properly.").format(t=when))
        if entries:
            parts.append(tr("Biruni closed suddenly - edits that were not saved were found on {n} layer(s) "
                            "({a} new, {c} changed, {d} deleted features). Restore them?").format(
                n=n, a=added, c=changed, d=deleted))
        bar = self.iface.messageBar()
        item = bar.createMessage(APP_NAME, " ".join(parts))
        yes = QPushButton(tr("Restore"))
        more = QPushButton(tr("Show details"))
        no = QPushButton(tr("Discard"))

        def restore_all():
            bar.popWidget(item)
            self.restore_all(rec, jpath, project_file)

        def drop_all():
            bar.popWidget(item)
            if rec is not None:
                discard(rec)
            if jpath is not None:
                ej.discard_left(project_file, self.temp_dir)

        def show_details():
            from qgis.PyQt.QtWidgets import QMessageBox

            text = ej.details_text(entries) if entries else ""
            if rec is not None:
                text = tr("Automatic project copy: {p}").format(p=rec) + ("\n\n" + text if text else "")
            QMessageBox.information(self.iface.mainWindow(), APP_NAME, text)

        yes.clicked.connect(restore_all)
        no.clicked.connect(drop_all)
        more.clicked.connect(show_details)
        for b in (yes, more, no):
            item.layout().addWidget(b)
        bar.pushWidget(item, Qgis.MessageLevel.Warning, 0)
        return rec or jpath

    def restore_all(self, rec, jpath, project_file):
        """The project copy first (it brings back the layers the edits belong to), then the unsaved edits."""
        from . import edit_journal as ej

        ok = True
        if rec is not None:
            ok = self.restore(rec, project_file)
        if jpath is None or not ok:
            return ok
        report = ej.restore(jpath, QgsProject.instance())
        ej.discard_left(project_file, self.temp_dir)
        if self.journal is not None:   # the restored buffers are journaled again by this session
            for lid in list(QgsProject.instance().mapLayers()):
                lyr = QgsProject.instance().mapLayer(lid)
                if ej.journaled(lyr) and lyr.isEditable():
                    self.journal._touch(lyr, full=True)
        if self.iface is not None:
            self.iface.messageBar().pushSuccess(
                APP_NAME, tr("Unsaved edits restored - check them, then save the layers.") + "\n" +
                ej.report_text(report))
        return report

    def restore(self, rec, project_file):
        """Put the recovery copy back.

        A project folder first keeps its current state as a history version, then the copy replaces the
        project file and the project is opened from its own path (so everything that follows the project -
        history, activity, folder defaults - sees the right file). A temporary map is opened from the copy and
        becomes an unsaved temporary map again."""
        prj = QgsProject.instance()
        if project_file and os.path.exists(project_file):
            # the plain file first: the history version below needs a <Name>.gpkg and may not be made
            try:
                keep_before_restore(project_file)
            except OSError as e:
                if self.iface is not None:
                    self.iface.messageBar().pushWarning(
                        APP_NAME, tr("The automatic copy was not restored: the project could not be backed up "
                                     "first ({e}).").format(e=e))
                return False
        if project_file:
            folder = os.path.dirname(project_file)
            gpkg = os.path.join(folder, os.path.splitext(os.path.basename(project_file))[0] + ".gpkg")
            if os.path.exists(gpkg) and os.path.exists(project_file):
                try:
                    from .history import make_version

                    make_version(folder, project_file, gpkg, note=tr("Before restoring the automatic copy"),
                                 manual=True)
                except Exception:
                    pass
        self._restoring = True
        try:
            prj.setDirty(False)
            if project_file:
                shutil.copy2(rec, project_file)
                ok = prj.read(project_file)
            else:
                ok = prj.read(rec)
                if ok:
                    prj.setFileName("")
        finally:
            self._restoring = False
        if ok:
            restore_memory_layers(rec, prj)
            prj.setDirty(True)  # temporary layers and edits since the last save are only in memory
            discard(rec)
            if self.iface is not None:
                self.iface.messageBar().pushSuccess(APP_NAME, tr("The automatic copy was restored. Save the "
                                                                 "project to keep it."))
        return ok
