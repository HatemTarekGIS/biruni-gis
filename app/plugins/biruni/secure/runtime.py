"""The locked safe inside the running program: open a locked database or project, show its layers with a lock
badge and the user's rights, refuse edits that are not allowed, save back into the locked file when edits are
saved, and wipe the memory copies when the layers are closed.

Export, print and copy rights are applied here, by the interface: Biruni disables the matching commands and
clears the clipboard. That is not encryption - said plainly in the dialogs."""
import os

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import QApplication, QFileDialog, QMessageBox

from ..i18n import tr

PROP = "biruni/secure"
_sessions = {}  # normalised path -> {"session", "layers": {layer id: MountedLayer}, "indicators": [...]}
_hooks = {"connected": False, "plugin": None}
PRINT_ACTIONS = ("mActionNewPrintLayout", "mActionShowLayoutManager", "mActionSaveMapAsImage", "mActionSaveMapAsPdf",
                 "mActionNewReport", "mActionPrint")
EXPORT_ACTIONS = ("mActionLayerSaveAs",)
COPY_ACTIONS = ("mActionCopyFeatures", "mActionCutFeatures")


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _mw(plugin):
    return plugin.iface.mainWindow() if plugin is not None else None


def _bar(plugin, text, level="info"):
    if plugin is None:
        return
    bar = plugin.iface.messageBar()
    title = tr("Locked database")
    {"info": bar.pushInfo, "ok": bar.pushSuccess, "warn": bar.pushWarning}.get(level, bar.pushInfo)(title, text)


def any_open():
    """True while a locked database or project is open (other tools - autosave, export - can check it)."""
    from . import project_lock

    return bool(_sessions) or bool(project_lock._open)


def session_of(layer):
    path = layer.customProperty(PROP, "") if layer is not None else ""
    entry = _sessions.get(_key(path)) if path else None
    return entry["session"] if entry else None


def allowed(layer, what):
    """May the user do 'what' (view, add, edit, delete, export, print, copy) with this layer? Always True for
    layers that are not from a locked database."""
    s = session_of(layer)
    if s is None:
        return True
    if what in ("export", "print", "copy"):
        return bool(s.can.get(what))
    lid = layer.customProperty(PROP + "_lid", "")
    letter = {"view": "v", "add": "a", "edit": "e", "delete": "d"}.get(what, "?")
    return letter in s.rights.get(lid, "")


def rights_text(rights):
    words = [w for k, w in (("v", tr("view")), ("a", tr("add")), ("e", tr("edit")), ("d", tr("delete")))
             if k in rights]
    return ", ".join(words) if words else tr("none")


# ---- opening ------------------------------------------------------------------------------------------------------
def open_locked(plugin, path=None):
    """Opens a .bgpkg or .bproj (asks for the file when path is None)."""
    if not path:
        path, _ = QFileDialog.getOpenFileName(_mw(plugin), tr("Open a locked file"), "",
                                              tr("Locked files") + " (*.bgpkg *.bproj)")
        if not path:
            return None
    from . import container, project_lock

    if project_lock.is_locked_project(path):
        return open_project(plugin, path)
    if container.is_locked_file(path):
        return open_database(plugin, path)
    QMessageBox.warning(_mw(plugin), tr("Open a locked file"), tr("This is not a locked file."))
    return None


def open_database(plugin, path):
    from .dialogs import LoginDialog

    entry = _sessions.get(_key(path))
    if entry:
        _bar(plugin, tr("This locked database is already open."))
        return entry["session"]
    dlg = LoginDialog(path, plugin, _mw(plugin))
    if dlg.exec() != dlg.DialogCode.Accepted or dlg.session is None:
        return None
    s = dlg.session
    attach(plugin, s)
    if s.is_admin:
        pending = [env for kind, env, _ in s.lf.records()
                   if kind == b"C" and env and env.get("id") not in s.akeys.get("merged", [])]
        if pending and QMessageBox.question(
                _mw(plugin), tr("Locked database"),
                tr("This file holds {n} change sets from users, not merged yet. Check them against each user's "
                   "signature and rights, and merge the allowed ones now?").format(n=len(pending))) \
                == QMessageBox.StandardButton.Yes:
            manage_users(plugin, merge_own=True)
    return s


