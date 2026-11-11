r"""Create Features (editing/): templates, construction tools, keyboard entry, HUD, fill and auto-complete.

Run:  set QT_QPA_PLATFORM=offscreen & engine\bin\python-qgis.bat tests\test_editing_create.py

Every tool is driven by mouse and key events on an offscreen map canvas (nothing is shown on screen).
The canvas is 800 px for 100 m, so whole metres fall on whole pixels and clicks are exact.
"""
import math
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (Qgis, QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsCoordinateTransform, QgsFeature, QgsFieldConstraints, QgsGeometry,
                       QgsPointXY, QgsProject, QgsRectangle, QgsRendererCategory, QgsRuleBasedRenderer,
                       QgsSnappingConfig, QgsSymbol, QgsTolerance, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QEvent, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtGui import QKeyEvent  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from qgis.gui import QgsAdvancedDigitizingDockWidget, QgsMapCanvas, QgsMapMouseEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QFrame  # noqa: E402

from biruni.editing import construct, entry, fill, hud  # noqa: E402
from biruni.editing import templates as tpl  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def near_pt(p, x, y, tol=1e-6):
    return abs(p[0] - x) <= tol and abs(p[1] - y) <= tol


CRS = QgsCoordinateReferenceSystem("EPSG:32638")
project = QgsProject.instance()
project.setCrs(CRS)
project.setEllipsoid("NONE")
project.setDistanceUnits(Qgis.DistanceUnit.Meters)
project.setAreaUnits(Qgis.AreaUnit.SquareMeters)

canvas = QgsMapCanvas()
canvas.setFrameShape(QFrame.Shape.NoFrame)
canvas.resize(800, 800)
canvas.show()
canvas.setDestinationCrs(CRS)
canvas.setExtent(QgsRectangle(0, 0, 100, 100))
check(near(canvas.mapUnitsPerPixel(), 0.125), "offscreen canvas: 0.125 m per pixel")
cad = QgsAdvancedDigitizingDockWidget(canvas)


def mem(kind, name, fields="field=kind:string&field=n:integer", crs="EPSG:32638"):
    lyr = QgsVectorLayer(f"{kind}?crs={crs}&{fields}", name, "memory")
    project.addMapLayer(lyr)
    return lyr


def add(lyr, wkt, **attrs):
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    for k, v in attrs.items():
        f[k] = v
    lyr.dataProvider().addFeatures([f])
    lyr.updateExtents()


def pix(x, y):
    p = canvas.getCoordinateTransform().transform(QgsPointXY(x, y))
    return QPoint(round(p.x()), round(p.y()))


def mouse(tool, kind, x, y, button=Qt.MouseButton.LeftButton):
    t = {"move": QEvent.Type.MouseMove, "press": QEvent.Type.MouseButtonPress,
         "release": QEvent.Type.MouseButtonRelease}[kind]
    buttons = Qt.MouseButton.NoButton if kind == "move" else button
    e = QgsMapMouseEvent(canvas, t, pix(x, y), button if kind != "move" else Qt.MouseButton.NoButton, buttons,
                         Qt.KeyboardModifier.NoModifier)
    {"move": tool.canvasMoveEvent, "press": tool.canvasPressEvent, "release": tool.canvasReleaseEvent}[kind](e)


def click(tool, x, y, button=Qt.MouseButton.LeftButton):
    mouse(tool, "move", x, y)
    mouse(tool, "press", x, y, button)
    mouse(tool, "release", x, y, button)


def key(tool, k, text="", mods=Qt.KeyboardModifier.NoModifier):
    e = QKeyEvent(QEvent.Type.KeyPress, k, mods, text)
    tool.keyPressEvent(e)
    return e.isAccepted()


def type_in(tool, field_key, text):
    key(tool, field_key, chr(field_key).lower() if field_key < 128 else "")
    for ch in text:
        key(tool, Qt.Key.Key_unknown, ch)


def enter(tool):
    key(tool, Qt.Key.Key_Return, "\r")


def tab(tool):
    key(tool, Qt.Key.Key_Tab, "\t")


def f2(tool):
    key(tool, Qt.Key.Key_F2)


def snapping(on, layers=None):
    cfg = QgsSnappingConfig()
    cfg.setEnabled(on)
    cfg.setMode(Qgis.SnappingMode.AllLayers)
    cfg.setTypeFlag(Qgis.SnappingType.Vertex | Qgis.SnappingType.Segment)
    cfg.setTolerance(12)
    cfg.setUnits(QgsTolerance.UnitType.Pixels)
    project.setSnappingConfig(cfg)
    canvas.snappingUtils().setConfig(cfg)


def make_tool(lyr, mode, template=None, layers=None, **opt):
    canvas.setLayers(layers if layers is not None else [lyr])
    if not lyr.isEditable():
        lyr.startEditing()
    t = construct.ConstructTool(canvas, cad, lyr, template, mode, opt, form_opener=lambda l, f: forms.append(f) or
                                l.addFeature(f))
    canvas.setMapTool(t)
    return t


forms = []


def last_geom(lyr, n=1):
    fids = sorted(lyr.editBuffer().addedFeatures().keys(), reverse=True)  # new ids go -1, -2, …
    feats = [lyr.getFeature(fid) for fid in fids[-n:]]
    return [f.geometry() for f in feats] if n > 1 else feats[0].geometry()


def last_feature(lyr):
    fid = min(lyr.editBuffer().addedFeatures().keys())
    return lyr.getFeature(fid)


def angle_at(a, b, c):
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    return math.degrees(math.acos((v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2))))


