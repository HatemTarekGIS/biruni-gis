import math
import os
import re
import shutil
import tempfile

import numpy as np
from qgis.core import (Qgis, QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
                       QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterString, QgsUnitTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from . import _scan_ocr as ocr
from .base import BiruniAlgorithm

MAX_PIXELS = 40_000_000      # larger scans are traced at a reduced resolution (lines are kept, see block_min)
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
CROSS = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)


# ---- control points and the plan -> map transform ------------------------------------------------------------

def parse_control_points(text):
    """Control points typed as "pixel_x pixel_y X Y" per line (any separators,
    Arabic digits allowed), a QGIS georeferencer .points text/file, or the
    path of such a file. Returns [(px, py, X, Y)] with py counted downwards."""
    text = ocr.normalise_digits(text or "").replace("٫", ".").strip()
    if text and "\n" not in text and os.path.isfile(text):
        with open(text, encoding="utf-8-sig", errors="replace") as f:
            text = ocr.normalise_digits(f.read())
    qgis_points = "mapx" in text.lower()
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        nums = [float(v) for v in _NUM.findall(s)]
        if len(nums) < 4:
            continue
        if qgis_points:  # mapX,mapY,sourceX,sourceY,enable,...
            if len(nums) >= 5 and nums[4] == 0:
                continue
            rows.append((nums[2], nums[3], nums[0], nums[1]))
        else:
            rows.append((nums[0], nums[1], nums[2], nums[3]))
    if rows and all(r[1] <= 0 for r in rows) and any(r[1] < 0 for r in rows):
        rows = [(px, -py, x, y) for px, py, x, y in rows]  # the georeferencer counts rows upwards (negative)
    return rows


class PlanTransform:
    """Pixel (column, row from the top-left corner) -> map coordinates."""

    MIN_POINTS = {"similarity": 2, "affine": 3, "projective": 4}

    def __init__(self, kind, params):
        self.kind, self.p = kind, params
        self.residuals = []

    @classmethod
    def fit(cls, points, kind="auto"):
        n = len(points)
        if kind == "auto":
            kind = "similarity" if n == 2 else "affine" if n < 5 else "projective"
        if n < cls.MIN_POINTS[kind]:
            raise ValueError(kind)
        a = np.asarray(points, float)
        px, py, X, Y = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        if kind == "similarity":
            # rows go down and northings up: fit on (px, -py) so the plan is not mirrored
            u, v = px, -py
            m = np.zeros((2 * n, 4))
            m[0::2] = np.column_stack([u, -v, np.ones(n), np.zeros(n)])
            m[1::2] = np.column_stack([v, u, np.zeros(n), np.ones(n)])
            rhs = np.empty(2 * n)
            rhs[0::2], rhs[1::2] = X, Y
            p = np.linalg.lstsq(m, rhs, rcond=None)[0]
        elif kind == "affine":
            m = np.column_stack([np.ones(n), px, py])
            p = np.concatenate([np.linalg.lstsq(m, X, rcond=None)[0], np.linalg.lstsq(m, Y, rcond=None)[0]])
        else:
            def norm(xs, ys):
                cx, cy = xs.mean(), ys.mean()
                s = math.sqrt(2) / max(np.mean(np.hypot(xs - cx, ys - cy)), 1e-12)
                return np.array([[s, 0, -s * cx], [0, s, -s * cy], [0, 0, 1]])
            ts, td = norm(px, py), norm(X, Y)
            sx, sy, _ = ts @ np.vstack([px, py, np.ones(n)])
            dx, dy, _ = td @ np.vstack([X, Y, np.ones(n)])
            m = np.zeros((2 * n, 9))
            m[0::2] = np.column_stack([sx, sy, np.ones(n), np.zeros((n, 3)), -dx * sx, -dx * sy, -dx])
            m[1::2] = np.column_stack([np.zeros((n, 3)), sx, sy, np.ones(n), -dy * sx, -dy * sy, -dy])
            h = np.linalg.svd(m)[2][-1].reshape(3, 3)
            h = np.linalg.inv(td) @ h @ ts
            p = (h / h[2, 2]).ravel()
        t = cls(kind, p)
        gx, gy = t.apply(px, py)
        t.residuals = list(np.hypot(gx - X, gy - Y))
        return t

    def apply(self, px, py):
        px, py, p = np.asarray(px, float), np.asarray(py, float), self.p
        if self.kind == "similarity":
            u, v = px, -py
            return p[0] * u - p[1] * v + p[2], p[1] * u + p[0] * v + p[3]
        if self.kind == "affine":
            return p[0] + p[1] * px + p[2] * py, p[3] + p[4] * px + p[5] * py
        w = p[6] * px + p[7] * py + p[8]
        return (p[0] * px + p[1] * py + p[2]) / w, (p[3] * px + p[4] * py + p[5]) / w

    @property
    def rms(self):
        return float(np.sqrt(np.mean(np.square(self.residuals)))) if self.residuals else 0.0

    def pixel_size(self, px, py):
        """Ground size of one pixel around (px, py)."""
        x0, y0 = self.apply(px, py)
        x1, y1 = self.apply(px + 1, py)
        x2, y2 = self.apply(px, py + 1)
        return float(math.sqrt(abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))))


