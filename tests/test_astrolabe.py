r"""The astrolabe: the command engine on real layers, the commands carried into the project, and the disc on the
map (off screen).

Run:  engine\bin\python-qgis.bat tests\test_astrolabe.py
"""
import math
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProject, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QPoint, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni import astrolabe  # noqa: E402
from biruni.astrolabe import engine  # noqa: E402
from biruni.astrolabe.layers import LayerSource, commit, work_crs  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def near(a, b, tol=.01):
    return abs(a - b) <= tol * max(1.0, abs(b))


# ---- a small town: 33 parcels in three blocks, 3 roads, 12 trees (metres, UTM 38N) -------------------------------
A0, O = -0.12, (500000.0, 2700000.0)


def lp(u, v):
    return QgsPointXY(O[0] + u * math.cos(A0) - v * math.sin(A0), O[1] + u * math.sin(A0) + v * math.cos(A0))


def layer(kind, name, fields="&field=no:integer"):
    lyr = QgsVectorLayer(f"{kind}?crs=EPSG:32638{fields}", name, "memory")
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def add(lyr, geom, no):
    f = QgsFeature(lyr.fields())
    f.setGeometry(geom)
    f.setAttributes([no])
    lyr.dataProvider().addFeatures([f])


parcels, roads, trees = layer("Polygon", "Parcels"), layer("LineString", "Roads"), layer("Point", "Trees")
n = 0


def rect(u, v, w, h):
    global n
    n += 1
    add(parcels, QgsGeometry.fromPolygonXY([[lp(u, v), lp(u + w, v), lp(u + w, v + h), lp(u, v + h), lp(u, v)]]), n)


for r in range(2):
    for c in range(6):
        rect(c * 28, r * 36, 28, 36)
for r in range(2):
    for c in range(6):
        rect(c * 28, 96 + r * 36, 28, 36)
for r in range(2):
    for c in range(3):
        rect(192 + c * 28, r * 36, 28, 36)
n += 1
add(parcels, QgsGeometry.fromPolygonXY([[lp(192, 96), lp(248, 96), lp(248, 132), lp(220, 132), lp(220, 168), lp(192, 168),
                                         lp(192, 96)]]), n)
rect(220, 132, 28, 36)
rect(248, 96, 28, 72)
for i, pts in enumerate(([lp(-24, 84), lp(300, 84)], [lp(180, -24), lp(180, 192)],
                         [lp(-24, 186), lp(60, 188), lp(130, 196), lp(200, 212), lp(250, 234), lp(300, 262)])):
    add(roads, QgsGeometry.fromPolylineXY(pts), i + 1)
for k in range(6):
    add(trees, QgsGeometry.fromPointXY(lp(14 + 28 * k, 76)), 2 * k + 1)
    add(trees, QgsGeometry.fromPointXY(lp(14 + 28 * k, 92)), 2 * k + 2)
for lyr in (parcels, roads, trees):
    lyr.updateExtents()
QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:32638"))
by_no = {f["no"]: f.id() for f in parcels.getFeatures()}
parcels.selectByIds([by_no[3], by_no[4], by_no[9]])

mw = QMainWindow()
canvas = QgsMapCanvas(mw)
mw.setCentralWidget(canvas)
mw.resize(1100, 760)
mw.show()
canvas.setDestinationCrs(QgsProject.instance().crs())
canvas.setLayers([trees, roads, parcels])
ext = parcels.extent()
ext.combineExtentWith(roads.extent())
ext.scale(1.2)
canvas.setExtent(ext)
app.processEvents()
P, RD, TR = "L:" + parcels.id(), "L:" + roads.id(), "L:" + trees.id()

# ---- 1. the engine ----------------------------------------------------------------------------------------------
src = LayerSource(canvas, parcels)
check(src.crs.authid() == "EPSG:32638", "a project in metres is worked in its own CRS")
check([x.name() for x in src.layers()][0] == "Parcels" and len(src.layers()) == 3, "the current layer comes first on the rings")


def run(*steps):
    return engine.run(list(steps), LayerSource(canvas, parcels))


def area(o):
    return sum(g.area() for g in o.geoms)