def ring(g):
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g = QgsGeometry(g.constGet().segmentize())
    if g.isMultipart():
        return [(p.x(), p.y()) for p in g.asMultiPolygon()[0][0]]
    return [(p.x(), p.y()) for p in g.asPolygon()[0]]


# ==== 1. keyboard entry parsing ==========================================================================
print("-- entry --")
table = [
    (entry.parse_number, "25.4", 25.4), (entry.parse_number, "25,4", 25.4), (entry.parse_number, "١٢٫٥", 12.5),
    (entry.parse_number, "1,234.5", 1234.5), (entry.parse_number, "1.234,5", 1234.5),
    (entry.parse_number, "۱۲۳", 123.0), (entry.parse_number, "-3", -3.0),
    (lambda t: entry.length_value(t), "25.4", 25.4), (lambda t: entry.length_value(t), "25,4 م", 25.4),
    (lambda t: entry.length_value(t), "25.4m", 25.4), (lambda t: entry.length_value(t), "12.5ft", 3.81),
    (lambda t: entry.length_value(t), "12.5 ft", 3.81), (lambda t: entry.length_value(t), "100 usft", 30.48006096),
    (lambda t: entry.length_value(t), "١٢٫٥ م", 12.5), (lambda t: entry.length_value(t), "250cm", 2.5),
    (lambda t: entry.length_value(t), "2 كم", 2000.0), (lambda t: entry.length_value(t), "10'", 3.048),
    (lambda t: entry.length_value(t, 0.3048), "10 m", 32.80839895),
    (entry.parse_angle, "45-30-00", 45.5), (entry.parse_angle, "45°30'", 45.5), (entry.parse_angle, "45 30 36", 45.51),
    (entry.parse_angle, "45d30m", 45.5), (entry.parse_angle, "-12.5", -12.5), (entry.parse_angle, "100 gr", 90.0),
    (entry.parse_angle, "٤٥٫٥", 45.5),
    (entry.parse_direction, "N45°30'E", 45.5), (entry.parse_direction, "S45E", 135.0),
    (entry.parse_direction, "N 30 W", 330.0), (entry.parse_direction, "S12-30W", 192.5),
    (entry.parse_direction, "90", 90.0), (lambda t: entry.parse_direction(t, "polar"), "0", 90.0),
    (lambda t: entry.parse_direction(t, "polar"), "90", 0.0), (entry.parse_direction, "450", 90.0),
    (entry.parse_deflection, "12.5L", -12.5), (entry.parse_deflection, "12.5 R", 12.5),
    (entry.parse_deflection, "30 يسار", -30.0), (entry.parse_deflection, "-15", -15.0),
    (entry.parse_coordinate, "512345,67", 512345.67), (entry.parse_coordinate, "31°30'S", -31.5),
]
for fn, text, want in table:
    try:
        got = fn(text)
    except ValueError as ex:
        got = f"error {ex}"
    check(isinstance(got, float) and near(got, want, 1e-6), f"parse {text!r} -> {got} (want {want})")
for bad in ("", "abc", "12 parsecs"):
    try:
        entry.length_value(bad)
        check(False, f"{bad!r} is not a length")
    except ValueError:
        check(True, f"{bad!r} is not a length")
check(entry.format_angle(45.5, dms=True) == "45°30'00\"", "DMS writing")
check(entry.format_deflection(-12.5) == "12.50° L", "deflection writing")
check(near(entry.deflection(350, 10), 20) and near(entry.deflection(10, 350), -20), "deflection wraps")

# ==== 2. templates =======================================================================================
print("-- templates --")
parcels = mem("Polygon", "parcels")
cats = []
for value, label in (("سكني", "سكني"), ("تجاري", "تجاري"), ("أرض فضاء", "أرض فضاء"), ("", "other")):
    cats.append(QgsRendererCategory(value, QgsSymbol.defaultSymbol(Qgis.GeometryType.Polygon), label))
parcels.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
made = tpl.make_from_symbology(parcels)
check([t.name for t in made] == ["سكني", "تجاري", "أرض فضاء"], f"one template per category: {[t.name for t in made]}")
check(made[1].attributes == {"kind": "تجاري"}, "category value prefilled")
again = tpl.make_from_symbology(parcels)
check(again == [] and len(tpl.load(parcels)) == 3, "making them twice adds nothing")
check(not tpl.icon_for(parcels, made[0]).isNull(), "template icon = symbol preview")
loaded = tpl.load(parcels)
check(loaded[2].attributes["kind"] == "أرض فضاء" and loaded[2].layer_id == parcels.id(), "templates stored with the layer")
entries = [(t, parcels.name()) for t in loaded]
check([t.name for t, _ in tpl.search(entries, "ارض")] == ["أرض فضاء"], "search: ارض finds أرض فضاء (hamza)")
check([t.name for t, _ in tpl.search(entries, "تجارى")] == ["تجاري"], "search: تجارى finds تجاري (ya)")
check(len(tpl.search(entries, "parcels")) == 3, "search by layer name")
check(len(tpl.search(entries, "")) == 3, "empty search lists all")
loaded[0].tags = ["مبنى", "residential"]
tpl.replace(parcels, loaded[0])
check([t.name for t, _ in tpl.search([(t, "x") for t in tpl.load(parcels)], "RESIDENTIAL")] == ["سكني"], "search by tag")

