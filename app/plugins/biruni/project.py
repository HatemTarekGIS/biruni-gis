"""Project folders.

A Biruni project is a folder that holds everything the project needs:

    <Name>/
        <Name>.qgz      the project
        <Name>.gpkg     project database - new and temporary layers are saved here
        data/           files copied into the project
        outputs/        tool results
        exports/        maps, PDFs, images and exported data

A *temporary* project lives only in memory until the user saves it, at which
point it becomes a project folder too.
"""
import os
import re
import shutil
import zipfile

from qgis.core import (Qgis, QgsFeature, QgsProject, QgsRasterLayer, QgsProviderRegistry, QgsSettings, QgsVectorFileWriter,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QObject, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QVBoxLayout)

from .i18n import tr
from .version import APP_NAME, VERSION

SUBFOLDERS = ("data", "outputs", "exports")
LOCATION_KEY = "biruni/projects-location"


def default_location():
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return QgsSettings().value(LOCATION_KEY, os.path.join(docs, "Biruni GIS Projects"))


def safe_name(text):
    s = re.sub(r'[\\/:*?"<>|]+', "", text).strip(" .")
    return s


def project_folder(prj=None):
    """The folder of the current project if it is a Biruni project folder."""
    prj = prj or QgsProject.instance()
    path = prj.fileName()
    if not path:
        return None
    folder = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]
    return folder if os.path.exists(os.path.join(folder, base + ".gpkg")) else None


def project_gpkg(prj=None):
    prj = prj or QgsProject.instance()
    folder = project_folder(prj)
    if not folder:
        return None
    return os.path.join(folder, os.path.splitext(os.path.basename(prj.fileName()))[0] + ".gpkg")


def create_folder_structure(parent, name):
    root = os.path.join(parent, name)
    if os.path.exists(root) and os.listdir(root):
        raise FileExistsError(root)
    for sub in ("",) + SUBFOLDERS:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    gpkg = os.path.join(root, name + ".gpkg")
    # A GeoPackage needs at least one table to be opened everywhere; start it
    # with a small project-information table, written by QGIS itself.
    info = QgsVectorLayer("None?field=key:string&field=value:string", "biruni_project", "memory")
    feats = []
    for k, v in (("name", name), ("created_by", f"{APP_NAME} {VERSION}")):
        f = QgsFeature(info.fields())
        f.setAttributes([k, v])
        feats.append(f)
    info.dataProvider().addFeatures(feats)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "biruni_project"
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(info, gpkg, QgsProject.instance().transformContext(),
                                                            opts)
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(msg)
    return root, os.path.join(root, name + ".qgz"), gpkg


def _unique_table(gpkg, base):
    existing = set()
    if os.path.exists(gpkg):
        md = QgsProviderRegistry.instance().providerMetadata("ogr")
        existing = {d.name().lower() for d in md.querySublayers(gpkg)}
    name = re.sub(r"[^\w]+", "_", base, flags=re.UNICODE).strip("_") or "layer"
    cand, k = name, 2
    while cand.lower() in existing:
        cand, k = f"{name}_{k}", k + 1
    return cand


def copy_vector_into_gpkg(layer, gpkg):
    """Write a vector layer into the project database and repoint the layer."""
    table = _unique_table(gpkg, layer.name())
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = table
    opts.fileEncoding = "UTF-8"
    opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                 if os.path.exists(gpkg) else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, gpkg, QgsProject.instance().transformContext(), opts)
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(msg)
    layer.setDataSource(f"{gpkg}|layername={table}", layer.name(), "ogr")
    return table


def _layer_file(layer):
    parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
    path = parts.get("path") or ""
    return path if path and os.path.exists(path) else None


def _inside(path, folder):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
    except ValueError:  # different drives
        return False


