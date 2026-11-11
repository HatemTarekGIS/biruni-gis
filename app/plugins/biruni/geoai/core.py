"""The geometry of click-to-digitise: the part of the image a model sees, pixels to map and back, a mask to a
polygon, a building's corners made square. Nothing here needs the model (see model.py)."""
import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsRectangle,
                       QgsWkbTypes)

SIDE = 1024          # the longest side of the image the model sees, in pixels


class Tile:
    """A piece of a raster layer, read at (up to) its own resolution: ``image`` (h, w, 3) uint8, its ``extent``
    in the layer's coordinates."""

    def __init__(self, layer, extent, width, height, image=None):
        self.layer_id = layer.id() if layer is not None else ""
        self.crs = layer.crs() if layer is not None else None
        self.extent = QgsRectangle(extent)
        self.width, self.height = int(width), int(height)
        self.image = image
        self.embedding = None      # the model's view of it, made once (model.Segmenter.encode)

    @property
    def key(self):
        e = self.extent
        return (self.layer_id, round(e.xMinimum(), 6), round(e.yMinimum(), 6), round(e.xMaximum(), 6),
                round(e.yMaximum(), 6), self.width, self.height)

    # pixels (column, row from the top left corner, 0..w, 0..h) and the map, in the layer's coordinates
    def to_pixel(self, x, y):
        e = self.extent
        return ((x - e.xMinimum()) / e.width() * self.width, (e.yMaximum() - y) / e.height() * self.height)

    def to_map(self, col, row):
        e = self.extent
        return e.xMinimum() + col / self.width * e.width(), e.yMaximum() - row / self.height * e.height()

    def contains(self, x, y, margin=0.0):
        e = self.extent
        mx, my = e.width() * margin, e.height() * margin
        return e.xMinimum() + mx <= x <= e.xMaximum() - mx and e.yMinimum() + my <= y <= e.yMaximum() - my

    def geotransform(self):
        e = self.extent
        return (e.xMinimum(), e.width() / self.width, 0.0, e.yMaximum(), 0.0, -e.height() / self.height)


def native_size(layer):
    """The size of one pixel of the raster, in its units (x, y)."""
    if layer.width() and layer.height():
        e = layer.extent()
        return e.width() / layer.width(), e.height() / layer.height()
    return 0.0, 0.0


def plan_tile(layer, at, visible=None, side=SIDE):
    """(extent, width, height) of the piece to read around ``at`` (QgsPointXY in the layer's coordinates): at the
    raster's own resolution, ``side`` pixels at most; the visible extent when it is smaller than that."""
    px, py = native_size(layer)
    full = layer.extent()
    if px <= 0 or py <= 0:                  # a service without its own pixels: what is seen, at ``side``
        ext = QgsRectangle(visible or full)
        scale = max(ext.width(), ext.height()) / side
        return ext, max(1, round(ext.width() / scale)), max(1, round(ext.height() / scale))
    half_w, half_h = side * px / 2.0, side * py / 2.0
    ext = QgsRectangle(at.x() - half_w, at.y() - half_h, at.x() + half_w, at.y() + half_h)
    if visible is not None and visible.width() < ext.width() and visible.height() < ext.height():
        v = QgsRectangle(visible)
        if v.contains(at):
            ext = v
    # kept inside the raster, moved rather than cut where it can be
    if ext.width() <= full.width():
        if ext.xMinimum() < full.xMinimum():
            ext = QgsRectangle(full.xMinimum(), ext.yMinimum(), full.xMinimum() + ext.width(), ext.yMaximum())
        if ext.xMaximum() > full.xMaximum():
            ext = QgsRectangle(full.xMaximum() - ext.width(), ext.yMinimum(), full.xMaximum(), ext.yMaximum())
    if ext.height() <= full.height():
        if ext.yMinimum() < full.yMinimum():
            ext = QgsRectangle(ext.xMinimum(), full.yMinimum(), ext.xMaximum(), full.yMinimum() + ext.height())
        if ext.yMaximum() > full.yMaximum():
            ext = QgsRectangle(ext.xMinimum(), full.yMaximum() - ext.height(), ext.xMaximum(), full.yMaximum())
    ext = ext.intersect(full)
    w, h = max(1, round(ext.width() / px)), max(1, round(ext.height() / py))
    if max(w, h) > side:                    # never more than the model's side
        f = side / max(w, h)
        w, h = max(1, round(w * f)), max(1, round(h * f))
    return ext, w, h


def read_tile(layer, extent, width, height):
    """The piece of ``layer`` as the map shows it (its style: bands, stretch, colours), as an RGB array."""
    renderer = layer.renderer()
    block = renderer.block(1, extent, width, height)
    raw = bytes(block.data())
    if len(raw) != width * height * 4:
        raise ValueError("the raster could not be read")
    argb = np.frombuffer(raw, dtype=np.uint32).reshape(height, width)
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[..., 0] = (argb >> 16) & 0xFF
    rgb[..., 1] = (argb >> 8) & 0xFF
    rgb[..., 2] = argb & 0xFF
    return rgb


