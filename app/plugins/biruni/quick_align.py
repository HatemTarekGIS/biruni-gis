"""Place an image on the map by hand - live and see-through.

A scan, a PDF page or a photo is laid half-transparent over the map. Drag it to move it, Shift+drag to
turn it, Ctrl+drag to resize it, or click pairs of points (a point on the image, then the same place on
the map) and it snaps into place. Then it is written as a GeoTIFF that any program can read.

The placement is a similarity transform kept as two complex numbers: map = a·(col − i·row) + b, so
moving, turning, scaling and fitting to point pairs are one line each.
"""
import cmath
import math
import os
import tempfile

from osgeo import gdal
from qgis.core import Qgis, QgsPointXY, QgsProject, QgsRasterLayer
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QObject, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                                 QMessageBox, QPushButton, QSlider, QVBoxLayout)

from .i18n import tr
from .version import APP_NAME

gdal.UseExceptions()


class Placement:
    """Where an image of ``width`` × ``height`` pixels lies on the map."""

    def __init__(self, width, height, a=1 + 0j, b=0j):
        self.width, self.height = int(width), int(height)
        self.a, self.b = complex(a), complex(b)

    def copy(self):
        return Placement(self.width, self.height, self.a, self.b)

    # -- construction --
    @classmethod
    def fit_extent(cls, width, height, xmin, ymin, xmax, ymax, fill=0.8):
        s = min((xmax - xmin) / width, (ymax - ymin) / height) * fill
        p = cls(width, height, complex(s, 0), 0j)
        centre = complex((xmin + xmax) / 2, (ymin + ymax) / 2)
        p.b = centre - p.a * complex(width / 2, -height / 2)
        return p

    @classmethod
    def from_pairs(cls, width, height, pairs):
        """Best similarity fit of [((col, row), (x, y)), …] (2 pairs fit exactly, more by least squares)."""
        if len(pairs) < 2:
            raise ValueError("two point pairs are needed")
        w = [complex(c, -r) for (c, r), _ in pairs]
        z = [complex(x, y) for _, (x, y) in pairs]
        wm, zm = sum(w) / len(w), sum(z) / len(z)
        den = sum(abs(wi - wm) ** 2 for wi in w)
        if den == 0:
            raise ValueError("the image points are all at the same place")
        a = sum((zi - zm) * (wi - wm).conjugate() for wi, zi in zip(w, z)) / den
        return cls(width, height, a, zm - a * wm)

    def residuals(self, pairs):
        return [abs(complex(*self.to_map(c, r)) - complex(x, y)) for (c, r), (x, y) in pairs]

    # -- use --
    def to_map(self, col, row):
        z = self.a * complex(col, -row) + self.b
        return z.real, z.imag

    def to_pixel(self, x, y):
        w = (complex(x, y) - self.b) / self.a
        return w.real, -w.imag

    def corners(self):
        return [self.to_map(c, r) for c, r in ((0, 0), (self.width, 0), (self.width, self.height), (0, self.height))]

    def centre(self):
        return self.to_map(self.width / 2, self.height / 2)

    @property
    def pixel_size(self):
        return abs(self.a)

    @property
    def rotation(self):
        """Degrees, counter-clockwise from north-up."""
        return math.degrees(cmath.phase(self.a))

    def geotransform(self):
        ar, ai = self.a.real, self.a.imag
        return (self.b.real, ar, ai, self.b.imag, ai, -ar)

    # -- changes (all about a map point) --
    def move(self, dx, dy):
        self.b += complex(dx, dy)

    def rotate(self, degrees, px, py):
        r = cmath.exp(1j * math.radians(degrees))
        p = complex(px, py)
        self.a *= r
        self.b = r * (self.b - p) + p

    def scale(self, k, px, py):
        if k <= 0:
            return
        p = complex(px, py)
        self.a *= k
        self.b = k * (self.b - p) + p

    def set_rotation(self, degrees):
        cx, cy = self.centre()
        self.rotate(degrees - self.rotation, cx, cy)

    def set_pixel_size(self, size):
        if size > 0:
            cx, cy = self.centre()
            self.scale(size / self.pixel_size, cx, cy)


