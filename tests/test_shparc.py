r"""A shapefile that remembers its arcs: the short lines keep the arc's area exactly and its length to a tenth of
a millimetre, neighbours share the same points, any program reads the file, and the arcs come back.

Run:  engine\bin\python-qgis.bat tests\test_shparc.py
"""
import json
import math
import os
import random
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.core import QgsProject  # noqa: E402

from biruni.shparc import arcs, io, ui  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def shoelace(pts):
    return sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts))) / 2.0


def length(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


# ---- 1. the geometry of one arc --------------------------------------------------------------------------------
r = 50.0
p0, pm, p1 = (r, 0.0), (r * math.cos(math.pi / 4), r * math.sin(math.pi / 4)), (0.0, r)   # a quarter circle
pts, info = arcs.arc_points(p0, pm, p1)
sector = abs(shoelace(pts + [(0.0, 0.0)]))
check(abs(sector - r * r * math.pi / 4) < 1e-9, f"the short lines close the arc's own area ({sector:.9f} = {r * r * math.pi / 4:.9f} m²)")
on_arc = [(r * math.cos(i * math.pi / 2 / info["n"]), r * math.sin(i * math.pi / 2 / info["n"])) for i in range(info["n"] + 1)]
loss = r * r * math.pi / 4 - abs(shoelace(on_arc + [(0.0, 0.0)]))
check(loss > 1e-5, f"points on the arc itself - what every program writes - lose {loss * 1e4:.2f} cm² with the same number of points")
arc_len, got = r * math.pi / 2, length(pts)
check(0 <= got - arc_len <= arcs.LENGTH_TOLERANCE and abs(got - info["lines"]) < 1e-9,
      f"the length is {(got - arc_len) * 1000:.3f} mm longer than the arc ({info['n']} lines for {arc_len:.3f} m)")
check(round(got, 2) == round(arc_len, 2), "written to the centimetre it is the arc's length")
back, _i = arcs.arc_points(p1, pm, p0)
check(back == pts[::-1], "the same arc given from its other end has the very same points")
check(pts[0] == p0 and pts[-1] == p1, "the ends of the arc stay where they are")
flat, i2 = arcs.arc_points((0, 0), (5, 0), (10, 0))
check(flat == [(0, 0), (10, 0)] and i2["r"] is None, "three points on a line are a line")
full, i3 = arcs.arc_points((10.0, 0.0), (-10.0, 0.0), (10.0, 0.0))
check(abs(abs(shoelace(full[:-1])) - math.pi * 100) < 1e-9, "a full circle keeps the circle's area")
random.seed(7)
worst_len = worst_area = 0.0
bad_round = 0
for _ in range(400):
    rr, a0, sw = random.uniform(.5, 3000), random.uniform(0, 6.28), random.uniform(.02, 5.5) * random.choice((1, -1))
    cx, cy = random.uniform(-1e5, 1e5), random.uniform(-1e5, 1e5)
    at = lambda t: (cx + rr * math.cos(a0 + t), cy + rr * math.sin(a0 + t))  # noqa: E731
    q, inf = arcs.arc_points(at(0), at(sw / 2), at(sw))
    worst_len = max(worst_len, inf["lines"] - inf["arc"])
    local = [(x - cx, y - cy) for x, y in q] + [(0.0, 0.0)]   # measured about the centre: no large numbers
    worst_area = max(worst_area, abs(abs(shoelace(local)) - rr * rr * abs(sw) / 2) / (rr * rr * abs(sw) / 2))
    bad_round += round(inf["lines"], 2) != round(inf["arc"], 2)
check(worst_len <= arcs.LENGTH_TOLERANCE + 1e-9 and worst_area < 1e-9 and bad_round == 0,
      f"400 arcs of every size: length at most {worst_len * 1000:.3f} mm longer, area off by {worst_area:.1e} of itself, "
      f"{bad_round} not equal to the centimetre")

# ---- 2. two parcels that share an arc, written as a shapefile --------------------------------------------------
lyr = QgsVectorLayer("CurvePolygon?crs=EPSG:32638&field=parcel_number_long:integer&field=owner:string", "Parcels", "memory")
shapes = ["CURVEPOLYGON(COMPOUNDCURVE((0 0, 100 0), CIRCULARSTRING(100 0, 120 50, 100 100), (100 100, 0 100, 0 0)))",
          "CURVEPOLYGON(COMPOUNDCURVE(CIRCULARSTRING(100 100, 120 50, 100 0), (100 0, 200 0, 200 100, 100 100)))",
          "CURVEPOLYGON((300 0, 400 0, 400 100, 300 100, 300 0))"]
for n, wkt in enumerate(shapes):
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    f.setAttributes([n + 1, ["أحمد", "منى", "plain"][n]])
    lyr.dataProvider().addFeatures([f])
true = [f.geometry() for f in lyr.getFeatures()]
folder = tempfile.mkdtemp()
shp = os.path.join(folder, "parcels.shp")
rep = io.export(lyr, shp)
check(not rep["error"] and rep["features"] == 3 and rep["curved"] == 2 and rep["arcs"] == 2, f"written: {rep['features']} features, {rep['arcs']} arcs")
check(rep["max_area_diff"] < 1e-6 and rep["max_length_diff"] <= 1e-4 and rep["not_rounded"] == 0,
      f"areas off by {rep['max_area_diff']:.1e} m², lengths by {rep['max_length_diff'] * 1000:.3f} mm at most")
plain = QgsVectorLayer(shp, "any program", "ogr")
got = list(plain.getFeatures())
check(plain.isValid() and len(got) == 3 and not any(f.geometry().constGet().hasCurvedSegments() for f in got),
      "any program opens it: an ordinary shapefile of plain polygons")
check(all(abs(f.geometry().area() - t.area()) < 1e-6 for f, t in zip(got, true)),
      "… whose areas are the true areas (" + ", ".join(f"{f.geometry().area():.4f}" for f in got) + ")")
usual = [QgsGeometry(t.constGet().segmentize()) for t in true[:2]]
check(all(abs(u.area() - t.area()) > 0.005 for u, t in zip(usual, true)),
      f"the usual way loses or adds {abs(usual[0].area() - true[0].area()):.4f} m² on the same parcel")
check(all(round(f.geometry().length(), 2) == round(t.length(), 2) for f, t in zip(got, true)), "the perimeters to the centimetre are the true ones")
a, b = got[0].geometry(), got[1].geometry()
check(a.intersection(b).area() < 1e-9 and abs(a.combine(b).area() - (true[0].area() + true[1].area())) < 1e-6,
      "the two neighbours meet along the arc without a gap or an overlap")
names = [x.name() for x in plain.fields()]
check("AREA_TRUE" in names and abs(got[0]["AREA_TRUE"] - true[0].area()) < 1e-3 and abs(got[0]["PERIM_TRUE"] - true[0].length()) < 1e-3,
      "the true area and perimeter are also written as fields")
check(got[0]["owner"] == "أحمد", "Arabic text is kept (UTF-8)")
side = json.load(open(io.sidecar(shp), encoding="utf-8"))
check(io.remembers(shp) and len(side["arcs"]) == 2 and side["fields"] == {"parcel_num": "parcel_number_long"},
      f"beside it: {os.path.basename(io.sidecar(shp))} with 2 true shapes and the field name the format shortened {side['fields']}")

# ---- 3. the arcs come back --------------------------------------------------------------------------------------
out, r2 = io.restore(shp)
feats = list(out.getFeatures())
check(r2 == {"features": 3, "restored": 2, "changed": 0} and feats[0].geometry().constGet().hasCurvedSegments(),
      "restoring gives the arcs back as arcs")
check(all(abs(f.geometry().area() - t.area()) < 1e-9 for f, t in zip(feats, true)) and
      QgsGeometry(feats[0].geometry().constGet().geometryN(0).clone()).asWkt(6) == QgsGeometry.fromWkt(shapes[0]).asWkt(6),
      "… the very shapes that were drawn")
check(out.fields().indexOf("parcel_number_long") >= 0 and feats[1]["owner"] == "منى" and out.crs().authid() == "EPSG:32638",
      "with the full field name, the attributes and the CRS")

# ---- 4. another program changes the file -------------------------------------------------------------------------
other = QgsVectorLayer(shp, "other program", "ogr")
ids = [f.id() for f in other.getFeatures()]
g = other.getFeature(ids[1]).geometry()
g.moveVertex(205.0, -3.0, next(i for i, v in enumerate(g.vertices()) if abs(v.x() - 200) < 1e-9 and abs(v.y()) < 1e-9))
other.dataProvider().changeGeometryValues({ids[1]: g})
del other
out, r3 = io.restore(shp)
feats = list(out.getFeatures())
check(r3 == {"features": 3, "restored": 1, "changed": 1}, f"a parcel changed elsewhere: its arcs are no longer claimed ({r3})")
check(not feats[1].geometry().constGet().hasCurvedSegments() and abs(feats[1].geometry().area() - g.area()) < 1e-6
      and feats[0].geometry().constGet().hasCurvedSegments(), "it is taken as the other program left it; the untouched one keeps its arc")
other = QgsVectorLayer(shp, "other program", "ogr")
other.dataProvider().deleteFeatures([ids[2]])
del other
out, r4 = io.restore(shp)
check(r4["restored"] == 1 and r4["features"] == 2, "a record deleted elsewhere does not confuse the others")

# ---- 5. lines with arcs, and a layer without any ------------------------------------------------------------------
road = QgsVectorLayer("CompoundCurve?crs=EPSG:32638&field=name:string", "Road", "memory")
f = QgsFeature(road.fields())
f.setGeometry(QgsGeometry.fromWkt("COMPOUNDCURVE((0 0, 50 0), CIRCULARSTRING(50 0, 85.355 14.645, 100 50), (100 50, 100 120))"))
f.setAttributes(["ring road"])
road.dataProvider().addFeatures([f])
rshp = os.path.join(folder, "road.shp")
rr = io.export(road, rshp)
line = next(QgsVectorLayer(rshp, "r", "ogr").getFeatures())
true_len = f.geometry().length()
check(not rr["error"] and rr["arcs"] == 1 and 0 <= line.geometry().length() - true_len <= 1e-4 and abs(line["LEN_TRUE"] - true_len) < 1e-3,
      f"a line with an arc: {(line.geometry().length() - true_len) * 1000:.3f} mm longer, its true length in a field")
back, r5 = io.restore(rshp)
check(r5["restored"] == 1 and abs(next(back.getFeatures()).geometry().length() - true_len) < 1e-9, "… and its arc comes back")
flat_lyr = QgsVectorLayer("Polygon?crs=EPSG:32638&field=id:integer", "Flat", "memory")
f = QgsFeature(flat_lyr.fields())
f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(0, 0), QgsPointXY(10, 0), QgsPointXY(10, 10), QgsPointXY(0, 0)]]))
f.setAttributes([1])
flat_lyr.dataProvider().addFeatures([f])
fshp = os.path.join(folder, "flat.shp")
fr = io.export(flat_lyr, fshp, measures=False)
check(not fr["error"] and fr["arcs"] == 0 and fr["sidecar"] is None and not io.remembers(fshp), "a layer without arcs gets no extra file")
check(io.restore(fshp)[1] == {"features": 1, "restored": 0, "changed": 0}, "… and restoring it is the plain shapefile")