def kind_label(kind):
    if kind == "similarity":
        return tr("scale and rotation")
    if kind == "affine":
        return tr("affine")
    return tr("projective")


# ---- image ----------------------------------------------------------------------------------------------------

def open_image(path, dpi):
    from osgeo import gdal

    opts = [f"DPI={int(dpi)}"] if path.lower().endswith(".pdf") else []
    ds = gdal.OpenEx(path, gdal.OF_RASTER, open_options=opts)
    if ds is None:
        raise QgsProcessingException(tr("The image could not be opened: {p}").format(p=path))
    return ds, opts


def read_gray(ds):
    """The scan as 8-bit grey, dark lines on white paper."""
    w, h = ds.RasterXSize, ds.RasterYSize
    gray = np.empty((h, w), np.uint8)
    b1 = ds.GetRasterBand(1)
    table = b1.GetColorTable()
    lut = None
    if table is not None:
        lut = np.zeros(256, np.uint8)
        for i in range(min(table.GetCount(), 256)):
            r, g, b = table.GetColorEntry(i)[:3]
            lut[i] = (299 * r + 587 * g + 114 * b) // 1000
    one_bit = b1.GetMetadataItem("NBITS", "IMAGE_STRUCTURE") == "1"
    step = max(1, 8_000_000 // max(w, 1))
    for y in range(0, h, step):
        n = min(step, h - y)
        if ds.RasterCount >= 3 and lut is None:
            acc = np.zeros((n, w), np.uint32)
            for band, k in ((1, 299), (2, 587), (3, 114)):
                acc += ds.GetRasterBand(band).ReadAsArray(0, y, w, n).astype(np.uint32) * k
            gray[y:y + n] = acc // 1000
        else:
            a = b1.ReadAsArray(0, y, w, n)
            if lut is not None:
                gray[y:y + n] = lut[a.astype(np.intp)]
            elif one_bit:
                gray[y:y + n] = np.where(a > 0, 255, 0)
            elif a.dtype != np.uint8:
                a = a.astype(np.float64)
                lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
                gray[y:y + n] = np.clip((a - lo) * 255.0 / max(hi - lo, 1e-9), 0, 255)
            else:
                gray[y:y + n] = a
    if np.median(gray[::7, ::7]) < 128:  # white lines on black (screen dumps, inverted 1-bit scans)
        np.subtract(255, gray, out=gray)
    return gray


def block_min(gray, k):
    """Shrink by k keeping the darkest pixel of each k×k block, so thin lines survive."""
    if k <= 1:
        return gray
    h, w = gray.shape
    hp, wp = -(-h // k) * k, -(-w // k) * k
    if (hp, wp) != (h, w):
        gray = np.pad(gray, ((0, hp - h), (0, wp - w)), constant_values=255)
    return gray.reshape(hp // k, k, wp // k, k).min(axis=(1, 3))


def otsu(gray):
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    levels = np.arange(256)
    w0 = np.cumsum(hist)
    m0 = np.cumsum(hist * levels)
    w1 = total - w0
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (m0[-1] * w0 / total - m0) ** 2 / (w0 * w1)
    between[~np.isfinite(between)] = 0
    return int(np.argmax(between))


def binarise(gray, adaptive=False):
    t = otsu(gray)
    if not adaptive:
        return gray <= t
    from scipy import ndimage

    win = max(15, (min(gray.shape) // 40) | 1)
    mean = ndimage.uniform_filter(gray.astype(np.float32), win)
    return (gray < mean * 0.88) & (gray < 235)


def drop_small_marks(ink, max_side):
    """Removes ink blobs no larger than max_side pixels (text, numbers, dashes,
    specks) and returns the cleaned ink with the median height of the text."""
    from scipy import ndimage

    lab, n = ndimage.label(ink, structure=np.ones((3, 3), bool))
    if n == 0:
        return ink, 0.0, 0
    sl = ndimage.find_objects(lab)
    hs = np.array([s[0].stop - s[0].start for s in sl])
    ws = np.array([s[1].stop - s[1].start for s in sl])
    small = np.maximum(hs, ws) <= max_side
    keep = np.concatenate([[False], ~small])
    glyphs = hs[small & (np.maximum(hs, ws) >= 3)]
    return keep[lab], float(np.median(glyphs)) if glyphs.size else 0.0, int(small.sum())


def line_width(ink):
    """Mean thickness of the lines in pixels: area / half the outline."""
    from scipy import ndimage

    area = int(ink.sum())
    edge = area - int(ndimage.binary_erosion(ink).sum())
    return 2.0 * area / edge if edge else 1.0


def label_regions(ink, gap):
    """The white areas enclosed by the lines (4-connected), after closing line
    breaks up to 2×gap pixels. Returns labels, their count and the labels that
    touch the edge of the image (the outside)."""
    from scipy import ndimage

    if gap > 0:
        ink = ndimage.binary_dilation(ink, structure=CROSS, iterations=gap)
    lab, n = ndimage.label(~ink)
    edge = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    return lab.astype(np.int32), n, edge


def grow_into_lines(lab, max_iter=200):
    """Gives every line pixel to the nearest area, so neighbouring parcels meet
    on the middle of the line they share. Alternates max/min so no side wins."""
    from scipy import ndimage

    big = np.iinfo(np.int32).max
    for i in range(max_iter):
        empty = lab == 0
        if not empty.any():
            break
        if i % 2 == 0:
            grown = ndimage.grey_dilation(lab, footprint=CROSS)
        else:
            grown = ndimage.grey_erosion(np.where(empty, big, lab), footprint=CROSS)
            grown[grown == big] = 0
        fill = empty & (grown > 0)
        if not fill.any():
            break
        lab[fill] = grown[fill]
    return lab


def polygonize(lab):
    """{label: QgsGeometry} in pixel units (x = column, y = row)."""
    from osgeo import gdal, ogr

    h, w = lab.shape
    ds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Int32)
    ds.SetGeoTransform((0, 1, 0, 0, 0, 1))
    band = ds.GetRasterBand(1)
    band.WriteArray(lab)
    drv = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
    src = drv.CreateDataSource("regions")
    layer = src.CreateLayer("regions", geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0, [])
    parts = {}
    for f in layer:
        g = QgsGeometry()
        g.fromWkb(bytes(f.GetGeometryRef().ExportToWkb()))
        parts.setdefault(f.GetField(0), []).append(g)
    return {k: (v[0] if len(v) == 1 else QgsGeometry.unaryUnion(v)) for k, v in parts.items()}


def outer(g):
    """The polygon without holes (a number circle or a symbol inside a parcel)."""
    polys = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
    polys = [p for p in polys if p]
    if not polys:
        return g
    best = max(polys, key=lambda p: abs(QgsGeometry.fromPolygonXY([p[0]]).area()))
    return QgsGeometry.fromPolygonXY([best[0]])


def to_map(g, k, model):
    ring = g.asPolygon()[0]
    xs, ys = model.apply([p.x() * k for p in ring], [p.y() * k for p in ring])
    return QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in zip(xs, ys)]])


def _same_place(a, b):
    return (abs(a["cx"] - b["cx"]) < 0.5 * (a["w"] + b["w"]) and abs(a["cy"] - b["cy"]) < 0.5 * (a["h"] + b["h"]))


def combine_readings(windows, shapes, ink, ox, oy):
    """Weighs the two readers of one parcel. windows: Windows OCR numbers, shapes:
    numbers read by shape, both in page pixels; ink: the binary crop of the parcel
    whose corner is (ox, oy). Each reading gets a confidence (our own estimate):
    both readers agree 0.95 · Windows alone, Latin digits, one ink blob per digit
    0.9 · shape alone 0.7 · they disagree, or Windows alone on Arabic digits 0.6
    (Windows misreads those) · Windows with the wrong number of blobs 0.3."""
    out, used = [], set()
    for w in windows:
        if not w["is_integer"]:
            out.append(dict(w, conf=0.0))  # lengths and the like: shown, never the parcel number
            continue
        twin = next((i for i, s in enumerate(shapes) if i not in used and _same_place(w, s)), None)
        if twin is not None:
            used.add(twin)
            s = shapes[twin]
            if s["norm"] == w["norm"]:
                out.append(dict(w, conf=0.95))
            else:
                out.append(dict(s if s["arabic"] else w, conf=0.6))
            continue
        digits = sum(c.isdigit() for c in w["norm"])
        blobs = ocr.count_blobs(ink, (w["x"] - ox, w["y"] - oy, w["w"], w["h"]))
        arabic = any(c in "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹" for c in w["text"])
        out.append(dict(w, conf=0.3 if blobs != digits else 0.6 if arabic else 0.9))
    out += [dict(s, conf=0.7) for i, s in enumerate(shapes) if i not in used]
    return out


def pick_number(cands, cx, cy, size):
    """The parcel number among the numbers read inside a parcel: a whole number
    from the most trusted reading, the biggest and nearest the middle (lengths
    along the edges are smaller and have decimals)."""
    ints = [c for c in cands if c["is_integer"] and c.get("conf", 0.9) > 0]
    if not ints:
        return "", 0.0
    best_conf = max(c.get("conf", 0.9) for c in ints)
    tier = [c for c in ints if c.get("conf", 0.9) == best_conf]
    top = max(c["h"] for c in tier)

    def score(c):
        return c["h"] / top - 0.8 * math.hypot(c["cx"] - cx, c["cy"] - cy) / max(size, 1e-9)

    best = max(tier, key=score)
    conf = best_conf
    if len({c["norm"] for c in ints if c.get("conf", 0.9) >= 0.6}) > 1:
        conf = min(conf, 0.6)  # two different trusted numbers in one parcel
    if best.get("disputed"):
        conf -= 0.3
    return best["norm"], round(max(conf, 0.0), 2)


def parcel_ink(gray, lab, k, rect, label, threshold):
    """The ink inside one parcel at full resolution: (binary crop, bolder binary
    crop, x0, y0). rect = (x0, y0, x1, y1) in traced pixels. The bolder one
    keeps thin joins of a glyph together (they are grey on a scan)."""
    x0, y0, x1, y1 = rect
    mask = lab[y0:y1, x0:x1] == label
    if k > 1:
        mask = np.repeat(np.repeat(mask, k, axis=0), k, axis=1)
    crop = gray[y0 * k:y1 * k, x0 * k:x1 * k]
    mask = mask[:crop.shape[0], :crop.shape[1]]
    t = threshold if threshold is not None else otsu(crop[mask]) if mask.any() else 128
    return (crop <= t) & mask, (crop <= t + 0.4 * (255 - t)) & mask, x0 * k, y0 * k


def read_shapes(ink, bold, max_px):
    """Numbers read by shape on the normal and the bolder ink; where both read
    something at one place the clearer reading wins."""
    found = ocr.glyph_numbers(ink, max_px=max_px)
    for b in ocr.glyph_numbers(bold, max_px=max_px):
        twin = next((i for i, a in enumerate(found) if _same_place(a, b)), None)
        if twin is None:
            found.append(b)
        elif b["score"] > found[twin]["score"] and len(b["norm"]) >= len(found[twin]["norm"]):
            found[twin] = b
    return found


# ---- the tool -------------------------------------------------------------------------------------------------

class ScanToParcels(BiruniAlgorithm):
    """Scanned subdivision plan -> georeferenced image + parcel polygons."""

    ID = "scantoparcels"
    NAME = "Parcels from a scanned plan"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Turns a scanned or photographed plan (subdivision plan, sketch) into parcels with real coordinates. "
            "Give at least two control points: the pixel position of a point on the image and its map coordinates, "
            "one per line as 'pixel_x pixel_y X Y' (pixels counted from the top-left corner), or a points layer "
            "with the fields pixel_x and pixel_y (the map position is the point itself or the fields x and y). "
            "A georeferencer .points file also works. Two points fit scale and rotation, three or four an "
            "affine transform, five or more a projective one (for photos); the error of every point is reported. "
            "The tool then finds the lines of the plan, leaves out the text, closes small breaks in the lines and "
            "turns every area enclosed by lines into a parcel whose edges run along the middle of the lines. "
            "Streets and the outside are left out. Parcel numbers (Arabic or Latin digits) are read with the text "
            "recognition built into Windows and by the shape of printed digits, and every number gets a "
            "confidence. Always check the result: parcels with no number, a doubtful number, an unusual area or "
            "an odd shape are marked 'needs review'. Areas are on the map projection of the chosen coordinate "
            "system.")

    INPUT, DPI, GCP_LAYER, GCP_TEXT, TRANSFORM, CRS = "INPUT", "DPI", "GCP_LAYER", "GCP_TEXT", "TRANSFORM", "CRS"
    THRESHOLD, TEXT_SIZE, GAP, SQUARE, OCR = "THRESHOLD", "TEXT_SIZE", "GAP", "SQUARE", "OCR"
    RASTER, PARCELS, LINES, NUMBERS = "RASTER", "PARCELS", "LINES", "NUMBERS"

    TRANSFORM_KEYS = ["auto", "similarity", "affine", "projective"]
    OCR_LANGS = [("ar-SA", "en-US"), ("ar-SA",), ("en-US",), ()]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("Scanned plan (image or PDF)"),
            fileFilter=tr("Images and PDF") + " (*.tif *.tiff *.png *.jpg *.jpeg *.bmp *.gif *.pdf)"))
        self.addParameter(QgsProcessingParameterNumber(
            self.DPI, tr("Resolution for PDF files (dpi)"), Qgis.ProcessingNumberParameterType.Integer, 300,
            minValue=72, maxValue=1200))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.GCP_LAYER, tr("Control points layer (fields pixel_x, pixel_y)"),
            [Qgis.ProcessingSourceType.VectorPoint, Qgis.ProcessingSourceType.Vector], optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.GCP_TEXT, tr("Control points: pixel_x pixel_y X Y, one per line"), multiLine=True,
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.TRANSFORM, tr("Transformation"),
            [tr("Automatic (from the number of points)"), tr("Scale and rotation (2+ points)"),
             tr("Affine (3+ points)"), tr("Projective, for photos (4+ points)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, tr("Coordinate system of the control points"),
                                                    "ProjectCrs"))
        self.addParameter(QgsProcessingParameterEnum(
            self.THRESHOLD, tr("Separating lines from paper"),
            [tr("Automatic"), tr("Uneven scan (shadows, yellowed paper)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.TEXT_SIZE, tr("Ignore marks smaller than (pixels, 0 = automatic)"),
            Qgis.ProcessingNumberParameterType.Integer, 0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.GAP, tr("Close breaks in the lines up to (pixels)"), Qgis.ProcessingNumberParameterType.Integer,
            2, minValue=0, maxValue=20))
        self.addParameter(QgsProcessingParameterBoolean(self.SQUARE, tr("Square nearly right angles"), False))
        self.addParameter(QgsProcessingParameterEnum(
            self.OCR, tr("Read parcel numbers"),
            [tr("Arabic and English digits"), tr("Arabic"), tr("English"), tr("Don't read")], defaultValue=0))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.RASTER, tr("Georeferenced plan (GeoTIFF)"), optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.PARCELS, tr("Parcels"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.LINES, tr("Parcel lines"), Qgis.ProcessingSourceType.VectorLine, optional=True,
            createByDefault=False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.NUMBERS, tr("Numbers read on the plan"), Qgis.ProcessingSourceType.VectorPoint, optional=True,
            createByDefault=False))

    # -- inputs

    def _control_points(self, parameters, context, crs):
        pts = parse_control_points(self.parameterAsString(parameters, self.GCP_TEXT, context))
        src = self.parameterAsSource(parameters, self.GCP_LAYER, context)
        if src is not None:
            names = {f.name().lower(): f.name() for f in src.fields()}

            def field(*options):
                return next((names[o] for o in options if o in names), None)

            fx = field("pixel_x", "px", "pixelx", "source_x", "sourcex", "col", "column", "image_x")
            fy = field("pixel_y", "py", "pixely", "source_y", "sourcey", "row", "line", "image_y")
            mx = field("x", "map_x", "mapx", "easting", "east")
            my = field("y", "map_y", "mapy", "northing", "north")
            if not fx or not fy:
                raise QgsProcessingException(tr("The control points layer needs the fields pixel_x and pixel_y."))
            xform = None
            if not (mx and my) and src.sourceCrs().isValid() and src.sourceCrs() != crs:
                xform = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
            rows = []
            for f in src.getFeatures():
                try:
                    px, py = float(f[fx]), float(f[fy])
                    if mx and my:
                        x, y = float(f[mx]), float(f[my])
                    else:
                        g = f.geometry()
                        if g.isNull() or g.isEmpty():
                            continue
                        p = g.asPoint() if not g.isMultipart() else g.asMultiPoint()[0]
                        if xform is not None:
                            p = xform.transform(p)
                        x, y = p.x(), p.y()
                except (TypeError, ValueError):
                    continue
                rows.append((px, py, x, y))
            if rows and all(r[1] <= 0 for r in rows) and any(r[1] < 0 for r in rows):
                rows = [(px, -py, x, y) for px, py, x, y in rows]
            pts += rows
        return pts

    # -- run

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        if not path or not os.path.exists(path):
            raise QgsProcessingException(tr("Choose the scanned plan."))
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            raise QgsProcessingException(tr("Choose the coordinate system of the control points."))
        pts = self._control_points(parameters, context, crs)
        kind = self.TRANSFORM_KEYS[self.parameterAsEnum(parameters, self.TRANSFORM, context)]
        try:
            model = PlanTransform.fit(pts, kind)
        except (ValueError, np.linalg.LinAlgError):
            need = PlanTransform.MIN_POINTS.get(kind, 2)
            raise QgsProcessingException(
                tr("At least {n} control points are needed ({m} given).").format(n=need, m=len(pts)))
        units = QgsUnitTypes.toAbbreviatedString(crs.mapUnits())
        feedback.pushInfo(tr("Transformation: {k}, {n} control points.").format(k=kind_label(model.kind),
                                                                                 n=len(pts)))
        for i, r in enumerate(model.residuals, 1):
            feedback.pushInfo(tr("Control point {i}: error {r:.3f} {u}").format(i=i, r=r, u=units))
        feedback.pushInfo(tr("Mean error (RMS): {r:.3f} {u}").format(r=model.rms, u=units))

        ds, open_opts = open_image(path, self.parameterAsInt(parameters, self.DPI, context))
        width, height = ds.RasterXSize, ds.RasterYSize
        pix = model.pixel_size(width / 2, height / 2)
        if pix <= 0:
            raise QgsProcessingException(tr("The control points are on one line: add points spread over the plan."))
        if len(pts) > PlanTransform.MIN_POINTS[model.kind] and model.rms > 5 * pix:
            feedback.pushWarning(tr("The control points disagree by more than five pixels: check the point with "
                                    "the largest error."))
        feedback.pushInfo(tr("One pixel of the scan is {p:.3f} {u} on the ground.").format(p=pix, u=units))
        results = {"GCP_RMS": model.rms, "TRANSFORMATION": model.kind}

        # georeferenced copy of the scan
        raster = self.parameterAsOutputLayer(parameters, self.RASTER, context)
        if raster:
            feedback.pushInfo(tr("Writing the georeferenced plan…"))
            self._write_raster(raster, path, open_opts, ds, model, crs)
            results[self.RASTER] = raster
        feedback.setProgress(15)
        if feedback.isCanceled():
            return {}

        # lines -> areas
        feedback.pushInfo(tr("Finding the lines of the plan…"))
        gray = read_gray(ds)
        ds = None
        k = max(1, math.ceil(math.sqrt(width * height / MAX_PIXELS)))
        small = block_min(gray, k)
        if k > 1:
            feedback.pushInfo(tr("Large scan: traced at 1/{k} resolution.").format(k=k))
        adaptive = self.parameterAsEnum(parameters, self.THRESHOLD, context) == 1
        ink = binarise(small, adaptive)
        threshold = None if adaptive else otsu(small)
        text_px = self.parameterAsInt(parameters, self.TEXT_SIZE, context)
        text_px = max(1, round(text_px / k)) if text_px else int(min(300, max(12, 0.02 * max(small.shape))))
        ink, glyph_h, removed = drop_small_marks(ink, text_px)
        feedback.pushInfo(tr("{n} pieces of text and small marks set aside.").format(n=removed))
        feedback.setProgress(30)
        if feedback.isCanceled():
            return {}
        gap = self.parameterAsInt(parameters, self.GAP, context)
        width_px = line_width(ink)
        lab, n, edge = label_regions(ink, max(0, round(gap / k)))
        del ink
        sizes = np.bincount(lab.ravel())
        lab = grow_into_lines(lab)
        feedback.setProgress(45)
        if feedback.isCanceled():
            return {}

        regions = polygonize(lab)
        tol = max(1.2, 0.5 * width_px)  # thick or blurred lines leave ragged pixel edges
        order = sorted(regions)
        coverage = QgsGeometry.collectGeometry([regions[r] for r in order])
        simple = coverage.simplifyCoverageVW(tol, False)
        if not simple.isNull() and simple.constGet().numGeometries() == len(order):
            regions = {r: QgsGeometry(simple.constGet().geometryN(i).clone()) for i, r in enumerate(order)}
        else:  # keep going without a shared simplification
            regions = {r: g.simplify(tol) for r, g in regions.items()}
        feedback.setProgress(55)

        # keep the parcels: not the outside, not crumbs, not the street network
        min_px = max(64.0, (1.5 * glyph_h) ** 2) if glyph_h else 64.0
        cands = [r for r in regions if r not in edge and r < len(sizes) and sizes[r] >= min_px]
        med = float(np.median([regions[r].area() for r in cands])) if cands else 0.0
        total = sum(regions[r].area() for r in cands)
        streets = set()
        for r in cands:
            # the streets are one big area around the blocks: large, with long edges (holes included)
            a, p = regions[r].area(), regions[r].length()
            compact = 4 * math.pi * a / (p * p) if p else 0
            if (a > 6 * med and compact < 0.12) or (a > 0.3 * total and compact < 0.3 and len(cands) > 3):
                streets.add(r)
        parcels = {r: outer(regions[r]) for r in cands if r not in streets}
        areas = {r: g.area() for r, g in parcels.items()}
        if streets:
            feedback.pushInfo(tr("{n} large open areas (streets, squares) left out.").format(n=len(streets)))
        if not parcels:
            raise QgsProcessingException(tr("No closed parcels were found. Try 'Uneven scan', or close larger "
                                            "breaks in the lines."))

        # parcel numbers
        langs = self.OCR_LANGS[self.parameterAsEnum(parameters, self.OCR, context)]
        nums, inside = [], {}
        ocr_on = bool(langs)
        if ocr_on:
            feedback.pushInfo(tr("Reading the parcel numbers…"))
            try:
                nums = self._read_numbers(gray, glyph_h * k, langs, feedback)
            except ocr.OcrUnavailable as e:
                feedback.pushWarning(tr("Windows text recognition is not available ({e}): the numbers are read "
                                        "by their shape only.").format(e=e))
            lh, lw = lab.shape
            by_parcel = {}
            for w in nums:
                r = int(lab[min(lh - 1, max(0, int(w["cy"] / k))), min(lw - 1, max(0, int(w["cx"] / k)))])
                w["region"] = r
                by_parcel.setdefault(r, []).append(w)
            shape_count = 0
            for i, (r, g) in enumerate(parcels.items()):
                if feedback.isCanceled():
                    return {}
                b = g.boundingBox()
                rect = (max(0, int(b.xMinimum()) - 1), max(0, int(b.yMinimum()) - 1),
                        min(lw, int(b.xMaximum()) + 2), min(lh, int(b.yMaximum()) + 2))
                pink, bold, ox, oy = parcel_ink(gray, lab, k, rect, r, threshold)
                shapes = read_shapes(pink, bold, text_px * k)
                for s in shapes:
                    s.update(x=s["x"] + ox, y=s["y"] + oy, cx=s["cx"] + ox, cy=s["cy"] + oy, region=r)
                shape_count += len(shapes)
                inside[r] = combine_readings(by_parcel.get(r, []), shapes, pink, ox, oy)
                nums += shapes
                feedback.setProgress(65 + 15 * i / max(len(parcels), 1))
            feedback.pushInfo(tr("{n} numbers read by their shape.").format(n=shape_count))
        del gray, lab
        feedback.setProgress(80)
        if feedback.isCanceled():
            return {}

        # map coordinates and measures
        to_m = QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters)
        da = None
        if crs.isGeographic():
            da = QgsDistanceArea()
            da.setSourceCrs(crs, context.transformContext())
            da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7030")
        square = self.parameterAsBoolean(parameters, self.SQUARE, context)
        rows = []
        for r, g in parcels.items():
            c = g.centroid().asPoint()
            mg = to_map(g, k, model)
            if square:
                sq = mg.orthogonalize(1e-8, 1000, 12)
                if not sq.isNull() and sq.isGeosValid():
                    mg = sq
            if da is not None:
                area, perim = da.measureArea(mg), da.measurePerimeter(mg)
            else:
                area, perim = mg.area() * to_m * to_m, mg.length() * to_m
            no, conf = pick_number(inside.get(r, []), c.x() * k, c.y() * k, math.sqrt(areas[r]) * k)
            rows.append({"r": r, "g": mg, "cx": c.x(), "cy": c.y(), "area": area, "perim": perim, "no": no,
                         "conf": conf, "ocr": " | ".join(sorted({w["norm"] for w in inside.get(r, [])})),
                         "px_area": areas[r], "box": g.orientedMinimumBoundingBox(),
                         "vertices": max(0, len(g.asPolygon()[0]) - 1)})

        # review marks and reading order (rows of the plan from the top, then left to right)
        med_area = float(np.median([x["area"] for x in rows]))
        band = math.sqrt(float(np.median([x["px_area"] for x in rows]))) / 2
        rows.sort(key=lambda x: (round(x["cy"] / max(band, 1)), x["cx"]))
        notes_count = 0
        for x in rows:
            notes = []
            if ocr_on and not x["no"]:
                notes.append(tr("no number read"))
            elif ocr_on and x["conf"] < 0.7:
                notes.append(tr("check the number"))
            if x["area"] < 0.2 * med_area or x["area"] > 5 * med_area:
                notes.append(tr("unusual area"))
            bw, bh = x["box"][3], x["box"][4]
            if min(bw, bh) > 0 and max(bw, bh) / min(bw, bh) > 6:
                notes.append(tr("very thin"))
            if x["vertices"] > 20:
                notes.append(tr("many corners"))
            x["notes"] = notes
            notes_count += bool(notes)

        pf = QgsFields()
        for name, t in (("parcel_id", QMetaType.Type.Int), ("parcel_no", QMetaType.Type.QString),
                        ("ocr_text", QMetaType.Type.QString), ("ocr_confidence", QMetaType.Type.Double),
                        ("area_m2", QMetaType.Type.Double), ("perimeter_m", QMetaType.Type.Double),
                        ("needs_review", QMetaType.Type.Bool), ("review_note", QMetaType.Type.QString)):
            pf.append(QgsField(name, t))
        psink, pid = self.parameterAsSink(parameters, self.PARCELS, context, pf, Qgis.WkbType.Polygon, crs)
        ids = {}
        for i, x in enumerate(rows, 1):
            ids[x["r"]] = i
            feat = QgsFeature(pf)
            feat.setGeometry(x["g"])
            feat.setAttributes([i, x["no"], x["ocr"], x["conf"], round(x["area"], 2), round(x["perim"], 2),
                                bool(x["notes"]), " · ".join(x["notes"])])
            psink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
        results.update({self.PARCELS: pid, "PARCEL_COUNT": len(rows),
                        "NUMBERED_COUNT": sum(1 for x in rows if x["no"]), "REVIEW_COUNT": notes_count})

        lsink, lid = self.parameterAsSink(parameters, self.LINES, context, self._line_fields(),
                                          Qgis.WkbType.LineString, crs)
        if lsink is not None:
            self._write_lines(lsink, [x["g"] for x in rows], da, to_m)
            results[self.LINES] = lid
        nf = QgsFields()
        for name, t in (("text", QMetaType.Type.QString), ("number", QMetaType.Type.QString),
                        ("parcel_id", QMetaType.Type.Int), ("height_px", QMetaType.Type.Double)):
            nf.append(QgsField(name, t))
        nsink, nid = self.parameterAsSink(parameters, self.NUMBERS, context, nf, Qgis.WkbType.Point, crs)
        if nsink is not None:
            for w in nums:
                x, y = model.apply(w["cx"], w["cy"])
                feat = QgsFeature(nf)
                feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
                feat.setAttributes([w["text"], w["norm"], ids.get(w["region"]), round(w["h"], 1)])
                nsink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            results[self.NUMBERS] = nid
        feedback.setProgress(100)
        feedback.pushInfo(tr("{n} parcels, {m} with a number read, {r} to review.").format(
            n=len(rows), m=results["NUMBERED_COUNT"], r=notes_count))
        return results

    # -- helpers

    def _read_numbers(self, gray, glyph_h, langs, feedback):
        from PIL import Image

        have = ocr.available_languages(langs)
        if not have:
            raise ocr.OcrUnavailable(tr("no Windows text recognition for {l}").format(l=", ".join(langs)))
        scale = int(min(3, max(1, round(32 / glyph_h)))) if glyph_h else 1
        size = min(3000, ocr.MAX_DIMENSION // scale)
        overlap = int(max(200, 6 * glyph_h))
        h, w = gray.shape
        work = tempfile.mkdtemp(prefix="biruni_scan_")
        try:
            tiles = []
            for y0 in range(0, h, size - overlap):
                for x0 in range(0, w, size - overlap):
                    crop = gray[y0:y0 + size, x0:x0 + size]
                    if crop.size == 0 or crop.min() > 200:  # blank paper
                        continue
                    img = Image.fromarray(crop)
                    if scale > 1:
                        img = img.resize((crop.shape[1] * scale, crop.shape[0] * scale), Image.LANCZOS)
                    p = os.path.join(work, f"t{len(tiles)}.png")
                    img.save(p)
                    tiles.append((p, x0, y0, scale))
                    if x0 + size >= w:
                        break
                if y0 + size >= h:
                    break
            words = ocr.read_words(tiles, have)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        nums = ocr.deduplicate(ocr.numbers(words))
        feedback.pushInfo(tr("{n} numbers read ({l}).").format(n=len(nums), l=", ".join(have)))
        return nums

    @staticmethod
    def _line_fields():
        lf = QgsFields()
        lf.append(QgsField("line_id", QMetaType.Type.Int))
        lf.append(QgsField("length_m", QMetaType.Type.Double))
        return lf

    def _write_lines(self, sink, geoms, da, to_m):
        bounds = [QgsGeometry(g.constGet().boundary()) for g in geoms]
        merged = QgsGeometry.unaryUnion(bounds).mergeLines()
        parts = merged.asGeometryCollection() if merged.isMultipart() else [merged]
        lf = self._line_fields()
        for i, part in enumerate(parts, 1):
            length = da.measureLength(part) if da is not None else part.length() * to_m
            feat = QgsFeature(lf)
            feat.setGeometry(part)
            feat.setAttributes([i, round(length, 2)])
            sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)

    @staticmethod
    def _write_raster(dest, path, open_opts, ds, model, crs):
        from osgeo import gdal

        w, h = ds.RasterXSize, ds.RasterYSize
        steps = 8 if model.kind != "projective" else 16
        gcps = []
        for i in range(steps + 1):
            for j in range(steps + 1):
                px, py = w * i / steps, h * j / steps
                x, y = model.apply(px, py)
                gcps.append(gdal.GCP(float(x), float(y), 0.0, px, py))
        b1 = ds.GetRasterBand(1)
        kw = {}
        if b1.GetColorTable() is not None:
            kw["rgbExpand"] = "rgb"
        elif b1.GetMetadataItem("NBITS", "IMAGE_STRUCTURE") == "1":
            kw["scaleParams"] = [[0, 1, 0, 255]]
            kw["outputType"] = gdal.GDT_Byte
        vrt = "/vsimem/biruni_scan_gcps.vrt"
        src = gdal.OpenEx(path, gdal.OF_RASTER, open_options=open_opts)
        gdal.Translate(vrt, src, format="VRT", GCPs=gcps, outputSRS=crs.toWkt(), **kw)
        src = None
        opts = dict(format="GTiff", dstSRS=crs.toWkt(), resampleAlg="bilinear", dstAlpha=True, multithread=True,
                    creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
        if model.kind == "projective":
            opts["tps"] = True
        else:
            opts["polynomialOrder"] = 1
        out = gdal.Warp(dest, vrt, **opts)
        if out is None:
            raise QgsProcessingException(tr("The georeferenced plan could not be written."))
        out = None
        gdal.Unlink(vrt)

