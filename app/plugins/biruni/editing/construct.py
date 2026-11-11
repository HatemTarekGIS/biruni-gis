"""Construction tools of the Create Features pane: one map tool that draws a new feature of a template
in one of many ways (point at a distance and direction, right-angle lines, arcs, rectangles, circles,
auto-complete polygons, fill …) with typed lengths and directions while drawing.

The tool is a QgsMapToolAdvancedDigitizing: when QGIS's Advanced Digitizing panel is on, its
constraints and snapping drive the points; otherwise the project's snapping is used directly. Every
finished feature is added to the layer's edit buffer in one edit command (one Undo step), in the
layer's CRS, with the template's attribute values.

Keys while drawing: L length, A direction, D deflection, X / Y coordinates (type the value, Tab to the
next field, Enter to apply), P parallel / R perpendicular to the segment under the cursor, F2 or a
right click finishes, Esc cancels the current segment (twice: the whole sketch), Ctrl+Z or Backspace
removes the last vertex.
"""
import math

from qgis.core import (Qgis, QgsAbstractGeometry, QgsCircle, QgsCircularString, QgsCompoundCurve,
                       QgsCoordinateTransform, QgsCurvePolygon, QgsEllipse, QgsFeatureRequest, QgsGeometry,
                       QgsLineString, QgsPoint, QgsPointXY, QgsPolygon, QgsProject, QgsRectangle,
                       QgsRegularPolygon, QgsTracer, QgsUnitTypes, QgsVectorLayer, QgsWkbTypes)
from qgis.gui import QgsMapToolAdvancedDigitizing, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..i18n import tr
from ..rules import renumber
from . import autoshape, entry, fill, hud
from . import templates as tpl

POINT, LINE, POLYGON = "point", "line", "polygon"


def geometry_kind(layer):
    gt = layer.geometryType()
    if gt == Qgis.GeometryType.Point:
        return POINT
    if gt == Qgis.GeometryType.Line:
        return LINE
    return POLYGON


def tools_for(kind):
    """[(tool id, label, icon)] of the construction tools for a geometry kind ("point"/"line"/"polygon")."""
    if kind == POINT:
        return [("point", tr("Point"), "point"),
                ("point_distance", tr("Point at distance and direction"), "compass"),
                ("points_along", tr("Points along a line"), "dots-circle-horizontal"),
                ("point_line_end", tr("Point at the end of a line"), "arrow-bar-to-right")]
    if kind == LINE:
        return [("polyline", tr("Line"), "line"),
                ("right_angle_line", tr("Right-angle line"), "angle"),
                ("trace", tr("Trace along features"), "route-2"),
                ("freehand", tr("Freehand"), "feather")]
    return [("polygon", tr("Polygon"), "polygon"),
            ("right_angle_polygon", tr("Right-angle polygon"), "square"),
            ("rectangle", tr("Rectangle"), "rectangle"),
            ("circle", tr("Circle"), "circle"),
            ("circle3", tr("Circle through 3 points"), "circle-dot"),
            ("ellipse", tr("Ellipse"), "circles"),
            ("regular_polygon", tr("Regular polygon"), "hexagon"),
            ("auto_complete", tr("Auto-complete polygon"), "lasso-polygon"),
            ("fill", tr("Fill an enclosed area"), "paint"),
            ("freehand", tr("Freehand"), "feather")]


def segment_kinds():
    """[(id, label, icon)] of the segment types inside a line or polygon."""
    return [("line", tr("Straight segment"), "line"),
            ("arc3", tr("Arc through a point"), "vector-spline"),
            ("arc_radius", tr("Arc to an end point with a radius"), "vector-bezier-2"),
            ("arc_tangent", tr("Tangent arc"), "wave-sine")]


DEFAULTS = {"segment": "line", "radius": 0.0, "spacing": 10.0, "parts": 4, "along": "distance",
            "include_ends": True, "sides": 6, "chord": 0.01, "convention": "azimuth", "dms": False,
            "simplify_px": 2.0, "fill_tolerance": 0.1, "snap_px": 12, "autoshape": True}


# ---- pure plane geometry (x, y tuples) ----------------------------------------------------------------
def _sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def _norm(v):
    d = math.hypot(v[0], v[1])
    return (v[0] / d, v[1] / d) if d else (0.0, 0.0)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def unit_of(az):
    a = math.radians(az)
    return math.sin(a), math.cos(a)


def rectangle_points(p1, p2, p3):
    """The 4 corners of the rectangle with side p1→p2 and its width given by p3 (distance from the side)."""
    u = _norm(_sub(p2, p1))
    n = (-u[1], u[0])
    w = _dot(_sub(p3, p2), n)
    return [p1, p2, (p2[0] + w * n[0], p2[1] + w * n[1]), (p1[0] + w * n[0], p1[1] + w * n[1])]


def square_close(pts):
    """Corner that closes a right-angle polygon: the last edge ⟂ to the one before, the closing edge ⟂ to
    the first edge. None when those are parallel (the polygon then closes straight)."""
    if len(pts) < 3:
        return None
    v0, v1 = pts[0], pts[1]
    vn, vp = pts[-1], pts[-2]
    d1 = _sub(vn, vp)
    d1 = (-d1[1], d1[0])            # direction of the new edge from vn
    d2 = _sub(v1, v0)
    d2 = (-d2[1], d2[0])            # direction of the closing edge into v0
    den = _cross(d1, d2)
    if abs(den) < 1e-12 * (math.hypot(*d1) * math.hypot(*d2) or 1):
        return None
    t = _cross(_sub(v0, vn), d2) / den
    return vn[0] + t * d1[0], vn[1] + t * d1[1]


def arc_mid_radius(a, b, radius, side):
    """Middle point of the minor arc from a to b with ``radius`` bulging to ``side`` (+1 left of a→b)."""
    c = math.hypot(b[0] - a[0], b[1] - a[1])
    if c == 0:
        return None
    r = max(abs(radius), c / 2.0)
    h = math.sqrt(max(r * r - c * c / 4.0, 0.0))
    sag = r - h
    u = _norm(_sub(b, a))
    n = (-u[1], u[0])
    m = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    s = 1.0 if side >= 0 else -1.0
    return m[0] + s * sag * n[0], m[1] + s * sag * n[1]


