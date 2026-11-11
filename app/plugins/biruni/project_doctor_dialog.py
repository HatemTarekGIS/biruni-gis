"""Project doctor window: overview, broken data paths, where data is used, renaming the project."""
import os

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                                 QMessageBox, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                                 QWidget)

from . import project_doctor as doc
from .i18n import tr
from .version import APP_NAME


def _default_folders():
    out = []
    try:
        from .project import default_location, project_folder

        pf = project_folder()
        prj = QgsProject.instance().fileName()
        for f in (pf, os.path.dirname(prj) if prj else None, default_location(),
                  os.path.join(os.path.expanduser("~"), "Documents"), os.path.join(os.path.expanduser("~"), "Desktop")):
            if f and os.path.isdir(f) and f not in out:
                out.append(f)
    except Exception:  # noqa: BLE001
        pass
    for p in doc.recent_project_paths():
        d = os.path.dirname(os.path.dirname(p))
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out[:8]


class FolderList(QWidget):
    def __init__(self, folders, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMaximumHeight(90)
        self.list.addItems(folders)
        h.addWidget(self.list, 1)
        col = QVBoxLayout()
        add = QPushButton(tr("Add folder…"))
        add.clicked.connect(self._add)
        rem = QPushButton(tr("Remove"))
        rem.clicked.connect(lambda: [self.list.takeItem(self.list.row(i)) for i in self.list.selectedItems()])
        col.addWidget(add)
        col.addWidget(rem)
        col.addStretch(1)
        h.addLayout(col)

    def _add(self):
        d = QFileDialog.getExistingDirectory(self, tr("Look in this folder too"))
        if d:
            self.list.addItem(d)

    def folders(self):
        return [self.list.item(i).text() for i in range(self.list.count())]


class ProjectDoctorDialog(QDialog):
    TABS = ("overview", "broken", "used", "rename")

    def __init__(self, plugin, tab="overview", parent=None):
        super().__init__(parent or (plugin.iface.mainWindow() if plugin else None))
        self.plugin = plugin
        self.setWindowTitle(tr("Project doctor"))
        self.resize(820, 560)
        v = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._overview_tab(), tr("Overview"))
        self.tabs.addTab(self._broken_tab(), tr("Broken data paths"))
        self.tabs.addTab(self._used_tab(), tr("Where is this data used?"))
        self.tabs.addTab(self._rename_tab(), tr("Rename the project"))
        v.addWidget(self.tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.tabs.setCurrentIndex(self.TABS.index(tab) if tab in self.TABS else 0)
        self.refresh_overview()

    # ---- overview ------------------------------------------------------------------------------------
    def _overview_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.ov_label = QLabel()
        self.ov_label.setWordWrap(True)
        self.ov_label.setTextFormat(Qt.TextFormat.RichText)
        self.ov_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        v.addWidget(self.ov_label, 1)
        self.relative = QCheckBox(tr("Store data paths relative to the project (the folder can be moved or "
                                     "copied to another computer)"))
        self.relative.toggled.connect(lambda on: doc.set_relative_paths(on))
        v.addWidget(self.relative)
        row = QHBoxLayout()
        b = QPushButton(tr("Fix broken paths…"))
        b.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        row.addWidget(b)
        b = QPushButton(tr("Save temporary layers into the project"))
        b.clicked.connect(self._store_temporary)
        row.addWidget(b)
        b = QPushButton(tr("Copy all data into the project folder"))
        b.clicked.connect(self._consolidate)
        row.addWidget(b)
        b = QPushButton(tr("Check again"))
        b.clicked.connect(self.refresh_overview)
        row.addWidget(b)
        v.addLayout(row)
        return w

    def refresh_overview(self):
        try:
            from .project import project_folder

            pf = project_folder()
        except Exception:  # noqa: BLE001
            pf = None
        rep = doc.health_report(project_folder=pf)
        self.relative.blockSignals(True)
        self.relative.setChecked(rep["relative"])
        self.relative.blockSignals(False)

        def item(names, ok, problem):
            if not names:
                return f"<li>✔ {ok}</li>"
            shown = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
            return f"<li>⚠ {problem.format(n=len(names))}: <i>{shown}</i></li>"

        html = "<ul>" + "".join([
            item(rep["broken"], tr("Every layer finds its data."), tr("{n} layers cannot find their data")),
            item(rep["no_crs"], tr("Every layer has a coordinate system."),
                 tr("{n} layers have no coordinate system")),
            item(rep["temporary"], tr("No temporary layers."),
                 tr("{n} temporary layers will be lost when Biruni closes")),
            item(rep["outside"], tr("All data is inside the project folder."),
                 tr("{n} layers are stored outside the project folder")),
            item(rep["big"], tr("No very large layers."), tr("{n} very large layers may make the map slow")),
        ]) + "</ul>"
        if not QgsProject.instance().fileName():
            html = "<p>" + tr("This map is not saved as a project yet.") + "</p>" + html
        self.ov_label.setText(html)

    def _store_temporary(self):
        self.plugin.project.store_temporary_layers()
        self.refresh_overview()

    def _consolidate(self):
        self.plugin.project.consolidate()
        self.refresh_overview()

    # ---- broken paths ----------------------------------------------------------------------------------
    def _broken_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(tr("Biruni looks for the missing files by name in these folders:")))
        self.br_folders = FolderList(_default_folders())
        v.addWidget(self.br_folders)
        self.br_tree = QTreeWidget()
        self.br_tree.setHeaderLabels([tr("Layer"), tr("Was at"), tr("Found at")])
        self.br_tree.setRootIsDecorated(False)
        self.br_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.br_tree, 1)
        row = QHBoxLayout()
        search = QPushButton(tr("Search"))
        search.clicked.connect(self.search_broken)
        pick = QPushButton(tr("Choose the file by hand…"))
        pick.clicked.connect(self._pick_file)
        self.fix_btn = QPushButton(tr("Fix all"))
        self.fix_btn.clicked.connect(self._fix_all)
        row.addWidget(search)
        row.addWidget(pick)
        row.addStretch(1)
        row.addWidget(self.fix_btn)
        v.addLayout(row)
        self.br_note = QLabel()
        self.br_note.setWordWrap(True)
        v.addWidget(self.br_note)
        self.fixes = []
        return w

    def search_broken(self):
        broken = doc.broken_layers()
        if not broken:
            self.br_tree.clear()
            self.br_note.setText(tr("Every layer finds its data."))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.fixes = doc.suggest_fixes(broken, self.br_folders.folders())
        finally:
            QApplication.restoreOverrideCursor()
        self._fill_broken()

    def _fill_broken(self):
        self.br_tree.clear()
        found = 0
        for f in self.fixes:
            it = QTreeWidgetItem([f["name"], f["path"], ""])
            self.br_tree.addTopLevelItem(it)
            combo = QComboBox()
            options = ([f["choice"]] if f.get("choice") else []) + ([f["best"]] if f.get("best") else []) + \
                f.get("alternatives", [])
            seen = []
            for o in options:
                if o and o not in seen:
                    seen.append(o)
                    combo.addItem(o, o)
            combo.addItem(tr("(leave as it is)"), "")
            combo.currentIndexChanged.connect(lambda _i, f=f, c=combo: f.__setitem__("choice", c.currentData() or None)
                                              or f.__setitem__("skip", not c.currentData()))
            self.br_tree.setItemWidget(it, 2, combo)
            found += bool(seen)
        self.br_note.setText(tr("Found {f} of {n} missing files.").format(f=found, n=len(self.fixes)))

    def _pick_file(self):
        it = self.br_tree.currentItem()
        if it is None:
            return
        f = self.fixes[self.br_tree.indexOfTopLevelItem(it)]
        path, _ = QFileDialog.getOpenFileName(self, tr("Where is {n} now?").format(n=f["name"]),
                                              os.path.dirname(f["path"]))
        if path:
            f["choice"] = path
            f["skip"] = False
            self._fill_broken()

    def _fix_all(self):
        todo = [f for f in self.fixes if not f.get("skip") and (f.get("choice") or f.get("best"))]
        fixed, failed = doc.fix_all(todo)
        msg = tr("{n} layers fixed.").format(n=len(fixed))
        if failed:
            msg += " " + tr("These still do not load: {l}").format(l=", ".join(failed))
        self.br_note.setText(msg)
        self.plugin.iface.mapCanvas().refreshAllLayers()
        self.refresh_overview()
        if fixed:
            QgsProject.instance().setDirty(True)

    # ---- where used --------------------------------------------------------------------------------------
    def _used_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()
        row = QHBoxLayout()
        self.used_target = QLineEdit()
        self.used_target.setPlaceholderText(tr("A data file, or a folder / .gdb"))
        bf = QPushButton(tr("File…"))
        bf.clicked.connect(lambda: self._pick_target(False))
        bd = QPushButton(tr("Folder…"))
        bd.clicked.connect(lambda: self._pick_target(True))
        row.addWidget(self.used_target, 1)
        row.addWidget(bf)
        row.addWidget(bd)
        form.addRow(tr("Data"), row)
        v.addLayout(form)
        v.addWidget(QLabel(tr("Look for projects in these folders:")))
        self.used_folders = FolderList(_default_folders())
        v.addWidget(self.used_folders)
        self.used_history = QCheckBox(tr("Also look in old versions of projects (history)"))
        v.addWidget(self.used_history)
        self.used_tree = QTreeWidget()
        self.used_tree.setHeaderLabels([tr("Project"), tr("Layer"), tr("Data source")])
        self.used_tree.setRootIsDecorated(False)
        self.used_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.used_tree.itemDoubleClicked.connect(self._open_hit)
        v.addWidget(self.used_tree, 1)
        row = QHBoxLayout()
        go = QPushButton(tr("Search"))
        go.clicked.connect(self.search_used)
        row.addStretch(1)
        row.addWidget(go)
        v.addLayout(row)
        self.used_note = QLabel()
        v.addWidget(self.used_note)
        try:
            lyr = self.plugin.iface.activeLayer()
            if lyr is not None:
                self.used_target.setText(doc.source_path(lyr))
        except Exception:  # noqa: BLE001
            pass
        return w

    def _pick_target(self, folder):
        if folder:
            p = QFileDialog.getExistingDirectory(self, tr("Choose the folder"), self.used_target.text())
        else:
            p, _ = QFileDialog.getOpenFileName(self, tr("Choose the data file"), self.used_target.text())
        if p:
            self.used_target.setText(p)

    def search_used(self):
        target = self.used_target.text().strip()
        if not target:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            hits = doc.where_used(target, self.used_folders.folders(), self.used_history.isChecked())
        finally:
            QApplication.restoreOverrideCursor()
        self.used_tree.clear()
        for h in hits:
            it = QTreeWidgetItem([h["project"], h["layer"], h["source"]])
            it.setToolTip(0, h["project"])
            self.used_tree.addTopLevelItem(it)
        projects = {h["project"] for h in hits}
        self.used_note.setText(tr("Used by {l} layers in {p} projects.").format(l=len(hits), p=len(projects))
                               if hits else tr("No project in these folders uses this data."))

    def _open_hit(self, item, _col):
        path = item.text(0)
        if os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    # ---- rename -------------------------------------------------------------------------------------------
    def _rename_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        path = QgsProject.instance().fileName()
        cur = os.path.splitext(os.path.basename(path))[0] if path else ""
        info = QLabel(tr("Renames the project folder, the project file and the project database together, and "
                         "keeps the history. The project closes and opens again under its new name."))
        info.setWordWrap(True)
        v.addWidget(info)
        form = QFormLayout()
        self.cur_name = QLabel(cur or tr("(no project open)"))
        form.addRow(tr("Current name"), self.cur_name)
        self.new_name = QLineEdit(cur)
        form.addRow(tr("New name"), self.new_name)
        v.addLayout(form)
        btn = QPushButton(tr("Rename"))
        btn.setEnabled(bool(path))
        btn.clicked.connect(self._rename)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn)
        v.addLayout(row)
        v.addStretch(1)
        return w

    def _rename(self):
        name = self.new_name.text().strip()
        try:
            new = doc.rename_open_project(self.plugin.iface, name)
        except doc.RenameError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.cur_name.setText(name)
        self.plugin.iface.messageBar().pushSuccess(APP_NAME, tr("The project is now {p}").format(p=new))


def open_dialog(plugin, tab="overview"):
    dlg = ProjectDoctorDialog(plugin, tab)
    dlg.exec()
    return dlg
