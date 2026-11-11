r"""Spatial fingerprints: what is in an area, proved. Nothing at all, or exactly these features: checked from the
proof alone against the certificate's code, with the values of the neighbours hidden. A feature taken out of the
proof, a value changed, a feature hidden, a square kept closed where it should be open: all refused. A road whose
centre is far away is still found where it crosses.

Run:  engine\bin\python-qgis.bat tests\test_area_proof.py
"""
import copy
import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.fingerprint import core, proof  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=parcel_id:integer&field=owner:string&field=area:double",
                     "Parcels", "memory")
feats = []
n = 0
for i in range(30):          # 30 x 30 parcels of 20 m, a street every 5
    for j in range(30):
        if i % 5 == 4 or j % 5 == 4:
            continue
        n += 1
        x, y = i * 25.0, j * 25.0
        f = QgsFeature(mem.fields())
        f.setAttributes([n, f"owner {n}", 400.0])
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

t = core.build(lyr)
ts = core.build(shl, fields=t.fields)
check(t.reach and t.reach == ts.reach and t.root == ts.root, "the reach is the same in a GPKG and a shapefile of it")
cert = core.certificate([("Parcels", t)])
check(cert["layers"][0]["reach"] == t.reach and cert["code"] == core.code([t.root, t.reach]),
      "the certificate holds the reach, and its code covers it")
check(core.check(shl, cert["layers"][0])["same"], "a copy checks against it as before")
bad = copy.deepcopy(cert["layers"][0])
bad["reach"] = "0" * 64
check(core.check(lyr, bad).get("reach") is True, "a certificate whose reach is not the data's is caught")

street = QgsGeometry.fromWkt("POLYGON((101 2,124 2,124 98,101 98,101 2))")   # inside the street at x=100..125
p = proof.prove(lyr, street, 0.05, cert, 0)
check(p["inside"] == 0, "nothing in the street")
r = proof.verify(json.loads(json.dumps(p)))
check(r.ok and not r.inside and r.code == cert["code"], f"… proved from the proof alone ({r.why}), code {r.code}")
size = len(json.dumps(p))
check(r.opened <= 16 and size < 60000, f"… opening {r.opened} squares of {len(t.leaf)}, {size // 1024} KB")
hidden = json.dumps(p)
check("owner 1" not in hidden and "owner " not in hidden, "… and no neighbour's value is in it")

wide = QgsGeometry.fromWkt("POLYGON((95 2,124 2,124 98,95 98,95 2))")      # into the parcels at x=75..95 by 0..5 cm? no: 95 is their edge
p2 = proof.prove(lyr, wide, 0.05, cert, 0)
check(p2["inside"] == 0 and proof.verify(p2).ok, "touching the parcels' edge is not being in the area")
wider = QgsGeometry.fromWkt("POLYGON((90 2,124 2,124 98,90 98,90 2))")
p3 = proof.prove(lyr, wider, 0.05, cert, 0)
r3 = proof.verify(p3)
owners = sorted(dict(v)["owner"] for _g, v in r3.inside)
check(r3.ok and len(r3.inside) == 4 == p3["inside"], f"5 m into a row of parcels: exactly those 4, proved ({owners})")


def tamper(pr, fn):
    q = copy.deepcopy(pr)
    fn(q)
    return proof.verify(q)


def leaves(n, out):
    if "items" in n:
        out.append(n)
    for c in n.get("kids", []):
        leaves(c, out)
    return out


def first(pr, cond):
    for top in pr["tree"]:
        for lf in leaves(top, []):
            for it in lf["items"]:
                if cond(it):
                    return lf, it
    return None, None


def drop_inside(q):
    lf, it = first(q, lambda it: "vals" in it)
    lf["items"].remove(it)


r = tamper(p3, drop_inside)
check(not r.ok and r.why == "reach", f"a feature in the area taken out of the proof: refused ({r.why})")


def change_value(q):
    _lf, it = first(q, lambda it: "vals" in it)
    for kv in it["vals"]:
        if kv[0] == "owner":
            kv[1] = "someone else"


r = tamper(p3, change_value)
check(not r.ok and r.why == "reach", f"a value changed: refused ({r.why})")


def hide(q):
    _lf, it = first(q, lambda it: "vals" in it)
    it["v"] = core.values_hash(it.pop("vals"))


r = tamper(p3, hide)
check(not r.ok and r.why == "hidden", f"a feature in the area with its values hidden: refused ({r.why})")


def close_near(q):
    _lf, it = first(q, lambda it: "g" in it)
    g = QgsGeometry()
    g.fromWkb(bytes.fromhex(it.pop("g")))
    it["s"] = core.shape_hash(core.shape_key(g, q["settings"]["step"]))
    it.setdefault("v", core.values_hash(it.pop("vals", [])))


r = tamper(p3, close_near)
check(not r.ok and r.why == "open", f"a feature near the area kept closed: refused ({r.why})")


def move_area(q):
    q["area"]["wkt"] = "POLYGON((0 0,60 0,60 60,0 60,0 0))"


r = tamper(p, move_area)
check(not r.ok and r.why in ("open", "hidden"), f"the same proof for another area: refused ({r.why})")


def other_code(q):
    q["certificate"]["layers"][0]["reach"] = "f" * 64
    q["certificate"]["code"] = core.code([q["certificate"]["layers"][0]["root"], "f" * 64])


