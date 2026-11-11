"""Smart placement: legend, north arrow, scale bar and inset go where the map has no data.

The map frame is rendered once, small and off screen, with the vector and data layers only (basemaps would
fill everything). Its ink becomes an occupancy grid; every element tries the corners and edge centres of the
frame and takes the free spot that covers the least data, keeps clear of the elements already placed, and
follows the reading order (legend on the reading side, scale and north arrow at the foot)."""
from qgis.core import QgsLayoutPoint, QgsMapRendererCustomPainterJob, QgsMapSettings, QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QImage, QPainter

GRID_PX = 160            # long side of the occupancy image
PAD = 2.5                # mm between an element and the map frame / another element
INK_ALPHA = 24           # pixel alpha above which a pixel counts as data


class Occupancy:
    """Share of data (0..1) under any rectangle of a map frame, from a small render of its data layers."""

    def __init__(self, layout_map, layers=None):
        self.map = layout_map
        size = layout_map.sizeWithUnits()
        self.w_mm, self.h_mm = size.width(), size.height()
        aspect = self.w_mm / max(self.h_mm, 1e-6)
        self.px = (GRID_PX, max(8, int(GRID_PX / aspect))) if aspect >= 1 else (max(8, int(GRID_PX * aspect)), GRID_PX)
        self.cells = self._render(layers)

    def _render(self, layers):
        layers = [l for l in (layers or self.map.layersToRender() or self.map.layers()) if is_data_layer(l)]
        w, h = self.px
        img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        if layers:
            ms = QgsMapSettings()
            ms.setLayers(layers)
            ms.setDestinationCrs(self.map.crs())
            ms.setExtent(self.map.extent())
            ms.setOutputSize(QSize(w, h))
            ms.setBackgroundColor(QColor(0, 0, 0, 0))
            p = QPainter(img)
            job = QgsMapRendererCustomPainterJob(ms, p)
            job.renderSynchronously()
            p.end()
        rows = []
        for y in range(h):
            rows.append([1 if (img.pixel(x, y) >> 24) & 0xFF > INK_ALPHA else 0 for x in range(w)])
        return rows

    def share(self, rect):
        """Share of data pixels under ``rect`` (mm, relative to the map frame's top-left corner)."""
        w, h = self.px
        x0 = max(0, int(rect.left() / self.w_mm * w))
        x1 = min(w, max(x0 + 1, int(round(rect.right() / self.w_mm * w))))
        y0 = max(0, int(rect.top() / self.h_mm * h))
        y1 = min(h, max(y0 + 1, int(round(rect.bottom() / self.h_mm * h))))
        total = (x1 - x0) * (y1 - y0)
        if total <= 0:
            return 1.0
        return sum(sum(row[x0:x1]) for row in self.cells[y0:y1]) / total

    def data_bbox(self):
        """Rectangle (mm, frame coordinates) holding the data, or None."""
        w, h = self.px
        xs = [x for row in self.cells for x, v in enumerate(row) if v]
        ys = [y for y, row in enumerate(self.cells) if any(row)]
        if not xs:
            return None
        return QRectF(min(xs) / w * self.w_mm, min(ys) / h * self.h_mm, (max(xs) - min(xs) + 1) / w * self.w_mm,
                      (max(ys) - min(ys) + 1) / h * self.h_mm)


def is_data_layer(layer):
    """Vector layers and data rasters count; web basemaps (XYZ/WMS/WMTS tiles) do not."""
    if isinstance(layer, QgsVectorLayer):
        return layer.isValid()
    if isinstance(layer, QgsRasterLayer):
        return layer.isValid() and layer.providerType() not in ("wms", "wcs", "arcgismapserver", "xyzvectortiles")
    return False


# candidate anchors: (horizontal 0/0.5/1, vertical 0/0.5/1)
ANCHORS = [(1, 0), (0, 0), (1, 1), (0, 1), (0.5, 1), (0.5, 0), (1, 0.5), (0, 0.5)]
# where each element prefers to be (LTR); RTL mirrors horizontally
PREFERENCE = {
    "legend": [(1, 1), (1, 0), (0, 1), (0, 0), (1, 0.5), (0, 0.5)],
    "inset": [(1, 0), (0, 0), (0, 1), (1, 1)],
    "scale": [(0, 1), (0.5, 1), (1, 1), (0, 0)],
    "north": [(1, 0), (0, 0), (1, 1), (0, 1)],
}


def candidates(frame_w, frame_h, w, h, kind, rtl=False):
    """Positions (mm, frame coordinates) an element of w × h can take, best-preferred first."""
    order = PREFERENCE.get(kind, ANCHORS)
    order = order + [a for a in ANCHORS if a not in order]
    out = []
    for ax, ay in order:
        if rtl:
            ax = 1 - ax
        x = PAD + ax * (frame_w - w - 2 * PAD)
        y = PAD + ay * (frame_h - h - 2 * PAD)
        if frame_w - w - 2 * PAD >= 0 and frame_h - h - 2 * PAD >= 0:
            out.append(QRectF(x, y, w, h))
    return out


def best_spot(occ, w, h, kind, taken, rtl=False):
    """The candidate with the least data under it that overlaps no element already placed; (rect, share)."""
    best = None
    for rank, r in enumerate(candidates(occ.w_mm, occ.h_mm, w, h, kind, rtl)):
        grown = r.adjusted(-PAD, -PAD, PAD, PAD)
        if any(grown.intersects(t) for t in taken):
            continue
        share = occ.share(r)
        score = share + rank * 0.004          # a small nudge towards the usual place
        if best is None or score < best[0]:
            best = (score, r, share)
    if best is None:
        return None, 1.0
    return best[1], best[2]


def item_size(item):
    s = item.sizeWithUnits()
    return s.width(), s.height()


def place(layout_map, items, rtl=False, layers=None):
    """Move each (kind, item) inside ``layout_map`` to its best free spot, in the given order.

    Returns {kind: (rect in page mm, share of data covered)}. Items that cannot fit anywhere are left where they
    are and reported with share None."""
    occ = Occupancy(layout_map, layers)
    origin = layout_map.positionWithUnits()
    taken, result = [], {}
    for kind, item in items:
        if item is None:
            continue
        w, h = item_size(item)
        rect, share = best_spot(occ, w, h, kind, taken, rtl)
        if rect is None:
            result[kind] = (None, None)
            continue
        item.attemptMove(QgsLayoutPoint(origin.x() + rect.x(), origin.y() + rect.y()))
        taken.append(rect)
        result[kind] = (QRectF(origin.x() + rect.x(), origin.y() + rect.y(), w, h), share)
    return result


def legend_box(frame_w, frame_h, share=0.34):
    """Largest box (mm) the legend may take inside a map frame."""
    return max(25.0, frame_w * share), max(20.0, frame_h * 0.55)
