r"""Modify Features (editing/modify_*): pure geometry of every tool, the map tools on an offscreen canvas,
one Undo step per apply, refusals, the pane (list, search, sections, favourites, tool view, strips) and the
icons.

Run:  set QT_QPA_PLATFORM=offscreen & engine\bin\python-qgis.bat tests\test_editing_modify.py

Screenshots of the pane (English / Arabic, light / dark) and a contact sheet of the icons are written to
the folder in BIRUNI_SHOTS (default: a temporary folder) - nothing is shown on screen.
"""
import math
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature,  # noqa: E402
                       QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QEvent, QPoint, Qt  # noqa: E402
from qgis.PyQt.QtGui import QKeyEvent  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from qgis.gui import QgsMapCanvas, QgsMapMouseEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QFrame  # noqa: E402

from biruni.editing import modify_geom as mg  # noqa: E402

SHOTS = os.environ.get("BIRUNI_SHOTS") or tempfile.mkdtemp(prefix="biruni_modify_")
os.makedirs(SHOTS, exist_ok=True)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def G(wkt):
    return QgsGeometry.fromWkt(wkt)


def pts(g):
    return [(round(p.x(), 6), round(p.y(), 6)) for p in g.vertices()]


# ==== 1. pure geometry ===================================================================================
print("-- geometry: alignment --")
sq = G("POLYGON((0 0,10 0,10 10,0 10,0 0))")
m = mg.transform(sq, dx=5, dy=-2)
check(pts(m)[0] == (5, -2), f"move by ΔX/ΔY: {pts(m)[0]}")
r = mg.transform(sq, angle=90, pivot=(0, 0))
check(pts(r)[1] == (0, -10), f"rotate 90° clockwise around (0, 0): {pts(r)[1]}")
s = mg.transform(sq, factor=2, pivot=(5, 5))
check(near(s.area(), 400) and pts(s)[0] == (-5, -5), f"scale ×2 around the centre: {s.area()}")
cps = mg.repeat_copies(G("POINT(10 0)"), 3, dx=0, dy=0, dangle=90, pivot=(0, 0))
check([pts(c)[0] for c in cps] == [(0, -10), (-10, 0), (0, 10)], f"repeat copy: 3 copies at 90° steps {[pts(c)[0] for c in cps]}")
cps = mg.repeat_copies(sq, 4, dx=12, dy=0)
check(len(cps) == 4 and pts(cps[3])[0] == (48, 0), "repeat copy: every 12 m to the east")
try:
    mg.repeat_copies(sq, 2)
    check(False, "repeat copy without a step is refused")
except mg.GeometryError:
    check(True, "repeat copy without a step is refused")
edge_poly = G("POLYGON((0 0.08,10 -0.06,10 10,0 10,0 0.08))")
target = G("LINESTRING(-5 0,5 0.0,15 0)")
al, n = mg.align_edge(edge_poly, target, 0.1)
check(n == 2 and (0, 0) in pts(al) and (10, 0) in pts(al) and (5, 0) in pts(al) and mg.usable(al),
      f"align edge: 2 vertices onto the target and its vertex added {pts(al)}")
al2, n2 = mg.align_edge(edge_poly, target, 0.05)
check(n2 == 0, "align edge: nothing farther than the tolerance moves")

print("-- geometry: construct --")
ln = G("LINESTRING(0 0,100 0,100 50)")
cp = mg.copy_parallel(ln, 2.5, "both")
check(len(cp) == 2 and all(near(c.distance(ln), 2.5, 1e-6) for c in cp) and
      all(near(QgsGeometry.fromPointXY(c.interpolate(0).asPoint()).distance(ln), 2.5, 1e-6) for c in cp) and
      all(c.hausdorffDistance(ln) <= 2.5 * math.sqrt(2) + 1e-6 for c in cp),
      "copy parallel: both sides exactly 2.5 m away (sharp corner outside)")
left = mg.copy_parallel(ln, 2.5, "left")[0]
check(pts(left)[0] == (0, 2.5), f"copy parallel: left of the line's direction {pts(left)[0]}")
out = mg.copy_parallel(sq, 1, "left")[0]
check(near(out.area(), 144), f"copy parallel of a polygon: outside with sharp corners (area {out.area()})")
pa = mg.points_along(ln, spacing=30)
check(len(pa) == 6 and pts(pa[-1])[0] == (100, 50), f"points along: every 30 m + the end ({len(pa)})")
pa = mg.points_along(ln, count=3, include_ends=False)
check(len(pa) == 2 and pts(pa[0])[0] == (50, 0), "points along: 3 equal parts, ends left out")
b = mg.buffer(sq, 1)
check(b.area() > 139 and b.area() < 144 and mg.usable(b, "polygon"), f"buffer edit {b.area():.2f}")
# fillet: a right-angle corner with radius 5 - tangent points 5 m from the corner, centre 5 m from both legs
t1, mid, t2, c = mg.fillet_corner((0, 0), (10, 0), (10, 10), 5)
check(near(t1[0], 5) and near(t1[1], 0) and near(t2[0], 10) and near(t2[1], 5) and near(c[0], 5) and near(c[1], 5),
      f"fillet: tangent points and centre {t1} {t2} {c}")
check(near(math.hypot(mid[0] - c[0], mid[1] - c[1]), 5), "fillet: the arc's middle lies on the radius")
fl = mg.fillet(G("LINESTRING(0 0,10 0,10 10)"), 1, 5, curves=True)
check(QgsWkbTypes.isCurvedType(fl.wkbType()) and near(fl.length(), 5 + 5 + math.pi * 5 / 2, 1e-6),
      f"fillet: a true arc when the layer stores curves (length {fl.length():.4f})")