rb = QgsRuleBasedRenderer(QgsSymbol.defaultSymbol(Qgis.GeometryType.Polygon))
root = rb.rootRule()
root.removeChildAt(0)
for expr, label in (("\"kind\" = 'مسجد' AND \"n\" = 2", "مسجد"), ("\"kind\" = 'مدرسة'", "مدرسة")):
    root.appendChild(QgsRuleBasedRenderer.Rule(QgsSymbol.defaultSymbol(Qgis.GeometryType.Polygon), 0, 0, expr, label))
rlyr = mem("Polygon", "rules")
rlyr.setRenderer(rb)
rts = tpl.from_symbology(rlyr)
check([(t.name, t.attributes) for t in rts] == [("مسجد", {"kind": "مسجد", "n": 2}), ("مدرسة", {"kind": "مدرسة"})],
      f"rule-based: one template per rule with its values {[(t.name, t.attributes) for t in rts]}")
check(tpl.equalities("\"a\" = 1 OR \"b\" = 2") is None, "an OR filter gives no values")

# ==== 3. points ==========================================================================================
print("-- point tools --")
pts = mem("Point", "points")
commercial = tpl.load(parcels)[1]
t = make_tool(pts, "point", tpl.Template("well", pts.id(), {"kind": "بئر", "n": "7"}))
click(t, 10, 20)
f = last_feature(pts)
check(near_pt(f.geometry().asPoint(), 10, 20), "point: at the click")
check(f["kind"] == "بئر" and f["n"] == 7, f"attributes come from the template ({f['kind']}, {f['n']!r} converted to int)")
check(forms == [], "no form when the template fills the required fields (automatic)")

lines = mem("LineString", "lines")
add(lines, "LINESTRING(10 50, 90 50)")
add(lines, "LINESTRING(10 10, 10 40)")
snapping(True)
t = make_tool(pts, "point_distance", layers=[lines, pts])
click(t, 10.25, 10.125)           # near the end of a line: snaps to (10, 10)
check(t.verts and near_pt(t.verts[0], 10, 10), "point at distance: the origin snapped to the line's end")
check(hud.snap_tip(t.match).startswith("Endpoint"), f"snap tip names the endpoint: {hud.snap_tip(t.match)!r}")
type_in(t, Qt.Key.Key_L, "25")
tab(t)
for ch in "N30E":
    key(t, Qt.Key.Key_unknown, ch)
enter(t)
p = last_feature(pts).geometry().asPoint()
check(near_pt(p, 10 + 25 * math.sin(math.radians(30)), 10 + 25 * math.cos(math.radians(30))),
      f"point at distance 25 and bearing N30E from the snapped point: {p}")
snapping(False)

t = make_tool(pts, "points_along", layers=[lines, pts], spacing=20.0)
n0 = pts.featureCount()
click(t, 40, 50.5)
ps = sorted(g.asPoint().x() for g in last_geom(pts, 5))
check(pts.featureCount() - n0 == 5 and all(near(a, b) for a, b in zip(ps, [10, 30, 50, 70, 90])),
      f"points every 20 m along an 80 m line: {ps}")
check(pts.undoStack().command(pts.undoStack().index() - 1).text() == "Create 5 features", "one undo step for them")
t.set_option("along", "parts")
t.set_option("parts", 8)
t.set_option("include_ends", False)
n0 = pts.featureCount()
click(t, 40, 50.5)
check(pts.featureCount() - n0 == 7, "8 equal parts without the ends: 7 points")
t = make_tool(pts, "point_line_end", layers=[lines, pts])
click(t, 85, 50.5)
check(near_pt(last_feature(pts).geometry().asPoint(), 90, 50), "point at the nearest end of a line")

# ==== 4. lines ===========================================================================================
print("-- line tools --")
roads = mem("LineString", "roads")
t = make_tool(roads, "polyline")
click(t, 10, 10)
click(t, 30, 10)
mouse(t, "move", 30, 30)
check(t.hud_lines()[:2] == ["Length: 20.00 m", "Bearing: 0.00°"], f"HUD while drawing: {t.hud_lines()}")
check("Deflection: 90.00° L" in t.hud_lines() and "Total: 40.00 m" in t.hud_lines(), "HUD deflection and total")
click(t, 30, 30)
click(t, 50, 30, Qt.MouseButton.RightButton)
g = last_geom(roads)
check(g.asWkt(0) == "LineString (10 10, 30 10, 30 30)", f"polyline finished with a right click: {g.asWkt(0)}")

# keyboard: length, direction, deflection, X/Y, Arabic digits, Esc, Ctrl+Z
t = make_tool(roads, "polyline")
click(t, 20, 20)
type_in(t, Qt.Key.Key_L, "10m")
tab(t)          # → A
for ch in "90":
    key(t, Qt.Key.Key_unknown, ch)
enter(t)
check(near_pt(t.verts[-1], 30, 20), f"L 10 / A 90 → east 10 m: {t.verts[-1]}")
type_in(t, Qt.Key.Key_L, "٥")
tab(t)          # → A
tab(t)          # → D
for ch in "90":
    key(t, Qt.Key.Key_unknown, ch)