def attach(plugin, s):
    """Mounts a session's layers in memory and adds them to the map, in a group named after the file."""
    mounted = s.mount()
    s.log("open")
    entry = {"session": s, "layers": {}, "indicators": []}
    _sessions[_key(s.path)] = entry
    prj = QgsProject.instance()
    root = prj.layerTreeRoot()
    group = root.insertGroup(0, os.path.splitext(os.path.basename(s.path))[0])
    for ml in mounted:
        lyr = QgsVectorLayer(ml.uri, ml.name, "ogr")
        if not lyr.isValid():
            continue
        lyr.setCustomProperty(PROP, s.path)
        lyr.setCustomProperty(PROP + "_lid", ml.lid)
        if not set(ml.rights) & set("aed"):
            lyr.setReadOnly(True)
        prj.addMapLayer(lyr, False)
        group.addLayer(lyr)
        entry["layers"][lyr.id()] = ml
        lyr.editCommandEnded.connect(lambda lyr=lyr: _check_edit(plugin, lyr))
        lyr.afterCommitChanges.connect(lambda lyr=lyr: _after_commit(plugin, lyr))
        _badge(plugin, entry, lyr, ml, s)
    _connect(plugin)
    _apply_policy(plugin)
    if plugin is not None:
        who = tr("administrator") if s.is_admin else s.name
        _bar(plugin, tr("Opened as {who}: {n} layers. The data stays in memory and is encrypted again when you save "
                        "edits.").format(who=who, n=len(entry["layers"])), "ok")
    return entry


def _badge(plugin, entry, lyr, ml, s):
    if plugin is None:
        return
    try:
        from qgis.gui import QgsLayerTreeViewIndicator

        from .widgets import icon

        view = plugin.iface.layerTreeView()
        node = QgsProject.instance().layerTreeRoot().findLayer(lyr.id())
        ind = QgsLayerTreeViewIndicator(view)
        ind.setIcon(icon("lock", plugin))
        tip = tr("Locked layer - your rights: {rights}").format(rights=rights_text(ml.rights))
        if ml.hidden:
            tip += "\n" + tr("{n} fields are hidden from you.").format(n=ml.hidden)
        if not s.is_admin:
            off = [w for k, w in (("export", tr("export")), ("print", tr("print")), ("copy", tr("copy")))
                   if not s.can.get(k)]
            if off:
                tip += "\n" + tr("Not allowed: {what}").format(what=", ".join(off))
        ind.setToolTip(tip)
        view.addIndicator(node, ind)
        entry["indicators"].append((node, ind))
    except Exception:  # noqa: BLE001 - the badge is a convenience
        pass


# ---- editing -----------------------------------------------------------------------------------------------------
def _violation(lyr, rights):
    buf = lyr.editBuffer()
    if buf is None:
        return None
    if buf.addedFeatures() and "a" not in rights:
        return "add"
    if buf.deletedFeatureIds() and "d" not in rights:
        return "delete"
    changed = [fid for fid in list(buf.changedAttributeValues().keys()) + list(buf.changedGeometries().keys())
               if fid >= 0]
    if changed and "e" not in rights:
        return "edit"
    return None


def _check_edit(plugin, lyr):
    s = session_of(lyr)
    if s is None or s.is_admin:
        return
    rights = s.rights.get(lyr.customProperty(PROP + "_lid", ""), "")
    what = _violation(lyr, rights)
    if what is None:
        return
    QTimer.singleShot(0, lambda: lyr.undoStack().undo() if lyr.undoStack().canUndo() else None)
    text = {"add": tr("You may not add features to {layer}. Ask the administrator for this right."),
            "delete": tr("You may not delete features of {layer}. Ask the administrator for this right."),
            "edit": tr("You may not change features of {layer}. Ask the administrator for this right.")}[what]
    _bar(plugin, text.format(layer=lyr.name()), "warn")