fl2 = mg.fillet(G("LINESTRING(0 0,10 0,10 10)"), 1, 5, curves=False, chord=0.001)
check(not QgsWkbTypes.isCurvedType(fl2.wkbType()) and near(fl2.length(), 10 + math.pi * 5 / 2, 0.01),
      f"fillet: densified otherwise ({fl2.length():.4f})")
dmax = max(abs(math.hypot(p.x() - 5, p.y() - 5) - 5) for p in fl2.vertices() if p.x() > 5 and p.y() < 5)
check(dmax < 1e-6, "fillet: densified vertices lie on the circle")
fp = mg.fillet(sq, 2, 2, curves=False, chord=0.001)
check(near(fp.area(), 100 - (4 - math.pi), 0.01) and mg.usable(fp), f"fillet a polygon corner {fp.area():.4f}")
fpc = mg.fillet(sq, 0, 2, curves=True)
check(QgsWkbTypes.isCurvedType(fpc.wkbType()) and near(fpc.area(), 100 - (4 - math.pi), 1e-3), "fillet the first corner as a curve")
try:
    mg.fillet(G("LINESTRING(0 0,10 0,10 3)"), 1, 5)
    check(False, "fillet: too big a radius is refused")
except mg.GeometryError:
    check(True, "fillet: too big a radius is refused")

print("-- geometry: divide --")
pieces = mg.split_into_parts(ln, 3)
check(len(pieces) == 3 and all(near(p.length(), 50) for p in pieces), f"split into 3 equal parts {[p.length() for p in pieces]}")
pieces = mg.split_every(ln, 40)
check([round(p.length(), 6) for p in pieces] == [40, 40, 40, 30], f"split every 40 m {[p.length() for p in pieces]}")
pieces = mg.split_at_point(ln, (100.4, 20))
check([round(p.length(), 6) for p in pieces] == [120, 30] and pts(pieces[0])[-1] == (100, 20), "split at the clicked point")
cut = mg.split_by_line(sq, G("LINESTRING(4 -1,4 11)"))
check(len(cut) == 2 and near(sum(c.area() for c in cut), 100) and sorted(round(c.area()) for c in cut) == [40, 60],
      "split a polygon by a drawn line")
check(mg.split_by_line(sq, G("LINESTRING(20 -1,20 11)")) is None, "a line that misses does not split")
lshape = G("POLYGON((0 0,20 0,20 10,10 10,10 20,0 20,0 0))")
tp = mg.two_point_cut(lshape, (5.0, 0.03), (5.0, 19.98))
check(len(tp) == 2 and near(sum(p.area() for p in tp), lshape.area(), 1e-6) and
      sorted(round(p.area(), 6) for p in tp) == [100, 200], f"two-point cut: areas {[p.area() for p in tp]} add up")
tp = mg.two_point_cut(sq, (0.02, 3), (10.04, 7))
check(len(tp) == 2 and near(sum(p.area() for p in tp), 100, 1e-6) and near(tp[0].area(), 50, 1e-6),
      "two-point cut: the clicks are moved onto the edge")
grid = [G("LINESTRING(0 10,30 10)"), G("LINESTRING(0 20,30 20)"), G("LINESTRING(10 0,10 30)"), G("LINESTRING(20 0,20 30)")]
pl = mg.planarize(grid)
check(len(pl) == 12 and near(sum(p.length() for _, p in pl), 120), f"planarize a # grid: {len(pl)} pieces")
check(sorted({i for i, _ in pl}) == [0, 1, 2, 3], "planarize keeps the source of each piece")
ex = mg.explode(G("MULTIPOLYGON(((0 0,1 0,1 1,0 0)),((5 5,6 5,6 6,5 5)))"))
check(len(ex) == 2 and not ex[0].isMultipart(), "explode multipart")

print("-- geometry: reshape --")
rs = mg.reshape(sq, G("LINESTRING(10 2,14 5,10 8)"))
check(rs is not None and near(rs.area(), 112), f"reshape with a sketch {rs.area() if rs else None}")
e = mg.extend_line(G("LINESTRING(0 0,5 0)"), G("LINESTRING(12 -5,12 5)"), "end")
check(pts(e)[-1] == (12, 0), f"extend to the target {pts(e)}")
e = mg.extend_line(G("LINESTRING(5 5,5 8)"), G("LINESTRING(-10 0,20 0)"), "start")
check(pts(e)[0] == (5, 0), f"extend the start {pts(e)}")
tr_ = mg.trim_line(G("LINESTRING(0 0,10 0)"), G("LINESTRING(7 -5,7 5)"), (9, 0.2))
check(len(tr_) == 1 and pts(tr_[0]) == [(0, 0), (7, 0)], f"trim at the target: the clicked piece goes {pts(tr_[0])}")
rows = mg.vertex_rows(sq)
check(len(rows) == 4, f"vertex table without the closing vertex ({len(rows)})")
mv = mg.move_vertices(sq, {0: (-1, -1)})
check(pts(mv)[0] == (-1, -1) and pts(mv)[-1] == (-1, -1), "moving the first vertex moves the ring's end too")
wig = G("LINESTRING(" + ",".join(f"{i} {0.05 * (-1) ** i}" for i in range(0, 101)) + ")")
gen = mg.generalize(wig, 0.2)
check(mg.vertex_count(gen) == 2, f"generalize (Douglas-Peucker) {mg.vertex_count(wig)} → {mg.vertex_count(gen)} vertices")
sm = mg.smooth(G("LINESTRING(0 0,10 0,10 10)"), 2)
check(mg.vertex_count(sm) == 6 and pts(sm)[0] == (0, 0) and pts(sm)[-1] == (10, 10),
      f"smooth: 2 iterations of corner cutting, ends kept ({mg.vertex_count(sm)} vertices)")