enter(t)
check(near_pt(t.verts[-1], 30, 15), f"deflection 90 right, length ٥ (Arabic digit) → south 5 m: {t.verts[-1]}")
type_in(t, Qt.Key.Key_X, "45")
tab(t)
for ch in "15":
    key(t, Qt.Key.Key_unknown, ch)
enter(t)
check(near_pt(t.verts[-1], 45, 15), "X / Y absolute coordinates")
type_in(t, Qt.Key.Key_L, "7")
key(t, Qt.Key.Key_Escape)
check(t.field is None and "L" not in t.locks and len(t.verts) == 4, "Esc cancels the field being typed")
key(t, Qt.Key.Key_Z, "z", Qt.KeyboardModifier.ControlModifier)
check(len(t.verts) == 3, "Ctrl+Z removes the last vertex")
type_in(t, Qt.Key.Key_L, "abc")
enter(t)
check(t.error.startswith("Cannot read") and len(t.verts) == 3, "an unreadable value is refused")
key(t, Qt.Key.Key_Escape)
key(t, Qt.Key.Key_Escape)
key(t, Qt.Key.Key_Escape)
check(t.verts == [], "Esc twice drops the sketch")

# P / R: parallel and perpendicular to the segment under the cursor
t = make_tool(roads, "polyline", layers=[lines, roads])
snapping(True)
click(t, 60, 70)
mouse(t, "move", 50, 50.25)      # over the horizontal line (10 50 → 90 50)
key(t, Qt.Key.Key_R, "r")
type_in(t, Qt.Key.Key_L, "12")
mouse(t, "move", 61, 80)
enter(t)
check(near_pt(t.verts[-1], 60, 82), f"R: perpendicular to the snapped segment, 12 m: {t.verts[-1]}")
mouse(t, "move", 50, 50.25)
key(t, Qt.Key.Key_P, "p")
mouse(t, "move", 70, 83)
click(t, 70, 83)
check(near_pt(t.verts[-1], 70, 82), f"P: parallel to the snapped segment: {t.verts[-1]}")
t.reset()
snapping(False)

# right-angle line
t = make_tool(roads, "right_angle_line")
click(t, 10, 10)
click(t, 40, 10)
click(t, 41, 30)
click(t, 20, 31)
f2(t)
g = last_geom(roads).asPolyline()
check(len(g) == 4 and all(near_pt(p, *q) for p, q in zip(g, [(10, 10), (40, 10), (40, 30), (20, 30)])),
      f"right-angle line: {g}")
check(all(near(angle_at(g[i - 1], g[i], g[i + 1]), 90.0) for i in (1, 2)), "every turn is 90°")

# arcs: in a curve layer (true arcs) and a plain layer (densified)
curves = mem("CompoundCurve", "curves")
t = make_tool(curves, "polyline", segment="arc3")
click(t, 10, 10)
click(t, 20, 20)
click(t, 30, 10)
f2(t)
g = last_geom(curves)
check(QgsWkbTypes.isCurvedType(g.wkbType()) and "CircularString (10 10, 20 20, 30 10)" in g.asWkt(0),
      f"3-point arc written as a true arc: {g.asWkt(0)}")
check(near(g.length(), math.pi * 10, 1e-6), "arc length = π·r")
t = make_tool(roads, "polyline", segment="arc3", chord=0.01)
click(t, 10, 10)
click(t, 20, 20)
click(t, 30, 10)
f2(t)
g = last_geom(roads)
dev = max(abs(math.hypot(p.x() - 20, p.y() - 10) - 10) for p in g.asPolyline())
check(g.wkbType() == QgsWkbTypes.Type.LineString and len(g.asPolyline()) > 20 and dev < 1e-6,
      f"arc densified on a layer without curves ({len(g.asPolyline())} vertices on the circle)")
mids = [QgsPointXY((a.x() + b.x()) / 2, (a.y() + b.y()) / 2) for a, b in zip(g.asPolyline(), g.asPolyline()[1:])]
check(max(10 - math.hypot(m.x() - 20, m.y() - 10) for m in mids) <= 0.0100001, "chord deviation ≤ 1 cm")

t = make_tool(curves, "polyline", segment="arc_radius")
click(t, 10, 10)
type_in(t, Qt.Key.Key_L, "10")    # radius
enter(t)
click(t, 30, 10)                  # end point
click(t, 20, 14)                  # side: above the chord
f2(t)
g = last_geom(curves)
cs = g.constGet().curveAt(0)
pts3 = [(cs.pointN(i).x(), cs.pointN(i).y()) for i in range(3)]
check(near_pt(pts3[1], 20, 20) and near_pt(pts3[2], 30, 10), f"end-point arc with radius 10: {pts3}")
centre, r = construct.circle_centre(*pts3)
check(near(r, 10.0) and near_pt(centre, 20, 10), "arc radius 10, centre (20, 10)")

t = make_tool(curves, "polyline")
click(t, 10, 10)
click(t, 20, 10)
t.set_option("segment", "arc_tangent")
click(t, 30, 20)
f2(t)
g = last_geom(curves)
arc = g.constGet().curveAt(1)
m = (arc.pointN(1).x(), arc.pointN(1).y())
check(near_pt(m, 20 + 10 * math.cos(math.radians(-45)), 20 + 10 * math.sin(math.radians(-45))),
      f"tangent arc: quarter circle centred (20, 20): mid {m}")
