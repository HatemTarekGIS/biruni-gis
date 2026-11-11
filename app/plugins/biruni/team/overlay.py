"""The others on your map: each member's pointer with their name, and their work area (where they
edited in the last minutes) as a light shape in their colour."""
import hashlib

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsRectangle
from qgis.gui import QgsMapCanvasItem, QgsRubberBand
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainterPath, QPen


def color_of(member_id):
    """A steady, distinct colour for a member."""
    h = int(hashlib.sha1((member_id or "").encode()).hexdigest()[:4], 16) % 360
    c = QColor()
    c.setHsv(h, 190, 210)
    return c


class Pointer(QgsMapCanvasItem):
    def __init__(self, canvas, name, color):
        super().__init__(canvas)
        self.name = name
        self.color = color
        self.pt = QgsPointXY()
        self.setZValue(1000)

    def set_point(self, pt):
        self.pt = pt
        self.updatePosition()
        self.update()

    def updatePosition(self):  # noqa: N802
        self.setPos(self.toCanvasCoordinates(self.pt))

    def boundingRect(self):  # noqa: N802
        return QRectF(-4, -4, 220, 40)

    def paint(self, p, *_):
        p.setRenderHint(p.RenderHint.Antialiasing)
        arrow = QPainterPath(QPointF(0, 0))
        for x, y in ((0, 16), (4.5, 12), (8, 19), (10.5, 18), (7.2, 11), (12.5, 11)):
            arrow.lineTo(x, y)
        arrow.closeSubpath()
        p.setPen(QPen(QColor("white"), 1.4))
        p.setBrush(QBrush(self.color))
        p.drawPath(arrow)
        f = QFont()
        f.setPointSizeF(8.5)
        f.setBold(True)
        p.setFont(f)
        w = QFontMetricsF(f).horizontalAdvance(self.name) + 12
        box = QRectF(12, 16, w, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(box, 9, 9)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.name)


class Overlay:
    def __init__(self, canvas):
        self.canvas = canvas
        self.pointers = {}
        self.zones = {}

    def update(self, presence, zones, me=None):
        seen = set()
        for mid, p in (presence or {}).items():
            if mid == me or not p.get("c"):
                continue
            seen.add(mid)
            item = self.pointers.get(mid)
            if item is None:
                item = Pointer(self.canvas, p.get("name", ""), color_of(mid))
                self.pointers[mid] = item
            item.name = p.get("name", "")
            item.set_point(QgsPointXY(*p["c"]))
        for mid in [m for m in self.pointers if m not in seen]:
            self.canvas.scene().removeItem(self.pointers.pop(mid))
        zseen = set()
        for mid, (rect, name) in (zones or {}).items():
            zseen.add(mid)
            band = self.zones.get(mid)
            if band is None:
                band = QgsRubberBand(self.canvas, Qgis.GeometryType.Polygon)
                c = color_of(mid)
                fill = QColor(c)
                fill.setAlpha(38)
                band.setFillColor(fill)
                band.setStrokeColor(c)
                band.setLineStyle(Qt.PenStyle.DashLine)
                band.setWidth(2)
                self.zones[mid] = band
            band.setToGeometry(QgsGeometry.fromRect(QgsRectangle(rect)), None)
            band.setToolTip(name)
        for mid in [m for m in self.zones if m not in zseen]:
            self.canvas.scene().removeItem(self.zones.pop(mid))

    def clear(self):
        self.update({}, {})