a = G("POLYGON((0 0,10 0,10 10,0 10,0 0))")
bb = G("POLYGON((10 0,20 0,20 10,10 10,10 0))")
na, nb = mg.shared_edge_reshape(a, bb, G("LINESTRING(10 0,13 5,10 10)"))
check(near(na.area(), 115) and near(nb.area(), 85) and near(na.area() + nb.area(), 200),
      f"shared edge: areas {na.area()} + {nb.area()}")
check(na.intersection(nb).length() > 11 and na.intersection(nb).area() < 1e-9 and na.touches(nb),
      "shared edge: the two polygons still touch along the new edge, no overlap")
try:
    mg.shared_edge_reshape(a, bb, G("LINESTRING(10 0,25 5,10 10)"))
    check(False, "shared edge: an edge leaving the pair is refused")
except mg.GeometryError:
    check(True, "shared edge: an edge leaving the pair is refused")

print("-- geometry: combine --")
mrg = mg.merge_geometries([a, bb])
check(not mrg.isMultipart() and near(mrg.area(), 200), "merge two polygons")
rows_ = [{"name": "A", "pop": 10, "code": "x"}, {"name": "B", "pop": 30, "code": "y"}]
ma = mg.merge_attributes(rows_, main=1)
check(ma == {"name": "B", "pop": 30, "code": "y"}, f"merge: the chosen feature's values win {ma}")
ma = mg.merge_attributes(rows_, main=0, rules={"pop": "sum", "code": "concat"})
check(ma == {"name": "A", "pop": 40, "code": "x, y"}, f"merge: rules per field {ma}")
ma = mg.merge_attributes(rows_, main=0, rules={"name": "largest", "pop": "mean"}, sizes=[5, 9])
check(ma["name"] == "B" and ma["pop"] == 20, f"merge: largest feature / mean {ma}")
ci = mg.clip(sq, G("POLYGON((5 -5,20 -5,20 20,5 20,5 -5))"))
ce = mg.clip(sq, G("POLYGON((5 -5,20 -5,20 20,5 20,5 -5))"), keep_inside=False)
check(near(ci.area(), 50) and near(ce.area(), 50) and ci.intersection(ce).area() < 1e-9, "clip keeps inside / erase removes")
check(mg.clip(sq, G("POLYGON((50 50,60 50,60 60,50 50))")) is None, "clip with nothing inside gives nothing (refused)")
# a 20 cm sliver between A (10 m shared) and B / C (5 m each) goes to A
A = G("POLYGON((0 0,10 0,10 10,0 10,0 0))")
B = G("POLYGON((10.2 5,20 5,20 10,10.2 10,10.2 5))")
C = G("POLYGON((10.2 0,20 0,20 5,10.2 5,10.2 0))")
gp = mg.gap_pieces([A, B, C], 0.3)
check(len(gp) == 1 and near(gp[0][0].area(), 2.0, 1e-6), f"gap fill finds the 20 cm sliver ({[g[0].area() for g in gp]})")
gf = mg.gap_fill([A, B, C], 0.3)
check(list(gf) == [0] and near(gf[0].area(), 102, 1e-6) and not gf[0].isMultipart() and mg.usable(gf[0]),
      f"gap fill gives it to the neighbour with the longest shared edge {list(gf)}")
check(mg.gap_fill([A, B, C], 0.15) == {}, "gap fill leaves gaps wider than the limit")
check(mg.gap_fill([A, G("POLYGON((30 0,40 0,40 10,30 10,30 0))")], 0.3) == {}, "nothing between polygons far apart")

print("-- geometry: clean up --")
spk = G("POLYGON((0 0,10 0,10 10,5 10,5.05 20,4.95 10,0 10,0 0))")
ns, nrm = mg.remove_spikes(spk, 5)
check(nrm >= 1 and near(ns.area(), 100, 1e-6) and mg.usable(ns), f"remove spikes: {nrm} removed, area {ns.area()}")
ns2, nrm2 = mg.remove_spikes(sq, 5)
check(nrm2 == 0, "remove spikes keeps right angles")
dup = G("LINESTRING(0 0,0 0,5 0,5.001 0,10 0)")
nd, ndr = mg.remove_duplicate_vertices(dup, 0.01)
check(ndr == 2 and pts(nd) == [(0, 0), (5, 0), (10, 0)], f"remove duplicate vertices / tiny segments {pts(nd)}")
gl = [G("LINESTRING(0 0,9.95 0)"), G("LINESTRING(10 -5,10 5)"), G("LINESTRING(20 0,30.04 0)"),
      G("LINESTRING(30 -5,30 5)"), G("LINESTRING(40 0,50 0)"), G("LINESTRING(50.03 0.02,60 0)")]
cg = mg.close_line_gaps(gl, 0.1)
check(pts(cg[0])[-1] == (10, 0), f"close gaps: undershoot reaches the line {pts(cg[0])}")
check(pts(cg[2])[-1] == (30, 0), f"close gaps: overshoot trimmed back to the crossing {pts(cg[2])}")
check((4 in cg and pts(cg[4])[-1] == (50.03, 0.02)) or (5 in cg and pts(cg[5])[0] == (50, 0)),
      "close gaps: two ends meet")