check(abs(entry.deflection(0.0, construct.arc_end_direction((20, 10), m, (30, 20)))) < 1e-6, "tangent arc ends heading north")

# trace along features
snapping(True)
trace_src = mem("LineString", "trace_src")
add(trace_src, "LINESTRING(10 60, 50 60, 50 90)")
t = make_tool(roads, "trace", layers=[trace_src, roads])
click(t, 10, 60)
click(t, 50, 90)
f2(t)
g = last_geom(roads)
check(g.asWkt(0) == "LineString (10 60, 50 60, 50 90)", f"trace follows the line round its corner: {g.asWkt(0)}")
snapping(False)

# freehand with simplification
t = make_tool(roads, "freehand")
mouse(t, "press", 10, 30)
for i in range(1, 41):
    mouse(t, "move", 10 + i, 30 + (0.125 if i % 2 else 0))
mouse(t, "release", 50, 30)
g = last_geom(roads)
check(len(g.asPolyline()) == 2 and near(g.length(), 40.0, 0.01), f"freehand simplified to {len(g.asPolyline())} vertices")

# ==== 5. polygons ========================================================================================
print("-- polygon tools --")
t = make_tool(parcels, "polygon", commercial)
click(t, 10, 10)
click(t, 40, 10)
click(t, 40, 30)
mouse(t, "move", 10, 30)
check("Polygon area: 600.00 m²" in t.hud_lines(), f"HUD area while drawing: {t.hud_lines()}")
click(t, 10, 30)
f2(t)
f = last_feature(parcels)
check(near(f.geometry().area(), 600.0) and f["kind"] == "تجاري", "polygon with the template's category value")

t = make_tool(parcels, "right_angle_polygon")
click(t, 10, 10)
click(t, 40, 10)
click(t, 41, 30)
f2(t)
r_ = ring(last_geom(parcels))
check(near(fill.polygon_area(r_[:-1]), 600.0) and len(r_) == 5, f"right-angle polygon squared on finish: {r_}")

t = make_tool(parcels, "rectangle")
click(t, 20, 20)
type_in(t, Qt.Key.Key_L, "30")
tab(t)
for ch in "60":
    key(t, Qt.Key.Key_unknown, ch)
enter(t)
mouse(t, "move", 10, 40)
type_in(t, Qt.Key.Key_L, "12")
enter(t)
r_ = ring(last_geom(parcels))
sides = [math.hypot(r_[i + 1][0] - r_[i][0], r_[i + 1][1] - r_[i][1]) for i in range(4)]
check(near(sides[0], 30) and near(sides[1], 12) and near(sides[2], 30) and near(sides[3], 12),
      f"rotated rectangle 30 × 12: {sides}")
check(all(near(angle_at(r_[i - 1], r_[i], r_[i + 1]), 90.0) for i in (1, 2, 3)), "rectangle corners are 90°")
check(near(entry.azimuth(*r_[0], *r_[1]), 60.0), "first side at bearing 60°")

t = make_tool(parcels, "circle", chord=0.01)
click(t, 50, 50)
mouse(t, "move", 55, 50)
check(t.hud_lines()[0] == "Radius: 5.00 m", f"HUD radius: {t.hud_lines()}")
type_in(t, Qt.Key.Key_L, "10")
enter(t)
g = last_geom(parcels)
check(abs(g.area() - math.pi * 100) / (math.pi * 100) < 0.002 and not QgsWkbTypes.isCurvedType(g.wkbType()),
      f"circle r 10 on a plain layer: area {g.area():.3f} (π·100 = {math.pi * 100:.3f})")
cpoly = mem("CurvePolygon", "cpoly")
t = make_tool(cpoly, "circle")
click(t, 50, 50)
click(t, 60, 50)
g = last_geom(cpoly)
check(QgsWkbTypes.isCurvedType(g.wkbType()) and abs(g.area() - math.pi * 100) < 0.05,
      f"circle on a curve layer is a true circle: area {g.area():.4f}")
t = make_tool(parcels, "circle3")
click(t, 40, 50)
click(t, 60, 50)
click(t, 50, 60)
g = last_geom(parcels)
check(abs(g.area() - math.pi * 100) / (math.pi * 100) < 0.002 and near(g.centroid().asPoint().x(), 50, 1e-3),
      "circle through 3 points")
t = make_tool(parcels, "ellipse")
click(t, 50, 50)
click(t, 70, 50)
click(t, 55, 60)
g = last_geom(parcels)
check(abs(g.area() - math.pi * 200) / (math.pi * 200) < 0.002, f"ellipse 20 × 10: area {g.area():.2f}")
t = make_tool(parcels, "regular_polygon", sides=6)
click(t, 50, 50)
click(t, 60, 50)
g = last_geom(parcels)
check(near(g.area(), 3 * math.sqrt(3) / 2 * 100, 1e-6) and len(ring(g)) == 7, "regular hexagon")

# auto-complete between two polygons
blocks = mem("Polygon", "blocks")
add(blocks, "POLYGON((20 20, 40 20, 40 40, 20 40, 20 20))")
add(blocks, "POLYGON((40 20, 60 20, 60 40, 40 40, 40 20))")
t = make_tool(blocks, "auto_complete")
click(t, 20, 40)
click(t, 20, 50)
click(t, 60, 50)
click(t, 60, 40)
f2(t)
g = last_geom(blocks)
check(near(g.area(), 400.0, 1e-6) and g.boundingBox().toString(0) == "20,40 : 60,50",
      f"auto-complete fills between the line and the two polygons: {g.asWkt(1)}")