def _after_commit(plugin, lyr):
    s = session_of(lyr)
    if s is None:
        return
    from .container import LockError
    from .dialogs import lock_error_text, report_line

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        out = s.save()
    except LockError as e:
        _bar(plugin, lock_error_text(e), "warn")
        return
    finally:
        QApplication.restoreOverrideCursor()
    if s.is_admin:
        if out:
            _bar(plugin, "; ".join(report_line(r) for r in out[:4]))
        _bar(plugin, tr("Saved into the locked database (version {n}).").format(n=s.m.get("version")), "ok")
        return
    for o in out or []:
        _bar(plugin, tr("{layer}: your changes are recorded in the locked file and signed with your key. The "
                        "administrator merges them.").format(layer=o["layer"]), "ok")
        if o["not_allowed"]:
            what = ", ".join({"adds": tr("additions"), "edits": tr("changes"), "deletes": tr("deletions")}[k]
                             for k in o["not_allowed"])
            _bar(plugin, tr("{layer}: the {what} are not within your rights - the administrator's merge will refuse "
                            "them.").format(layer=o["layer"], what=what), "warn")


# ---- export, print, copy (interface level) -------------------------------------------------------------------------
def _restricted(what):
    return any(not e["session"].can.get(what) for e in _sessions.values())


def _apply_policy(plugin):
    if plugin is None:
        return
    from qgis.PyQt.QtGui import QAction

    mw = plugin.iface.mainWindow()
    active = plugin.iface.activeLayer()

    def set_actions(names, enabled):
        for n in names:
            a = mw.findChild(QAction, n)
            if a is not None and not enabled:
                a.setEnabled(False)
                a.setProperty("biruni_secure_off", True)
            elif a is not None and a.property("biruni_secure_off"):
                a.setEnabled(True)
                a.setProperty("biruni_secure_off", False)

    set_actions(PRINT_ACTIONS, not _restricted("print"))
    set_actions(EXPORT_ACTIONS, allowed(active, "export"))
    set_actions(COPY_ACTIONS, allowed(active, "copy"))


def _clipboard_changed(plugin):
    active = plugin.iface.activeLayer() if plugin else None
    if active is not None and not allowed(active, "copy") and QApplication.clipboard().text():
        QApplication.clipboard().clear()
        _bar(plugin, tr("Copying from {layer} is not allowed for you.").format(layer=active.name()), "warn")


def _connect(plugin):
    if _hooks["connected"]:
        return
    _hooks["connected"] = True
    _hooks["plugin"] = plugin
    prj = QgsProject.instance()
    prj.layersRemoved.connect(lambda *_: QTimer.singleShot(0, _sweep))
    prj.cleared.connect(lambda: QTimer.singleShot(0, _sweep))
    if plugin is not None:
        plugin.iface.currentLayerChanged.connect(lambda *_: QTimer.singleShot(0, lambda: _apply_policy(plugin)))
        QApplication.clipboard().dataChanged.connect(lambda: _clipboard_changed(plugin))


def _sweep():
    """Closes (and wipes from memory) the locked files none of whose layers are open any more."""
    from . import project_lock

    prj = QgsProject.instance()
    for k, entry in list(_sessions.items()):
        if not any(prj.mapLayer(lid) is not None for lid in entry["layers"]):
            close_session(k)
    current = prj.fileName()
    for lp in list(project_lock._open.values()):
        if project_lock.uri(lp.path) != current:
            lp.close()
    _apply_policy(_hooks["plugin"])


def close_session(key):
    entry = _sessions.pop(key, None)
    if entry is None:
        return
    prj = QgsProject.instance()
    for lid in list(entry["layers"]):
        if prj.mapLayer(lid) is not None:
            prj.removeMapLayer(lid)
    entry["session"].close()


def close_all():
    from . import project_lock

    for k in list(_sessions):
        close_session(k)
    project_lock.close_all()


def reload_layers(path):
    entry = _sessions.get(_key(path))
    if not entry:
        return
    for lid in entry["layers"]:
        lyr = QgsProject.instance().mapLayer(lid)
        if lyr is not None:
            lyr.reload()
            lyr.triggerRepaint()