# ---- 6. in the program: the offer when such a shapefile is added, and the report ---------------------------------
class Bar:
    said, items = [], []

    def pushMessage(self, title, text, level=None, duration=0):
        Bar.said.append(text)

    def createMessage(self, title, text):
        from qgis.PyQt.QtWidgets import QHBoxLayout, QWidget

        w = QWidget()
        QHBoxLayout(w)
        w.text = text
        return w

    def pushWidget(self, item, level=None, duration=0):
        Bar.items.append(item)

    def popWidget(self, item):
        pass


class Iface:
    def messageBar(self):
        return Bar()

    def mainWindow(self):
        return None


plugin = type("P", (), {"iface": Iface()})()
shp2 = os.path.join(folder, "again.shp")
rep2 = io.export(lyr, shp2)
text, _level = ui.export_report(rep2, shp2)
check("3 features written with 2 arcs" in text and "0.10 mm" in text and "again.crv" in text, "the report after writing: " + text)
undo = ui.install(plugin)
added = QgsVectorLayer(shp2, "again", "ogr")
QgsProject.instance().addMapLayer(added)
for _ in range(5):
    app.processEvents()
check(len(Bar.items) == 1 and "again remembers its arcs" in Bar.items[0].text, "adding such a shapefile to the map offers to restore its arcs")
from qgis.PyQt.QtWidgets import QPushButton  # noqa: E402

Bar.items[0].findChild(QPushButton).click()
layers = list(QgsProject.instance().mapLayers().values())
check(len(layers) == 1 and layers[0].providerType() == "memory" and layers[0].name() == "again" and
      next(layers[0].getFeatures()).geometry().constGet().hasCurvedSegments() and layers[0].customProperty(ui.MARK) == shp2,
      "the button puts the layer with its arcs in the plain one's place")
check(Bar.said and "2 features got their arcs back" in Bar.said[-1], "and says so: " + (Bar.said[-1] if Bar.said else ""))
n_before = len(Bar.items)
QgsProject.instance().addMapLayer(QgsVectorLayer(fshp, "flat", "ogr"))
for _ in range(5):
    app.processEvents()
check(len(Bar.items) == n_before, "an ordinary shapefile is added without an offer")
undo()

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
