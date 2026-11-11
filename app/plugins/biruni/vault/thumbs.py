"""Small pictures for the Vault's cards: the shapes of a vector file, an overview of a raster, the first page of a
layout, field photos. Each is drawn once and kept on disk in the profile's cache folder, keyed by the file's path,
its date and size - a changed file gets a new picture, an unchanged one is never drawn again.

make_vector / make_raster / make_photo run in the worker thread (QImage and QPainter are safe there);
layout_image needs the main thread (it renders the layout's maps)."""
import hashlib
import itertools
import os
import threading

from qgis.PyQt.QtCore import QPointF, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QImage, QImageReader, QPainter, QPainterPath, QPen, QPolygonF

SIZE = 128            # pixels of a stored picture (cards show it at about half: sharp on high-DPI screens)
VERSION = "v3"        # bump when the drawing changes
MAX_FEATURES = 4000
MAX_POINTS = 250000
_names = itertools.count()


def cache_dir():
    from qgis.core import QgsApplication

    return os.path.join(QgsApplication.qgisSettingsDirPath(), "cache", "biruni-vault")


def file_key(path, sub="", extra=""):
    """The cache key of a file's picture: path + layer + date + size (+ colour …)."""
    try:
        st = os.stat(path)
        stamp = f"{st.st_mtime:.3f}|{st.st_size}"
    except OSError:
        stamp = "missing"
    raw = f"{VERSION}|{os.path.normcase(os.path.abspath(path))}|{sub}|{stamp}|{extra}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ThumbCache:
    """Pictures on disk (<cache>/<key>.png). Counts what it made and what it found, so reuse can be measured."""

    def __init__(self, folder=None):
        self.folder = folder or cache_dir()
        self.made = 0
        self.reused = 0
        self._lock = threading.Lock()

    def path(self, key):
        return os.path.join(self.folder, key + ".png")

    def load(self, key):
        p = self.path(key)
        if not os.path.exists(p):
            return None
        img = QImage(p)
        if img.isNull():
            return None
        with self._lock:
            self.reused += 1
        return img

    def save(self, key, img):
        if img is None or img.isNull():
            return
        try:
            os.makedirs(self.folder, exist_ok=True)
            tmp = self.path(key) + f".{threading.get_ident()}.tmp"
            if img.save(tmp, "PNG"):
                os.replace(tmp, self.path(key))
        except OSError:
            pass
        with self._lock:
            self.made += 1

    def get_or_make(self, key, maker, *args):
        """The stored picture, or a new one made by maker(*args) (then stored). None when nothing can be drawn."""
        img = self.load(key)
        if img is not None:
            return img
        img = maker(*args)
        if img is not None and not img.isNull():
            self.save(key, img)
        return img


# ---- drawing (worker thread) -----------------------------------------------------------------------------------
def _blank(w=SIZE, h=SIZE):
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    return img


