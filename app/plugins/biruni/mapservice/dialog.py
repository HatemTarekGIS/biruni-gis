"""Dialogs: publish a map service (one folder, no server) and check a published one."""
import json
import os

from qgis.core import QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
                                 QProgressDialog, QPushButton, QSpinBox, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import core


def _window(plugin):
    iface = getattr(plugin, "iface", plugin)
    try:
        return iface.mainWindow()
    except Exception:  # noqa: BLE001 - no main window (tests)
        return None


class MapServiceDialog(QDialog):
    """Publish layers as a map service folder: PMTiles + FlatGeobuf + static OGC API + viewer + signature."""

    def __init__(self, plugin):
        from ..share.widgets import LayerPicker, PathEdit, exports_dir, project_name

        super().__init__(_window(plugin))
        self.plugin = plugin
        self.result_info = None
        self.setWindowTitle(tr("Publish a map service"))
        self.setMinimumWidth(580)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        intro = QLabel(tr("Makes one folder that works as a full map service with no server: the map in one file, "
                          "identify and queries read in pieces, an OGC API that other GIS programs can add, and a "
                          "signature that shows if anything was changed. Put the folder on GitHub Pages, a shared "
                          "folder or any web host."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        v.addWidget(QLabel(tr("Layers")))
        self.layers = LayerPicker(self)
        v.addWidget(self.layers, 1)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.raster = QComboBox()
        self.raster.addItem(tr("(none)"), "")
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer) and lyr.isValid() and lyr.providerType() == "gdal":
                self.raster.addItem(lyr.name(), lyr.id())
        form.addRow(tr("Raster"), self.raster)
        name = project_name()
        self.title = QLineEdit(name)
        form.addRow(tr("Title"), self.title)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "map"
        self.folder = PathEdit(os.path.join(exports_dir(), safe + "-service"), folder=True)
        form.addRow(tr("Folder"), self.folder)
        self.max_zoom = QSpinBox()
        self.max_zoom.setRange(8, 18)
        self.max_zoom.setValue(14)
        form.addRow(tr("Most detailed zoom"), self.max_zoom)
        self.basemap = QComboBox()
        self.basemap.addItem(tr("OpenStreetMap (needs the internet)"), "osm")
        self.basemap.addItem(tr("No basemap"), "none")
        form.addRow(tr("Basemap"), self.basemap)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://user.github.io/my-map")
        form.addRow(tr("Final address (optional)"), self.base_url)
        v.addLayout(form)
        self.ogc = QCheckBox(tr("OGC API - Features (other GIS programs add it as a service)"))
        self.ogc.setChecked(True)
        self.xyz = QCheckBox(tr("Also write the tiles as separate files (TileJSON, for other programs)"))
        self.sign = QCheckBox(tr("Sign with this computer's key"))
        self.sign.setChecked(True)
        for cb in (self.ogc, self.xyz, self.sign):
            v.addWidget(cb)
        self.done_label = QLabel("")
        self.done_label.setWordWrap(True)
        self.done_label.setVisible(False)
        v.addWidget(self.done_label)
        row = QHBoxLayout()
        self.preview_btn = QPushButton(tr("Preview"))
        self.open_btn = QPushButton(tr("Open folder"))
        self.copy_btn = QPushButton(tr("Copy the OGC address"))
        for b in (self.preview_btn, self.open_btn, self.copy_btn):
            b.setVisible(False)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.preview_btn.clicked.connect(self.preview)
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder.path())))
        self.copy_btn.clicked.connect(self.copy_ogc)
        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Publish"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self.run)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def options(self):
        rid = self.raster.currentData()
        return {"max_zoom": self.max_zoom.value(), "ogc": self.ogc.isChecked(), "xyz": self.xyz.isChecked(),
                "sign": self.sign.isChecked(), "basemap": self.basemap.currentData(),
                "raster": QgsProject.instance().mapLayer(rid) if rid else None,
                "base_url": self.base_url.text().strip()}

    def run(self, quiet=False):
        layers = self.layers.selected_layers()
        opts = self.options()
        if not layers and opts["raster"] is None:
            if not quiet:
                QMessageBox.information(self, APP_NAME, tr("Choose at least one layer."))
            return None
        prog = None
        if not quiet:
            prog = QProgressDialog(tr("Publishing…"), "", 0, 100, self)
            prog.setCancelButton(None)
            prog.setMinimumDuration(0)
            prog.setWindowTitle(tr("Publish a map service"))

        def say(pct, text=""):
            if prog is not None:
                prog.setValue(pct)
                prog.setLabelText(text or tr("Publishing…"))
                QApplication.processEvents()

        try:
            info = core.publish(layers, self.folder.path(), self.title.text().strip(), opts, progress=say)
        except Exception as e:  # noqa: BLE001 - shown to the user
            if prog is not None:
                prog.close()
            if not quiet:
                QMessageBox.warning(self, APP_NAME, tr("The map service could not be written: {e}").format(e=e))
            return None
        if prog is not None:
            prog.close()
        self.result_info = info
        total = sum(sz for _p, sz in info["files"])
        lines = [tr("Done: {n} layers, {size} in {files} files.").format(
            n=len(info["layers"]) + (1 if info["raster"] else 0), size=core.file_size_text(total), files=len(info["files"]))]
        if info["signed"]:
            lines.append(tr("Signed with the key {fp}.").format(fp=info["fingerprint"]))
        lines += info["warnings"]
        self.done_label.setText("\n".join(lines))
        self.done_label.setVisible(True)
        for b in (self.preview_btn, self.open_btn):
            b.setVisible(True)
        self.copy_btn.setVisible(bool(info["ogc"]))
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            try:
                act.log("share", "map_service", self.title.text().strip(), "", len(info["layers"]), info["folder"])
            except Exception:  # noqa: BLE001 - the log never stops a publish
                pass
        return info

    def preview(self, open_browser=True):
        from ..share import offline_maps as om

        if not self.result_info:
            return None
        url = om.serve(self.result_info["folder"]) + self.result_info.get("link_suffix", "")
        if open_browser:
            QDesktopServices.openUrl(QUrl(url))
        return url

    def copy_ogc(self):
        from ..share import offline_maps as om

        if not self.result_info or not self.result_info["ogc"]:
            return ""
        folder = self.result_info["folder"]
        base = self.base_url.text().strip().rstrip("/")
        local = not base
        if local:     # no final address yet: this PC's preview (GIS programs need absolute links)
            base = om.serve(folder).rstrip("/")
        try:
            with open(os.path.join(folder, "service.json"), encoding="utf-8") as f:
                was = (json.load(f).get("base_url") or "").rstrip("/")
        except (OSError, ValueError):
            was = ""
        if was != base:
            core.rebase(folder, base, sign=self.sign.isChecked())
        if local:
            self.done_label.setText(self.done_label.text() + "\n" + tr(
                "The OGC address works on this computer only, while Biruni is open. Write the final address and "
                "copy it again before you put the folder online."))
        addr = base + "/" + self.result_info["ogc"]
        QApplication.clipboard().setText(addr)
        return addr