# ---- commands --------------------------------------------------------------------------------------------------------
def lock_database(plugin, source=""):
    from .dialogs import LockDatabaseWizard

    if not source and plugin is not None:
        lyr = plugin.iface.activeLayer()
        src = lyr.source().split("|")[0] if lyr is not None else ""
        source = src if src.lower().endswith(".gpkg") and os.path.exists(src) else ""
    w = LockDatabaseWizard(plugin, _mw(plugin), source)
    if w.exec() != w.DialogCode.Accepted or not w.out_path:
        return None
    pw, w.admin_password = w.admin_password, ""
    if w.action in ("open", "users"):
        from .container import LockedFile

        s = LockedFile(w.out_path).unlock(w.admin_name.text(), pw)
        attach(plugin, s)
        if w.action == "users":
            manage_users(plugin)
    return w.out_path


def manage_users(plugin, merge_own=False):
    """Users and permissions of an open locked database (as administrator); asks for one when none is open."""
    from .dialogs import LoginDialog, UsersDialog

    s = next((e["session"] for e in _sessions.values() if e["session"].is_admin), None)
    temporary = False
    if s is None:
        path, _ = QFileDialog.getOpenFileName(_mw(plugin), tr("Users and permissions"), "",
                                              tr("Locked database") + " (*.bgpkg)")
        if not path:
            return
        dlg = LoginDialog(path, plugin, _mw(plugin))
        if dlg.exec() != dlg.DialogCode.Accepted or dlg.session is None:
            return
        s = dlg.session
        temporary = True
        if not s.is_admin:
            s.close()
            QMessageBox.information(_mw(plugin), tr("Users and permissions"),
                                    tr("Only the administrator manages users and permissions."))
            return
    d = UsersDialog(s, plugin, _mw(plugin), on_saved=lambda: reload_layers(s.path))
    if merge_own:
        d.tabs.setCurrentIndex(2)
        d.merge_file(s.path)
    d.exec()
    if temporary:
        s.close()


def open_project(plugin, path):
    from . import project_lock
    from .widgets import ask_password

    while True:
        pw = ask_password(_mw(plugin), tr("Locked project"), os.path.basename(path), plugin)
        if pw is None:
            return None
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                lp = project_lock.LockedProject(path, pw)
            finally:
                QApplication.restoreOverrideCursor()
            break
        except project_lock.ProjectLockError:
            QMessageBox.warning(_mw(plugin), tr("Locked project"), tr("Wrong password."))
    project_lock.register(lp)
    _connect(plugin)
    if plugin is not None:
        plugin.iface.addProject(project_lock.uri(path))
        _bar(plugin, tr("Locked project opened: its data stays in memory. Save as usual - it is encrypted again."),
             "ok")
    return lp


def lock_project(plugin):
    from .dialogs import LockProjectDialog

    prj = QgsProject.instance()
    path = prj.fileName()
    if not path or prj.isDirty() or not path.lower().endswith((".qgz", ".qgs")):
        QMessageBox.information(_mw(plugin), tr("Lock this project with a password"),
                                tr("Save the project first; then lock it."))
        return None
    d = LockProjectDialog(path, plugin, _mw(plugin))
    if d.exec() != d.DialogCode.Accepted:
        return None
    if d.open_after.isChecked() and plugin is not None and plugin.iface.newProject(True):
        from . import project_lock

        lp = project_lock.LockedProject(d.out_path, d.password)
        project_lock.register(lp)
        _connect(plugin)
        plugin.iface.addProject(project_lock.uri(d.out_path))
    d.password = ""
    return d.out_path


def remove_lock(plugin):
    from . import project_lock
    from .dialogs import RemoveLockDialog

    current = project_lock.path_of(QgsProject.instance().fileName()) or ""
    d = RemoveLockDialog(current, plugin, _mw(plugin))
    if d.exec() == d.DialogCode.Accepted and d.result_project:
        if QMessageBox.question(_mw(plugin), tr("Remove the lock"),
                                tr("The project was written to {path}. Open it now?").format(path=d.result_project)) \
                == QMessageBox.StandardButton.Yes:
            plugin.iface.addProject(d.result_project)
