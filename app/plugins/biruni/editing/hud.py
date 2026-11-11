"""The live sketch HUD: a small box near the cursor while drawing with the segment length, its direction,
the deflection from the previous segment, the total length and (polygons) the area - and the snap tip
(what the cursor snapped to).

``sketch_values`` and ``hud_lines`` are pure and testable; ``SketchHud`` draws them on the canvas.
"""
import math

from qgis.core import Qgis, QgsDistanceArea, QgsPointXY, QgsProject, QgsUnitTypes
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontMetricsF, QPen

from ..i18n import tr
from . import entry


def _area(points):
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def sketch_values(points, cursor=None, polygon=False, length_of=None, area_of=None):
    """Numbers of a sketch: ``points`` are the placed vertices [(x, y), …] and ``cursor`` the rubber-band
    end. ``length_of(p, q)`` / ``area_of(ring)`` measure (planar when not given). Returns a dict with
    segment, direction (azimuth), deflection, total, area (None when not applicable)."""
    pts = [tuple(p) for p in points]
    if cursor is not None:
        pts = pts + [tuple(cursor)]
    length_of = length_of or (lambda p, q: math.hypot(q[0] - p[0], q[1] - p[1]))
    area_of = area_of or _area
    out = {"segment": None, "direction": None, "deflection": None, "total": 0.0, "area": None,
           "vertices": len(points)}
    for p, q in zip(pts, pts[1:]):
        out["total"] += length_of(p, q)
    if len(pts) >= 2:
        p, q = pts[-2], pts[-1]
        out["segment"] = length_of(p, q)
        if p != q:
            out["direction"] = entry.azimuth(p[0], p[1], q[0], q[1])
    if len(pts) >= 3 and out["direction"] is not None:
        a, b = pts[-3], pts[-2]
        if a != b:
            out["deflection"] = entry.deflection(entry.azimuth(a[0], a[1], b[0], b[1]), out["direction"])
    if polygon and len(pts) >= 3:
        out["area"] = area_of(pts)
        out["perimeter"] = out["total"] + length_of(pts[-1], pts[0])
    return out


def hud_lines(values, convention="azimuth", dms=False, length_unit="m", area_unit="m²", decimals=2,
              length_factor=1.0, area_factor=1.0):
    """The HUD's text lines for ``sketch_values`` output."""
    lines = []

    def ln(v):
        return f"{v * length_factor:,.{decimals}f} {length_unit}"

    if values.get("segment") is not None:
        lines.append(tr("Length") + ": " + ln(values["segment"]))
    if values.get("direction") is not None:
        label = tr("Bearing") if convention == "azimuth" else tr("Angle")
        lines.append(label + ": " + entry.format_direction(values["direction"], convention, dms))
    if values.get("deflection") is not None:
        lines.append(tr("Deflection") + ": " + entry.format_deflection(values["deflection"], dms))
    if values.get("total") and values.get("vertices", 0) >= 2:
        lines.append(tr("Total") + ": " + ln(values["total"]))
    if values.get("area") is not None:
        lines.append(tr("Polygon area") + ": " + f"{values['area'] * area_factor:,.{decimals}f} {area_unit}")
    return lines


class Measurer:
    """Lengths and areas of the sketch in the project's units: planar on a projected map, on the
    ellipsoid on a geographic one."""

    def __init__(self, crs, project=None):
        project = project or QgsProject.instance()
        self.crs = crs
        self.da = None
        map_units = crs.mapUnits() if crs.isValid() else Qgis.DistanceUnit.Meters
        if crs.isValid() and crs.isGeographic():
            self.da = QgsDistanceArea()
            self.da.setSourceCrs(crs, project.transformContext())
            self.da.setEllipsoid(project.ellipsoid() if project.ellipsoid() not in ("", "NONE") else "EPSG:7030")
            map_units = Qgis.DistanceUnit.Meters
        self.length_unit = project.distanceUnits()
        self.area_unit = project.areaUnits()
        self.length_factor = QgsUnitTypes.fromUnitToUnitFactor(map_units, self.length_unit)
        self.area_factor = QgsUnitTypes.fromUnitToUnitFactor(QgsUnitTypes.distanceToAreaUnit(map_units),
                                                             self.area_unit)
        self.length_label = QgsUnitTypes.toAbbreviatedString(self.length_unit)
        self.area_label = QgsUnitTypes.toAbbreviatedString(self.area_unit)

    def length(self, p, q):
        if self.da is None:
            return math.hypot(q[0] - p[0], q[1] - p[1])
        return self.da.measureLine(QgsPointXY(*p), QgsPointXY(*q))

    def area(self, ring):
        if self.da is None:
            return _area(ring)
        from qgis.core import QgsGeometry

        return self.da.measureArea(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in ring]]))

    def lines(self, points, cursor, polygon, convention="azimuth", dms=False):
        v = sketch_values(points, cursor, polygon, self.length, self.area)
        return hud_lines(v, convention, dms, self.length_label, self.area_label, 2, self.length_factor,
                         self.area_factor)