o, = run({"t": "sel", "v": "buffer", "val": 10})
check(o.n == 3 and o.made and len(o.geoms) == 3 and all(g.area() > 1008 for g in o.geoms), "buffer: one shape for each selected parcel")
o1, o2, o3 = run({"t": RD, "v": "buffer", "val": 15}, {"t": P, "v": "intersect", "other": "prev"},
                 {"t": "prev", "v": "measure", "opt": "m2"})
check(len(o2.geoms) == 20 and near(area(o2), 2340), f"road widening: 20 parcels lose {area(o2):.1f} m² in all (2340)")
check(len(o3.labels) == 20 and sorted(round(v) for _p, v, _k in o3.labels)[:3] == [84, 84, 84] and o3.labels[0][2] == "area",
      "… and each piece is measured")
check(all(i.attrs is not None and i.layer == parcels.id() for i in o2.last), "the pieces keep their parcel's attributes")
o, = run({"t": P, "v": "union"})
check(len(o.geoms) == 1 and near(area(o), 36288, .0001), "dissolve: one shape of the whole area")
o, = run({"t": "sel", "v": "split", "val": 3})
check(len(o.geoms) == 9 and all(near(g.area(), 336, .0005) for g in o.geoms), "split: 3 parcels into 9 parts of 336 m² each")
check(len(o.changed) == 3 and len(o.added) == 6 and len(o.labels) == 9, "… the parcel keeps its first part, two parts are added")
big = next(f for f in parcels.getFeatures() if f["no"] == 31)
parts = engine.split_equal(big.geometry(), 4)
check(len(parts) == 4 and all(near(g.area(), 756, .0005) for g in parts), "an L-shaped parcel splits into equal areas too")
o, = run({"t": P, "v": "select", "other": "sel"})
check(len(o.sel) == 8 and o.note == ("select", 8, 33), "select what touches the selection: the neighbours")
o1, o2 = run({"t": RD, "v": "buffer", "val": 20}, {"t": P, "v": "select", "other": "prev"})
check(len(o2.sel) == 23, f"buffer then select: {len(o2.sel)} parcels within 20 m of a road")
o1, o2 = run({"t": RD, "v": "buffer", "val": 15}, {"t": P, "v": "erase", "other": "prev"})
check(len(o2.geoms) == 33 and near(area(o2), 36288 - 2340), "erase: what is left of the parcels")
o1, o2 = run({"t": "sel", "v": "rotate", "val": 30}, {"t": "prev", "v": "scale", "val": 150})
check(near(area(o1), 3024) and near(area(o2), 6804) and len(o2.changed) == 3, "rotate then resize: areas 3024 then 6804")
o1, o2 = run({"t": TR, "v": "hull", "opt": "all"}, {"t": P, "v": "centroid"})
check(len(o1.geoms) == 1 and near(area(o1), 2240) and len(o2.geoms) == 33, "hull of the trees, centres of the parcels")
check(run({"t": TR, "v": "split", "val": 3})[0].err == "polygons_only", "splitting points is refused")
check(run({"t": P, "v": "intersect", "other": P})[0].err == "same_layer", "a layer is not intersected with itself")
parcels.removeSelection()
check(run({"t": "sel", "v": "buffer", "val": 5})[0].err == "no_selection", "nothing selected is said")
parcels.selectByIds([by_no[3], by_no[4], by_no[9]])
check(parcels.featureCount() == 33 and not parcels.isEditable(), "working a command out changes nothing")

# ---- 2. a geographic project: 100 on the ring is 100 m ----------------------------------------------------------
QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
geo = QgsVectorLayer("Point?crs=EPSG:4326", "Wells", "memory")
f = QgsFeature()
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(31.2, 30.05)))
geo.dataProvider().addFeatures([f])
QgsProject.instance().addMapLayer(geo)
canvas.setExtent(geo.extent().buffered(.01))
app.processEvents()
gsrc = LayerSource(canvas, geo)
o, = engine.run([{"t": "L:" + geo.id(), "v": "buffer", "val": 100}], gsrc)
check(gsrc.crs.authid() == "EPSG:32636" and near(gsrc.area(o.geoms[0]), math.pi * 100 ** 2, .01),
      f"in a project in degrees the work is done in UTM ({gsrc.crs.authid()}): a 100 m buffer has {gsrc.area(o.geoms[0]):.0f} m²")