# ---- files ----------------------------------------------------------------------------------------------
def prepare_image(path, page=1, dpi=200, out_dir=None):
    """A raster GDAL can read for the image: PDF pages are drawn to a GeoTIFF first.
    Returns (raster path, width, height)."""
    src = path
    if path.lower().endswith(".pdf"):
        out_dir = out_dir or tempfile.mkdtemp(prefix="biruni_align_")
        tif = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + f"_p{page}.tif")
        name = path if page == 1 else f"PDF:{page}:{path}"
        ds = gdal.OpenEx(name, open_options=[f"DPI={dpi}"])
        gdal.Translate(tif, ds, format="GTiff", creationOptions=["COMPRESS=DEFLATE"])
        ds = None
        tif_ds = gdal.Open(tif, gdal.GA_Update)  # drop any georeference of a GeoPDF: we place it by hand
        tif_ds.SetGeoTransform((0, 1, 0, 0, 0, 1))
        tif_ds = None
        src = tif
    ds = gdal.Open(src)
    w, h = ds.RasterXSize, ds.RasterYSize
    ds = None
    return src, w, h


def write_vrt(src, placement, crs_wkt, vrt_path):
    """A VRT over the image with the placement's geotransform (fast: no pixels are copied)."""
    gdal.Translate(vrt_path, src, format="VRT")
    ds = gdal.Open(vrt_path, gdal.GA_Update)
    ds.SetGeoTransform(placement.geotransform())
    if crs_wkt:
        ds.SetProjection(crs_wkt)
    ds = None
    return vrt_path


def export_geotiff(src, placement, crs_wkt, out_path, north_up=True, world_file=False):
    """Write the placed image as a GeoTIFF. ``north_up`` resamples it so every program reads it (a turned
    image otherwise needs a rotated geotransform, which CAD programs ignore)."""
    tmp = tempfile.mkdtemp(prefix="biruni_align_")
    vrt = write_vrt(src, placement, crs_wkt, os.path.join(tmp, "placed.vrt"))
    co = ["COMPRESS=DEFLATE", "TILED=YES"]
    if north_up and abs(placement.a.imag) > 1e-12 * abs(placement.a):
        gdal.Warp(out_path, vrt, format="GTiff", dstSRS=crs_wkt or None, dstAlpha=True, resampleAlg="bilinear",
                  creationOptions=co + (["TFW=YES"] if world_file else []))
    else:
        gdal.Translate(out_path, vrt, format="GTiff", creationOptions=co + (["TFW=YES"] if world_file else []))
    return out_path