def snap_tip(match):
    """What a QgsPointLocator.Match snapped to, in words ("" when nothing)."""
    if match is None or not match.isValid():
        return ""
    from qgis.core import QgsPointLocator

    t = match.type()
    T = QgsPointLocator.Type
    layer = match.layer()
    if t & T.LineEndpoint:
        what = tr("Endpoint")
    elif t & T.MiddleOfSegment:
        what = tr("Midpoint")
    elif t & T.Centroid:
        what = tr("Centroid")
    elif t & T.Vertex:
        if layer is None:
            what = tr("Intersection")
        else:
            what = tr("Vertex")
            try:
                f = layer.getFeature(match.featureId())
                g = f.geometry()
                if g.type() == Qgis.GeometryType.Line and not g.isMultipart():
                    n = len(g.asPolyline())
                    if match.vertexIndex() in (0, n - 1):
                        what = tr("Endpoint")
            except Exception:  # noqa: BLE001 - a tip is never worth an error
                pass
    elif t & T.Edge:
        what = tr("Edge")
    elif t & T.Area:
        what = tr("Inside")
    else:
        return ""
    return f"{what} · {layer.name()}" if layer is not None else what


class SketchHud(QgsMapCanvasItem):
    """A floating box of text lines near the cursor."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.lines = []
        self.tip = ""
        self.entry_line = ""
        self.anchor = QgsPointXY(0, 0)
        self._rect = QRectF()
        self.setZValue(1000)
        self.font = QFont()
        self.font.setPointSizeF(8.5)

    def set_content(self, anchor, lines, tip="", entry_line=""):
        self.prepareGeometryChange()
        self.anchor = QgsPointXY(anchor)
        self.lines = list(lines)
        self.tip = tip or ""
        self.entry_line = entry_line or ""
        self.updatePosition()
        self.update()

    def text_lines(self):
        out = []
        if self.tip:
            out.append(self.tip)
        out.extend(self.lines)
        if self.entry_line:
            out.append(self.entry_line)
        return out

    def updatePosition(self):  # noqa: N802
        self.setPos(self.toCanvasCoordinates(self.anchor))

    def boundingRect(self):  # noqa: N802
        fm = QFontMetricsF(self.font)
        rows = self.text_lines()
        if not rows:
            return QRectF()
        w = max(fm.horizontalAdvance(r) for r in rows) + 12
        h = fm.height() * len(rows) + 8
        return QRectF(16, 16, w, h)

    def paint(self, painter, option=None, widget=None):
        rows = self.text_lines()
        if not rows:
            return
        rect = self.boundingRect()
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
        painter.setBrush(QColor(20, 24, 33, 205))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setFont(self.font)
        fm = QFontMetricsF(self.font)
        y = rect.top() + 4 + fm.ascent()
        for i, r in enumerate(rows):
            if i == 0 and self.tip:
                painter.setPen(QColor("#FBBF24"))
            elif self.entry_line and i == len(rows) - 1:
                painter.setPen(QColor("#7DD3FC"))
            else:
                painter.setPen(QColor(Qt.GlobalColor.white))
            painter.drawText(QPointF(rect.left() + 6, y), r)
            y += fm.height()
        painter.restore()