class ProjectManager(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self._offered = set()

    def install(self):
        prj = QgsProject.instance()
        prj.readProject.connect(self._on_project_changed)
        prj.projectSaved.connect(self._on_project_changed)
        prj.layersAdded.connect(self._on_layers_added)

    def unload(self):
        prj = QgsProject.instance()
        for sig, slot in ((prj.readProject, self._on_project_changed), (prj.projectSaved, self._on_project_changed),
                          (prj.layersAdded, self._on_layers_added)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    # ---- creating / saving --------------------------------------------
    def new_project(self, template="blank"):
        dlg = ProjectDialog(self.iface.mainWindow(), save_mode=False, template=template)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        if not self.iface.newProject(True):  # asks to save the current one first
            return False
        if dlg.template() == "world":
            self.plugin.add_basemap("osm", zoom_world=True)
        return self._write_new(dlg.location(), dlg.name())

    def start_temporary(self):
        self.plugin.start.hide_page()
        self.iface.messageBar().pushInfo(
            APP_NAME, tr("This is a temporary map. Use Save to keep it as a project folder."))

    def save(self):
        """Save; a temporary project becomes a project folder first."""
        prj = QgsProject.instance()
        if prj.fileName():
            self.plugin.runner.qaction("mActionSaveProject").trigger()
            return True
        return self.save_as_folder()

    def save_as_folder(self):
        dlg = ProjectDialog(self.iface.mainWindow(), save_mode=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        return self._write_new(dlg.location(), dlg.name(), keep_temporary=True)

    def _write_new(self, location, name, keep_temporary=False):
        try:
            root, qgz, gpkg = create_folder_structure(location, name)
        except FileExistsError as e:
            QMessageBox.warning(self.iface.mainWindow(), APP_NAME,
                                tr("The folder already exists and is not empty:\n{p}").format(p=str(e)))
            return False
        QgsSettings().setValue(LOCATION_KEY, location)
        prj = QgsProject.instance()
        self.plugin.activity.log("project", "created", name, "", 1, root)
        if keep_temporary:
            self.store_temporary_layers(gpkg, quiet=True)
        if not prj.title():
            prj.setTitle(name)
        prj.writeEntry("Biruni", "/folder-project", True)
        if not prj.write(qgz):
            QMessageBox.critical(self.iface.mainWindow(), APP_NAME, prj.error())
            return False
        self.plugin.start.hide_page()
        self.iface.messageBar().pushSuccess(APP_NAME, tr("Project saved in {p}").format(p=root))
        return True

    # ---- defaults while a project folder is open ------------------------
    def _on_project_changed(self, *args):
        folder = project_folder()
        if not folder:
            return
        s = QgsSettings()
        s.setValue("UI/lastProjectDir", folder)
        s.setValue("UI/lastSaveAsImageDir", os.path.join(folder, "exports"))
        s.setValue("UI/lastVectorFileFilterDir", os.path.join(folder, "data"))
        s.setValue("UI/lastRasterFileFilterDir", os.path.join(folder, "data"))
        try:
            from processing.core.ProcessingConfig import ProcessingConfig

            ProcessingConfig.setSettingValue(ProcessingConfig.OUTPUT_FOLDER, os.path.join(folder, "outputs"))
        except Exception:  # processing not loaded yet
            pass

    def _on_layers_added(self, layers):
        folder = project_folder()
        if not folder:
            return
        outside = [lyr for lyr in layers if lyr.id() not in self._offered and _layer_file(lyr)
                   and not _inside(_layer_file(lyr), folder)]
        if not outside:
            return
        for lyr in outside:
            self._offered.add(lyr.id())
        bar = self.iface.messageBar()
        names = ", ".join(lyr.name() for lyr in outside[:3]) + ("…" if len(outside) > 3 else "")
        item = bar.createMessage(APP_NAME, tr("{n} is stored outside the project folder.").format(n=names))
        btn = QPushButton(tr("Copy into the project"))
        btn.clicked.connect(lambda: (self.consolidate([lyr.id() for lyr in outside]), bar.popWidget(item)))
        item.layout().addWidget(btn)
        bar.pushWidget(item, Qgis.MessageLevel.Info, 12)

    # ---- project tools ---------------------------------------------------
    def _require_folder(self):
        folder = project_folder()
        if folder:
            return folder
        r = QMessageBox.question(self.iface.mainWindow(), APP_NAME,
                                 tr("This tool needs a project folder. Save the current map as a project now?"))
        if r == QMessageBox.StandardButton.Yes and self.save_as_folder():
            return project_folder()
        return None

    def store_temporary_layers(self, gpkg=None, quiet=False):
        """Move every temporary (memory) layer into the project database."""
        gpkg = gpkg or (self._require_folder() and project_gpkg())
        if not gpkg:
            return 0
        temps = [lyr for lyr in QgsProject.instance().mapLayers().values()
                 if isinstance(lyr, QgsVectorLayer) and lyr.providerType() == "memory"]
        done = 0
        for lyr in temps:
            if lyr.isEditable():
                lyr.commitChanges()
            try:
                copy_vector_into_gpkg(lyr, gpkg)
                done += 1
            except RuntimeError as e:
                self.iface.messageBar().pushWarning(APP_NAME, f"{lyr.name()}: {e}")
        if not quiet:
            msg = (tr("{n} temporary layers were saved into the project database.").format(n=done) if done
                   else tr("There are no temporary layers."))
            self.iface.messageBar().pushSuccess(APP_NAME, msg)
        return done

    def consolidate(self, layer_ids=None):
        """Copy data stored outside the project folder into it and repoint the layers."""
        folder = self._require_folder()
        if not folder:
            return 0
        gpkg = project_gpkg()
        data_dir = os.path.join(folder, "data")
        os.makedirs(data_dir, exist_ok=True)
        layers = [lyr for lid, lyr in QgsProject.instance().mapLayers().items()
                  if layer_ids is None or lid in layer_ids]
        done, skipped = 0, []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for lyr in layers:
                path = _layer_file(lyr)
                if isinstance(lyr, QgsVectorLayer) and lyr.providerType() == "memory":
                    copy_vector_into_gpkg(lyr, gpkg)
                    done += 1
                elif path is None or _inside(path, folder):
                    continue
                elif isinstance(lyr, QgsVectorLayer):
                    copy_vector_into_gpkg(lyr, gpkg)
                    done += 1
                elif isinstance(lyr, QgsRasterLayer) and lyr.providerType() == "gdal":
                    new = self._copy_with_sidecars(path, data_dir)
                    lyr.setDataSource(new, lyr.name(), "gdal")
                    done += 1
                else:
                    skipped.append(lyr.name())
        except Exception as e:
            self.iface.messageBar().pushCritical(APP_NAME, str(e))
        finally:
            QApplication.restoreOverrideCursor()
        QgsProject.instance().setDirty(True)
        msg = tr("{n} layers were copied into the project folder.").format(n=done)
        if skipped:
            msg += " " + tr("Not copied: {names}").format(names=", ".join(skipped))
        self.iface.messageBar().pushSuccess(APP_NAME, msg)
        return done

    @staticmethod
    def _copy_with_sidecars(path, dest_dir):
        """Copy a raster with its sidecar files (.tfw, .aux.xml, .ovr, .prj …)."""
        src_dir, fname = os.path.split(path)
        stem = os.path.splitext(fname)[0]
        for f in os.listdir(src_dir):
            if f == fname or f.startswith(stem + "."):
                target = os.path.join(dest_dir, f)
                if not os.path.exists(target):
                    shutil.copy2(os.path.join(src_dir, f), target)
        return os.path.join(dest_dir, fname)

    def package(self):
        """Consolidate, save, and zip the whole project folder for sharing."""
        folder = self._require_folder()
        if not folder:
            return None
        self.consolidate()
        QgsProject.instance().write()
        default = os.path.join(os.path.dirname(folder), os.path.basename(folder) + ".zip")
        path, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), tr("Package project"), default,
                                              "ZIP (*.zip)")
        if not path:
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for root, dirs, files in os.walk(folder):
                    dirs[:] = [d for d in dirs if d != ".history"]  # project versions stay local
                    for f in files:
                        full = os.path.join(root, f)
                        if os.path.abspath(full) == os.path.abspath(path):
                            continue
                        z.write(full, os.path.relpath(full, os.path.dirname(folder)))
        finally:
            QApplication.restoreOverrideCursor()
        self.iface.messageBar().pushSuccess(APP_NAME, tr("Project packaged: {p}").format(p=path))
        return path

    def activity_report(self):
        """Export the project's activity log with statistics to Excel."""
        from .activity_report import write_report

        events, sessions, active = self.plugin.activity.snapshot()
        if not events:
            self.iface.messageBar().pushInfo(APP_NAME, tr("No activity has been recorded for this project yet."))
            return None
        prj = QgsProject.instance()
        name = prj.title() or os.path.splitext(os.path.basename(prj.fileName()))[0] or tr("Temporary map")
        folder = project_folder()
        start_dir = os.path.join(folder, "exports") if folder else os.path.expanduser("~")
        default = os.path.join(start_dir, f"{safe_name(name) or 'project'} - {tr('Activity report')}.xlsx")
        path, _ = QFileDialog.getSaveFileName(self.iface.mainWindow(), tr("Activity report"), default,
                                              "Excel (*.xlsx)")
        if not path:
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            write_report(path, name, events, sessions, active)
        except PermissionError:
            QMessageBox.warning(self.iface.mainWindow(), APP_NAME,
                                tr("The file is open in another program. Close it and try again."))
            return None
        finally:
            QApplication.restoreOverrideCursor()
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return path

    def open_phone_gps(self):
        d = self.plugin.tracking
        d.show()
        d.raise_()

    def open_folder(self):
        folder = project_folder() or (os.path.dirname(QgsProject.instance().fileName())
                                      if QgsProject.instance().fileName() else None)
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            self.iface.messageBar().pushInfo(APP_NAME, tr("The map has not been saved yet."))


class ProjectDialog(QDialog):
    """New project / save temporary map as a project folder."""

    def __init__(self, parent, save_mode=False, template="blank"):
        super().__init__(parent)
        self.setWindowTitle(tr("Save as project") if save_mode else tr("New project"))
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(12)
        intro = QLabel(tr("A project is a folder that keeps the map and all of its data together: the "
                          "project file, a project database for new layers, and folders for data, tool "
                          "outputs and exports."))
        intro.setWordWrap(True)
        v.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.name_edit = QLineEdit(tr("My project") if not QgsProject.instance().title()
                                   else QgsProject.instance().title())
        self.name_edit.selectAll()
        form.addRow(tr("Name"), self.name_edit)
        row = QHBoxLayout()
        self.loc_edit = QLineEdit(default_location())
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.loc_edit, 1)
        row.addWidget(browse)
        form.addRow(tr("Location"), row)
        v.addLayout(form)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.preview)

        self.tpl_blank = QRadioButton(tr("Blank map"))
        self.tpl_world = QRadioButton(tr("Map with a world basemap (OpenStreetMap)"))
        (self.tpl_world if template == "world" else self.tpl_blank).setChecked(True)
        if not save_mode:
            v.addWidget(self.tpl_blank)
            v.addWidget(self.tpl_world)

        buttons = QDialogButtonBox()
        self.ok = buttons.addButton(tr("Save project") if save_mode else tr("Create project"),
                                    QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.name_edit.textChanged.connect(self._update)
        self.loc_edit.textChanged.connect(self._update)
        self._update()

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr("Location"), self.loc_edit.text())
        if d:
            self.loc_edit.setText(os.path.normpath(d))

    def _update(self):
        name, loc = self.name(), self.location()
        ok = bool(name) and bool(loc)
        target = os.path.join(loc, name) if ok else ""
        exists = ok and os.path.exists(target) and os.listdir(target)
        self.ok.setEnabled(ok and not exists)
        if not ok:
            self.preview.setText(tr("Enter a name and a location."))
        elif exists:
            self.preview.setText(tr("A folder with this name already exists here - choose another name."))
        else:
            self.preview.setText(tr("Will create:") + f"  <b>{target}</b><br>"
                                 f"&nbsp;&nbsp;{name}.qgz · {name}.gpkg · data · outputs · exports")

    def name(self):
        return safe_name(self.name_edit.text())

    def location(self):
        return self.loc_edit.text().strip()

    def template(self):
        return "world" if self.tpl_world.isChecked() else "blank"