# ---- live placement on the map ---------------------------------------------------------------------------
class AlignTool(QgsMapTool):
    """Drag = move, Shift+drag = turn, Ctrl+drag = resize. In two-point mode: click on the image, then on
    the map where that point belongs; two pairs or more place the image."""

    def __init__(self, canvas, aligner):
        super().__init__(canvas)
        self.aligner = aligner
        self.start = None
        self.base = None
        self.mode = "move"
        self.pairs = []
        self.pending = None   # image pixel waiting for its map point
        self.two_point = False

    def set_two_point(self, on):
        self.two_point = on
        self.pairs, self.pending = [], None
        self.aligner.show_pairs(self.pairs, None)

    def canvasPressEvent(self, e):  # noqa: N802
        p = self.toMapCoordinates(e.pos())
        if self.two_point:
            if e.button() != Qt.MouseButton.LeftButton:
                return
            if self.pending is None:
                self.pending = self.aligner.placement.to_pixel(p.x(), p.y())
            else:
                self.pairs.append((self.pending, (p.x(), p.y())))
                self.pending = None
                if len(self.pairs) >= 2:
                    self.aligner.fit_pairs(self.pairs)
            self.aligner.show_pairs(self.pairs, self.pending)
            return
        mods = e.modifiers()
        self.mode = "rotate" if mods & Qt.KeyboardModifier.ShiftModifier else \
            "scale" if mods & Qt.KeyboardModifier.ControlModifier else "move"
        self.start = p
        self.base = self.aligner.placement.copy()

    def _dragged(self, p):
        pl = self.base.copy()
        cx, cy = pl.centre()
        if self.mode == "move":
            pl.move(p.x() - self.start.x(), p.y() - self.start.y())
        elif self.mode == "rotate":
            a0 = math.atan2(self.start.y() - cy, self.start.x() - cx)
            a1 = math.atan2(p.y() - cy, p.x() - cx)
            pl.rotate(math.degrees(a1 - a0), cx, cy)
        else:
            d0 = math.hypot(self.start.x() - cx, self.start.y() - cy)
            d1 = math.hypot(p.x() - cx, p.y() - cy)
            if d0 > 0 and d1 > 0:
                pl.scale(d1 / d0, cx, cy)
        return pl

    def canvasMoveEvent(self, e):  # noqa: N802
        if self.start is None:
            return
        self.aligner.preview(self._dragged(self.toMapCoordinates(e.pos())))

    def canvasReleaseEvent(self, e):  # noqa: N802
        if self.start is None:
            return
        pl = self._dragged(self.toMapCoordinates(e.pos()))
        self.start = self.base = None
        self.aligner.set_placement(pl)

    def keyPressEvent(self, e):  # noqa: N802
        if self.two_point and e.key() == Qt.Key.Key_Backspace:
            if self.pending is not None:
                self.pending = None
            elif self.pairs:
                self.pairs.pop()
                if len(self.pairs) >= 2:
                    self.aligner.fit_pairs(self.pairs)
            self.aligner.show_pairs(self.pairs, self.pending)
        elif e.key() == Qt.Key.Key_Escape:
            if self.two_point:
                self.aligner.panel_set_two_point(False)
            else:
                self.aligner.close()