check(1 not in cg and 3 not in cg, "close gaps: lines without dangling ends near others stay")

print("-- geometry: attributes and validity --")
tv = mg.transfer_values({"Name": "x", "AREA": 5, "other": 1}, ["name", "area", "z"])
check(tv == {"name": "x", "area": 5}, f"transfer attributes by matching names {tv}")
tv = mg.transfer_values({"a": 1, "b": 2}, ["x"], {"x": "b"})
check(tv == {"x": 2}, "transfer attributes with a field map")
check(not mg.usable(G("POLYGON((0 0,10 10,10 0,0 10,0 0))")), "a bow-tie polygon is not usable")
check(not mg.usable(QgsGeometry()) and not mg.usable(G("LINESTRING(1 1,1 1)")), "empty / zero-length geometries are not usable")

# ==== 2. the tools on layers, through the pane ===========================================================
from qgis.PyQt.QtGui import QFontDatabase  # noqa: E402

for _fn in ("segoeui.ttf", "segoeuib.ttf"):     # the offscreen platform has no fonts of its own
    if os.path.exists("C:/Windows/Fonts/" + _fn):
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/" + _fn)
from qgis.PyQt.QtGui import QFont  # noqa: E402

app.setFont(QFont("Segoe UI", 9))

from qgis.gui import QgsAdvancedDigitizingDockWidget  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni import i18n  # noqa: E402
from biruni.editing import modify_pane, modify_tools as mt  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:32638")
project = QgsProject.instance()
project.setCrs(CRS)
canvas = QgsMapCanvas()
canvas.setFrameShape(QFrame.Shape.NoFrame)
canvas.resize(800, 800)
canvas.show()
canvas.setDestinationCrs(CRS)
canvas.setExtent(QgsRectangle(0, 0, 100, 100))
check(near(canvas.mapUnitsPerPixel(), 0.125), "offscreen canvas: 0.125 m per pixel")
cad = QgsAdvancedDigitizingDockWidget(canvas)


def mem(kind, name, fields="field=kind:string&field=n:integer"):
    lyr = QgsVectorLayer(f"{kind}?crs=EPSG:32638&{fields}", name, "memory")
    project.addMapLayer(lyr)
    return lyr


def add(lyr, wkt, **attrs):
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    for k, v in attrs.items():
        f[k] = v
    ok, out = lyr.dataProvider().addFeatures([f])
    lyr.updateExtents()
    return out[0].id()


parcels = mem("Polygon", "parcels")
P1 = add(parcels, "POLYGON((0 0,10 0,10 10,0 10,0 0))", kind="A", n=1)
P2 = add(parcels, "POLYGON((10 0,20 0,20 10,10 10,10 0))", kind="B", n=2)
P3 = add(parcels, "POLYGON((30 0,40 0,40 10,30 10,30 0))", kind="C", n=3)
G1 = add(parcels, "POLYGON((50 0,60 0,60 10,50 10,50 0))", kind="G1", n=4)
G2 = add(parcels, "POLYGON((60.2 5,70 5,70 10,60.2 10,60.2 5))", kind="G2", n=5)
G3 = add(parcels, "POLYGON((60.2 0,70 0,70 5,60.2 5,60.2 0))", kind="G3", n=6)
roads = mem("LineString", "roads")
R1 = add(roads, "LINESTRING(0 50,30 50)", kind="r1", n=1)
R2 = add(roads, "LINESTRING(0 60,30 60)", kind="r2", n=2)
R3 = add(roads, "LINESTRING(15 45,15 65)", kind="r3", n=3)
R4 = add(roads, "LINESTRING(40 50,45 50)", kind="r4", n=4)
T = add(roads, "LINESTRING(50 40,50 60)", kind="target", n=5)
points = mem("Point", "points")
curved = mem("CurvePolygon", "curved")
C1 = add(curved, "POLYGON((70 70,80 70,80 80,70 80,70 70))", kind="c", n=1)
canvas.setLayers([parcels, roads, points, curved])
for lyr in (parcels, roads, points, curved):
    lyr.startEditing()

state = {"active": parcels}


class FakeIface:
    def __init__(self):
        self.mw = QMainWindow()

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return canvas

    def cadDockWidget(self):
        return cad

    def activeLayer(self):
        return state["active"]


class FakeTheme:
    dark = False

    def tokens(self):
        from biruni.theme import TOKENS
        return TOKENS["dark" if self.dark else "light"]


class FakePlugin:
    def __init__(self):
        self.iface = FakeIface()
        self.theme = FakeTheme()
        self.toolbox = None


def pix(x, y):
    p = canvas.getCoordinateTransform().transform(QgsPointXY(x, y))
    return QPoint(round(p.x()), round(p.y()))


def mouse(tool, kind, x, y, button=Qt.MouseButton.LeftButton):
    t = {"move": QEvent.Type.MouseMove, "press": QEvent.Type.MouseButtonPress,
         "release": QEvent.Type.MouseButtonRelease}[kind]
    e = QgsMapMouseEvent(canvas, t, pix(x, y), button if kind != "move" else Qt.MouseButton.NoButton,
                         Qt.MouseButton.NoButton if kind == "move" else button, Qt.KeyboardModifier.NoModifier)
    {"move": tool.canvasMoveEvent, "press": tool.canvasPressEvent, "release": tool.canvasReleaseEvent}[kind](e)


def click(x, y):
    tool = canvas.mapTool()
    mouse(tool, "move", x, y)
    mouse(tool, "press", x, y)
    mouse(tool, "release", x, y)


