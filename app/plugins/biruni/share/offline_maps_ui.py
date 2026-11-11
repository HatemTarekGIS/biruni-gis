"""The buttons of the offline maps: a basemap kept in one file (opened, or cut out of the OpenStreetMap build
for the area on the map), a basemap pack made of the project's layers, and a preview of a published site."""
import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsSettings,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton,
                                 QSpinBox, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import offline_maps as om
from . import pmtiles as pm

DIR_KEY = "biruni/offline-maps/dir"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _dir():
    return QgsSettings().value(DIR_KEY, "") or QgsProject.instance().homePath() or os.path.expanduser("~")


def _remember(path):
    QgsSettings().setValue(DIR_KEY, os.path.dirname(path) if os.path.isfile(path) or "." in os.path.basename(path)
                           else path)


def _bar(plugin, text, level=Qgis.MessageLevel.Success, seconds=10):
    plugin.iface.messageBar().pushMessage(APP_NAME, text, level, seconds)


def _why(e):
    s = str(e)
    if s.startswith("offline"):
        return tr("The address could not be reached: check the internet connection.")
    if s.startswith("not found"):
        return tr("Nothing was found at this address ({c}).").format(c=s.split(": ", 1)[-1])
    if s == "raster":
        return tr("This file holds pictures, not vector tiles: add it with Add Data.")
    if s.startswith("too many tiles"):
        return tr("The area has too many tiles: zoom in on the map, or lower the highest zoom.")
    if "range requests" in s:
        return tr("The server does not send parts of files, so an area cannot be cut out of it.")
    if s.startswith("compression"):
        return tr("This archive uses a compression the program does not read ({c}).").format(c=s.split(" ", 1)[-1])
    if s == "not a PMTiles archive":
        return tr("This is not a PMTiles file.")
    if s == "the archive has no tiles in this area":
        return tr("The basemap has nothing in this area.")
    if s == "no build":
        return tr("No basemap build was found in the list.")
    return s


def canvas_bbox(plugin):
    """(west, south, east, north) of the map's view, in degrees."""
    canvas = plugin.iface.mapCanvas()
    ext = canvas.extent()
    crs = canvas.mapSettings().destinationCrs()
    if crs != WGS84:
        ext = QgsCoordinateTransform(crs, WGS84, QgsProject.instance()).transformBoundingBox(ext)
    return (max(ext.xMinimum(), -180.0), max(ext.yMinimum(), -85.05), min(ext.xMaximum(), 180.0),
            min(ext.yMaximum(), 85.05))


class _Progress:
    def __init__(self, parent, text):
        self.dlg = QProgressDialog(text, tr("Cancel"), 0, 100, parent)
        self.dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self.dlg.setMinimumDuration(300)

    def __call__(self, pct):
        self.dlg.setValue(int(pct))
        QApplication.processEvents()
        return not self.dlg.wasCanceled()

    def close(self):
        self.dlg.close()


class BasemapDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.reader = None
        self.plan = None
        self.setWindowTitle(tr("Offline Basemap"))
        self.setMinimumWidth(520)
        v = QVBoxLayout(self)
        intro = QLabel(tr("A basemap kept in one file on this computer (PMTiles): it draws without the internet, "
                          "with place names in Arabic. Open a file you have, or cut the area on the map out of "
                          "the daily OpenStreetMap build: only that area's tiles are downloaded, once."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        openb = QPushButton(tr("Open a Basemap File…"))
        openb.clicked.connect(lambda: self.open_file())
        v.addWidget(openb)
        form = QFormLayout()
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://build.protomaps.com/YYYYMMDD.pmtiles")
        latest = QPushButton(tr("Latest Build"))
        latest.clicked.connect(lambda: self.find_latest())
        row.addWidget(self.url, 1)
        row.addWidget(latest)
        form.addRow(tr("Source"), row)
        self.maxz = QSpinBox()
        self.maxz.setRange(0, 15)
        self.maxz.setValue(15)
        self.maxz.setToolTip(tr("15 shows every street and building; each level less makes the file about four "
                                "times smaller."))
        form.addRow(tr("Highest zoom"), self.maxz)
        v.addLayout(form)
        btns = QHBoxLayout()
        size = QPushButton(tr("How Big?"))
        size.clicked.connect(lambda: self.measure())
        self.get = QPushButton(tr("Download the Area on the Map"))
        self.get.clicked.connect(lambda: self.download())
        btns.addWidget(size)
        btns.addWidget(self.get)
        v.addLayout(btns)
        self.out = QLabel(tr("The data: © OpenStreetMap contributors (ODbL). The map must say so wherever it is "
                             "shown."))
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def open_file(self, path=None):
        if path is None:
            path, _f = QFileDialog.getOpenFileName(self, self.windowTitle(), _dir(), "PMTiles (*.pmtiles)")
            if not path:
                return None
        _remember(path)
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                lyr = om.add_basemap(path)
            finally:
                QApplication.restoreOverrideCursor()
        except (pm.PMTilesError, om.OfflineMapError, OSError) as e:
            self.out.setText(_why(e))
            return None
        self.out.setText(tr("«{n}» added under the other layers.").format(n=lyr.name()))
        return lyr

    def find_latest(self):
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.url.setText(om.latest_build())
            finally:
                QApplication.restoreOverrideCursor()
        except om.OfflineMapError as e:
            self.out.setText(_why(e))

    def _plan(self, bbox=None):
        src = self.url.text().strip()
        if not src:
            self.find_latest()
            src = self.url.text().strip()
            if not src:
                return None
        if self.reader is None or self.reader.src.where != src:
            self.reader = pm.Reader(src)
        self.plan = pm.plan(self.reader, bbox or canvas_bbox(self.plugin), 0, self.maxz.value())
        return self.plan

    def measure(self, bbox=None):
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                p = self._plan(bbox)
            finally:
                QApplication.restoreOverrideCursor()
        except (pm.PMTilesError, OSError) as e:
            self.out.setText(_why(e))
            return None
        if p is None:
            return None
        self.out.setText(tr("{n} tiles, about {mb} MB to download.").format(
            n=len(p.tiles), mb=round(p.size / (1024 * 1024), 1)))
        return p

    def download(self, path=None, bbox=None):
        p = self.measure(bbox)
        if p is None:
            return None
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.windowTitle(), os.path.join(_dir(), "basemap.pmtiles"),
                                                   "PMTiles (*.pmtiles)")
            if not path:
                return None
        if not path.lower().endswith(".pmtiles"):
            path += ".pmtiles"
        _remember(path)
        prog = _Progress(self, tr("Downloading the basemap…"))
        try:
            pm.extract(self.reader.src.where, path, bbox or canvas_bbox(self.plugin), 0, self.maxz.value(),
                       progress=prog, plan_=p, reader=self.reader)
        except (pm.PMTilesError, OSError) as e:
            self.out.setText(_why(e) if str(e) != "cancelled" else tr("Cancelled."))
            return None
        finally:
            prog.close()
        return self.open_file(path)


def basemap_ui(plugin):
    dlg = BasemapDialog(plugin)
    dlg.show()
    plugin._basemap_dialog = dlg
    return dlg


def _chosen(plugin):
    chosen = [l for l in plugin.iface.layerTreeView().selectedLayers() if isinstance(l, QgsVectorLayer)]
    if chosen:
        return chosen
    root = QgsProject.instance().layerTreeRoot()
    return [n.layer() for n in root.findLayers()
            if n.isVisible() and isinstance(n.layer(), QgsVectorLayer) and n.layer().isValid()]


class PackDialog(QDialog):
    def __init__(self, plugin, layers):
        super().__init__(plugin.iface.mainWindow())
        self.plugin, self.layers = plugin, layers
        self.setWindowTitle(tr("Basemap Pack from Layers"))
        v = QVBoxLayout(self)
        lbl = QLabel(tr("{n} layers into one PMTiles file of vector tiles: for a published site, or to carry the "
                        "project's map to another computer: {l}.").format(
            n=len(layers), l=", ".join(l.name() for l in layers[:6]) + ("…" if len(layers) > 6 else "")))
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        form = QFormLayout()
        self.minz, self.maxz = QSpinBox(), QSpinBox()
        self.minz.setRange(0, 22)
        self.maxz.setRange(0, 22)
        self.minz.setValue(0)
        self.maxz.setValue(16)
        form.addRow(tr("Lowest zoom"), self.minz)
        form.addRow(tr("Highest zoom"), self.maxz)
        self.simplify = QCheckBox(tr("Simplify the shapes of the far zooms"))
        self.simplify.setChecked(True)
        form.addRow("", self.simplify)
        v.addLayout(form)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        go = QPushButton(tr("Make the Pack"))
        bb.addButton(go, QDialogButtonBox.ButtonRole.ActionRole)
        go.clicked.connect(lambda: self.make())
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def make(self, path=None):
        if self.minz.value() > self.maxz.value():
            self.out.setText(tr("The lowest zoom is above the highest."))
            return None
        if path is None:
            path, _f = QFileDialog.getSaveFileName(self, self.windowTitle(), os.path.join(_dir(), "pack.pmtiles"),
                                                   "PMTiles (*.pmtiles)")
            if not path:
                return None
        if not path.lower().endswith(".pmtiles"):
            path += ".pmtiles"
        _remember(path)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            names = om.make_pack(self.layers, path, self.minz.value(), self.maxz.value(), self.simplify.isChecked())
        except (om.OfflineMapError, OSError) as e:
            self.out.setText(tr("The pack could not be made: {e}").format(e=e))
            return None
        finally:
            QApplication.restoreOverrideCursor()
        mb = os.path.getsize(path) / (1024 * 1024)
        self.out.setText(tr("Saved: {n} layers, {mb} MB.").format(n=len(names), mb=round(mb, 2)))
        return names


def pack_ui(plugin):
    layers = _chosen(plugin)
    if not layers:
        _bar(plugin, tr("Choose the layers in the Layers panel first."), Qgis.MessageLevel.Warning, 8)
        return None
    dlg = PackDialog(plugin, layers)
    dlg.show()
    plugin._pack_dialog = dlg
    return dlg


def preview_ui(plugin, folder=None, open_browser=True):
    """A published site, opened in the browser from a preview server on this computer."""
    if folder is None:
        folder = QFileDialog.getExistingDirectory(plugin.iface.mainWindow(), tr("Preview Website"),
                                                  QgsSettings().value("biruni/publish-site/dir", _dir()))
        if not folder:
            return None
    if not os.path.isfile(os.path.join(folder, "index.html")):
        QMessageBox.warning(plugin.iface.mainWindow(), tr("Preview Website"),
                            tr("This folder has no index.html: choose the folder of a published site."))
        return None
    url = om.serve(folder)
    if open_browser:
        QDesktopServices.openUrl(QUrl(url))
    _bar(plugin, tr("The site is shown from {u} (on this computer only, while the program is open).").format(u=url),
         seconds=15)
    return url