t = make_tool(blocks, "auto_complete", fill_tolerance=0.2)
click(t, 30, 20.125)  # starts 12.5 cm inside a block (overshoot)
click(t, 30, 10)
click(t, 50, 10)
click(t, 50, 19.875)  # ends 12.5 cm short of the other (gap)
f2(t)
g = last_geom(blocks)
check(near(g.area(), 200.0, 1e-6), f"auto-complete with an overshoot and a gap: area {g.area():.3f}")

# fill: a grid of lines with a 5 cm gap
grid = mem("LineString", "grid")
for x in (10, 30, 50):
    add(grid, f"LINESTRING({x} 10, {x} 30)")
add(grid, "LINESTRING(10 10, 50 10)")
add(grid, "LINESTRING(10 30, 29.95 30)")     # 5 cm short of the line x = 30
add(grid, "LINESTRING(30 30, 50 30)")
fills = mem("Polygon", "fills")
t = make_tool(fills, "fill", layers=[grid, fills], fill_tolerance=0.1)
click(t, 20, 20)
g = last_geom(fills)
check(near(g.area(), 400.0, 1e-3), f"fill: the cell left of the gap is found ({g.area():.4f} m²)")
n0 = fills.featureCount()
t.set_option("fill_tolerance", 0.0)
canvas.setLayers([grid])       # the new polygon itself would close the cell
click(t, 20, 20)
check(fills.featureCount() == n0 and "enclosed" in t.error, "without a tolerance the open cell is not filled")
geoms = [f.geometry() for f in grid.getFeatures()]
face = fill.fill_face(QgsPointXY(40, 20), geoms, 0.1)
check(face is not None and near(face.area(), 400.0), "pure fill: the right cell")
check(fill.fill_face(QgsPointXY(70, 20), geoms, 0.1) is None, "pure fill: outside the grid → None")
check(len(fill.faces(geoms, 0.1)) == 2, "two faces in the grid")

# ==== 6. undo, forms, CRS ================================================================================
print("-- undo, forms, CRS --")
t = make_tool(parcels, "rectangle")
n0 = parcels.featureCount()
click(t, 70, 70)
click(t, 90, 70)
click(t, 90, 80)
check(parcels.featureCount() == n0 + 1, "rectangle added")
parcels.undoStack().undo()
check(parcels.featureCount() == n0, "one Undo removes the whole new feature")
parcels.undoStack().redo()
check(parcels.featureCount() == n0 + 1, "Redo brings it back")

req = mem("Point", "required", "field=code:string&field=note:string")
req.setFieldConstraint(0, QgsFieldConstraints.Constraint.ConstraintNotNull)
forms.clear()
t = make_tool(req, "point", tpl.Template("x", req.id()))
click(t, 30, 30)
check(len(forms) == 1, "automatic: the form opens when a required field is empty")
t = make_tool(req, "point", tpl.Template("x", req.id(), {"code": "A1"}))
click(t, 31, 30)
check(len(forms) == 1 and last_feature(req)["code"] == "A1", "automatic: no form when the template fills it")
t = make_tool(req, "point", tpl.Template("x", req.id(), {"code": "A1"}, form=tpl.FORM_ALWAYS))
click(t, 32, 30)
check(len(forms) == 2, "template set to always ask: the form opens")
check(tpl.missing_required(req, tpl.Template("x", req.id())) == ["code"], "missing required fields listed")

geo = mem("Point", "geo", crs="EPSG:4326")
t = make_tool(geo, "point")
click(t, 50, 50)
p = last_feature(geo).geometry().asPoint()
back = QgsCoordinateTransform(geo.crs(), CRS, project).transform(p)
check(abs(p.x()) < 180 and near_pt(back, 50, 50, 1e-4), f"canvas CRS → layer CRS: {p.x():.6f}, {p.y():.6f}")
z = mem("PointZ", "z3d")
t = make_tool(z, "point")
click(t, 5, 5)
check(QgsWkbTypes.hasZ(last_feature(z).geometry().wkbType()), "Z added for a 3D layer")
multi = mem("MultiPolygon", "multi")
t = make_tool(multi, "rectangle")
click(t, 70, 70)
click(t, 90, 70)
click(t, 90, 80)
check(last_geom(multi).isMultipart(), "multipart for a multi layer")

# ==== 7. HUD values ======================================================================================
print("-- HUD --")
v = hud.sketch_values([(0, 0), (0, 10)], (10, 10), polygon=True)
check(near(v["segment"], 10) and near(v["direction"], 90) and near(v["deflection"], 90) and near(v["total"], 20)
      and near(v["area"], 50), f"sketch values {v}")
lines_ = hud.hud_lines(v)
check(lines_ == ["Length: 10.00 m", "Bearing: 90.00°", "Deflection: 90.00° R", "Total: 20.00 m", "Polygon area: 50.00 m²"],
      f"HUD text {lines_}")
