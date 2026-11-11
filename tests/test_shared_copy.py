"""A shared copy with fields hidden: it proves itself against the certificate's code, from the copy alone.

Every shape and value it shows is the certified one; a value changed, a feature taken out or added, a seal
altered, a code passed off: refused, and the features not as certified are pointed at. The values left out are not
in the copy, and their seals are salted: two parcels with the same hidden "yes" do not have the same seal.

Run:  engine\\bin\\python-qgis.bat tests\\test_shared_copy.py
"""
import json
import os
import shutil
import sqlite3
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

from biruni.fingerprint import core, disclose, proof  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=parcel_id:integer&field=owner:string&field=sold:string"
                     "&field=price:double&field=use:string", "Parcels", "memory")
feats = []
for i in range(30):
    for j in range(30):
        n = i * 30 + j + 1
        x, y = i * 25.0, j * 25.0
        f = QgsFeature(mem.fields())
        f.setAttributes([n, f"owner {n}", "yes" if n % 3 else "no", 1000.0 + n, "home" if n % 2 else "shop"])
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 20} {y},{x + 20} {y + 20},{x} {y + 20},{x} {y}))"))
        feats.append(f)
mem.dataProvider().addFeatures(feats)
gpkg = os.path.join(root, "parcels.gpkg")
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
lyr = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(lyr)
shp = os.path.join(root, "parcels.shp")
o2 = QgsVectorFileWriter.SaveVectorOptions()
o2.driverName = "ESRI Shapefile"
QgsVectorFileWriter.writeAsVectorFormatV3(lyr, shp, QgsCoordinateTransformContext(), o2)
shl = QgsVectorLayer(shp, "Parcels shp", "ogr")

t0 = time.time()
t = core.build(lyr, commit=True, sums=True)
took = time.time() - t0
ts = core.build(shl, fields=t.fields, commit=True)
check(t.commit and t.commit == ts.commit, "the seals are the same in a GPKG and a shapefile of it")
check(core.build(lyr).commit == "", "no seals unless asked for (the comparisons stay as fast as they were)")
print(f"     {t.count} features sealed in {took:.2f} s")

cert = core.certificate([("Parcels", t)])
e = cert["layers"][0]
check(e["commit"] == t.commit and cert["code"] == core.code([t.root, t.reach, t.commit, t.sums]),
      "the certificate holds the seals, and its code covers them")
check(core.check(lyr, cert["layers"][0])["same"], "the layer checks against its certificate")
bad = json.loads(json.dumps(e))
bad["commit"] = "0" * 64
check(core.check(lyr, bad).get("reach") is True, "a certificate whose seals are not the data's is caught")

# ---- the copy ---------------------------------------------------------------------------------------
doc, keep, shown = disclose.share(lyr, ["Owner", "sold"], cert, 0)
out = disclose.write(lyr, doc, keep, shown, os.path.join(root, "shared.gpkg"))
r = disclose.verify(out)
check(r.ok and r.shown == 900 and r.withheld == 0 and r.code == cert["code"],
      f"the copy proves itself ({r.why}): {r.shown} features, code {r.code}")
names = [f.name().lower() for f in r.layer.fields()]
check("owner" not in names and "sold" not in names and "price" in names and "use" in names,
      "the hidden fields are not in it: " + ", ".join(names))
raw = json.dumps(doc, ensure_ascii=False)
check("owner 5" not in raw and '"yes"' not in raw and '"no"' not in raw, "no hidden value anywhere in the file")
seals = [it["c"]["sold"] for it in doc["items"]]
check(len(set(seals)) == 900, "two parcels both sold do not have the same seal: the salt hides a yes or a no")


def copy_of(tag):
    p = os.path.join(root, f"shared-{tag}.gpkg")
    shutil.copy(out, p)
    return p


def edit(path, fn):
    lr = QgsVectorLayer(path + "|layername=Parcels", "x", "ogr")
    lr.startEditing()
    fn(lr)
    lr.commitChanges()
    del lr


def first_id(path):
    lr = QgsVectorLayer(path + "|layername=Parcels", "x", "ogr")
    return next(lr.getFeatures()).id(), lr


p1 = copy_of("price")
fid, lr = first_id(p1)
pi = lr.fields().indexOf("price")
del lr
edit(p1, lambda l: l.changeAttributeValue(fid, pi, 1.0))
r = disclose.verify(p1)
check(not r.ok and r.why == "changed" and r.bad == [fid], f"a price changed in the copy: refused, that parcel pointed at ({r.bad})")

p2 = copy_of("moved")
fid, _l = first_id(p2)
del _l
edit(p2, lambda l: l.changeGeometry(fid, QgsGeometry.fromWkt("POLYGON((0 0,21 0,21 20,0 20,0 0))")))
r = disclose.verify(p2)
check(not r.ok and r.bad == [fid], "a parcel moved in the copy: refused")

p3 = copy_of("gone")
fid, _l = first_id(p3)
del _l
edit(p3, lambda l: l.deleteFeature(fid))
r = disclose.verify(p3)
check(not r.ok and r.missing == 1 and not r.bad, "a parcel taken out of the copy: refused, one missing")

p4 = copy_of("added")
def add(l):
    f = QgsFeature(l.fields())
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((900 900,910 900,910 910,900 900))"))
    l.addFeature(f)