def circle_centre(a, m, b):
    c = QgsCircle.from3Points(QgsPoint(*a), QgsPoint(*m), QgsPoint(*b))
    if c.isEmpty() or c.radius() <= 0 or not math.isfinite(c.radius()):
        return None, 0.0
    return (c.center().x(), c.center().y()), c.radius()


def tangent_arc_mid(a, az, b):
    """Middle point of the arc from a to b that leaves a in direction ``az`` (None when b is straight
    ahead or behind)."""
    t = unit_of(az)
    ch = _sub(b, a)
    cr = _cross(t, ch)
    if abs(cr) < 1e-12 * max(math.hypot(*ch), 1e-12):
        return None
    left = cr > 0
    n = (-t[1], t[0]) if left else (t[1], -t[0])
    r = _dot(ch, ch) / (2.0 * _dot(ch, n))
    c = (a[0] + r * n[0], a[1] + r * n[1])
    a0 = math.atan2(a[1] - c[1], a[0] - c[0])
    a1 = math.atan2(b[1] - c[1], b[0] - c[0])
    sweep = a1 - a0
    if left:   # counter-clockwise
        while sweep <= 0:
            sweep += 2 * math.pi
    else:
        while sweep >= 0:
            sweep -= 2 * math.pi
    am = a0 + sweep / 2.0
    return c[0] + r * math.cos(am), c[1] + r * math.sin(am)


def arc_end_direction(a, m, b):
    """Azimuth of the tangent at the end b of the arc a → m → b."""
    c, _ = circle_centre(a, m, b)
    if c is None:
        return entry.azimuth(a[0], a[1], b[0], b[1])
    rv = _sub(b, c)
    ccw = _cross(_sub(m, a), _sub(b, m)) > 0
    t = (-rv[1], rv[0]) if ccw else (rv[1], -rv[0])
    return entry.azimuth(0, 0, t[0], t[1])


def along_distances(length, spacing=None, parts=None, include_ends=True):
    """Distances along a line of ``length`` for points every ``spacing`` or at ``parts`` equal parts."""
    out = []
    if parts:
        n = max(int(parts), 1)
        out = [length * i / n for i in range(n + 1)]
    elif spacing and spacing > 0:
        k = 0
        while k * spacing <= length + 1e-9:
            out.append(min(k * spacing, length))
            k += 1
        if include_ends and length - out[-1] > 1e-9:
            out.append(length)
    if not include_ends:
        out = [d for d in out if 1e-9 < d < length - 1e-9]
    return out