def tile_at(layer, at, visible=None, side=SIDE):
    ext, w, h = plan_tile(layer, at, visible, side)
    return Tile(layer, ext, w, h, read_tile(layer, ext, w, h))


# ---- a mask to a polygon --------------------------------------------------------------------------------------
def mask_polygons(mask, tile):
    """The shapes of a boolean mask (h, w), in the tile's (layer's) coordinates: [QgsGeometry]."""
    h, w = mask.shape
    drv = gdal.GetDriverByName("MEM")
    ds = drv.Create("", w, h, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(tile.geotransform())
    band = ds.GetRasterBand(1)
    band.WriteArray(mask.astype(np.uint8))
    odrv = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
    ods = odrv.CreateDataSource("m")
    srs = osr.SpatialReference()
    olyr = ods.CreateLayer("p", srs=srs, geom_type=ogr.wkbPolygon)
    olyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, olyr, 0, ["8CONNECTED=8"])
    out = []
    for f in olyr:
        if f.GetField(0) == 1:
            out.append(QgsGeometry.fromWkt(f.GetGeometryRef().ExportToWkt()))
    return out


def fill_holes(g, min_share=0.02):
    """The shape without its holes smaller than ``min_share`` of its area (a car on a roof is not a courtyard)."""
    if g.isEmpty() or g.type() != QgsWkbTypes.PolygonGeometry:
        return g
    area = g.area()
    parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
    kept = []
    for rings in parts:
        if not rings:
            continue
        holes = [r for r in rings[1:] if QgsGeometry.fromPolygonXY([r]).area() >= min_share * area]
        kept.append([rings[0]] + holes)
    return QgsGeometry.fromMultiPolygonXY(kept) if len(kept) > 1 else QgsGeometry.fromPolygonXY(kept[0])


def mask_to_polygon(mask, tile, at=None, simplify_px=1.0):
    """The one shape of the mask: the one under ``at`` (the first click, in the layer's coordinates), else the
    largest; its small holes filled, its stairs of pixels made straight."""
    polys = mask_polygons(mask, tile)
    if not polys:
        return None
    chosen = None
    if at is not None:
        pt = QgsGeometry.fromPointXY(QgsPointXY(at))
        inside = [p for p in polys if p.contains(pt)]
        if inside:
            chosen = inside[0]
    if chosen is None:
        chosen = max(polys, key=lambda p: p.area())
    chosen = fill_holes(chosen)
    px = tile.extent.width() / tile.width
    s = chosen.simplify(px * simplify_px)
    return s if s is not None and not s.isEmpty() and s.isGeosValid() else chosen


def oriented_rectangle(g):
    r = g.orientedMinimumBoundingBox()
    return r[0] if isinstance(r, tuple) else r


def iou(a, b):
    u = a.combine(b).area()
    return a.intersection(b).area() / u if u else 0.0


def square(g, fit=0.85):
    """A building's corners made right angles: the shape squared, or its rectangle when it is one (``fit``: how
    much of the two must be shared). The shape as it is when neither fits."""
    if g is None or g.isEmpty():
        return g
    rect = oriented_rectangle(g)
    if rect is not None and not rect.isEmpty() and iou(g, rect) >= 0.9:
        return rect
    try:
        o = g.orthogonalize(1.0e-8, 1000, 15.0)
    except TypeError:
        o = g.orthogonalize()
    if o is not None and not o.isEmpty() and o.isGeosValid() and iou(g, o) >= fit:
        return o
    if rect is not None and not rect.isEmpty() and iou(g, rect) >= fit:
        return rect
    return g


def shaped(g, mode):
    """``mode``: "as is", "square" (right angles when they fit), "rectangle" (always its rectangle)."""
    if g is None:
        return None
    if mode == "square":
        return square(g)
    if mode == "rectangle":
        return oriented_rectangle(g)
    return g


def to_layer(g, src_crs, layer, project=None):
    """The shape in the coordinates (and the single or multi kind) of the polygon layer it goes to."""
    g = QgsGeometry(g)
    if src_crs is not None and src_crs != layer.crs():
        g.transform(QgsCoordinateTransform(src_crs, layer.crs(), project or QgsProject.instance()))
    if QgsWkbTypes.isMultiType(layer.wkbType()):
        g.convertToMultiType()
    else:
        g.convertToSingleType()
    return g


def is_image(layer):
    return isinstance(layer, QgsRasterLayer) and layer.isValid()


def model_points(tile, points, scale):
    """Clicks [(x, y, positive)] in the layer's coordinates → the model's coordinates and labels (1 in, 0 out)."""
    coords, labels = [], []
    for x, y, positive in points:
        c, r = tile.to_pixel(x, y)
        coords.append([c * scale, r * scale])
        labels.append(1.0 if positive else 0.0)
    return coords, labels


def box_points(tile, rect, scale):
    c0, r0 = tile.to_pixel(rect.xMinimum(), rect.yMaximum())
    c1, r1 = tile.to_pixel(rect.xMaximum(), rect.yMinimum())
    return [[c0 * scale, r0 * scale], [c1 * scale, r1 * scale]], [2.0, 3.0]


def resize_scale(width, height, side=SIDE):
    return side / max(width, height)