class QuickAligner(QObject):
    """Keeps the see-through image layer, the outline and the tool in step."""

    def __init__(self, iface, image_path, page=1, quiet_ids=None):
        super().__init__()
        self.iface = iface
        # ids the project manager should not offer to copy into the project (the preview lives in a temp folder)
        self.quiet_ids = quiet_ids
        self.canvas = iface.mapCanvas()
        self.src, w, h = prepare_image(image_path, page)
        self.name = os.path.splitext(os.path.basename(image_path))[0]
        ext = self.canvas.extent()
        self.placement = Placement.fit_extent(w, h, ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
        self.crs = self.canvas.mapSettings().destinationCrs()
        self.tmp = tempfile.mkdtemp(prefix="biruni_align_")
        self._flip = 0
        self.layer = None
        self.outline = QgsRubberBand(self.canvas, Qgis.GeometryType.Polygon)
        self.outline.setStrokeColor(QColor(230, 80, 20))
        self.outline.setFillColor(QColor(0, 0, 0, 0))
        self.outline.setWidth(2)
        self.links = QgsRubberBand(self.canvas, Qgis.GeometryType.Line)
        self.links.setStrokeColor(QColor(20, 120, 230))
        self.links.setWidth(2)
        self.opacity = 0.5
        self.tool = AlignTool(self.canvas, self)
        self.previous_tool = self.canvas.mapTool()
        self.panel = None
        self._update_layer()
        self._draw_outline(self.placement)
        self.canvas.setMapTool(self.tool)

    # -- drawing --
    def _vrt(self):
        self._flip = 1 - self._flip  # a new file name each time: GDAL caches opened files
        return write_vrt(self.src, self.placement, self.crs.toWkt(), os.path.join(self.tmp, f"placed_{self._flip}.vrt"))

    def _update_layer(self):
        vrt = self._vrt()
        if self.layer is None:
            self.layer = QgsRasterLayer(vrt, tr("{n} (placing)").format(n=self.name), "gdal")
            self.layer.setCrs(self.crs)
            self.layer.renderer().setOpacity(self.opacity)
            if self.quiet_ids is not None:
                self.quiet_ids.add(self.layer.id())
            QgsProject.instance().addMapLayer(self.layer)
        else:
            self.layer.setDataSource(vrt, self.layer.name(), "gdal")
            self.layer.setCrs(self.crs)
            self.layer.renderer().setOpacity(self.opacity)
            self.layer.triggerRepaint()
        if self.panel is not None:
            self.panel.sync()

    def _draw_outline(self, pl):
        self.outline.reset(Qgis.GeometryType.Polygon)
        corners = pl.corners()
        for k, (x, y) in enumerate(corners):
            self.outline.addPoint(QgsPointXY(x, y), k == len(corners) - 1)
        self.outline.show()

    def preview(self, pl):
        self._draw_outline(pl)

    def set_placement(self, pl):
        self.placement = pl
        self._draw_outline(pl)
        self._update_layer()

    def fit_pairs(self, pairs):
        self.set_placement(Placement.from_pairs(self.placement.width, self.placement.height, pairs))

    def show_pairs(self, pairs, pending):
        self.links.reset(Qgis.GeometryType.Line)
        for k, ((c, r), (x, y)) in enumerate(pairs):
            sx, sy = self.placement.to_map(c, r)
            self.links.addPoint(QgsPointXY(sx, sy), False, k)
            self.links.addPoint(QgsPointXY(x, y), True, k)
        if self.panel is not None:
            self.panel.show_pairs(pairs, pending)

    def set_opacity(self, value):
        self.opacity = value
        if self.layer is not None:
            self.layer.renderer().setOpacity(value)
            self.layer.triggerRepaint()

    def panel_set_two_point(self, on):
        self.tool.set_two_point(on)
        if self.panel is not None:
            self.panel.two_point.setChecked(on)

    # -- finishing --
    def save(self, out_path, north_up=True, world_file=False):
        export_geotiff(self.src, self.placement, self.crs.toWkt(), out_path, north_up, world_file)
        self.close()
        lyr = QgsRasterLayer(out_path, os.path.splitext(os.path.basename(out_path))[0], "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
        return out_path

    def close(self):
        self.outline.reset(Qgis.GeometryType.Polygon)
        self.links.reset(Qgis.GeometryType.Line)
        self.canvas.scene().removeItem(self.outline)
        self.canvas.scene().removeItem(self.links)
        if self.canvas.mapTool() is self.tool:
            self.canvas.setMapTool(self.previous_tool) if self.previous_tool else self.canvas.unsetMapTool(self.tool)
        if self.layer is not None and QgsProject.instance().mapLayer(self.layer.id()):
            QgsProject.instance().removeMapLayer(self.layer.id())
        self.layer = None
        if self.panel is not None:
            panel, self.panel = self.panel, None
            panel.close()


class AlignPanel(QDialog):
    """Small floating window next to the map while placing an image."""

    def __init__(self, aligner, parent=None):
        super().__init__(parent)
        self.aligner = aligner
        aligner.panel = self
        self.setWindowTitle(tr("Place the image on the map"))
        self.setModal(False)
        v = QVBoxLayout(self)
        help_ = QLabel(tr("Drag to move · Shift+drag to turn · Ctrl+drag to resize.\nOr use two points: click a "
                          "point on the image, then the same place on the map - twice or more."))
        help_.setWordWrap(True)
        v.addWidget(help_)
        form = QFormLayout()
        self.op = QSlider(Qt.Orientation.Horizontal)
        self.op.setRange(10, 100)
        self.op.setValue(int(aligner.opacity * 100))
        self.op.valueChanged.connect(lambda n: aligner.set_opacity(n / 100))
        form.addRow(tr("See-through"), self.op)
        self.rot = QDoubleSpinBox()
        self.rot.setRange(-360, 360)
        self.rot.setDecimals(3)
        self.rot.setSuffix("°")
        self.rot.editingFinished.connect(self._set_rotation)
        form.addRow(tr("Rotation"), self.rot)
        self.px = QDoubleSpinBox()
        self.px.setRange(0.000001, 1e9)
        self.px.setDecimals(6)
        self.px.editingFinished.connect(self._set_size)
        form.addRow(tr("Pixel size (map units)"), self.px)
        v.addLayout(form)
        row = QHBoxLayout()
        self.two_point = QPushButton(tr("Two points"))
        self.two_point.setCheckable(True)
        self.two_point.toggled.connect(aligner.tool.set_two_point)
        fit = QPushButton(tr("Fit to the view"))
        fit.clicked.connect(self._fit)
        row.addWidget(self.two_point)
        row.addWidget(fit)
        v.addLayout(row)
        self.pairs_label = QLabel()
        self.pairs_label.setWordWrap(True)
        v.addWidget(self.pairs_label)
        row = QHBoxLayout()
        save = QPushButton(tr("Save as GeoTIFF…"))
        save.setDefault(True)
        save.clicked.connect(self._save)
        cancel = QPushButton(tr("Cancel"))
        cancel.clicked.connect(aligner.close)
        row.addWidget(save)
        row.addWidget(cancel)
        v.addLayout(row)
        self.sync()

    def sync(self):
        pl = self.aligner.placement
        self.rot.blockSignals(True)
        self.px.blockSignals(True)
        self.rot.setValue(pl.rotation)
        self.px.setValue(pl.pixel_size)
        self.rot.blockSignals(False)
        self.px.blockSignals(False)

    def show_pairs(self, pairs, pending):
        if not pairs and pending is None:
            self.pairs_label.setText("")
            return
        text = tr("{n} point pairs").format(n=len(pairs))
        if len(pairs) >= 2:
            res = self.aligner.placement.residuals(pairs)
            text += " · " + tr("largest error {e:.3f} map units").format(e=max(res))
        if pending is not None:
            text += " · " + tr("now click the same place on the map")
        self.pairs_label.setText(text)

    def _set_rotation(self):
        pl = self.aligner.placement.copy()
        pl.set_rotation(self.rot.value())
        self.aligner.set_placement(pl)

    def _set_size(self):
        pl = self.aligner.placement.copy()
        pl.set_pixel_size(self.px.value())
        self.aligner.set_placement(pl)

    def _fit(self):
        ext = self.aligner.canvas.extent()
        pl = self.aligner.placement
        self.aligner.set_placement(Placement.fit_extent(pl.width, pl.height, ext.xMinimum(), ext.yMinimum(),
                                                        ext.xMaximum(), ext.yMaximum()))

    def _save(self):
        folder = os.path.dirname(self.aligner.src)
        try:
            from .project import project_folder

            pf = project_folder()
            folder = os.path.join(pf, "data") if pf else folder
        except Exception:  # noqa: BLE001
            pass
        path, _ = QFileDialog.getSaveFileName(self, tr("Save as GeoTIFF…"),
                                              os.path.join(folder, self.aligner.name + "_placed.tif"),
                                              "GeoTIFF (*.tif)")
        if not path:
            return
        try:
            self.aligner.save(path, north_up=True, world_file=True)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self.aligner.iface.messageBar().pushSuccess(APP_NAME, tr("Saved: {p}").format(p=path))

    def closeEvent(self, e):  # noqa: N802
        if self.aligner.panel is self:
            self.aligner.panel = None
            self.aligner.close()
        super().closeEvent(e)


def start(plugin, path=None, page=1):
    """Ask for an image (unless given) and start placing it."""
    iface = plugin.iface
    if not path:
        path, _ = QFileDialog.getOpenFileName(iface.mainWindow(), tr("Choose a scan, a PDF or a photo"), "",
                                              tr("Images and PDF") + " (*.tif *.tiff *.png *.jpg *.jpeg *.bmp "
                                                                     "*.gif *.pdf)")
        if not path:
            return None
    try:
        aligner = QuickAligner(iface, path, page, getattr(getattr(plugin, "project", None), "_offered", None))
    except Exception as e:  # noqa: BLE001
        QMessageBox.warning(iface.mainWindow(), APP_NAME, tr("The image could not be opened: {e}").format(e=e))
        return None
    panel = AlignPanel(aligner, iface.mainWindow())
    panel.show()
    plugin._quick_align = aligner  # keep a Python reference while the tool is in use
    return aligner
