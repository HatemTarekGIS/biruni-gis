"""The same parcels in two sources that share no id, and their fields told apart by the values.

B is A drawn by another agency: in degrees instead of metres, offset 0.5 m as a whole, every corner a few
centimetres off, other numbers, Arabic field names, the area in hectares, the names written another way
(«عبدالله» for «عبد الله», «احمد» for «أحمد»); one parcel drawn as two, three of A not in B, two of B not in A.
The parcels are found, the split too, and the fields: the area with its factor 10000, the owner, the use; not the
numbers, which are not the same. Copying B's values into A is one step to undo.

Run:  engine\\bin\\python-qgis.bat tests\\test_conflate.py
"""
import os
import random
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,  # noqa: E402
                       QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.conflate import fields, match  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


FIRST = ["أحمد", "محمد", "عبد الله", "عبد الرحمن", "خالد", "فاطمة", "عائشة", "إبراهيم", "يوسف", "مريم"]
LAST = ["الأحمدي", "القحطاني", "العتيبي", "الشمري", "الدوسري", "الحربي", "الزهراني", "الغامدي"]


def variant(s):
    return s.replace("عبد ال", "عبدال").replace("أ", "ا").replace("إ", "ا").replace("ة", "ه")


def ring(x, y, w, h):
    return [QgsPointXY(x, y), QgsPointXY(x + w, y), QgsPointXY(x + w, y + h), QgsPointXY(x, y + h), QgsPointXY(x, y)]


def layers(n, drop=(5, 77, 300), split=150):
    rnd = random.Random(7)
    a = QgsVectorLayer("Polygon?crs=EPSG:32638&field=parcel_id:integer&field=area:double&field=owner:string"
                       "&field=use:string", "A", "memory")
    b = QgsVectorLayer("Polygon?crs=EPSG:4326&field=رقم_القطعة:integer&field=المساحة_هكتار:double"
                       "&field=اسم_المالك:string&field=الاستعمال:string", "B", "memory")
    xf = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:32638"), QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsProject.instance())
    fa, fb = [], []
    ids = list(range(n * n))
    rnd.shuffle(ids)
    x0, y0 = 500000.0, 2700000.0
    truth = {}
    for i in range(n):
        for j in range(n):
            k = i * n + j
            w, h = 15 + (k * 7) % 10, 16 + (k * 3) % 8
            x, y = x0 + i * 30.0, y0 + j * 30.0
            owner = f"{FIRST[k % len(FIRST)]} {LAST[(k // 3) % len(LAST)]}"
            use = "سكني" if k % 3 else "تجاري"
            f = QgsFeature(a.fields())
            f.setAttributes([k + 1, float(w * h), owner, use])
            f.setGeometry(QgsGeometry.fromPolygonXY([ring(x, y, w, h)]))
            fa.append(f)
            if k in drop:
                continue
            pieces = [(x, y, w / 2, h), (x + w / 2, y, w / 2, h)] if k == split else [(x, y, w, h)]
            for px, py, pw, ph in pieces:
                pts = [QgsPointXY(p.x() + 0.5 + rnd.uniform(-0.05, 0.05), p.y() + 0.2 + rnd.uniform(-0.05, 0.05))
                       for p in ring(px, py, pw, ph)[:-1]]
                g = QgsGeometry.fromPolygonXY([pts + [pts[0]]])
                g.transform(xf)
                f = QgsFeature(b.fields())
                f.setAttributes([7000 + ids[k], w * h / 10000.0, variant(owner), use.replace("ي", "ى")])
                f.setGeometry(g)
                fb.append(f)
            truth[len(fa)] = len(fb)
    for e in range(2):                       # two of B not in A, far away
        g = QgsGeometry.fromPolygonXY([ring(x0 - 200 - e * 40, y0, 20, 20)])
        g.transform(xf)
        f = QgsFeature(b.fields())
        f.setAttributes([1, 0.04, "سالم", "سكنى"])
        f.setGeometry(g)
        fb.append(f)
    a.dataProvider().addFeatures(fa)
    b.dataProvider().addFeatures(fb)
    return a, b


a, b = layers(20)
QgsProject.instance().addMapLayers([a, b])
t0 = time.time()
r = match.match(a, b)
took = time.time() - t0
c = r.counts()
print("     ", c, "shift", tuple(round(v, 3) for v in r.shift), f"{took:.2f} s")
check(abs(r.shift[0] - 0.5) < 0.05 and abs(r.shift[1] - 0.2) < 0.05, "the offset of B as a whole found: 0.5 m, 0.2 m")
check(c["pairs"] == 396, f"396 parcels found one to one ({c['pairs']})")
ida = {f.id(): f["parcel_id"] for f in a.getFeatures()}
names_b = {f.id(): f["اسم_المالك"] for f in b.getFeatures()}
owners_a = {f.id(): f["owner"] for f in a.getFeatures()}
check(all(variant(owners_a[x]) == names_b[y] for x, y, _s in r.pairs), "every pair is the same parcel")
check(len(r.splits) == 1 and ida[r.splits[0][0]] == 151 and len(r.splits[0][1]) == 2,
      f"the parcel drawn as two in B: found as a split ({r.splits})")
check(sorted(ida[f] for f in r.only_a) == [6, 78, 301], f"the 3 parcels only in A ({[ida[f] for f in r.only_a]})")
check(len(r.only_b) == 2, f"the 2 parcels only in B ({len(r.only_b)})")
check(min(s for _a, _b, s in r.pairs) > 0.8, "the pairs score high despite the centimetres")