# ---- the map tool ---------------------------------------------------------------------------------------
class ConstructTool(QgsMapToolAdvancedDigitizing):
    featuresCreated = pyqtSignal(object, list)   # layer, [feature ids]
    message = pyqtSignal(str)

    def __init__(self, canvas, cad_dock, layer, template, mode, options=None, form_opener=None):
        super().__init__(canvas, cad_dock)
        self.layer = layer
        self.template = template or tpl.Template(layer.name(), layer.id())
        self.kind = geometry_kind(layer)
        self.mode = mode or tools_for(self.kind)[0][0]
        self.opt = dict(DEFAULTS)
        self.opt.update(options or {})
        self.form_opener = form_opener
        self.setAutoSnapEnabled(True)
        self.verts = []          # placed vertices (x, y) in map CRS
        self.segs = []           # per segment: None (straight) or (mx, my) (arc middle point)
        self.typed = set()       # indexes of vertices placed by a typed length and direction (a traverse)
        self.closure_opener = None  # (info) -> "adjust" | "keep" | None: asks what to do with a misclosure
        self.pending = None      # arc middle point / arc end waiting for its second click
        self.cursor = None
        self.raw_cursor = None
        self.match = None
        self.edge = None         # ((x1, y1), (x2, y2)) of the segment under the cursor
        self.locks = {}          # "L": map units, "A": azimuth, "D": deflection, "X", "Y"
        self.dir_lock = None     # azimuth from P / R
        self.field = None
        self.buffer = ""
        self.error = ""
        self.freehand_on = False
        self.last_fids = []
        self._band = self._vband = self._hud = None
        self._ghost = None       # the suggested shape (autoshape.py), faint
        self._suggest = (None, [])   # (sketch it was worked out for, suggestions)
        self._broken = (None, [])    # (sketch, notes of the learned rules it breaks)
        self._tracer = None
        self._measurer = None

    # -- life cycle --
    def activate(self):
        super().activate()
        self._ensure_items()
        self.canvas().setFocus()

    def deactivate(self):
        self.reset()
        for item in (self._band, self._vband, self._hud, self._ghost):
            if item is not None:
                self.canvas().scene().removeItem(item)
        self._band = self._vband = self._hud = self._ghost = None
        super().deactivate()

    def isEditTool(self):  # noqa: N802
        return True

    def set_mode(self, mode):
        self.reset()
        self.mode = mode

    def set_option(self, key, value):
        self.opt[key] = value
        self._refresh()

    def reset(self):
        self.verts, self.segs = [], []
        self.typed = set()
        self.pending = None
        self.locks, self.dir_lock = {}, None
        self.field, self.buffer, self.error = None, "", ""
        self.freehand_on = False
        self._refresh()

    def _ensure_items(self):
        if self._band is None:
            c = self.canvas()
            self._band = QgsRubberBand(c, Qgis.GeometryType.Polygon if self.kind == POLYGON else
                                       Qgis.GeometryType.Line)
            self._band.setColor(QColor(255, 120, 0, 200))
            self._band.setFillColor(QColor(255, 120, 0, 50))
            self._band.setWidth(2)
            self._vband = QgsRubberBand(c, Qgis.GeometryType.Point)
            self._vband.setColor(QColor(255, 120, 0))
            self._vband.setIconSize(6)
            self._vband.setIcon(QgsRubberBand.IconType.ICON_BOX)
            self._hud = hud.SketchHud(c)
            self._ghost = QgsRubberBand(c, Qgis.GeometryType.Polygon)
            self._ghost.setStrokeColor(QColor(40, 110, 220, 170))
            self._ghost.setFillColor(QColor(40, 110, 220, 35))
            self._ghost.setWidth(2)
            self._ghost.setLineStyle(Qt.PenStyle.DashLine)

    # -- coordinates --
    def map_crs(self):
        return self.canvas().mapSettings().destinationCrs()

    def metres_per_map_unit(self):
        crs = self.map_crs()
        if not crs.isValid():
            return 1.0
        return QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), Qgis.DistanceUnit.Meters) or 1.0

    def to_layer(self):
        return QgsCoordinateTransform(self.map_crs(), self.layer.crs(), QgsProject.instance())

    def from_layer(self, layer):
        return QgsCoordinateTransform(layer.crs(), self.map_crs(), QgsProject.instance())

    def measurer(self):
        if self._measurer is None or self._measurer.crs != self.map_crs():
            self._measurer = hud.Measurer(self.map_crs())
        return self._measurer

    def _cad_on(self):
        cad = self.cadDockWidget()
        return cad is not None and cad.cadEnabled()

    def _event_point(self, e):
        """The (snapped / CAD-constrained) map point of a mouse event and the snap match."""
        if self._cad_on():
            m = e.mapPointMatch()
            p = e.mapPoint()
            return (p.x(), p.y()), (m if m.isValid() else None)
        orig = e.originalMapPoint()
        m = self.canvas().snappingUtils().snapToMap(orig)
        if m.isValid():
            return (m.point().x(), m.point().y()), m
        return (orig.x(), orig.y()), None

    def _remember_match(self, m):
        self.match = m
        if m is not None and m.hasEdge():
            a, b = m.edgePoints()
            self.edge = ((a.x(), a.y()), (b.x(), b.y()))

    # -- constraints --
    def _base(self):
        if self.mode in ("circle", "regular_polygon") and self.verts:
            return self.verts[0]
        if self.mode == "ellipse" and len(self.verts) == 2:
            return self.verts[1]
        return self.verts[-1] if self.verts else None

    def _prev_direction(self):
        if len(self.verts) < 2:
            return None
        a, b = self.verts[-2], self.verts[-1]
        mid = self.segs[-1] if self.segs else None
        if mid is not None:
            return arc_end_direction(a, mid, b)
        return entry.azimuth(a[0], a[1], b[0], b[1])

    def _perpendicular_step(self):
        """Direction (azimuth) the next point must follow, without sign, or None."""
        if self.mode in ("right_angle_line", "right_angle_polygon") and len(self.verts) >= 2:
            return (self._prev_direction() + 90.0) % 360.0
        if self.mode == "rectangle" and len(self.verts) == 2:
            return (self._prev_direction() + 90.0) % 360.0
        if self.mode == "ellipse" and len(self.verts) == 2:
            a, b = self.verts
            return (entry.azimuth(a[0], a[1], b[0], b[1]) + 90.0) % 360.0
        return None

    def constrain(self, p):
        """Apply the typed / locked values to a cursor point (x, y)."""
        base = self._base()
        x, y = p if p is not None else (base if base is not None else (0.0, 0.0))
        if base is None:
            return self.locks.get("X", x), self.locks.get("Y", y)
        az, signed = None, True
        if "A" in self.locks:
            az = self.locks["A"]
        elif "D" in self.locks and self._prev_direction() is not None:
            az = (self._prev_direction() + self.locks["D"]) % 360.0
        elif self.dir_lock is not None:
            az, signed = self.dir_lock, False
        else:
            perp = self._perpendicular_step()
            if perp is not None:
                az, signed = perp, False
        if az is not None and not signed:
            u = unit_of(az)
            if _dot((x - base[0], y - base[1]), u) < 0 and "L" not in self.locks:
                az = (az + 180.0) % 360.0
            elif "L" in self.locks and p is not None and _dot((x - base[0], y - base[1]), u) < 0:
                az = (az + 180.0) % 360.0
        if az is not None:
            u = unit_of(az)
            if "L" in self.locks:
                d = self.locks["L"]
            elif "X" in self.locks and abs(u[0]) > 1e-12:
                d = (self.locks["X"] - base[0]) / u[0]
            elif "Y" in self.locks and abs(u[1]) > 1e-12:
                d = (self.locks["Y"] - base[1]) / u[1]
            else:
                d = _dot((x - base[0], y - base[1]), u)
            return base[0] + d * u[0], base[1] + d * u[1]
        if "L" in self.locks:
            v = (x - base[0], y - base[1])
            if v == (0.0, 0.0):
                v = (0.0, 1.0)
            u = _norm(v)
            return base[0] + self.locks["L"] * u[0], base[1] + self.locks["L"] * u[1]
        return self.locks.get("X", x), self.locks.get("Y", y)

    # -- mouse --
    def cadCanvasMoveEvent(self, e):  # noqa: N802
        p, m = self._event_point(e)
        self._remember_match(m)
        self.raw_cursor = p
        self.cursor = self.constrain(p)
        if self.mode == "freehand" and self.freehand_on:
            last = self.verts[-1]
            if math.hypot(p[0] - last[0], p[1] - last[1]) >= self.canvas().mapUnitsPerPixel():
                self.verts.append(p)
                self.segs.append(None)
        self._refresh()

    def cadCanvasPressEvent(self, e):  # noqa: N802
        if self.mode == "freehand" and e.button() == Qt.MouseButton.LeftButton:
            p, _ = self._event_point(e)
            self.verts, self.segs = [p], []
            self.freehand_on = True

    def cadCanvasReleaseEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.RightButton:
            if self.verts or self.pending is not None:
                self.finish()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        p, m = self._event_point(e)
        self._remember_match(m)
        if self.mode == "freehand":
            if self.freehand_on:
                if p != self.verts[-1]:
                    self.verts.append(p)
                    self.segs.append(None)
                self.freehand_on = False
                self.finish()
            return
        self.raw_cursor = p
        self.click(self.constrain(p))

    # -- placing points --
    def click(self, p):
        """One click (or Enter) at map point ``p`` (already constrained)."""
        self.error = ""
        mode = self.mode
        typed = "L" in self.locks and ("A" in self.locks or "D" in self.locks or self.dir_lock is not None)
        before = len(self.verts)
        self.locks, self.dir_lock = {}, None
        self.field, self.buffer = None, ""
        if mode == "point":
            self.commit([QgsGeometry.fromPointXY(QgsPointXY(*p))])
        elif mode == "point_distance":
            if not self.verts:
                self.verts = [p]
            else:
                self.commit([QgsGeometry.fromPointXY(QgsPointXY(*p))])
                self.reset()
        elif mode in ("points_along", "point_line_end"):
            line = self.nearest_line(p)
            if line is None:
                self.error = tr("Click on a line.")
            else:
                self.commit(self.points_on_line(line, p))
        elif mode == "fill":
            g = self.fill_at(p)
            if g is None:
                self.error = tr("No enclosed area here.")
            else:
                self.commit([g])
        elif mode in ("rectangle", "circle3"):
            self.verts.append(p)
            if len(self.verts) == 3:
                self.finish()
        elif mode in ("circle", "regular_polygon"):
            self.verts.append(p)
            if len(self.verts) == 2:
                self.finish()
        elif mode == "ellipse":
            self.verts.append(p)
            if len(self.verts) == 3:
                self.finish()
        elif mode == "trace" and self.verts:
            self._add_traced(p)
        else:
            self._add_vertex(p)
            if typed and len(self.verts) == before + 1:
                self.typed.add(len(self.verts) - 1)
        self._refresh()

    def _add_vertex(self, p):
        seg = self.opt.get("segment", "line") if self.mode in ("polyline", "polygon", "auto_complete") else "line"
        if not self.verts or seg == "line":
            if not self.verts or p != self.verts[-1]:
                self.verts.append(p)
                if len(self.verts) > 1:
                    self.segs.append(None)
            return
        a = self.verts[-1]
        if seg == "arc3":
            if self.pending is None:
                self.pending = p
                return
            mid, self.pending = self.pending, None
        elif seg == "arc_radius":
            if self.pending is None:
                self.pending = p      # the end point; the next click gives the side (and the bulge)
                return
            end, side_point = self.pending, p
            self.pending = None
            r = self.opt.get("radius") or 0.0
            side = 1 if _cross(_sub(end, a), _sub(side_point, a)) >= 0 else -1
            if r > 0:
                mid = arc_mid_radius(a, end, r, side)
            else:
                mid = side_point   # through the clicked point
            p = end
        else:   # tangent arc
            prev = self._prev_direction()
            mid = tangent_arc_mid(a, prev, p) if prev is not None else None
        if mid is None or p == a:
            self.verts.append(p)
            self.segs.append(None)
            return
        self.verts.append(p)
        self.segs.append(tuple(mid))

    def _add_traced(self, p):
        a = self.verts[-1]
        path = self.trace_path(a, p)
        for q in (path[1:] if path else [p]):
            self.verts.append(q)
            self.segs.append(None)

    def tracer(self):
        if self._tracer is None:
            self._tracer = QgsTracer()
        layers = [lyr for lyr in self.canvas().layers() if isinstance(lyr, QgsVectorLayer)
                  and lyr.geometryType() in (Qgis.GeometryType.Line, Qgis.GeometryType.Polygon)]
        self._tracer.setLayers(layers)
        self._tracer.setDestinationCrs(self.map_crs(), QgsProject.instance().transformContext())
        self._tracer.setExtent(self.canvas().extent())
        return self._tracer

    def trace_path(self, a, b):
        try:
            pts, err = self.tracer().findShortestPath(QgsPointXY(*a), QgsPointXY(*b))
        except Exception:  # noqa: BLE001 - fall back to a straight segment
            return None
        if not pts or err != QgsTracer.PathError.ErrNone:
            return None
        return [(q.x(), q.y()) for q in pts]

    # -- keyboard --
    _FIELD_KEYS = {Qt.Key.Key_L: "L", Qt.Key.Key_A: "A", Qt.Key.Key_D: "D", Qt.Key.Key_X: "X", Qt.Key.Key_Y: "Y"}
    _FIELD_ORDER = ["L", "A", "D", "X", "Y"]

    def shortcutEvent(self, e):  # noqa: N802
        """Keep Ctrl+Z (last vertex) and typed letters from the window's shortcuts while sketching."""
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        busy = bool(self.verts) or self.pending is not None
        if (ctrl and e.key() == Qt.Key.Key_Z and busy) or (self.field is not None and not ctrl) or \
                (not ctrl and e.key() in self._FIELD_KEYS and self._accepts_entry()):
            e.accept()
            return True
        return False

    def keyPressEvent(self, e):  # noqa: N802
        if self.handle_key(e.key(), e.text(), e.modifiers()):
            e.accept()
        else:
            e.ignore()

    def handle_key(self, key, text="", mods=Qt.KeyboardModifier.NoModifier):
        """Returns True when the key was used."""
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if self.field is not None:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._commit_field():
                    if self.locks or self.dir_lock is not None:
                        self.apply()
                    else:   # e.g. only the arc radius was typed
                        self.field, self.buffer = None, ""
                        self._refresh()
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                if self._commit_field():
                    order = self._FIELD_ORDER
                    step = -1 if key == Qt.Key.Key_Backtab else 1
                    self.field = order[(order.index(self.field) + step) % len(order)] if self.field else "L"
                    self.buffer = ""
                self._refresh()
                return True
            if key == Qt.Key.Key_Escape:
                self.field, self.buffer, self.error = None, "", ""
                self._refresh()
                return True
            if key == Qt.Key.Key_Backspace:
                self.buffer = self.buffer[:-1]
                self._refresh()
                return True
            if text and text.isprintable() and not ctrl:
                self.buffer += text
                self._refresh()
                return True
            return False
        if ctrl and key == Qt.Key.Key_Z or key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not ctrl:
            if self.pending is not None:
                self.pending = None
            elif self.verts:
                self.verts.pop()
                if self.segs and len(self.segs) >= len(self.verts):
                    self.segs.pop()
            else:
                return False
            self._refresh()
            return True
        if ctrl:
            return False
        if key in self._FIELD_KEYS and self._accepts_entry():
            self.field = self._FIELD_KEYS[key]
            self.buffer = ""
            self.error = ""
            self._refresh()
            return True
        if key == Qt.Key.Key_Tab and self.take_suggestion():
            return True
        if key == Qt.Key.Key_Tab and self._accepts_entry():
            self.field, self.buffer = "L", ""
            self._refresh()
            return True
        if key in (Qt.Key.Key_P, Qt.Key.Key_R) and self._accepts_entry():
            if self.edge is None:
                self.error = tr("Point at a segment first, then press P or R.")
            else:
                (x1, y1), (x2, y2) = self.edge
                az = entry.azimuth(x1, y1, x2, y2)
                self.dir_lock = az if key == Qt.Key.Key_P else (az + 90.0) % 360.0
                self.locks.pop("A", None)
                self.locks.pop("D", None)
            self._refresh()
            return True
        if key == Qt.Key.Key_F2:
            self.finish()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.locks or self.dir_lock is not None:
                self.apply()
            elif self.verts:
                self.finish()
            return True
        if key == Qt.Key.Key_Escape:
            if self.locks or self.dir_lock is not None or self.error:
                self.locks, self.dir_lock, self.error = {}, None, ""
            elif self.pending is not None:
                self.pending = None
            elif self.verts:
                self.reset()
            else:
                return False
            self._refresh()
            return True
        return False

    # -- shape completion --
    def suggestion(self):
        """The shape offered for the polygon being drawn (autoshape.py), or None: only while one or two
        sides are drawn by clicking (a typed traverse is the surveyor's own and is never completed)."""
        if self.mode != "polygon" or self.kind != POLYGON or not self.opt.get("autoshape", True) or \
                self.typed or self.pending is not None or any(sg is not None for sg in self.segs) or \
                not 2 <= len(self.verts) <= 3:
            return None
        key = tuple(self.verts)
        if self._suggest[0] != key:
            x0, y0, x1, y1 = autoshape.near_rect(self.verts)
            try:
                near = self.features_near(QgsRectangle(x0, y0, x1, y1), (), extra=[self.layer])
            except Exception:  # noqa: BLE001 - no suggestion rather than a broken tool
                near = []
            self._suggest = (key, autoshape.suggestions(self.verts, near))
        return autoshape.pick(self._suggest[1], self.verts, self.cursor)

    def rule_notes(self):
        """What the polygon being drawn breaks among the rules learned from this layer's data
        ("Rules from your data", insight/learned.py) - only rules already learned: drawing never waits."""
        if self.kind != POLYGON or len(self.verts) < 3 or self.mode not in ("polygon", "right_angle_polygon"):
            return []
        key = tuple(self.verts)
        if self._broken[0] != key:
            notes = []
            try:
                from ..insight import learned

                if learned.cached_rules(self.layer):
                    g = QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in self.verts + self.verts[:1]]])
                    ct = self.to_layer()
                    if not ct.isShortCircuited():
                        g.transform(ct)
                    notes = [v.short() for v in learned.broken_rules(self.layer, g, mine=False)][:2]
            except Exception:  # noqa: BLE001 - a hint, never a broken tool
                notes = []
            self._broken = (key, notes)
        return self._broken[1]

    def take_suggestion(self):
        """Tab: the suggested shape becomes the polygon. False when nothing is suggested."""
        s = self.suggestion()
        if s is None or self.field is not None:
            return False
        self.verts = list(s[0])
        self.segs = [None] * (len(self.verts) - 1)
        self.finish()
        return True

    def _accepts_entry(self):
        return self.mode not in ("fill", "points_along", "point_line_end", "freehand")

    def _commit_field(self):
        """Store the typed value of the current field as a lock; False (and an error) when unreadable."""
        f, text = self.field, self.buffer.strip()
        if not text:
            self.locks.pop(f, None)
            return True
        try:
            if f == "L":
                v = entry.length_value(text, self.metres_per_map_unit())
                if self.mode == "polyline" and self.opt.get("segment") == "arc_radius" or \
                        self.mode in ("polygon", "auto_complete") and self.opt.get("segment") == "arc_radius":
                    self.opt["radius"] = abs(v)
                else:
                    self.locks["L"] = v
            elif f == "A":
                self.locks["A"] = entry.parse_direction(text, self.opt.get("convention", "azimuth"))
                self.locks.pop("D", None)
                self.dir_lock = None
            elif f == "D":
                self.locks["D"] = entry.parse_deflection(text)
                self.locks.pop("A", None)
                self.dir_lock = None
            else:
                self.locks[f] = entry.parse_coordinate(text)
        except ValueError:
            self.error = tr("Cannot read “{t}”.").format(t=text)
            self._refresh()
            return False
        self.error = ""
        return True

    def apply(self):
        """Enter: place the next point from the locked values (and the cursor for what is not locked)."""
        self.field, self.buffer = None, ""
        p = self.constrain(self.raw_cursor)
        self.click(p)

    # -- finishing --
    def finish(self):
        mode = self.mode
        geoms = None
        try:
            if mode in ("polyline", "right_angle_line", "trace", "freehand") and self.kind == LINE:
                if len(self.verts) >= 2:
                    g = self.line_geometry()
                    if mode == "freehand":
                        g = self.simplified(g)
                    geoms = [g]
            elif mode == "freehand" and self.kind == POLYGON:
                if len(self.verts) >= 3:
                    g = self.simplified(self.line_geometry())
                    pl = g.asPolyline()
                    geoms = [QgsGeometry.fromPolygonXY([pl + [pl[0]]])] if len(pl) >= 3 else None
            elif mode in ("polygon", "right_angle_polygon"):
                if mode == "right_angle_polygon" and len(self.verts) >= 3:
                    c = square_close(self.verts)
                    if c is not None and math.hypot(c[0] - self.verts[0][0], c[1] - self.verts[0][1]) > 1e-9:
                        self.verts.append(c)
                        self.segs.append(None)
                if mode == "polygon" and not self._close_traverse():
                    self._refresh()
                    return   # the user went back to the sketch
                if len(self.verts) >= 3:
                    geoms = [self.polygon_geometry()]
            elif mode == "auto_complete":
                if len(self.verts) >= 2:
                    g = self.auto_complete_geometry()
                    if g is None:
                        self.error = tr("The line does not close an area with the polygons around it.")
                        self._refresh()
                        return
                    geoms = [g]
            elif mode in ("rectangle", "circle", "circle3", "ellipse", "regular_polygon"):
                g = self.shape_geometry(self.verts)
                geoms = [g] if g is not None else None
        except Exception as ex:  # noqa: BLE001 - a bad sketch must not break the tool
            self.error = str(ex)
            self._refresh()
            return
        if geoms:
            self.commit(geoms)
        self.reset()

    def _close_traverse(self):
        """A polygon whose sides were typed (lengths and directions) and that comes back near its start is a
        closed traverse: its misclosure and precision (1:N) are shown, and the error can be distributed by
        the compass (Bowditch) rule. False when the user chose to go on drawing."""
        info = traverse_closure(self.verts, self.typed, self.segs)
        if info is None:
            return True
        if info["linear"] <= 1e-9:
            self.verts.pop()   # it closes exactly: the last point is the first
            return True
        mpu = self.metres_per_map_unit()
        info.update(linear_m=info["linear"] * mpu, dx_m=info["dx"] * mpu, dy_m=info["dy"] * mpu,
                    perimeter_m=info["perimeter"] * mpu)
        answer = (self.closure_opener or _default_closure_opener)(info)
        if answer is None:
            return False
        if answer == "adjust":
            self.verts = bowditch(self.verts)[:-1]
            self.segs = self.segs[:len(self.verts) - 1]
            self.message.emit(tr("Closed by the compass rule: misclosure {e} m, precision 1:{n}.").format(
                e=f"{info['linear_m']:.3f}", n=f"{info['precision']:,.0f}"))
        return True

    def line_geometry(self, closed=False):
        """The sketch as a geometry in map CRS: a LineString, or a CompoundCurve when it has arcs."""
        pts = list(self.verts)
        segs = list(self.segs) + [None] * (len(pts) - 1 - len(self.segs))
        if closed and pts[0] != pts[-1]:
            pts.append(pts[0])
            segs.append(None)
        if not any(s is not None for s in segs):
            return QgsGeometry(QgsLineString([QgsPoint(x, y) for x, y in pts]))
        cc = QgsCompoundCurve()
        run = [pts[0]]
        for i, s in enumerate(segs):
            a, b = pts[i], pts[i + 1]
            if s is None:
                run.append(b)
                continue
            if len(run) >= 2:
                cc.addCurve(QgsLineString([QgsPoint(x, y) for x, y in run]))
            cc.addCurve(QgsCircularString(QgsPoint(*a), QgsPoint(*s), QgsPoint(*b)))
            run = [b]
        if len(run) >= 2:
            cc.addCurve(QgsLineString([QgsPoint(x, y) for x, y in run]))
        return QgsGeometry(cc)

    def polygon_geometry(self):
        line = self.line_geometry(closed=True)   # keep it alive while its ring is copied
        ring = line.constGet().clone()
        if isinstance(ring, QgsLineString):
            poly = QgsPolygon()
        else:
            poly = QgsCurvePolygon()
        poly.setExteriorRing(ring)
        return QgsGeometry(poly)

    def shape_geometry(self, pts):
        mode = self.mode
        if mode == "rectangle" and len(pts) == 3:
            corners = rectangle_points(*pts)
            if fill.polygon_area(corners) <= 0:
                return None
            return QgsGeometry.fromPolygonXY([[QgsPointXY(*c) for c in corners + [corners[0]]]])
        if mode == "circle" and len(pts) == 2:
            r = math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
            if r <= 0:
                return None
            circ = QgsCircle(QgsPoint(*pts[0]), r)
            poly = QgsCurvePolygon()
            poly.setExteriorRing(circ.toCircularString())
            return QgsGeometry(poly)
        if mode == "circle3" and len(pts) == 3:
            circ = QgsCircle.from3Points(*[QgsPoint(*p) for p in pts])
            if circ.isEmpty() or not math.isfinite(circ.radius()) or circ.radius() <= 0:
                return None
            poly = QgsCurvePolygon()
            poly.setExteriorRing(circ.toCircularString())
            return QgsGeometry(poly)
        if mode == "ellipse" and len(pts) == 3:
            c, e1, p3 = pts
            a = math.hypot(e1[0] - c[0], e1[1] - c[1])
            u = _norm(_sub(e1, c))
            b = abs(_cross(u, _sub(p3, c)))
            if a <= 0 or b <= 0:
                return None
            az = entry.azimuth(c[0], c[1], e1[0], e1[1])
            ell = QgsEllipse(QgsPoint(*c), a, b, az)
            tol = max(self.opt.get("chord", 0.01), 1e-9)
            big = max(a, b)
            n = int(math.ceil(math.pi / math.acos(max(-1.0, 1 - tol / big)))) if tol < big else 32
            return QgsGeometry(ell.toPolygon(min(max(n, 32), 1440)))
        if mode == "regular_polygon" and len(pts) == 2:
            rp = QgsRegularPolygon(QgsPoint(*pts[0]), QgsPoint(*pts[1]), int(self.opt.get("sides", 6)),
                                   QgsRegularPolygon.ConstructionOption.InscribedCircle)
            return QgsGeometry(rp.toPolygon())
        return None

    def simplified(self, g):
        tol = self.opt.get("simplify_px", 2.0) * self.canvas().mapUnitsPerPixel()
        s = g.simplify(tol)
        return s if s is not None and not s.isEmpty() else g

    # -- geometry from the map --
    def _layers(self, kinds):
        return [lyr for lyr in self.canvas().layers() if isinstance(lyr, QgsVectorLayer)
                and lyr.geometryType() in kinds]

    def features_near(self, rect, kinds, extra=None):
        """Geometries (map CRS) of the visible layers of ``kinds`` that touch ``rect`` (map CRS)."""
        out = []
        layers = self._layers(kinds)
        for lyr in (extra or []):
            if lyr not in layers:
                layers.append(lyr)
        for lyr in layers:
            ct = self.from_layer(lyr)
            try:
                lrect = ct.transformBoundingBox(rect, Qgis.TransformDirection.Reverse)
            except Exception:  # noqa: BLE001
                continue
            req = QgsFeatureRequest().setFilterRect(lrect).setNoAttributes()
            for f in lyr.getFeatures(req):
                g = QgsGeometry(f.geometry())
                if g.isNull() or g.isEmpty():
                    continue
                if not ct.isShortCircuited():
                    g.transform(ct)
                out.append(g)
        return out

    def _search_rect(self, p, px=None):
        r = (px or self.opt.get("snap_px", 12)) * self.canvas().mapUnitsPerPixel()
        return QgsRectangle(p[0] - r, p[1] - r, p[0] + r, p[1] + r)

    def nearest_line(self, p):
        """The line (or polygon outline) nearest to map point ``p`` within the snapping distance, in map CRS."""
        pg = QgsGeometry.fromPointXY(QgsPointXY(*p))
        best, best_d = None, None
        for g in self.features_near(self._search_rect(p), (Qgis.GeometryType.Line, Qgis.GeometryType.Polygon)):
            for part in fill._parts(g):
                d = part.distance(pg)
                if best_d is None or d < best_d:
                    best, best_d = part, d
        return best

    def points_on_line(self, line, p):
        if self.mode == "point_line_end":
            pl = line.asPolyline()
            a, b = pl[0], pl[-1]
            q = a if a.distance(QgsPointXY(*p)) <= b.distance(QgsPointXY(*p)) else b
            return [QgsGeometry.fromPointXY(q)]
        length = line.length()
        if self.opt.get("along") == "parts":
            ds = along_distances(length, parts=self.opt.get("parts", 4), include_ends=self.opt.get("include_ends", True))
        else:
            ds = along_distances(length, spacing=self.opt.get("spacing", 10.0),
                                 include_ends=self.opt.get("include_ends", True))
        return [line.interpolate(d) for d in ds]

    def fill_at(self, p):
        ext = self.canvas().extent()
        geoms = self.features_near(ext, (Qgis.GeometryType.Line, Qgis.GeometryType.Polygon))
        return fill.fill_face(QgsPointXY(*p), geoms, self.opt.get("fill_tolerance", 0.1), ext)

    def auto_complete_geometry(self):
        sketch = self.line_geometry()
        rect = sketch.boundingBox()
        rect.grow(max(rect.width(), rect.height()) * 0.5 + self.opt.get("fill_tolerance", 0.1))
        polys = self.features_near(rect, (Qgis.GeometryType.Polygon,), extra=[self.layer])
        return fill.auto_complete(sketch, polys, self.opt.get("fill_tolerance", 0.1))

    # -- writing the features --
    def fit_to_layer(self, g):
        """A map-CRS geometry made right for the layer: curves densified when the layer has none, CRS,
        Z/M and single/multi type of the layer."""
        g = QgsGeometry(g)
        wkb = self.layer.wkbType()
        if QgsWkbTypes.isCurvedType(g.wkbType()) and not QgsWkbTypes.isCurvedType(wkb):
            tol = max(self.opt.get("chord", 0.01), 1e-9) / self.metres_per_map_unit()
            g = QgsGeometry(g.constGet().segmentize(tol, QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference))
        ct = self.to_layer()
        if not ct.isShortCircuited():
            g.transform(ct)
        geom = g.get()
        if QgsWkbTypes.hasZ(wkb) and not geom.is3D():
            geom.addZValue(0)
        if QgsWkbTypes.hasM(wkb) and not geom.isMeasure():
            geom.addMValue(0)
        if QgsWkbTypes.isMultiType(wkb) and not g.isMultipart():
            g.convertToMultiType()
        elif not QgsWkbTypes.isMultiType(wkb) and g.isMultipart():
            g.convertToSingleType()
        return g

    def commit(self, geoms):
        """Add one feature per geometry (map CRS) in one edit command; returns the new feature ids."""
        layer = self.layer
        if not layer.isEditable() and not layer.startEditing():
            self.error = tr("The layer cannot be edited.")
            self.message.emit(self.error)
            return []
        feats = [tpl.make_feature(layer, self.fit_to_layer(g), self.template) for g in geoms]
        renumber(layer, feats)
        label = tr("Create feature") if len(feats) == 1 else tr("Create {n} features").format(n=len(feats))
        before = set(layer.editBuffer().addedFeatures().keys()) if layer.editBuffer() else set()
        if len(feats) == 1 and tpl.needs_form(layer, self.template):
            # the form adds the feature itself (in its own edit command) when it is accepted
            opener = self.form_opener or _default_form_opener
            if not opener(layer, feats[0]):
                return []
        else:
            layer.beginEditCommand(label)
            if not layer.addFeatures(feats):
                layer.destroyEditCommand()
                return []
            layer.endEditCommand()
        after = set(layer.editBuffer().addedFeatures().keys()) if layer.editBuffer() else set()
        self.last_fids = sorted(after - before)
        layer.triggerRepaint()
        self.featuresCreated.emit(layer, self.last_fids)
        return self.last_fids

    # -- drawing the sketch --
    def preview(self):
        """The sketch with the cursor as a map-CRS geometry (None when there is nothing to show)."""
        pts = list(self.verts)
        cur = self.cursor
        mode = self.mode
        if mode in ("rectangle", "circle", "circle3", "ellipse", "regular_polygon"):
            if cur is not None:
                need = {"rectangle": 3, "circle": 2, "circle3": 3, "ellipse": 3, "regular_polygon": 2}[mode]
                if len(pts) == need - 1:
                    try:
                        g = self.shape_geometry(pts + [cur])
                    except Exception:  # noqa: BLE001
                        g = None
                    if g is not None:
                        return g
                pts = pts + [cur]
            if len(pts) >= 2:
                return QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])
            return None
        if cur is not None and pts and mode not in ("point", "fill", "points_along", "point_line_end"):
            pts = pts + [cur]
        if len(pts) < 2:
            return None
        return QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])

    def hud_lines(self):
        m = self.measurer()
        polygon = self.kind == POLYGON and self.mode not in ("rectangle", "circle", "circle3", "ellipse",
                                                             "regular_polygon", "fill")
        base_pts = list(self.verts)
        cur = self.cursor if self.mode not in ("point", "fill", "points_along", "point_line_end") else None
        if self.mode in ("circle", "regular_polygon") and base_pts and cur is not None:
            r = m.length(base_pts[0], cur)
            out = [tr("Radius") + f": {r * m.length_factor:,.2f} {m.length_label}"]
            if self.mode == "circle":
                out.append(tr("Polygon area") + f": {math.pi * r * r * m.area_factor:,.2f} {m.area_label}")
            return out
        if self.mode == "rectangle" and len(base_pts) == 2 and cur is not None:
            c = rectangle_points(base_pts[0], base_pts[1], cur)
            w = m.length(c[1], c[2])
            ln = m.length(c[0], c[1])
            return [tr("Length") + f": {ln * m.length_factor:,.2f} {m.length_label}",
                    tr("Width") + f": {w * m.length_factor:,.2f} {m.length_label}",
                    tr("Polygon area") + f": {m.area(c) * m.area_factor:,.2f} {m.area_label}"]
        if not base_pts:
            return []
        return m.lines(base_pts, cur, polygon, self.opt.get("convention", "azimuth"), self.opt.get("dms", False))

    def entry_text(self):
        names = {"L": tr("Length"), "A": tr("Direction"), "D": tr("Deflection"), "X": "X", "Y": "Y"}
        if self.field == "L" and self.opt.get("segment") == "arc_radius" and \
                self.mode in ("polyline", "polygon", "auto_complete"):
            names["L"] = tr("Radius")
        parts = []
        for k in self._FIELD_ORDER:
            if k in self.locks and k != self.field:
                parts.append(f"{names[k]}🔒")
        if self.dir_lock is not None:
            parts.append(tr("Direction") + "🔒")
        if self.field is not None:
            parts.append(f"{names[self.field]}: {self.buffer}▏")
        if self.error:
            parts.append(self.error)
        return "  ".join(parts)

    def _refresh(self):
        if self._band is None:
            return
        g = self.preview()
        if g is None:
            self._band.reset(Qgis.GeometryType.Polygon if self.kind == POLYGON else Qgis.GeometryType.Line)
        else:
            if self.kind == POLYGON and g.type() == Qgis.GeometryType.Line and len(g.asPolyline()) >= 3:
                pl = g.asPolyline()
                g = QgsGeometry.fromPolygonXY([pl + [pl[0]]])
            if QgsWkbTypes.isCurvedType(g.wkbType()):
                g = QgsGeometry(g.constGet().segmentize())
            self._band.setToGeometry(g, None)
        self._vband.reset(Qgis.GeometryType.Point)
        for p in self.verts + ([self.pending] if self.pending else []):
            self._vband.addPoint(QgsPointXY(*p), False)
        self._vband.updatePosition()
        self._vband.update()
        sug = self.suggestion()
        if self._ghost is not None:
            if sug is None:
                self._ghost.reset(Qgis.GeometryType.Polygon)
            else:
                self._ghost.setToGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in sug[0] + sug[0][:1]]]),
                                          None)
        anchor = self.cursor or (self.verts[-1] if self.verts else None)
        if anchor is not None:
            lines = self.hud_lines()
            if sug is not None:
                lines = lines + [tr("Tab: take the suggested shape")]
            lines = lines + ["⚠ " + n for n in self.rule_notes()]
            self._hud.set_content(QgsPointXY(*anchor), lines, hud.snap_tip(self.match), self.entry_text())
            self._hud.show()
        else:
            self._hud.hide()