def open_dialog(plugin):
    """The Publish a map service dialog (kept on the plugin so it is not collected). -> the dialog"""
    dlg = MapServiceDialog(plugin)
    plugin._map_service_dialog = dlg
    dlg.show()
    return dlg


def check_text(info):
    """The result of core.verify() in words."""
    if not info["signed"]:
        return tr("This map service is not signed.")
    head = []
    if info["signature_ok"] and not info["bad"] and not info["missing"]:
        head.append(tr("The signature is valid and no file was changed."))
    elif not info["signature_ok"]:
        head.append(tr("The signature does not match: do not trust this copy."))
    if info["bad"]:
        head.append(tr("Changed after signing: {files}").format(files=", ".join(info["bad"][:12])))
    if info["missing"]:
        head.append(tr("Missing: {files}").format(files=", ".join(info["missing"][:12])))
    head.append(tr("Signed by {name}").format(name=info["signer"] or "?"))
    head.append(tr("Key {fp}").format(fp=info["fingerprint"]))
    if info["by_me"]:
        head.append(tr("This is this computer's key."))
    head.append(tr("{n} files checked").format(n=info["checked"]))
    return "\n".join(head)


def check_ui(plugin, where=None):
    """Check a map service: a folder, or an address (http/https). -> the verify() result, or None"""
    win = _window(plugin)
    if where is None:
        text, ok = QInputDialog.getText(win, tr("Check a map service"),
                                        tr("The address of the map service (leave empty to choose a folder):"))
        if not ok:
            return None
        where = text.strip()
        if not where:
            where = QFileDialog.getExistingDirectory(win, tr("Check a map service"))
            if not where:
                return None
    info = core.verify(where)
    if plugin is not None and win is not None:
        box = QMessageBox.information if info["ok"] else QMessageBox.warning
        box(win, tr("Check a map service"), check_text(info))
    return info
