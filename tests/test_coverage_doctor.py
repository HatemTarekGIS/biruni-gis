"""The parcel coverage doctor: gaps, overlaps, unmatched sides, slivers and invalid shapes found on a parcel
fabric; «Clean» mends it in the edits (one Undo takes it back), arcs are left as they are; simplifying keeps the
shared sides shared.

Run:  engine\\bin\\python-qgis.bat tests\\test_coverage_doctor.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsFeatureRequest,  # noqa: E402
                       QgsGeometry, QgsProject,
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

import shapely  # noqa: E402

from biruni.coverage import core  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


check(shapely.__version__.startswith("2.2") and hasattr(shapely, "coverage_clean"),
      f"Shapely {shapely.__version__} (GEOS {shapely.geos_version_string}) has the coverage cleaner")


def sq(x, y, w=20.0, h=20.0):
    return f"(({x} {y},{x + w} {y},{x + w} {y + h},{x} {y + h},{x} {y}))"


def fabric(n=6, crs="EPSG:32638", ox=500000.0, oy=2700000.0, step=20.0, m=1.0):
    """m: layer units in one metre (the gap, the overlap and the sliver are in metres)."""
    lyr = QgsVectorLayer(f"MultiPolygon?crs={crs}&field=pid:integer", "Parcels", "memory")
    feats, pid = [], 0
    for i in range(n):
        for j in range(n):
            pid += 1
            x, y = ox + i * step, oy + j * step
            wkt = f"MULTIPOLYGON({sq(x, y, step, step)})"
            if (i, j) == (1, 1):        # a gap 0.2 m wide: its east side pulled in
                wkt = f"MULTIPOLYGON({sq(x, y, step - 0.2 * m, step)})"
            if (i, j) == (3, 3):        # an overlap 0.3 m into the east neighbour
                wkt = f"MULTIPOLYGON({sq(x, y, step + 0.3 * m, step)})"
            if (i, j) == (4, 1):        # its north side drawn again with a vertex 2 mm off
                wkt = (f"MULTIPOLYGON((({x} {y},{x + step} {y},{x + step} {y + step},{x + step / 2} {y + step + 0.002 * m},"
                       f"{x} {y + step},{x} {y})))")
            if (i, j) == (2, 4):        # taken out: a courtyard 20 m wide, not a gap
                continue
            f = QgsFeature(lyr.fields())
            f.setAttributes([pid])
            f.setGeometry(QgsGeometry.fromWkt(wkt))
            feats.append(f)
    f = QgsFeature(lyr.fields())         # a sliver 0.1 m wide along the west edge of the fabric
    f.setAttributes([900])
    f.setGeometry(QgsGeometry.fromWkt(f"MULTIPOLYGON({sq(ox - 0.1 * m, oy, 0.1 * m, step * 2)})"))
    feats.append(f)
    f = QgsFeature(lyr.fields())         # a bow-tie: not a valid shape
    f.setAttributes([901])
    bx, by = ox + n * step + 50 * m, oy
    f.setGeometry(QgsGeometry.fromWkt(f"MULTIPOLYGON((({bx} {by},{bx + 10 * m} {by + 10 * m},{bx + 10 * m} {by},{bx} {by + 10 * m},{bx} {by})))"))
    feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


lyr = fabric()
QgsProject.instance().addMapLayer(lyr)
rep = core.examine(lyr, gap_width=0.5, sliver_width=0.2)
counts = {k: rep.count(k) for k in ("gap", "overlap", "edge", "sliver", "invalid")}
check(counts["gap"] == 1 and counts["overlap"] == 2 and counts["sliver"] == 1 and counts["invalid"] == 1,
      f"one gap, two overlaps (the 0.3 m one, and the 2 mm one of the side drawn again), one sliver, one invalid "
      f"shape found: {counts}")
check(counts["edge"] >= 2 and not any(p.kind == "edge" and 900 in [f["pid"] for f in lyr.getFeatures(
    QgsFeatureRequest().setFilterFids(p.fids))] for p in rep.problems), f"the unmatched and overlapping sides are pointed at ({counts['edge']} parcels)")
gap = [p for p in rep.problems if p.kind == "gap"][0]
check(abs(gap.width - 0.2) < 0.01 and abs(gap.size - 4.0) < 0.01 and len(gap.fids) >= 2,
      f"the gap: {gap.width:.3f} m wide, {gap.size:.2f} m², between {gap.fids}")
ovs = sorted(p.size for p in rep.problems if p.kind == "overlap")
check(abs(ovs[1] - 6.0) < 0.01 and abs(ovs[0] - 0.02) < 0.001, f"the overlaps: {ovs[1]:.2f} m² and {ovs[0]:.3f} m²")
check(not any(p.kind == "gap" and p.size > 100 for p in rep.problems), "the 20 m courtyard is not a gap")
check("0.200 m wide" in gap.text() and "Clean" in gap.text(), "each problem says what it is: " + gap.text())
layers = core.problem_layers(rep, lyr)
check(len(layers) == 2 and layers[0].featureCount() == 5 and layers[1].featureCount() == counts["edge"],
      "on the map as two layers: the areas and the unmatched sides")

before = {f.id(): f.geometry().asWkt() for f in lyr.getFeatures()}
res = core.clean(lyr, gap_width=0.5)
check(lyr.isEditable() and lyr.isModified() and res.changed >= 3 and res.invalid == 1,
      f"cleaned in the layer's edits, not saved: {res.changed} parcels mended, the invalid one left")
after = core.examine(lyr, gap_width=0.5, sliver_width=0.2)
left = {k: after.count(k) for k in ("gap", "overlap", "edge", "invalid")}
check(left == {"gap": 0, "overlap": 0, "edge": 0, "invalid": 1}, f"after: no gap, no overlap, every side one ({left})")
check(lyr.undoStack().count() == 1, "one edit command")
lyr.undoStack().undo()
check({f.id(): f.geometry().asWkt() for f in lyr.getFeatures()} == before, "one Undo takes it all back")
lyr.rollBack()

# ---- simplify keeping the shared sides --------------------------------------------------------------
wavy = QgsVectorLayer("Polygon?crs=EPSG:32638", "wavy", "memory")
fs = []
for i in range(4):
    pts = [(i * 20.0, 0.0)] + [(i * 20.0 + 0.02 * (k % 2), 2.0 * k) for k in range(1, 10)] + [(i * 20.0, 20.0)]
    nxt = [((i + 1) * 20.0, 20.0)] + [((i + 1) * 20.0 + 0.02 * (k % 2), 2.0 * k) for k in range(9, 0, -1)] + [((i + 1) * 20.0, 0.0)]
    ring = pts + nxt + [pts[0]]
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((" + ",".join(f"{x} {y}" for x, y in ring) + "))"))
    fs.append(f)
wavy.dataProvider().addFeatures(fs)
nv0 = sum(f.geometry().constGet().nCoordinates() for f in wavy.getFeatures())
check(core.examine(wavy).clean, "a clean wavy fabric to simplify")
r = core.simplify(wavy, 0.5, keep_outline=True)
nv1 = sum(f.geometry().constGet().nCoordinates() for f in wavy.getFeatures())
check(r.changed == 4 and nv1 < nv0 and core.examine(wavy).clean,
      f"simplified ({nv0} → {nv1} vertices) and the shared sides still one")
wavy.rollBack()

# ---- arcs are left as they are ---------------------------------------------------------------------
arcs = QgsVectorLayer("MultiSurface?crs=EPSG:32638", "arcs", "memory")
a = QgsFeature()
a.setGeometry(QgsGeometry.fromWkt("MULTISURFACE(CURVEPOLYGON(COMPOUNDCURVE((0 0,20 0,20 20),CIRCULARSTRING(20 20,10 25,0 20),(0 20,0 0))))"))
b = QgsFeature()
b.setGeometry(QgsGeometry.fromWkt("MULTISURFACE(((19.7 0,40 0,40 20,19.7 20,19.7 0)))"))     # straight, in a curved type
arcs.dataProvider().addFeatures([a, b])
rep = core.examine(arcs)
check(rep.curved == [1] and rep.count("gap") == 0, "only the parcel that really has an arc counts as one")
wkt_a = next(arcs.getFeatures()).geometry().asWkt()
r = core.clean(arcs, gap_width=0.5, merge="min_index")      # the overlap goes to the first: the arc parcel
ga = [f.geometry() for f in arcs.getFeatures()]
check(r.curved == 1 and ga[0].asWkt() == wkt_a and ga[0].constGet().hasCurvedSegments(),
      "the arc parcel is never rewritten (its arc stays an arc)")
check(r.changed == 1 and ga[1].asWkt().startswith("MultiSurface") and abs(ga[1].boundingBox().xMinimum() - 20.0) < 1e-6,
      f"its straight neighbour gives back the 0.3 m it overlapped, and stays in the layer's type: {ga[1].asWkt()[:40]}")
arcs.rollBack()

# ---- degrees ---------------------------------------------------------------------------------------
geo = fabric(4, "EPSG:4326", 46.7, 24.7, 0.0002, 1 / 111320.0)        # about 20 m parcels near Riyadh
check(core.metres_per_unit(geo) == 111320.0, "a layer in degrees: widths given in metres all the same")
rep = core.examine(geo, gap_width=0.5, sliver_width=0.2)
gg = [p for p in rep.problems if p.kind == "gap"]
check(len(gg) == 1 and abs(gg[0].width - 0.2) < 0.02 and rep.count("sliver") == 1,
      f"…its gap found and measured in metres ({gg[0].width if gg else None})")

# ---- speed ----------------------------------------------------------------------------------------
big = fabric(100)
t0 = time.time()
rep = core.examine(big, 0.5, 0.2)
t1 = time.time()
res = core.clean(big, 0.5)
t2 = time.time()
print(f"     {rep.features} parcels: examined in {t1 - t0:.1f} s, cleaned in {t2 - t1:.1f} s")
check(core.examine(big, 0.5, 0.2).count("overlap") == 0, "10,000 parcels cleaned")
big.rollBack()

# ---- a GeoPackage -----------------------------------------------------------------------------------
root = tempfile.mkdtemp()
gp = os.path.join(root, "parcels.gpkg")
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(fabric(), gp, QgsCoordinateTransformContext(), o)
gl = QgsVectorLayer(gp + "|layername=parcels", "gp", "ogr")
res = core.clean(gl, 0.5)
check(gl.commitChanges() and core.examine(gl, 0.5, 0.2).count("gap") == 0, "a GeoPackage cleaned and saved")

# ---- the window --------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class Bar:
    def pushMessage(self, *a, **k):
        pass


class Iface:
    def __init__(self):
        self.w = QWidget()

    def mainWindow(self):
        return self.w

    def activeLayer(self):
        return lyr

    def messageBar(self):
        return Bar()


class Plugin:
    iface = Iface()


from biruni.coverage import ui  # noqa: E402

dlg = ui.open_ui(Plugin())
n0 = len(QgsProject.instance().mapLayers())
r = dlg.examine()
check(r is not None and len(QgsProject.instance().mapLayers()) == n0 + 2 and "1 gaps" in dlg.out.text(),
      "examined from the window: " + dlg.out.text()[:80])
res = dlg.clean()
check(res is not None and res.changed >= 3 and len(QgsProject.instance().mapLayers()) == n0 and "Undo" in dlg.out.text(),
      "mended from the window, the problem layers taken away: " + dlg.out.text()[:70])
lyr.rollBack()

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