def traverse_closure(verts, typed, segs=()):
    """The misclosure of a sketch drawn as a closed traverse, or None when it is not one: at least three
    sides, all straight, every point after the first typed (length and direction), and the last point back
    near the first (within 1:50 of the perimeter - farther, the user is letting the tool close it).
    -> {"dx", "dy", "linear", "perimeter", "precision"} in map units."""
    n = len(verts)
    if n < 4 or any(s is not None for s in segs) or not all(i in typed for i in range(1, n)):
        return None
    perimeter = sum(math.hypot(verts[i + 1][0] - verts[i][0], verts[i + 1][1] - verts[i][1]) for i in range(n - 1))
    dx, dy = verts[-1][0] - verts[0][0], verts[-1][1] - verts[0][1]
    linear = math.hypot(dx, dy)
    if perimeter <= 0 or linear > perimeter / 50.0:
        return None
    return {"dx": dx, "dy": dy, "linear": linear, "perimeter": perimeter,
            "precision": perimeter / linear if linear > 0 else float("inf")}


def bowditch(verts):
    """Compass (Bowditch) rule: each point moves against the misclosure in proportion to the distance
    travelled to it, so the last point lands on the first. Returns the adjusted points (same count)."""
    dx, dy = verts[-1][0] - verts[0][0], verts[-1][1] - verts[0][1]
    legs = [math.hypot(verts[i + 1][0] - verts[i][0], verts[i + 1][1] - verts[i][1]) for i in range(len(verts) - 1)]
    total = sum(legs)
    out, run = [verts[0]], 0.0
    for i, leg in enumerate(legs, 1):
        run += leg
        k = run / total if total else 0.0
        out.append((verts[i][0] - dx * k, verts[i][1] - dy * k))
    return out


