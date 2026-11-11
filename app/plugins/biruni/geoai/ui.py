"""Click-to-digitise: click a building (or a field, a pool, a road) on an image and its outline is drawn.

Left click: a point in the shape; right click: a point outside it; drag: a box around it. Enter (or «Add»)
puts the shape in the chosen polygon layer, in its edits (undo, save as with any edit); Esc clears; Backspace
takes the last point back. The model runs on this computer, offline: it is downloaded once, with consent.
"""
import os

from qgis.core import (Qgis, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer,
                       QgsWkbTypes)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import core, model


def _bar(plugin, text, level=Qgis.MessageLevel.Info, seconds=8):
    plugin.iface.messageBar().pushMessage(APP_NAME, text, level, seconds)


# ---- getting the model ---------------------------------------------------------------------------------------
def ensure_model(plugin, fetch=None, ask=True):
    """True when the model is there (downloaded or imported now, with the user's consent)."""
    if model.installed():
        return True
    if not model.runtime_ok():
        QMessageBox.warning(plugin.iface.mainWindow(), tr("Smart Digitise"),
                            tr("The engine that runs local models is missing from this installation."))
        return False
    m = model.MODEL
    mb = model.total_size() / 1e6
    box = QMessageBox(plugin.iface.mainWindow())
    box.setWindowTitle(tr("Smart Digitise"))
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(tr("Smart Digitise needs a model that runs on this computer, offline: {n}, {s:.0f} MB, "
                   "downloaded once.").format(n=m["name"], s=mb))
    box.setInformativeText(tr("Licence: {l}, free for any use, companies and governments included.\nSource: {u}\n"
                              "Every file is checked against its fingerprint (SHA-256) before it is kept. Nothing "
                              "of your data is sent.").format(l=m["license"], u=m["source"]))
    dl = box.addButton(tr("Download"), QMessageBox.ButtonRole.AcceptRole)
    imp = box.addButton(tr("Import from a Folder…"), QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    if not ask:
        return False
    box.exec()
    if box.clickedButton() is dl:
        return download_ui(plugin, fetch)
    if box.clickedButton() is imp:
        folder = QFileDialog.getExistingDirectory(plugin.iface.mainWindow(), tr("The folder with the model files"))
        return import_ui(plugin, folder) if folder else False
    return False


def download_ui(plugin, fetch=None):
    prog = QProgressDialog(tr("Downloading the model…"), tr("Cancel"), 0, 1000, plugin.iface.mainWindow())
    prog.setWindowModality(Qt.WindowModality.WindowModal)
    prog.setMinimumDuration(0)

    def each(done, total):
        prog.setValue(int(1000 * done / max(total, 1)))
        QApplication.processEvents()
        return not prog.wasCanceled()
    try:
        model.download(fetch=fetch or model.http_get, progress=each)
    except model.ModelError as e:
        prog.close()
        why = {"offline": tr("The model could not be downloaded: check the internet connection, or bring its files "
                             "on a flash drive and import them."),
               "checksum": tr("A downloaded file is not the published one (its fingerprint differs): it was not "
                              "kept."),
               "cancelled": tr("Download cancelled.")}.get(str(e), str(e))
        _bar(plugin, why, Qgis.MessageLevel.Warning, 12)
        return False
    prog.close()
    _bar(plugin, tr("The model is ready: it works offline from now on."), Qgis.MessageLevel.Success)
    return True


def import_ui(plugin, folder):
    try:
        model.import_from(folder)
    except model.ModelError as e:
        msg = str(e)
        if msg == "checksum":
            msg = tr("A file in the folder is not the published one (its fingerprint differs).")
        elif msg.startswith("missing"):
            msg = tr("The folder does not have the model files: {f}").format(f=", ".join(
                s["name"] for s in model.MODEL["files"]))
        _bar(plugin, msg, Qgis.MessageLevel.Warning, 12)
        return False
    _bar(plugin, tr("The model is ready: it works offline from now on."), Qgis.MessageLevel.Success)
    return True


# ---- the map tool ----------------------------------------------------------------------------------------------
class SegmentTool(QgsMapTool):
    """Clicks on the canvas → points (and a box) for the model; the shape previewed as it is found."""

    def __init__(self, canvas, panel):
        super().__init__(canvas)
        self.panel = panel
        self._press = None
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(42, 157, 143, 200))
        self.band.setFillColor(QColor(42, 157, 143, 60))
        self.band.setWidth(2)
        self.marks = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self.marks.setColor(QColor(230, 57, 70))
        self.marks.setIconSize(9)
        self.boxband = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.boxband.setColor(QColor(244, 162, 97, 200))
        self.boxband.setFillColor(QColor(0, 0, 0, 0))
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, e):  # noqa: N802
        self._press = (e.pos(), e.button(), self.toMapCoordinates(e.pos()))

    def canvasMoveEvent(self, e):  # noqa: N802
        if self._press and self._press[1] == Qt.MouseButton.LeftButton and \
                (e.pos() - self._press[0]).manhattanLength() > 6:
            p0, p1 = self._press[2], self.toMapCoordinates(e.pos())
            self.boxband.setToGeometry(QgsGeometry.fromRect(QgsRectangle(p0, p1)), None)

    def canvasReleaseEvent(self, e):  # noqa: N802
        if not self._press:
            return
        pos0, button, p0 = self._press
        self._press = None
        p1 = self.toMapCoordinates(e.pos())
        self.boxband.reset(QgsWkbTypes.PolygonGeometry)
        try:
            if button == Qt.MouseButton.LeftButton and (e.pos() - pos0).manhattanLength() > 6:
                self.panel.set_box(QgsRectangle(p0, p1))
            elif button in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
                self.panel.add_point(p1, button == Qt.MouseButton.LeftButton)
        except Exception as ex:  # noqa: BLE001 - a slot that raises takes the program down
            self.panel.say(str(ex))

    def keyPressEvent(self, e):  # noqa: N802
        k = e.key()
        try:
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.panel.accept_shape()
            elif k == Qt.Key.Key_Escape:
                self.panel.clear()
            elif k == Qt.Key.Key_Backspace:
                self.panel.undo_point()
        except Exception as ex:  # noqa: BLE001
            self.panel.say(str(ex))

    def show(self, geom, points, crs):
        """The shape and the clicks, drawn (``crs``: of the shape and the clicks)."""
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.marks.reset(QgsWkbTypes.PointGeometry)
        if geom is not None and not geom.isEmpty():
            self.band.setToGeometry(geom, crs)
        for x, y, _pos in points:
            self.marks.addGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)), crs)

    def deactivate(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.boxband.reset(QgsWkbTypes.PolygonGeometry)
        self.marks.reset(QgsWkbTypes.PointGeometry)
        super().deactivate()


class SmartDigitisePanel(QDialog):
    def __init__(self, plugin, segmenter=None):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.canvas = plugin.iface.mapCanvas()
        self.seg = segmenter
        self.tile = None
        self.points = []           # (x, y, positive) in the image's coordinates
        self.box = None
        self.shape = None          # in the image's coordinates
        self.setWindowTitle(tr("Smart Digitise"))
        self.setMinimumWidth(380)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Click a building, a field or a pool on the image: its outline is drawn. Left click: inside "
                          "it; right click: outside it; drag: a box around it. Enter adds it, Esc clears, Backspace "
                          "takes the last click back. Runs on this computer, offline."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.image = QComboBox()
        self.target = QComboBox()
        self.mode = QComboBox()
        self.mode.addItem(tr("As drawn"), "as is")
        self.mode.addItem(tr("Square the corners"), "square")
        self.mode.addItem(tr("Rectangle"), "rectangle")
        self.mode.setCurrentIndex(1)
        form.addRow(tr("The image"), self.image)
        form.addRow(tr("Add to"), self.target)
        form.addRow(tr("Corners"), self.mode)
        v.addLayout(form)
        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        row = QHBoxLayout()
        self.add_btn = QPushButton(tr("Add (Enter)"))
        self.add_btn.clicked.connect(lambda: self._safe(self.accept_shape))
        clear = QPushButton(tr("Clear (Esc)"))
        clear.clicked.connect(lambda: self._safe(self.clear))
        row.addWidget(self.add_btn)
        row.addWidget(clear)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.close)
        v.addWidget(bb)
        self.mode.currentIndexChanged.connect(lambda _i: self._safe(self.refresh))
        self.fill()
        self.tool = SegmentTool(self.canvas, self)
        self._previous = self.canvas.mapTool()
        self.canvas.setMapTool(self.tool)

    def _safe(self, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 - a slot that raises takes the program down
            self.say(str(ex))

    def say(self, text):
        self.out.setText(text)

    def fill(self):
        self.image.clear()
        self.target.clear()
        layers = list(QgsProject.instance().mapLayers().values())
        for lyr in layers:
            if core.is_image(lyr):
                self.image.addItem(lyr.name(), lyr.id())
            elif isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
                self.target.addItem(lyr.name(), lyr.id())
        act = self.plugin.iface.activeLayer()
        if act is not None:
            for combo in (self.image, self.target):
                i = combo.findData(act.id())
                if i >= 0:
                    combo.setCurrentIndex(i)

    def image_layer(self):
        return QgsProject.instance().mapLayer(self.image.currentData() or "")

    def target_layer(self):
        return QgsProject.instance().mapLayer(self.target.currentData() or "")

    def _to_image(self, p):
        """A canvas point in the image's coordinates."""
        lyr = self.image_layer()
        dest = self.canvas.mapSettings().destinationCrs()
        if lyr is None or lyr.crs() == dest:
            return QgsPointXY(p)
        from qgis.core import QgsCoordinateTransform
        return QgsCoordinateTransform(dest, lyr.crs(), QgsProject.instance()).transform(QgsPointXY(p))

    def _visible(self):
        lyr = self.image_layer()
        ext = self.canvas.extent()
        dest = self.canvas.mapSettings().destinationCrs()
        if lyr is not None and lyr.crs() != dest:
            from qgis.core import QgsCoordinateTransform
            ext = QgsCoordinateTransform(dest, lyr.crs(), QgsProject.instance()).transformBoundingBox(ext)
        return ext

    def _segmenter(self):
        if self.seg is None:
            self.seg = model.Segmenter()
        return self.seg

    def _tile_for(self, p):
        lyr = self.image_layer()
        if lyr is None:
            raise ValueError(tr("Choose the image first."))
        if self.tile is not None and self.tile.layer_id == lyr.id() and self.tile.contains(p.x(), p.y(), 0.02):
            return self.tile
        if self.points:        # a click outside the piece being worked on: a new shape there
            self.points, self.box, self.shape = [], None, None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.tile = core.tile_at(lyr, p, self._visible())
            self._segmenter().encode(self.tile)
        finally:
            QApplication.restoreOverrideCursor()
        return self.tile

    def add_point(self, map_point, positive=True):
        p = self._to_image(map_point)
        tile = self._tile_for(p)
        self.points.append((p.x(), p.y(), positive))
        self._run(tile)

    def set_box(self, rect):
        a, b = self._to_image(QgsPointXY(rect.xMinimum(), rect.yMinimum())), \
            self._to_image(QgsPointXY(rect.xMaximum(), rect.yMaximum()))
        r = QgsRectangle(a, b)
        tile = self._tile_for(r.center())
        self.box = r
        self._run(tile)

    def undo_point(self):
        if self.points:
            self.points.pop()
        if self.tile is not None and (self.points or self.box):
            self._run(self.tile)
        else:
            self.clear()

    def _run(self, tile):
        mask, score = self._segmenter().segment(tile, self.points, self.box)
        at = None
        pos = [p for p in self.points if p[2]]
        if pos:
            at = QgsPointXY(pos[0][0], pos[0][1])
        elif self.box is not None:
            at = self.box.center()
        self.raw = core.mask_to_polygon(mask, tile, at)
        self.score = score
        self.refresh()

    def refresh(self):
        lyr = self.image_layer()
        self.shape = core.shaped(self.raw, self.mode.currentData()) if getattr(self, "raw", None) is not None else None
        self.tool.show(self.shape, self.points, lyr.crs() if lyr is not None else None)
        if self.shape is None:
            self.say(tr("Nothing found there: click inside the shape, or drag a box around it."))
            return
        s = self._segmenter()
        self.say(tr("Found: {a:.1f} m², sure at {q:.0f}%. Seen in {e:.1f} s, drawn in {d:.2f} s.").format(
            a=self.shape.area(), q=min(100.0, max(0.0, self.score * 100)), e=s.last_encode, d=s.last_decode))

    def clear(self):
        self.points, self.box, self.shape, self.raw = [], None, None, None
        self.tool.show(None, [], None)
        self.say("")

    def accept_shape(self):
        lyr, img = self.target_layer(), self.image_layer()
        if self.shape is None:
            self.say(tr("Click a shape on the image first."))
            return None
        if lyr is None:
            self.say(tr("Choose the polygon layer to add to."))
            return None
        g = core.to_layer(self.shape, img.crs() if img is not None else None, lyr)
        if not lyr.isEditable() and not lyr.startEditing():
            self.say(tr("«{l}» cannot be edited.").format(l=lyr.name()))
            return None
        from qgis.core import QgsFeature, QgsVectorLayerUtils
        f = QgsVectorLayerUtils.createFeature(lyr, g, {}, lyr.createExpressionContext())
        if not isinstance(f, QgsFeature):
            f = QgsFeature(lyr.fields())
            f.setGeometry(g)
        lyr.beginEditCommand(tr("Smart Digitise"))
        ok = lyr.addFeature(f)
        lyr.endEditCommand()
        lyr.triggerRepaint()
        self.points, self.box, self.raw = [], None, None
        self.tool.show(None, [], None)
        self.say(tr("Added to «{l}» (in its edits: save or undo as with any edit).").format(l=lyr.name()) if ok
                 else tr("«{l}» did not take the shape.").format(l=lyr.name()))
        added = self.shape
        self.shape = None
        return added if ok else None

    def closeEvent(self, e):  # noqa: N802
        try:
            if self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
                if self._previous is not None:
                    self.canvas.setMapTool(self._previous)
        except RuntimeError:
            pass
        super().closeEvent(e)


def open_ui(plugin, segmenter=None, fetch=None):
    layers = [l for l in QgsProject.instance().mapLayers().values() if core.is_image(l)]
    if not layers:
        _bar(plugin, tr("Add an image (an orthophoto) to the map first."), Qgis.MessageLevel.Warning)
        return None
    if segmenter is None and not ensure_model(plugin, fetch):
        return None
    panel = SmartDigitisePanel(plugin, segmenter)
    panel.show()
    plugin._smart_digitise = panel
    return panel


def model_ui(plugin):
    """Where the model is, and a way to take it off this computer."""
    d = model.model_dir()
    if not model.installed():
        return ensure_model(plugin)
    size = sum(os.path.getsize(os.path.join(d, s["name"])) for s in model.MODEL["files"]) / 1e6
    r = QMessageBox.question(plugin.iface.mainWindow(), tr("Smart Digitise"),
                             tr("The model is on this computer ({s:.0f} MB, in {d}). Remove it?").format(s=size, d=d))
    if r == QMessageBox.StandardButton.Yes:
        model.remove()
        return False
    return True