def _parts(ogr, g, out, budget):
    """Flattens an OGR geometry into ("pt"|"ln"|"pg", coordinates) parts."""
    if g is None or budget[0] <= 0:
        return
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbPoint:
        out.append(("pt", [(g.GetX(), g.GetY())]))
        budget[0] -= 1
    elif t in (ogr.wkbLineString, ogr.wkbLinearRing):
        pts = g.GetPoints() or []
        out.append(("ln", pts))
        budget[0] -= len(pts)
    elif t == ogr.wkbPolygon:
        rings = []
        for i in range(g.GetGeometryCount()):
            pts = g.GetGeometryRef(i).GetPoints() or []
            rings.append(pts)
            budget[0] -= len(pts)
        out.append(("pg", rings))
    elif t in (ogr.wkbMultiPoint, ogr.wkbMultiLineString, ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for i in range(g.GetGeometryCount()):
            _parts(ogr, g.GetGeometryRef(i), out, budget)
    else:  # curves, surfaces …
        try:
            lin = g.GetLinearGeometry()
        except Exception:  # noqa: BLE001
            lin = None
        if lin is not None and ogr.GT_Flatten(lin.GetGeometryType()) != t:
            _parts(ogr, lin, out, budget)


def make_vector(path, layer_name, color):
    """The shapes of a vector layer (the first one when layer_name is empty), fitted in the picture."""
    from .scan import _gdal

    gdal, ogr, _osr = _gdal()
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
    except RuntimeError:
        return None
    if ds is None or not ds.GetLayerCount():
        return None
    lyr = ds.GetLayerByName(layer_name) if layer_name else None
    if lyr is None:
        lyr = ds.GetLayer(0)
    parts, budget = [], [MAX_POINTS]
    lyr.ResetReading()
    for k, feat in enumerate(lyr):
        if k >= MAX_FEATURES or budget[0] <= 0:
            break
        _parts(ogr, feat.GetGeometryRef(), parts, budget)
    xs = [p[0] for kind, coords in parts for c in ([coords] if kind != "pg" else coords)
          for p in (c if kind != "pt" else c)]
    ys = [p[1] for kind, coords in parts for c in ([coords] if kind != "pg" else coords)
          for p in (c if kind != "pt" else c)]
    if not xs:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w, h = x1 - x0, y1 - y0
    w, h = w or h or 1.0, h or w or 1.0  # points on one line: keep them centred
    if x1 == x0:
        x0 -= w / 2
    if y1 == y0:
        y0 -= h / 2
    pad = 10
    scale = (SIZE - 2 * pad) / max(w, h)
    ox = pad + ((SIZE - 2 * pad) - w * scale) / 2
    oy = pad + ((SIZE - 2 * pad) - h * scale) / 2

    def pt(p):
        return QPointF(ox + (p[0] - x0) * scale, SIZE - (oy + (p[1] - y0) * scale))

    img = _blank()
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    fill = QColor(c)
    fill.setAlpha(70)
    painter.setPen(QPen(c, 1.6))
    painter.setBrush(QBrush(fill))
    dots = []
    for kind, coords in parts:
        if kind == "pg":
            path_ = QPainterPath()
            path_.setFillRule(Qt.FillRule.OddEvenFill)
            for ring in coords:
                if len(ring) > 1:
                    path_.addPolygon(QPolygonF([pt(p) for p in ring]))
                    path_.closeSubpath()
            painter.drawPath(path_)
        elif kind == "ln":
            if len(coords) > 1:
                painter.drawPolyline(QPolygonF([pt(p) for p in coords]))
        else:
            dots.append(pt(coords[0]))
    if dots:
        r = 3.2 if len(dots) < 400 else 2.0
        painter.setPen(QPen(QColor("#FFFFFF"), 0.9))
        painter.setBrush(QBrush(c))
        for d in dots:
            painter.drawEllipse(d, r, r)
    painter.end()
    return img


def make_raster(path):
    """An overview of a raster (its first three bands, or the first one, stretched to its range)."""
    from .scan import _gdal

    gdal, _ogr, _osr = _gdal()
    try:
        ds = gdal.Open(path)
    except RuntimeError:
        return None
    if ds is None or not ds.RasterCount:
        return None
    w, h = ds.RasterXSize, ds.RasterYSize
    if w <= 0 or h <= 0:
        return None
    k = SIZE / max(w, h)
    tw, th = max(1, round(w * k)), max(1, round(h * k))
    bands = [1, 2, 3] if ds.RasterCount >= 3 else [1]
    byte = all(ds.GetRasterBand(b).DataType == gdal.GDT_Byte for b in bands)
    name = f"/vsimem/biruni_vault_{next(_names)}_{threading.get_ident()}.png"
    try:
        opts = gdal.TranslateOptions(format="PNG", width=tw, height=th, bandList=bands, outputType=gdal.GDT_Byte,
                                     scaleParams=None if byte else [[] for _ in bands],
                                     resampleAlg="average")
        gdal.Translate(name, ds, options=opts).FlushCache()  # (the result is closed when dropped)
        f = gdal.VSIFOpenL(name, "rb")
        if f is None:
            return None
        gdal.VSIFSeekL(f, 0, 2)
        n = gdal.VSIFTellL(f)
        gdal.VSIFSeekL(f, 0, 0)
        data = gdal.VSIFReadL(1, n, f)
        gdal.VSIFCloseL(f)
    except RuntimeError:
        return None
    finally:
        try:
            gdal.Unlink(name)
        except RuntimeError:
            pass
    img = QImage.fromData(bytes(data))
    return None if img.isNull() else img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def make_photo(path, side=SIZE):
    """A photo, turned upright and cut to a square."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        k = side / min(size.width(), size.height())
        reader.setScaledSize(QSize(max(1, round(size.width() * k)), max(1, round(size.height() * k))))
    img = reader.read()
    if img.isNull():
        return None
    x = max(0, (img.width() - side) // 2)
    y = max(0, (img.height() - side) // 2)
    return img.copy(x, y, min(side, img.width()), min(side, img.height()))


# ---- layouts (main thread) -------------------------------------------------------------------------------------
def layout_image(layout, width=150):
    """The first page of a print layout, small."""
    from qgis.core import QgsLayoutExporter

    pages = layout.pageCollection()
    if pages.pageCount() == 0:
        return None
    size = pages.page(0).pageSize()
    pw, ph = size.width(), size.height()
    if pw <= 0 or ph <= 0:
        return None
    height = max(1, round(width * ph / pw))
    img = QgsLayoutExporter(layout).renderPageToImage(0, QSize(width, height))
    return None if img.isNull() else img


def rounded(img, side, radius=8, fit="cover"):
    """A rounded-corner square picture of ``side`` pixels (cover: cut to fill; contain: whole picture centred)."""
    out = _blank(side, side)
    if img is None or img.isNull():
        return out
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, side, side), radius, radius)
    p.setClipPath(clip)
    mode = Qt.AspectRatioMode.KeepAspectRatioByExpanding if fit == "cover" else Qt.AspectRatioMode.KeepAspectRatio
    s = img.scaled(side, side, mode, Qt.TransformationMode.SmoothTransformation)
    p.drawImage(QPointF((side - s.width()) / 2, (side - s.height()) / 2), s)
    p.end()
    return out