def closure_text(info):
    return tr("The sides you typed do not meet the start: misclosure {e} m (ΔX {dx} m, ΔY {dy} m) on a "
              "perimeter of {p} m - precision 1:{n}.").format(
        e=f"{info['linear_m']:.3f}", dx=f"{info['dx_m']:+.3f}", dy=f"{info['dy_m']:+.3f}",
        p=f"{info['perimeter_m']:,.2f}", n=f"{info['precision']:,.0f}")


def _default_closure_opener(info):
    from qgis.PyQt.QtWidgets import QApplication, QMessageBox

    box = QMessageBox(QApplication.activeWindow())
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(tr("Closing the traverse"))
    box.setText(closure_text(info))
    box.setInformativeText(tr("Distribute the error over the sides (compass rule), or close it as drawn?"))
    adjust = box.addButton(tr("Distribute the error"), QMessageBox.ButtonRole.AcceptRole)
    keep = box.addButton(tr("Close as drawn"), QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(tr("Back to the sketch"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(adjust)
    box.exec()
    return {adjust: "adjust", keep: "keep"}.get(box.clickedButton())


def _default_form_opener(layer, feature):
    """Open QGIS's attribute form for a new feature; the feature is added when the form is accepted."""
    from qgis.utils import iface

    if iface is None:
        layer.beginEditCommand(tr("Create feature"))
        ok = layer.addFeature(feature)
        if ok:
            layer.endEditCommand()
        else:
            layer.destroyEditCommand()
        return ok
    return iface.openFeatureForm(layer, feature, False, True)