def key(target, k, text=""):
    e = QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier, text)
    target.keyPressEvent(e)
    return e.isAccepted()


def select(lyr, fids):
    for other in (parcels, roads, points, curved):
        if other is not lyr:
            other.removeSelection()
    lyr.selectByIds(list(fids))
    pane.selection_changed()


def geom(lyr, fid):
    return lyr.getFeature(fid).geometry()


def strip(kind):
    return pane.strips.messages().get(kind, "")


def undo(lyr, steps=1):
    for _ in range(steps):
        lyr.undoStack().undo()


plugin = FakePlugin()
print("-- pane: the list --")
pane = modify_pane.open_pane(plugin)
check(pane.objectName() == "BiruniModifyFeatures" and pane.stack.currentIndex() == 0, "the pane opens on the list")
check(pane.section_names() == ["Alignment", "Construct", "Divide", "Reshape", "Combine", "Attributes", "Clean up"],
      f"sections like Pro: {pane.section_names()}")
check(len(pane.visible_tools()) == len(mt.tools()) == 28, f"{len(mt.tools())} tools listed")
pane.search.setText("split")
check(set(pane.visible_tools()) == {"split_sketch", "split_parts", "split_at_point"}, f"search: {pane.visible_tools()}")
pane.search.setText("simplify")
check(pane.visible_tools() == ["generalize"], "search by keyword (simplify → Generalize)")
pane.search.setText("")
pane.toggle_favourite("two_point_cut")
app.processEvents()
check(pane.section_names()[0] == "Favourites" and pane.visible_tools()[0] == "two_point_cut",
      "a favourite is pinned on top")
pane.toggle_favourite("two_point_cut")
app.processEvents()
check(pane.section_names()[0] == "Alignment", "… and unpinned")

print("-- pane: a tool --")
pane.open_tool("split_parts")
check(pane.stack.currentIndex() == 1 and pane.tool_name.text() == "Split into parts" and not pane.back.icon().isNull(),
      "the tool view: back arrow, icon and name")
check(strip("info") == "Make a selection.", "blue strip: Make a selection.")
check(pane.buttons.isVisibleTo(pane) and pane.apply_btn.text() == "Apply", "Apply / Reset shown for a tool that applies")
check(key(pane, Qt.Key.Key_Escape) and pane.stack.currentIndex() == 0, "Esc goes back to the list")
pane.open_tool("split_parts")
pane.back.click()
check(pane.stack.currentIndex() == 0, "the back arrow goes back")

print("-- split into parts / undo / refusals --")
pane.open_tool("split_parts")
select(roads, [R1])
check(strip("info") == "" and pane.sel.topLevelItemCount() == 1 and pane.sel.topLevelItem(0).childCount() == 1,
      "selection row lists the layer and its feature; the blue strip goes")
pane.set_value("parts", 3)
n_before, undo_before = roads.featureCount(), roads.undoStack().count()
check(key(pane, Qt.Key.Key_Return) and roads.featureCount() == n_before + 2, "Enter applies: the line is now 3 lines")
check(strip("success") == "Split into parts completed: 3 features.", f"green strip: {strip('success')}")
check(roads.undoStack().count() == undo_before + 1, "one Undo step for the apply")
lens = sorted(round(f.geometry().length(), 6) for f in roads.getFeatures() if f["kind"] == "r1")
check(lens == [10, 10, 10], f"three equal parts with the line's attributes {lens}")
undo(roads)
check(roads.featureCount() == n_before and near(geom(roads, R1).length(), 30), "one Undo reverts the whole apply")
roads.commitChanges()
roads.selectByIds([R1])
pane.selection_changed()
check(pane.apply() == 0 and "Turn on editing for “roads” first." in strip("warning"), f"not editable: refused ({strip('warning')})")
roads.startEditing()
select(parcels, [P1])
check(pane.apply() == 0 and "works on line features" in strip("warning"), "wrong geometry type: refused")
pane.go_back()

pane.open_tool("buffer_edit")
select(parcels, [P3])
pane.set_value("distance", -6.0)
before = geom(parcels, P3).asWkt()
check(pane.apply() == 0 and "not be a valid polygon" in strip("warning") and geom(parcels, P3).asWkt() == before,
      f"an empty / invalid result is never written ({strip('warning')})")
pane.go_back()
pane.open_tool("generalize")
pane.set_value("tolerance", 0.5)
pv = pane.update_preview()
check(len(pv) == 1, "live preview of Generalize")
pane.go_back()

print("-- interactive tools --")
pane.open_tool("two_point_cut")
check(isinstance(canvas.mapTool(), mt.ClickTool) and not pane.buttons.isVisibleTo(pane), "two-point cut sets a click tool")
select(parcels, [])
parcels.removeSelection()
pane.selection_changed()
n0 = parcels.featureCount()
click(30, 3)
click(40, 7)
check(parcels.featureCount() == n0 + 1, "two-point cut: the polygon under the clicks is cut in two")
areas = sorted(round(f.geometry().area(), 6) for f in parcels.getFeatures() if f["kind"] == "C")
check(areas == [50, 50] and "completed: 2 features" in strip("success"), f"two-point cut: areas {areas}")
undo(parcels)
check(parcels.featureCount() == n0 and near(geom(parcels, P3).area(), 100), "… undone in one step")
check(key(canvas.mapTool(), Qt.Key.Key_Escape) and pane.stack.currentIndex() == 0, "Esc on the map goes back")