# a datum shift of 4 m: still found once the shift is taken out
a4, b4 = layers(10, drop=(), split=-1)
for f in b4.getFeatures():
    g = f.geometry()
    g.transform(QgsCoordinateTransform(b4.crs(), a4.crs(), QgsProject.instance()))
    g.translate(3.5, 0)
    g.transform(QgsCoordinateTransform(a4.crs(), b4.crs(), QgsProject.instance()))
    b4.dataProvider().changeGeometryValues({f.id(): g})
r4 = match.match(a4, b4, max_shift=10)
check(len(r4.pairs) == 100 and abs(r4.shift[0] - 4.0) < 0.1, f"4 m apart as a whole: all 100 found, shift {r4.shift}")

# lines and points
la = QgsVectorLayer("LineString?crs=EPSG:32638&field=name:string", "roads A", "memory")
lb = QgsVectorLayer("LineString?crs=EPSG:32638&field=اسم:string", "roads B", "memory")
pa = QgsVectorLayer("Point?crs=EPSG:32638", "poles A", "memory")
pb = QgsVectorLayer("Point?crs=EPSG:4326", "poles B", "memory")
toll = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:32638"), QgsCoordinateReferenceSystem("EPSG:4326"),
                              QgsProject.instance())
for k in range(20):
    x, y = 500000.0 + k * 50, 2700000.0
    for lyr, d in ((la, 0.0), (lb, 0.3)):
        f = QgsFeature(lyr.fields())
        f.setAttributes([f"road {k}"])
        f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x + d, y), QgsPointXY(x + d + 10, y + 300)]))
        lyr.dataProvider().addFeatures([f])
    for lyr, d in ((pa, 0.0), (pb, 0.4)):
        g = QgsGeometry.fromPointXY(QgsPointXY(x + d, y + 7))
        if lyr is pb:
            g.transform(toll)
        f = QgsFeature(lyr.fields())
        f.setGeometry(g)
        lyr.dataProvider().addFeatures([f])
rl, rp = match.match(la, lb), match.match(pa, pb)
check(len(rl.pairs) == 20 and len(rp.pairs) == 20 and all(x == y for x, y, _s in rp.pairs),
      f"lines ({len(rl.pairs)}) and points ({len(rp.pairs)}) found too")

# ---- the fields -------------------------------------------------------------------------------------
cw = fields.crosswalk(a, b, r.pairs)
by = {p.a: p for p in cw}
print("     ", cw)
check("area" in by and by["area"].b == "المساحة_هكتار" and by["area"].factor == 10000.0,
      "the area in m² is the one in hectares, times 10000")
check("owner" in by and by["owner"].b == "اسم_المالك" and by["owner"].confidence > 0.9,
      "the owner is اسم_المالك, however the names are written")
check("use" in by and by["use"].b == "الاستعمال", "the use is الاستعمال")
check("parcel_id" not in by, "the parcel numbers are not the same numbers: not matched")
cw0 = fields.crosswalk(a, b)
by0 = {p.a: p for p in cw0}
check(by0.get("owner") and by0["owner"].b == "اسم_المالك" and by0.get("area") and by0["area"].factor == 10000.0,
      f"without the pairs, from the values alone: owner and area still found ({cw0})")

# ---- copying ----------------------------------------------------------------------------------------
a.startEditing()
n = fields.copy_values(a, b, r.pairs, [by["area"]], new=["رقم_القطعة"])
fa = {f.id(): f for f in a.getFeatures()}
fbv = {f.id(): f for f in b.getFeatures()}
x, y, _s = r.pairs[10]
check(n == 2 * len(r.pairs) and abs(fa[x]["area"] - fbv[y]["المساحة_هكتار"] * 10000) < 1e-6 and
      fa[x]["رقم_القطعة"] == fbv[y]["رقم_القطعة"], "B's area (in m²) and its number copied into A by the pairs")
a.undoStack().undo()
check(a.fields().indexOf("رقم_القطعة") < 0 and not a.editBuffer().isModified(), "one step to undo")
a.rollBack()

# ---- the window ---------------------------------------------------------------------------------------
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
        return a

    def messageBar(self):
        return Bar()


class Plugin:
    iface = Iface()


from biruni.conflate import ui  # noqa: E402

n0 = len(QgsProject.instance().mapLayers())
dlg = ui.open_ui(Plugin())
check(dlg is not None and dlg.layers() == (a, b), "the window opens on A and another layer")
res = dlg.run()
check(res is not None and "396" in dlg.summary.text() and len(QgsProject.instance().mapLayers()) == n0 + 1 and
      dlg.links.featureCount() == 396 + 2, "found from the window, the links on the map: " + dlg.summary.text()[:70])
check(sorted(a.selectedFeatureIds()) == sorted(res.only_a), "what is only in A is selected")
maps, new = dlg.ticked()
check(sorted(p.a for p in maps) == ["area", "owner", "use"] and not new, "the sure fields ticked, no new field")
for i in range(dlg.tree.topLevelItemCount()):
    it = dlg.tree.topLevelItem(i)
    if it.text(1) == "رقم_القطعة":
        it.setCheckState(0, ui.Qt.CheckState.Checked)
dlg.new.setChecked(True)
k = dlg.copy()
check(k == 4 * 396 and a.fields().indexOf("رقم_القطعة") >= 0 and a.isEditable(),
      "copied from the window into A's edits, with a new field: " + dlg.summary.text()[:60])
a.rollBack()

# ---- 10 000 parcels ----------------------------------------------------------------------------------
ab, bb = layers(100, drop=(), split=-1)
t0 = time.time()
rb = match.match(ab, bb)
t1 = time.time()
cwb = fields.crosswalk(ab, bb, rb.pairs)
t2 = time.time()
check(len(rb.pairs) == 10000, f"10 000 parcels: all found in {t1 - t0:.1f} s, fields in {t2 - t1:.1f} s")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