QgsProject.instance().removeMapLayer(geo.id())
QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:32638"))
canvas.setDestinationCrs(QgsProject.instance().crs())
canvas.setExtent(ext)
app.processEvents()


# ---- 3. the astrolabe on the map --------------------------------------------------------------------------------
class Bar:
    said = []

    def createMessage(self, title, text):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

        w = QWidget()
        QHBoxLayout(w)
        w.text = text
        return w

    def pushWidget(self, item, level=None, duration=0):
        Bar.said.append(item.text)

    def popWidget(self, item):
        pass


class Iface:
    def __init__(self):
        self.active = parcels
        self.bar = Bar()

    def mainWindow(self):
        return mw

    def mapCanvas(self):
        return canvas

    def activeLayer(self):
        return self.active

    def setActiveLayer(self, lyr):
        self.active = lyr

    def messageBar(self):
        return self.bar


class Theme:
    def tokens(self):
        return {"text": "#1E252B", "text_dim": "#5B6670", "accent": "#0E7C74", "accent_text": "#FFFFFF",
                "panel": "#FFFFFF", "border": "#DDE2E7"}

    def is_dark(self):
        return False

    def on_change(self, fn):
        pass


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.activity = type("A", (), {"logged": [], "log": lambda self, *a: self.logged.append(a)})()


plugin = Plugin()
undo_install = astrolabe.install(plugin)
a = astrolabe.get(plugin)
check(a.open(QPoint(500, 380)) and a.is_open() and a.disc.isVisible() and a.say.isVisible(), "the astrolabe opens on the map")
check(a.ring0.key == "sel" and a.ring1.key == "buffer" and a.step() == {"t": "sel", "v": "buffer", "val": 10},
      f"it starts on the selection with a buffer ({a.step()})")
check("Buffer 10 m around the selected features (3)" in a.sentence(a.step(), a.outs[-1]), "the sentence: " + a.sentence(a.step(), a.outs[-1]))
bands = sum(rb.numberOfVertices() for rb in a.ghost.bands.values())
check(bands > 0, f"the preview is drawn on the map ({bands} vertices)")
inside = canvas.rect().contains(a.disc.geometry()) and not a.disc.geometry().intersects(a.say.geometry())
check(inside, "the disc is inside the map and the sentence does not cover it")

# turning the outer ring with the mouse: from 10 m at the top, a quarter turn
d = a.disc
c, rr = d.rect().center(), d.width() * 167 / 400.0
QTest.mousePress(d, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(c.x(), int(c.y() - rr)))
for deg in range(3, 46, 3):
    t = math.radians(deg)
    QTest.mouseMove(d, QPoint(int(c.x() + rr * math.sin(t)), int(c.y() - rr * math.cos(t))))
QTest.mouseRelease(d, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                   QPoint(int(c.x() + rr * math.sin(t)), int(c.y() - rr * math.cos(t))))
app.processEvents()
check(22 <= a.step()["val"] <= 26, f"dragging the outer ring 45° clockwise raises the buffer to {a.step()['val']} m")
d.setFocus()
QTest.keyClicks(d, "12.5")
check(a.step()["val"] == 12.5, "a number typed goes to the outer ring: 12.5 m")
QTest.keyClick(d, Qt.Key.Key_Down)
QTest.keyClick(d, Qt.Key.Key_Right if d.layoutDirection() == Qt.LayoutDirection.LeftToRight else Qt.Key.Key_Left)
check(d.focus_ring == 1 and a.ring1.key == "split", f"the arrows turn the middle ring to the next verb ({a.ring1.key})")
a._refresh_now()
check(a.step() == {"t": "sel", "v": "split", "val": 3} and len(a.outs[-1].geoms) == 9, "split is previewed: 9 parts")
shot = os.path.join(tempfile.gettempdir(), "biruni_astrolabe.png")
mw.grab().save(shot)
print("screenshot:", shot, flush=True)