pane.open_tool("shared_edge")
state["active"] = parcels
check(isinstance(canvas.mapTool(), mt.SketchTool), "shared edge uses the Create Features sketch (typed values, HUD)")
click(10, 0)
click(13, 5)
click(10, 10)
key(canvas.mapTool(), Qt.Key.Key_F2)
check(near(geom(parcels, P1).area(), 115) and near(geom(parcels, P2).area(), 85) and
      geom(parcels, P1).touches(geom(parcels, P2)), "shared edge: both polygons follow the new edge")
undo(parcels)
check(near(geom(parcels, P1).area(), 100), "shared edge undone in one step")
pane.go_back()

pane.open_tool("fillet")
select(curved, [C1])
pane.set_value("radius", 2.0)
click(80, 80)
g = geom(curved, C1)
check(QgsWkbTypes.isCurvedType(g.wkbType()) and near(g.area(), 100 - (4 - math.pi), 1e-3),
      f"fillet on a curve layer stores a true arc ({g.asWkt()[:40]}…)")
undo(curved)
select(parcels, [P1])
click(10, 10)
g = geom(parcels, P1)
check(not QgsWkbTypes.isCurvedType(g.wkbType()) and near(g.area(), 100 - (4 - math.pi), 0.03) and g.isGeosValid(),
      f"fillet on a plain layer is densified ({g.area()}, {pane.strips.messages()})")
undo(parcels)
pane.go_back()

pane.open_tool("split_sketch")
select(parcels, [P3])
click(35, -2)
click(35, 12)
key(canvas.mapTool(), Qt.Key.Key_F2)
check(sorted(round(f.geometry().area()) for f in parcels.getFeatures() if f["kind"] == "C") == [50, 50],
      "split by a drawn line")
undo(parcels)
pane.go_back()

pane.open_tool("split_at_point")
select(roads, [R2])
click(12, 60)
check(sorted(round(f.geometry().length(), 3) for f in roads.getFeatures() if f["kind"] == "r2") == [12, 18],
      "split a line at the clicked point")
undo(roads)
pane.go_back()

pane.open_tool("reshape")
select(parcels, [P3])
click(40, 2)
click(44, 5)
click(40, 8)
key(canvas.mapTool(), Qt.Key.Key_F2)
check(near(geom(parcels, P3).area(), 112), f"reshape with a sketch {geom(parcels, P3).area()}")
undo(parcels)
pane.go_back()

pane.open_tool("extend_trim")
state["active"] = roads
select(roads, [T])
click(44, 50)
check(near(geom(roads, R4).length(), 10) and pts(geom(roads, R4))[-1] == (50, 50), "extend to the selected line")
undo(roads)
pane.set_value("mode", "trim")
select(roads, [R2])
click(15, 63)
check(near(geom(roads, R3).length(), 15) and "completed" in strip("success"), f"trim the clicked piece at the selected line ({geom(roads, R3).length()}, {pane.strips.messages()})")
undo(roads)
pane.go_back()

pane.open_tool("align_edge")
state["active"] = parcels
select(parcels, [P3])
pane.set_value("tolerance", 0.5)
parcels.changeGeometry(P3, G("POLYGON((30 0,40 0,40 10,30 10.3,30 0))"))
click(35, 50)     # nothing there
check("No line or edge" in strip("info"), "align edge: a click on nothing asks again")
roads.changeGeometry(R2, G("LINESTRING(25 10,45 10)"))
click(35, 10)
check(pts(geom(parcels, P3))[3] == (30, 10), f"align edge: the vertex moved onto the target {pts(geom(parcels, P3))}")
undo(parcels, 2)
undo(roads)
pane.go_back()

pane.open_tool("clip")
select(parcels, [P1])
parcels.removeSelection()
parcels.selectByIds([P1])
pane.selection_changed()
pane.set_value("keep", "outside")
click(15, 5)        # P2 is the cutter: P1 touches it only, nothing overlaps
check("Nothing would be left" in strip("info"), f"clip: nothing overlapping is refused {pane.strips.messages()}")
cut_id = add(curved, "POLYGON((5 -5,25 -5,25 20,5 20,5 -5))", kind="cutter", n=9)
curved.dataProvider().deleteFeatures([C1])
canvas.setLayers([curved, parcels, roads, points])
click(6, 15)
check(near(geom(parcels, P1).area(), 50), "erase by the clicked polygon of another layer")
undo(parcels)
pane.set_value("keep", "inside")
click(6, 15)
check(near(geom(parcels, P1).area(), 50) and geom(parcels, P1).boundingBox().xMinimum() == 5, "clip keeps the inside")
undo(parcels)
curved.dataProvider().deleteFeatures([cut_id])
pane.go_back()

pane.open_tool("transfer")
state["active"] = parcels
parcels.removeSelection()
pane.selection_changed()
click(35, 5)                  # source: P3 (kind C, n 3)
check("Source" in strip("info"), "transfer: the source is picked")
click(5, 5)
f1 = parcels.getFeature(P1)
check(f1["kind"] == "C" and f1["n"] == 3 and "completed: 1 feature." in strip("success"), "transfer: values copied")
undo(parcels)
check(parcels.getFeature(P1)["kind"] == "A", "transfer undone in one step")
pane.go_back()

pane.open_tool("replace_geometry")
select(parcels, [P3])
click(30, 20)
click(40, 20)
click(35, 30)
key(canvas.mapTool(), Qt.Key.Key_F2)
check(near(geom(parcels, P3).area(), 50) and parcels.getFeature(P3)["kind"] == "C", "replace geometry keeps the attributes")
undo(parcels)
pane.go_back()

