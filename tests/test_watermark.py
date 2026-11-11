"""Marked copies: whose copy a file is, from the file alone; the marked copy is still the certified data.

Run:  engine\\bin\\python-qgis.bat tests\\test_watermark.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.fingerprint import core as fp  # noqa: E402
from biruni.watermark import core  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
reg = os.path.join(root, "register.json")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=parcel_id:integer&field=owner:string", "Parcels", "memory")
feats = []
for i in range(30):
    for j in range(30):
        n = i * 30 + j + 1
        x, y = 500000.123456 + i * 20.0, 2700000.654321 + j * 20.0     # neighbours share their sides
        f = QgsFeature(mem.fields())
        f.setAttributes([n, f"owner {n}"])
        f.setGeometry(QgsGeometry.fromWkt(
            f"POLYGON(({x} {y},{x + 20} {y},{x + 20} {y + 20},{x + 10.000037} {y + 20},{x} {y + 20},{x} {y}))"))
        feats.append(f)
mem.dataProvider().addFeatures(feats)
gpkg = os.path.join(root, "parcels.gpkg")
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
lyr = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(lyr)
t = fp.build(lyr, commit=True)
cert = fp.certificate([("Parcels", t)])

t0 = time.time()
ea = core.mark(lyr, "Office A", os.path.join(root, "a.gpkg"), "contract 12", reg=reg)
took = time.time() - t0
eb = core.mark(lyr, "Company B", os.path.join(root, "b.gpkg"), reg=reg)
print(f"     900 parcels marked in {took:.2f} s")
ca = QgsVectorLayer(os.path.join(root, "a.gpkg") + "|layername=Parcels", "a", "ogr")
cb = QgsVectorLayer(os.path.join(root, "b.gpkg") + "|layername=Parcels", "b", "ogr")
check(ca.featureCount() == 900 and len(core.register(reg)) == 2, "two copies made, both in the register")

f1 = next(lyr.getFeatures())
g1 = next(ca.getFeatures()).geometry()
moved = max(abs(p.x() - q.x()) + abs(p.y() - q.y()) for p, q in zip(f1.geometry().vertices(), g1.vertices()))
check(0 < moved < 2e-4, f"the coordinates moved less than a fifth of a millimetre ({moved * 1000:.3f} mm)")

res = fp.check(ca, cert["layers"][0])
check(res["same"], "the marked copy is still the certified data: the same certificate checks it")

w = core.who(ca, reg)
check(w is not None and w.entry["recipient"] == "Office A" and w.rate == 1.0,
      f"copy A is found to be Office A's ({w and w.total} marks)")
w = core.who(cb, reg)
check(w is not None and w.entry["recipient"] == "Company B", "copy B is found to be Company B's")
found, _n = core.find(ca, reg)
check(abs(found[1].rate - 0.5) < 0.05, f"the other copy's secret agrees only by chance ({found[1].rate:.3f})")
check(core.who(lyr, reg) is None, "the original carries no one's mark")

a_ids = [f.id() for f in ca.getFeatures()][:5]
part = QgsVectorLayer("Polygon?crs=EPSG:32638", "part", "memory")
part.dataProvider().addFeatures([QgsFeature(f) for f in ca.getFeatures() if f.id() in a_ids])
w = core.who(part, reg)
check(w is not None and w.entry["recipient"] == "Office A", f"five parcels cut out of copy A still tell ({w and w.total} marks)")

shp = os.path.join(root, "a.shp")
o2 = QgsVectorFileWriter.SaveVectorOptions()
o2.driverName = "ESRI Shapefile"
QgsVectorFileWriter.writeAsVectorFormatV3(ca, shp, QgsCoordinateTransformContext(), o2)
w = core.who(QgsVectorLayer(shp, "s", "ogr"), reg)
check(w is not None and w.entry["recipient"] == "Office A", "copy A saved again as a shapefile still tells")

rounded = QgsVectorLayer("Polygon?crs=EPSG:32638", "r", "memory")
rf = []
for f in ca.getFeatures():
    g = QgsGeometry(f.geometry())
    rf.append(QgsFeature())
    rf[-1].setGeometry(g.snappedToGrid(0.001, 0.001))
rounded.dataProvider().addFeatures(rf)
check(core.who(rounded, reg) is None, "rounded to the millimetre: the mark is gone (said so, not a wrong name)")

a_feats = {f["parcel_id"]: f.geometry() for f in ca.getFeatures()}
g_a, g_b = a_feats[1], a_feats[31]       # side by side
inter = g_a.intersection(g_b)
check(g_a.touches(g_b) and inter.area() == 0 and inter.length() > 19.99,
      "neighbours still share their side exactly: the same point is moved the same way in both")

raw = open(os.path.join(root, "a.gpkg"), "rb").read()
check(ea["key"].encode() not in raw and bytes.fromhex(ea["key"]) not in raw, "the secret is not in the copy")

# ---- the dialogs ----------------------------------------------------------------------------------
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


from biruni.watermark import ui  # noqa: E402

plug = Plugin()
dlg = ui.mark_ui(plug)
check(dlg.make(os.path.join(root, "x.gpkg"), reg) is None and "Write" in dlg.out.text(), "no recipient: said so")
dlg.who.setText("Survey Office C")
e = dlg.make(os.path.join(root, "c"), reg)
check(e is not None and os.path.exists(os.path.join(root, "c.gpkg")) and "Survey Office C" in dlg.out.text(),
      "made from the dialog: " + dlg.out.text()[:70])
wd = ui.who_ui(plug)
wd.open_file(os.path.join(root, "c.gpkg"))
b = wd.find(reg)
check(b is not None and b.entry["recipient"] == "Survey Office C" and "Survey Office C" in wd.out.text(),
      "found from the dialog: " + wd.out.text()[:80])
text, _b = ui.describe(lyr, reg)
check("No mark" in text or "None of" in text, "the original from the dialog: " + text[:60])

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