lines_ = hud.hud_lines(hud.sketch_values([(0, 0)], (10, 10)), convention="polar", dms=True)
check(lines_[1] == "Angle: 45°00'00\"", f"polar angle in DMS: {lines_}")
lines_ = hud.hud_lines(hud.sketch_values([(0, 0)], (3, 4)), length_unit="ft", length_factor=1 / 0.3048)
check(lines_[0] == "Length: 16.40 ft", f"HUD in the project's units: {lines_[0]}")
hud_item = hud.SketchHud(canvas)
hud_item.set_content(QgsPointXY(10, 10), ["Length: 1.00 m"], "Endpoint · roads", "L: 25▏")
check(hud_item.text_lines() == ["Endpoint · roads", "Length: 1.00 m", "L: 25▏"] and
      not hud_item.boundingRect().isEmpty(), "HUD item lines and size")

# ==== 8. the pane builds offscreen =======================================================================
print("-- pane --")
try:
    from biruni.editing import create_pane

    class FakeIface:
        def __init__(self):
            from qgis.PyQt.QtWidgets import QMainWindow
            self.mw = QMainWindow()

        def mainWindow(self):
            return self.mw

        def mapCanvas(self):
            return canvas

        def cadDockWidget(self):
            return cad

        def activeLayer(self):
            return parcels

    class FakeTheme:
        def tokens(self):
            from biruni.theme import TOKENS
            return TOKENS["light"]

    class FakePlugin:
        def __init__(self):
            self.iface = FakeIface()
            self.theme = FakeTheme()
            self.toolbox = None

    plugin = FakePlugin()
    pane = create_pane.open_pane(plugin)
    names = pane.layer_names()
    check("parcels" in names and "lines" in names, f"pane lists the editable layers: {len(names)}")
    pane.search.setText("تجارى")
    check(pane.visible_template_names() == ["تجاري"], f"pane search (Arabic): {pane.visible_template_names()}")
    pane.search.setText("")
    pane.select_template(parcels, "تجاري")
    check([b for b in pane.tool_ids()][:3] == ["polygon", "right_angle_polygon", "rectangle"], "polygon tools shown")
    pane.start_tool("rectangle")
    check(isinstance(canvas.mapTool(), construct.ConstructTool) and canvas.mapTool().mode == "rectangle",
          "starting a tool sets the map tool")
    n0 = parcels.featureCount()
    click(canvas.mapTool(), 70, 60)
    click(canvas.mapTool(), 80, 60)
    click(canvas.mapTool(), 80, 65)
    check(parcels.featureCount() == n0 + 1 and last_feature(parcels)["kind"] == "تجاري", "drawing from the pane")
    from qgis.core import QgsEditorWidgetSetup
    from qgis.PyQt.QtWidgets import QComboBox, QLineEdit

    parcels.setEditorWidgetSetup(0, QgsEditorWidgetSetup("ValueMap", {"map": [{"Residential": "سكني"},
                                                                              {"Commercial": "تجاري"}]}))
    dlg = create_pane.TemplateDialog(parcels, tpl.load(parcels)[1])
    check(isinstance(dlg.editors["kind"], QComboBox) and dlg.editors["kind"].currentText() == "Commercial",
          "template form: a value map field is a list showing the template's value")
    check(isinstance(dlg.editors["n"], QLineEdit), "template form: other fields are text boxes")
    dlg.editors["kind"].setCurrentIndex(dlg.editors["kind"].findData("سكني"))
    dlg.editors["n"].setText("5")
    dlg.form.setCurrentIndex(dlg.form.findData(tpl.FORM_ALWAYS))
    nt = dlg.result_template()
    check(nt.attributes == {"kind": "سكني", "n": "5"} and nt.form == tpl.FORM_ALWAYS, f"template form result {nt}")
    dlg.deleteLater()
    tool = canvas.mapTool()
    tool.reset()
    click(tool, 70, 60)
    sc = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier, "z")
    check(tool.shortcutEvent(sc), "Ctrl+Z is kept from the window's Undo while a sketch is open")
    tool.reset()
    sc = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier, "z")
    check(not tool.shortcutEvent(sc), "… and left to Undo when there is no sketch")
    create_pane.close_pane(plugin)
    check(create_pane._PANES == {}, "pane closed")
except ImportError as ex:
    check(False, f"pane import: {ex}")

# ==== closing a typed traverse (compass rule) ==============================================================
print("-- traverse closure --")
lots = mem("Polygon", "lots")


def traverse(t, legs, start=(0, 0)):
    click(t, *start)
    for length, az in legs:
        type_in(t, Qt.Key.Key_L, str(length))
        tab(t)
        for ch in str(az):
            key(t, Qt.Key.Key_unknown, ch)
        enter(t)


LEGS = [(100, 90), (100, 0), (100, 270), (100.05, 180)]   # the last side 5 cm too long
asked = []
t = make_tool(lots, "polygon", layers=[lots])
t.closure_opener = lambda info: asked.append(info) or "adjust"
n0 = lots.featureCount()
traverse(t, LEGS)
check(len(t.typed) == 4, f"the four typed sides are known as typed ({sorted(t.typed)})")
t.finish()
check(len(asked) == 1 and abs(asked[0]["linear_m"] - 0.05) < 1e-6 and abs(asked[0]["precision"] - 8001) < 1,
      f"closing shows the misclosure (0.05 m) and the precision (1:8001): {asked and asked[0]}")
r = ring(last_geom(lots))
check(lots.featureCount() == n0 + 1 and len(r) == 5 and near_pt(r[3], 0, 100 + 0.05 * 300 / 400.05)
      and near_pt(r[1], 100, 0.05 * 100 / 400.05),
      f"the compass rule moves each corner in proportion to the distance to it: {r}")