pane.open_tool("transform")
select(parcels, [P3])
pane.set_value("dx", 5.0)
pane.set_value("angle", 90.0)
click(30, 0)         # the pivot
check(pane.values()["pivot"] == "click" and pane.pivot == (30, 0), "transform: clicking the map sets the pivot")
key(pane, Qt.Key.Key_Return)
bb = geom(parcels, P3).boundingBox()
check(near(bb.xMinimum(), 35) and near(bb.yMaximum(), 0) and near(bb.yMinimum(), -10), f"rotate 90° about the click, then move 5 m: {bb.toString(2)}")
undo(parcels)
pane.go_back()

print("-- tools that apply --")


def run_tool(tid, lyr, fids, **values):
    pane.open_tool(tid)
    select(lyr, fids)
    for k_, v_ in values.items():
        pane.set_value(k_, v_)
    return pane.apply()


n0 = parcels.featureCount()
check(run_tool("repeat_copy", parcels, [P3], count=3, dx=12.0, dy=0.0) == 3 and parcels.featureCount() == n0 + 3,
      "repeat copy: 3 copies added")
undo(parcels)
check(run_tool("copy_parallel", roads, [R1], distance=2.0, side="both") == 2, "copy parallel: 2 lines added")
undo(roads)
check(run_tool("points_along", roads, [R1], along="parts", parts=3) == 4 and points.featureCount() == 4,
      "points along a line into the point layer")
undo(points)
check(run_tool("buffer_edit", parcels, [P3], distance=1.0) == 1 and geom(parcels, P3).area() > 139, "buffer edit")
undo(parcels)
n0 = roads.featureCount()
nplan = run_tool("planarize", roads, [R1, R2, R3])
check(nplan == 7 and roads.featureCount() == n0 + 4, f"planarize: R3 cut in 3, R1 and R2 in 2 ({nplan}, {pane.strips.messages()})")
undo(roads)
mp = add(parcels, "MULTIPOLYGON(((80 0,85 0,85 5,80 0)),((90 0,95 0,95 5,90 0)))", kind="M", n=7)
check(run_tool("explode", parcels, [mp]) == 2, "explode: a multipart polygon becomes 2 features")
undo(parcels)
check(run_tool("smooth", roads, [R4], iterations=2) == 1, "smooth")
undo(roads)
check(run_tool("gap_fill", parcels, [G1, G2, G3], width=0.3) == 1 and near(geom(parcels, G1).area(), 102, 1e-6),
      "fill gaps: the 20 cm sliver goes to the neighbour with the longest shared edge")
undo(parcels)
check(near(geom(parcels, G1).area(), 100), "fill gaps undone")
spiky = add(parcels, "POLYGON((0 20,10 20,10 30,5 30,5.05 40,4.95 30,0 30,0 20))", kind="S", n=8)
check(run_tool("remove_spikes", parcels, [spiky], angle=5.0) == 1 and near(geom(parcels, spiky).area(), 100, 1e-6), "remove spikes")
undo(parcels)
dup = add(roads, "LINESTRING(0 80,0 80,5 80,5.001 80,10 80)", kind="d", n=9)
check(run_tool("remove_duplicates", roads, [dup], tolerance=0.01) == 1 and len(pts(geom(roads, dup))) == 3, "remove duplicate vertices")
undo(roads)
und = add(roads, "LINESTRING(0 90,14.95 90)", kind="u", n=10)
check(run_tool("close_gaps", roads, [und, R3], tolerance=0.1) == 0, "close gaps: nothing within reach")
cross = add(roads, "LINESTRING(15 85,15 95)", kind="x", n=11)
check(run_tool("close_gaps", roads, [und, cross], tolerance=0.1) == 1 and pts(geom(roads, und))[-1] == (15, 90),
      "close gaps: the undershoot reaches the line")
undo(roads)

pane.open_tool("change_values")
select(parcels, [P1, P2])
pane.set_value("field", "kind")
pane.set_value("value", "Z")
check(pane.apply() == 2 and {parcels.getFeature(f)["kind"] for f in (P1, P2)} == {"Z"}, "change field values for the selection")
pane.set_value("field", "n")
pane.set_value("value", "abc")
check(pane.apply() == 0 and "does not fit" in strip("warning"), "a value that does not fit the field is refused")
undo(parcels)

pane.open_tool("merge")
select(parcels, [P1, P2])
w = pane.widgets["main"]
w.setCurrentIndex(w.findData(f"{parcels.id()}|{P2}"))
rules = pane.widgets["rules"]
for r in range(rules.rowCount()):
    if rules.item(r, 0).data(Qt.ItemDataRole.UserRole) == "n":
        rules.cellWidget(r, 1).setCurrentIndex(rules.cellWidget(r, 1).findData("sum"))
n0 = parcels.featureCount()
check(pane.apply() == 2 and parcels.featureCount() == n0 - 1, "merge two parcels into one")
f2 = parcels.getFeature(P2)
check(near(f2.geometry().area(), 200) and f2["kind"] == "B" and f2["n"] == 3, f"merge: kept feature's values + sum rule ({f2['kind']}, {f2['n']})")
undo(parcels)
check(parcels.featureCount() == n0, "merge undone in one step")

pane.open_tool("edit_vertices")
select(parcels, [P3])
table = pane.widgets["vertices"].table
check(table.rowCount() == 4, "vertex table: one row per vertex")
table.item(2, 0).setText("41,5")
check(pane.apply() == 1 and pts(geom(parcels, P3))[2] == (41.5, 10), "vertex table: a typed X moves the vertex (Arabic comma decimal)")
undo(parcels)