# a command of three steps, built with "Then +"
a.load_step({"t": RD, "v": "buffer", "val": 15})
a._refresh_now()
a.then()
check(a.chain == [{"t": RD, "v": "buffer", "val": 15}] and a.ring0.key == "prev" and ("prev", "Result") in a.ring0.items,
      "Then +: the next step works on the result")
a.load_step({"t": P, "v": "intersect", "other": "prev"})
a._refresh_now()
check(len(a.outs) == 2 and len(a.outs[1].geoms) == 20, "the second step is previewed on the first step's result")
a.then()
a.load_step({"t": "prev", "v": "measure", "opt": "m2"})
a._refresh_now()
text = a.command_text(a.steps(), a.outs)
check(text.count(", then ") == 2 and "Measure the previous step's result (20) in m²" in text, "the command reads: " + text)
a.back()
check(len(a.chain) == 1 and a.step()["v"] == "intersect", "back one step")
a.then()
a.load_step({"t": "prev", "v": "measure", "opt": "m2"})
before = set(QgsProject.instance().mapLayers())
check(a.execute() and not a.is_open(), "the hub carries the command out and the astrolabe closes")
new = [QgsProject.instance().mapLayer(k) for k in set(QgsProject.instance().mapLayers()) - before]
names = sorted(x.name() for x in new)
check(names == ["Intersection", "Measurements"], f"two layers are made, the buffer was only a stepping stone ({names})")
inter = next(x for x in new if x.name() == "Intersection")
check(inter.featureCount() == 20 and inter.fields().indexOf("no") >= 0 and
      near(sum(f.geometry().area() for f in inter.getFeatures()), 2340), "the pieces are real features with their parcel number")
meas = next(x for x in new if x.name() == "Measurements")
check(meas.featureCount() == 20 and meas.labelsEnabled() and "84.0 m²" in [f["text"] for f in meas.getFeatures()],
      "the measures are a labelled layer")
check(Bar.said and "Total: 2,340.0 m²" in Bar.said[-1], "the total is said: " + (Bar.said[-1] if Bar.said else ""))
check(plugin.iface.activeLayer() is parcels, "the layer being worked on stays the current layer")
check(plugin.activity.logged and plugin.activity.logged[-1][1] == "buffer+intersect+measure", "the command is in the activity log")
a.undo_last()
check(set(QgsProject.instance().mapLayers()) == before, "Undo takes the new layers away")

# a change in place: split the selected parcels
check(a.execute([{"t": "sel", "v": "split", "val": 3}]), "split is carried out")
check(parcels.isEditable() and parcels.featureCount() == 39, f"the layer is being edited and has {parcels.featureCount()} parcels (39)")
parts = [f for f in parcels.getFeatures() if f["no"] == 3]
check(len(parts) == 3 and all(near(f.geometry().area(), 336, .0005) for f in parts), "the three parts keep the parcel's number")
a.undo_last()
check(parcels.featureCount() == 33, "one Undo brings the parcels back")
parcels.rollBack()

# selecting, and the last command again
parcels.selectByIds([by_no[3], by_no[4], by_no[9]])
check(a.execute([{"t": P, "v": "select", "other": "sel"}]) and parcels.selectedFeatureCount() == 8, "select the neighbours: 8 selected")
parcels.selectByIds([by_no[20]])
check(a.repeat() and parcels.selectedFeatureCount() > 1, f"the last command again, on the new selection ({parcels.selectedFeatureCount()})")
check(len(a.history) == 4, "the commands are kept in the history")
a.load(a.history[0][0])
check(a.is_open() and len(a.chain) == 2 and a.step()["v"] == "measure", "a command of the history goes back on the rings")
a.close()
check(not a.is_open() and all(rb.numberOfVertices() == 0 for rb in a.ghost.bands.values()), "closing clears the preview")

# nothing selected: it opens on the current layer
parcels.removeSelection()
a.open()
check(a.ring0.key == P, "with nothing selected it opens on the current layer")
a.close()
undo_install()
check(getattr(plugin, "_astrolabe", None) is None, "unloading removes it")

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