edit(p4, add)
r = disclose.verify(p4)
check(not r.ok and len(r.bad) == 1, "a parcel added to the copy: refused")


def doc_edit(path, fn):
    con = sqlite3.connect(path)
    d = json.loads(con.execute("SELECT doc FROM biruni_shared").fetchone()[0])
    fn(d)
    con.execute("UPDATE biruni_shared SET doc=?", (json.dumps(d),))
    con.commit()
    con.close()


p5 = copy_of("seal")
doc_edit(p5, lambda d: d["items"][0]["c"].__setitem__("sold", "f" * 32))
r = disclose.verify(p5)
check(not r.ok and r.why == "seals", f"a hidden value's seal altered: refused ({r.why})")
p6 = copy_of("code")
doc_edit(p6, lambda d: d["certificate"].__setitem__("code", "AAAA-AAAA-AAAA-AAAA"))
check(disclose.verify(p6).why == "code", "a code that is not the certificate's: refused")


def other(d):
    d["certificate"]["layers"][0]["commit"] = "e" * 64
    d["certificate"]["code"] = core.code(core.layer_codes(d["certificate"]["layers"]))
p7 = copy_of("other")
doc_edit(p7, other)
r = disclose.verify(p7)
check(not r.ok and r.code != cert["code"], f"passed off under another certificate: refused ({r.why}), and its code is not the delivery's")
p8 = copy_of("unhide")
doc_edit(p8, lambda d: (d["hidden"].remove("sold"), d["shown"].append("sold")))
check(disclose.verify(p8).why == "fields", "a copy that says it shows a field it does not: refused")

# ---- only some features ---------------------------------------------------------------------------
doc2, keep2, shown2 = disclose.share(lyr, ["owner"], cert, 0, only=list(range(1, 101)))
out2 = disclose.write(lyr, doc2, keep2, shown2, os.path.join(root, "some.gpkg"))
r = disclose.verify(out2)
check(r.ok and r.shown == 100 and r.withheld == 800, f"100 parcels shown, 800 held back and counted ({r.why})")

# ---- against the certificate ------------------------------------------------------------------------
old = json.loads(json.dumps(cert))
del old["layers"][0]["commit"]
try:
    disclose.share(lyr, ["owner"], old, 0)
    check(False, "a copy against an old certificate")
except core.FingerprintError as ex:
    check(str(ex) == "old certificate", "an old certificate without seals: said so")
lyr.startEditing()
lyr.changeAttributeValue(next(lyr.getFeatures()).id(), lyr.fields().indexOf("use"), "farm")
try:
    disclose.share(lyr, ["owner"], cert, 0)
    check(False, "a copy of a layer changed since its certificate")
except core.FingerprintError as ex:
    check(str(ex) == "changed", "the layer changed since the certificate: no copy made against it")
lyr.rollBack()
d3, k3, s3 = disclose.share(lyr, ["owner"])
check(d3["certificate"]["code"] == cert["code"], "without a certificate: the same code as the layer's certificate")
pr = proof.prove(lyr, QgsGeometry.fromWkt("POLYGON((1 1,19 1,19 19,1 19,1 1))"), 0.05, cert, 0)
check(proof.verify(pr).ok and pr["certificate"]["code"] == cert["code"], "an area proof against a sealed certificate still holds")

# ---- the dialog ---------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class Bar:
    def pushMessage(self, *a, **k):
        pass

    def clearWidgets(self):
        pass


class Canvas:
    def setExtent(self, *a):
        pass

    def refresh(self):
        pass

    def mapSettings(self):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()


class Iface:
    def __init__(self):
        self.w = QWidget()

    def mainWindow(self):
        return self.w

    def activeLayer(self):
        return lyr

    def messageBar(self):
        return Bar()

    def mapCanvas(self):
        return Canvas()


class Plugin:
    iface = Iface()


from biruni.fingerprint import disclose_ui  # noqa: E402

plug = Plugin()
dlg = disclose_ui.share_ui(plug)
check(dlg is not None and dlg.fields.count() == 5, "the dialog lists the fields")
check(dlg.make(os.path.join(root, "x")) is None and "Tick" in dlg.out.text(), "nothing to hide: said so")
dlg.set_hidden(["owner", "price"])
proof_path = os.path.join(root, "parcels" + ".biruni-proof.json")
core.write_json(cert, proof_path)
dlg.pick_cert(proof_path)
d = dlg.make(os.path.join(root, "from-dialog"))
check(d is not None and os.path.exists(os.path.join(root, "from-dialog.gpkg")) and cert["code"] in dlg.out.text(),
      "made from the dialog, with the certificate's code: " + dlg.out.text()[:80])
n0 = len(QgsProject.instance().mapLayers())
r = disclose_ui.verify_ui(plug, os.path.join(root, "from-dialog.gpkg"), quiet=True)
check(r.ok and cert["code"] in r.text and len(QgsProject.instance().mapLayers()) == n0 + 1,
      "checked from the button, the copy on the map")
r = disclose_ui.verify_ui(plug, p1, quiet=True)
check(not r.ok and list(r.layer.selectedFeatureIds()) == r.bad, "a copy not as certified: its wrong parcel selected")
r = disclose_ui.verify_ui(plug, gpkg, quiet=True)
check(not r.ok and r.why == "format", "a GeoPackage that is not a shared copy: said so")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