pane.open_tool("smooth")
pane.set_value("iterations", 5)
pane.go_back()
pane.open_tool("smooth")
check(pane.values()["iterations"] == 5, "the options are remembered for the session")
pane.go_back()
pane.close()
pane = modify_pane.open_pane(plugin)
check(modify_pane._SESSION["last"] == "smooth" and pane.list.currentItem() is not None and
      pane.list.currentItem().data(0, modify_pane.ROLE_TOOL) == "smooth", "the last tool is remembered")

# ==== 3. icons ===========================================================================================
print("-- icons --")
from biruni import icons  # noqa: E402
from qgis.PyQt.QtCore import QByteArray, QRect  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage, QPainter  # noqa: E402
from qgis.PyQt.QtSvg import QSvgRenderer  # noqa: E402

edit_dir = os.path.join(icons.ICON_DIR, "edit")
names = sorted(f[:-4] for f in os.listdir(edit_dir) if f.endswith(".svg"))
bad = [n for n in names if not QSvgRenderer(QByteArray(open(os.path.join(edit_dir, n + ".svg"), "rb").read())).isValid()]
check(not bad and len(names) >= 33, f"{len(names)} edit icons parse ({bad})")
missing = [t.icon for t in mt.tools() if not icons.exists(t.icon)]
check(not missing, f"every tool has its own icon ({missing})")
check(len({t.icon for t in mt.tools()}) == len(mt.tools()), "no two tools share an icon")
big = [n for n in names if os.path.getsize(os.path.join(edit_dir, n + ".svg")) > 1200]
check(not big, f"icons stay small hand-written SVGs ({big})")
cols, cw, chh = 6, 250, 92
rows_n = (len(names) + cols - 1) // cols
sheet = QImage(cols * cw, rows_n * chh, QImage.Format.Format_ARGB32)
sheet.fill(QColor("#FFFFFF"))
qp = QPainter(sheet)
qp.setRenderHint(QPainter.RenderHint.Antialiasing)
qp.setFont(QFont("Segoe UI", 8))
for i, n in enumerate(names):
    x0, y0 = (i % cols) * cw, (i // cols) * chh
    qp.fillRect(QRect(x0 + cw // 2, y0, cw // 2, chh), QColor("#20252B"))
    for half, fg in ((0, "#33414C"), (1, "#D3D9DF")):
        ic = icons.icon("edit/" + n, fg)
        bx = x0 + half * cw // 2 + 8
        qp.drawPixmap(bx, y0 + 10, ic.pixmap(16, 16))
        qp.drawPixmap(bx + 22, y0 + 6, ic.pixmap(24, 24))
        qp.drawPixmap(bx + 54, y0 + 6, ic.pixmap(48, 48))
    qp.setPen(QColor("#33414C"))
    qp.drawText(x0 + 6, y0 + chh - 10, n)
qp.end()
sheet_path = os.path.join(SHOTS, "edit_icons_sheet.png")
check(sheet.save(sheet_path), f"icon contact sheet: {sheet_path}")

# ==== 4. screenshots: English / Arabic, light / dark =====================================================
print("-- screenshots --")


def shoot(name, dark, rtl):
    from biruni.theme import TOKENS, build_palette

    plugin.theme.dark = dark
    app.setPalette(build_palette(TOKENS["dark" if dark else "light"]))
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
    pane.setLayoutDirection(app.layoutDirection())
    pane.refresh_theme()
    pane.go_back()
    pane.setFloating(True)          # offscreen: nothing appears on the screen
    pane.resize(330, 680)
    pane.show()
    app.processEvents()
    pane.list.scrollToTop()
    p1 = os.path.join(SHOTS, f"modify_{name}_list.png")
    pane.widget().grab().save(p1)
    select(parcels, [P1, P2, P3])
    pane.open_tool("split_parts")
    app.processEvents()
    pane.strips.show_message("success", tr_("{tool} completed: {n} features.").format(tool=pane.current.name, n=3))
    pane.strips.show_message("info", tr_("Make a selection."))
    app.processEvents()
    p2 = os.path.join(SHOTS, f"modify_{name}_tool.png")
    pane.widget().grab().save(p2)
    return p1, p2


def tr_(s):
    return i18n.tr(s)


shots = []
shots += shoot("en_light", False, False)
shots += shoot("en_dark", True, False)
ar_json = os.environ.get("BIRUNI_MODIFY_I18N")      # the translations before they are merged into ar.json
if ar_json and os.path.exists(ar_json):
    import json

    data = json.load(open(ar_json, encoding="utf-8"))
    i18n.init("ar")
    i18n._strings.update({k: v[0] for k, v in data.items()})
    modify_pane.close_pane(plugin)          # the pane is built in the language of the program
    app.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    pane = modify_pane.open_pane(plugin)
    check(pane.windowTitle() == "تعديل العناصر" and pane.section_names()[0] == "المحاذاة", "the pane in Arabic")
    pane.search.setText("دَمج")
    check(pane.visible_tools() == ["merge"], f"Arabic search ignores tashkeel: {pane.visible_tools()}")
    pane.search.setText("")
    shots += shoot("ar_light", False, True)
    shots += shoot("ar_dark", True, True)
check(all(os.path.getsize(s) > 2000 for s in shots), f"{len(shots)} screenshots in {SHOTS}")
app.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

modify_pane.close_pane(plugin)
check(modify_pane._PANES == {}, "pane closed")
for lyr in project.mapLayers().values():
    if lyr.isEditable():
        lyr.rollBack()
print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED:\n  " + "\n  ".join(failures))
sys.exit(1 if failures else 0)