r = tamper(p, other_code)
check(not r.ok and r.why == "reach", f"a proof passed off under another certificate: refused ({r.why})")
r = tamper(p, lambda q: q["certificate"].__setitem__("code", "AAAA-AAAA-AAAA-AAAA"))
check(not r.ok and r.why == "code", "a code that is not the certificate's: refused")

# a long road: its centre far from the area, its reach through it
lyr.startEditing()
road = QgsFeature(lyr.fields())
road["parcel_id"] = 9999
road["owner"] = "road"
road.setGeometry(QgsGeometry.fromWkt("POLYGON((-400 40,1200 40,1200 44,-400 44,-400 40))"))
lyr.addFeature(road)
lyr.commitChanges()
t2 = core.build(lyr)
cert2 = core.certificate([("Parcels", t2)])
check(t2.cell_of(road.geometry()) != t2.cell_of(street), "the road's square is not the street's")
p4 = proof.prove(lyr, street, 0.05, cert2, 0)
r4 = proof.verify(p4)
check(r4.ok and [dict(v)["owner"] for _g, v in r4.inside] == ["road"], "the road crossing the street is found and proved")
try:
    proof.prove(lyr, street, 0.05, cert, 0)
    check(False, "a proof against a certificate the layer no longer matches")
except core.FingerprintError as e:
    check(str(e) == "changed", "the layer changed since the certificate: no proof made against it")
old = copy.deepcopy(cert2)
del old["layers"][0]["reach"]
try:
    proof.prove(lyr, street, 0.05, old, 0)
    check(False, "a proof against an old certificate")
except core.FingerprintError as e:
    check(str(e) == "old certificate", "an old certificate without the reach: said so")
p5 = proof.prove(lyr, street, 0.05)
check(proof.verify(p5).ok and p5["certificate"]["code"] == core.certificate([("Parcels", core.build(lyr, commit=True, sums=True))])["code"],
      "without a certificate: a code of its own, from the layer as it is")
try:
    proof.prove(lyr, QgsGeometry.fromWkt("LINESTRING(0 0,10 10)"), 0.05)
    check(False, "a line as an area")
except core.FingerprintError as e:
    check(str(e) == "area", "the area must be a polygon")

# ---- the buttons ------------------------------------------------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.fingerprint import proof_ui  # noqa: E402


class Bar:
    said = []

    def pushMessage(self, title, text, level=None, duration=0):
        Bar.said.append(text)


canvas = QgsMapCanvas()


class Iface:
    def __init__(self):
        self.active = None

    def mainWindow(self):
        return None

    def activeLayer(self):
        return self.active

    def messageBar(self):
        return Bar()

    def mapCanvas(self):
        return canvas


class Plugin:
    iface = Iface()


areas = QgsVectorLayer("Polygon?crs=EPSG:4326", "Areas", "memory")    # drawn in degrees: taken to the layer's
f = QgsFeature()
g = QgsGeometry(wider)
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform  # noqa: E402

g.transform(QgsCoordinateTransform(lyr.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()))
f.setGeometry(g)
areas.dataProvider().addFeatures([f])
QgsProject.instance().addMapLayer(areas)
areas.selectAll()
Plugin.iface.active = areas
certp = os.path.join(root, "delivery.biruni-proof.json")
core.write_json(cert2, certp)
dlg = proof_ui.prove_ui(Plugin)
check(dlg.area.currentData() == areas.id(), "the area is the active layer with its selection")
dlg.data.setCurrentIndex(dlg.data.findData(lyr.id()))
dlg.pick_cert(certp)
out = os.path.join(root, "row")
pr = dlg.make(out)
check(pr is not None and pr["inside"] == 5 and os.path.exists(out + proof_ui.AREA_PROOF) and cert2["code"] in dlg.out.text(),
      "made from the dialog: the 4 parcels and the road, with the certificate's code: " + dlg.out.text()[:90])
n0 = len(QgsProject.instance().mapLayers())
r = proof_ui.verify_ui(Plugin, out + proof_ui.AREA_PROOF, quiet=True)
check(r.ok and len(r.inside) == 5 and len(QgsProject.instance().mapLayers()) == n0 + 2 and cert2["code"] in r.text,
      "checked from the file: the area and the 5 features on the map")
bad = core.read_json(out + proof_ui.AREA_PROOF)
bad["inside"] = 0
lf, it = first(bad, lambda it: "vals" in it)
lf["items"].remove(it)
core.write_json(bad, out + "-bad.json")
check(not proof_ui.verify_ui(Plugin, out + "-bad.json", quiet=True).ok, "a proof with a parcel taken out: not proved")
dlg2 = proof_ui.prove_ui(Plugin)
dlg2.data.setCurrentIndex(dlg2.data.findData(lyr.id()))
dlg2.pick_cert(os.path.join(root, "delivery.biruni-proof.json"))
lyr.startEditing()
lyr.changeAttributeValue(1, lyr.fields().indexOf("owner"), "changed")
lyr.commitChanges()
check(dlg2.make(out + "2") is None and "no longer" in dlg2.out.text(), "the layer changed since: " + dlg2.out.text()[:60])

QgsProject.instance().removeAllMapLayers()
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