t.closure_opener = lambda info: "keep"
traverse(t, LEGS)
t.finish()
check(len(ring(last_geom(lots))) == 6, "'close as drawn' keeps the small closing side")
t.closure_opener = lambda info: None
n0 = lots.featureCount()
traverse(t, LEGS)
t.finish()
check(lots.featureCount() == n0 and len(t.verts) == 5, "'back to the sketch' adds nothing and keeps the points")
t.reset()
asked.clear()
t.closure_opener = lambda info: asked.append(info) or "adjust"
traverse(t, [(100, 90), (100, 0), (100, 270), (100, 180)])
t.finish()
check(not asked and len(ring(last_geom(lots))) == 5, "a traverse that closes exactly is not asked about")
traverse(t, [(100, 90), (100, 0), (100, 270)])
t.finish()
check(not asked, "three typed sides closed by the tool (far from the start) are not a misclosure")
click(t, 0, 0)
click(t, 50, 0)
click(t, 50, 50)
click(t, 0.01, 0.01)
t.finish()
check(not asked, "clicked points are not a traverse")

# ==== shape completion (Tab takes the suggested shape) =====================================================
print("-- shape completion --")
plots = mem("Polygon", "plots")
for x in (0, 20, 40):                                  # a row of 20 × 30 plots
    add(plots, f"POLYGON(({x} 0,{x + 20} 0,{x + 20} 30,{x} 30,{x} 0))")
t = make_tool(plots, "polygon", layers=[plots])
n0 = plots.featureCount()
click(t, 60, 0)
click(t, 80, 0)
t.cursor = (70, 10)                                    # the cursor on the side of the row
t._refresh()
s = t.suggestion()
check(s is not None and s[2] == "neighbour" and near(QgsGeometry.fromPolygonXY(
    [[QgsPointXY(*p) for p in s[0] + s[0][:1]]]).area(), 600), f"one side drawn: the neighbours' plot is offered ({s})")
check(key(t, Qt.Key.Key_Tab, "\t") and plots.featureCount() == n0 + 1 and near(last_geom(plots).area(), 600),
      "Tab takes it: the next plot of the row in one side and one key")
click(t, 80, 0)
click(t, 100, 0)
t.cursor = (90, -10)                                   # the other side: there is no plot there to copy over…
t._refresh()
s2 = t.suggestion()
check(s2 is not None and all(p[1] <= 1e-9 for p in s2[0]), "… the copy goes on the cursor's side")
t.reset()
click(t, 200, 0)
click(t, 230, 0)
click(t, 230, 12)                                      # a square corner, no neighbour of that size
t.cursor = (220, 5)
t._refresh()
s3 = t.suggestion()
check(s3 is not None and s3[2] == "rectangle" and len(s3[0]) == 4 and near_pt(s3[0][3], 200, 12),
      f"two sides at a square corner: the rectangle is offered ({s3})")
t.reset()
click(t, 300, 0)
click(t, 320, 0)
t.cursor = (310, 5)
t.set_option("autoshape", False)
t._refresh()
check(t.suggestion() is None, "it can be switched off")
t.set_option("autoshape", True)
t.reset()
click(t, 300, 0)
type_in(t, Qt.Key.Key_L, "20")
tab(t)
for ch in "90":
    key(t, Qt.Key.Key_unknown, ch)
enter(t)
t.cursor = (310, 5)
t._refresh()
check(t.suggestion() is None, "a typed traverse is never completed for the surveyor")
t.reset()
check(key(t, Qt.Key.Key_Tab, "\t") and t.field == "L", "without a suggestion Tab still opens the length field")
t.field = None

# ==== the rules learned from the data, while drawing ==========================================================
print("-- learned rules while drawing --")
from biruni.insight import learned  # noqa: E402

square = mem("Polygon", "square plots")
for i in range(40):                                   # 39 square plots and one with a 95° corner
    x = (i % 10) * 30
    y = (i // 10) * 40
    dx = 3 if i == 7 else 0
    add(square, f"POLYGON(({x} {y},{x + 20} {y},{x + 20 + dx} {y + 30},{x} {y + 30},{x} {y}))")
t = make_tool(square, "polygon", layers=[square])
click(t, 0, 500)
click(t, 20, 500)
click(t, 23, 530)
t.cursor = (5, 530)
t._refresh()
check(t.rule_notes() == [], "nothing is said before the rules were learned (drawing never waits for them)")
learned.learn(project, [square])
t._broken = (None, [])
click(t, 0, 530)
t._refresh()
notes = t.rule_notes()
check(any("95" in n or "°" in n for n in notes), f"a skewed corner is pointed out while drawing: {notes}")
t.reset()
click(t, 0, 600)
click(t, 20, 600)
click(t, 20, 630)
click(t, 0, 630)
t._refresh()
check(t.rule_notes() == [], "a square plot breaks nothing")
t.reset()

for lyr in project.mapLayers().values():
    if lyr.isEditable():
        lyr.rollBack()
print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED:\n  " + "\n  ".join(failures), flush=True)
# os._exit, as the newer tests do: Python's own teardown destroys the canvas, the tools and the layers in no fixed
# order, and that crashed now and then after every check had passed
os._exit(1 if failures else 0)
