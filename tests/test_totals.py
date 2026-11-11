"""The totals of an area proved without the data, against the certificate's code.

The count, the area (or length) and the field totals of the features all inside an area are the true ones; the
squares all inside it are kept closed (no shape shown); a sum changed, a square dropped, a feature's shape kept back
on the edge, an area moved, a code passed off: refused. Coordinates moved inside their own step (a marked copy) give
the same sums; a GPKG and a shapefile of it too.

Run:  engine\\bin\\python-qgis.bat tests\\test_totals.py
"""
import copy
import json
import math
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProject, QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.fingerprint import core, totals  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=parcel_id:integer&field=owner:string&field=price:double",
                     "Parcels", "memory")
feats = []
n = 0
for i in range(30):
    for j in range(30):
        if i % 5 == 4 or j % 5 == 4:
            continue
        n += 1
        x, y = i * 25.0 + 0.123456, j * 25.0 + 0.654321
        f = QgsFeature(mem.fields())
        f.setAttributes([n, f"owner {n}", 1000.5 + n])
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
print(f"     {t.count} features with sums in {time.time() - t0:.2f} s")
ts = core.build(shl, fields=t.fields, commit=True, sums=True)
check(t.sums and t.sums == ts.sums, "the sums are the same in a GPKG and a shapefile of it")
check(t.sum_fields == ["parcel_id", "price"], "the numeric fields are totalled: " + ", ".join(t.sum_fields))
check(t.totals[0] == 576 and t.totals[1] == 576 * 400 * 1000, f"the whole layer: {t.totals[:2]}")
check(core.build(lyr).sums == "", "no sums unless asked for")

# coordinates moved inside their own step (as a marked copy has them): the same sums
moved = QgsVectorLayer("Polygon?crs=EPSG:32638&field=parcel_id:integer&field=owner:string&field=price:double",
                       "Moved", "memory")
mf = []
for k, f in enumerate(lyr.getFeatures()):
    g = QgsGeometry(f.geometry())
    for vi in range(g.constGet().nCoordinates()):
        p = g.vertexAt(vi)
        s = 1e-4
        d = 0.25 * s if (vi + k) % 2 else -0.25 * s
        g.moveVertex(math.floor(p.x() / s + 0.5) * s + d, math.floor(p.y() / s + 0.5) * s - d, vi)
    nf = QgsFeature(moved.fields())
    nf.setAttributes([f["parcel_id"], f["owner"], f["price"]])
    nf.setGeometry(g)
    mf.append(nf)
moved.dataProvider().addFeatures(mf)
tm = core.build(moved, fields=t.fields, commit=True, sums=True)
check(tm.root == t.root and tm.sums == t.sums, "vertices moved inside their step: the same fingerprint and sums")

cert = core.certificate([("Parcels", t)])
e = cert["layers"][0]
check(e["sums"] == t.sums and cert["code"] == core.code([t.root, t.reach, t.commit, t.sums]),
      "the certificate holds the sums, and its code covers them")
check(core.check(lyr, e)["same"], "the layer checks against its certificate")
bad = copy.deepcopy(e)
bad["sums"] = "0" * 64
check(core.check(lyr, bad).get("reach") is True, "a certificate whose sums are not the data's is caught")


def truth(layer, area, tol):
    inner, outer = area.buffer(-tol, 8), area.buffer(tol, 8)
    cnt, amt, fs, cross = 0, 0.0, {}, 0
    for f in layer.getFeatures():
        g = f.geometry()
        if outer.contains(g):
            cnt += 1
            amt += g.area() if g.type() == 2 else g.length()
            for nm in ("parcel_id", "price"):
                if layer.fields().indexOf(nm) >= 0:
                    fs[nm] = fs.get(nm, 0) + (f[nm] or 0)
        elif inner.intersects(g):
            cross += 1
    return cnt, amt, fs, cross


# ---- a square area: everything inside closed -----------------------------------------------------
square = QgsGeometry.fromWkt("POLYGON((0 0,300 0,300 300,0 300,0 0))")
p1 = totals.prove_totals(lyr, square, 0.05, cert, 0)
r = totals.verify_totals(p1)
cnt, amt, fs, cross = truth(lyr, square, 0.05)
check(r.ok and r.count == cnt == 100 and abs(r.amount - amt) < 0.01 and r.crossing == cross,
      f"a square: {r.count} parcels, {r.amount} m², {r.crossing} crossing ({r.why}) - truth {cnt}, {amt:.3f}, {cross}")
check(abs(r.fields["price"] - fs["price"]) < 1e-6 and r.fields["parcel_id"] == fs["parcel_id"],
      f"the field totals are the true ones: {r.fields}")
check(r.code == cert["code"], "the proof's code is the certificate's")
raw = json.dumps(p1)
check(r.shown == 0 and '"g"' not in raw, f"no shape shown: the area's edge crosses no parcel ({r.closed} closed)")
check("owner 5" not in raw and "1005.5" not in raw, "no value of any parcel in the file")

# ---- a circle: parcels crossing its edge ---------------------------------------------------------
circle = QgsGeometry.fromPointXY(QgsPointXY(375, 375)).buffer(130, 32)
p2 = totals.prove_totals(lyr, circle, 0.05, cert, 0)
r2 = totals.verify_totals(p2)
cnt, amt, fs, cross = truth(lyr, circle, 0.05)
check(r2.ok and r2.count == cnt and abs(r2.amount - amt) < 0.01 and r2.crossing == cross and cross > 0,
      f"a circle: {r2.count} inside, {r2.crossing} crossing ({r2.why}) - truth {cnt}, {cross}")
check(r2.shown < 3 * cross and r2.shown >= cross,
      f"only the parcels near the edge show their shape: {r2.shown} of {t.count}")


# ---- tampering ---------------------------------------------------------------------------------------
def nodes(n, out):
    out.append(n)
    for c in n.get("kids", []):
        nodes(c, out)
    return out


def all_nodes(pr):
    out = []
    for n in pr["tree"]:
        nodes(n, out)
    return out


def tamper(pr, fn):
    q = copy.deepcopy(pr)
    fn(q)
    return totals.verify_totals(q)


def first(pr, cond):
    for n in all_nodes(pr):
        if cond(n):
            return n
    return None


leaf_items = [i for n in all_nodes(p1) if "items" in n for i in n["items"]]
check(leaf_items and all("n" not in i for i in leaf_items if totals._Area(square, 0.05, t.step).box(i["b"]) == "out"),
      f"in the squares opened, the parcels outside show their sums sealed, not their numbers ({len(leaf_items)} items)")


def bump_open(q):
    n = first(q, lambda n: "items" in n)
    n["s"][1] += 400000


r = tamper(p2, bump_open)
check(not r.ok and r.why == "sums", f"an opened square's sums raised: refused ({r.why})")


def bump_closed(q):
    n = first(q, lambda n: "h" in n and n["s"][0] > 0)
    n["s"][0] += 1
    n["s"][1] += 400000


r = tamper(p2, bump_closed)
check(not r.ok and r.why == "sums", f"a closed square's sums raised: refused ({r.why})")


def drop_kid(q):
    n = first(q, lambda n: "kids" in n and len(n["kids"]) > 1)
    n["kids"].pop()


r = tamper(p2, drop_kid)
check(not r.ok and r.why == "sums", f"a square dropped: refused ({r.why})")


def drop_item(q):
    n = first(q, lambda n: "items" in n and len(n["items"]) > 1)
    n["items"].pop()


r = tamper(p2, drop_item)
check(not r.ok and r.why == "sums", f"a feature dropped: refused ({r.why})")


def hide_shape(q):
    n = first(q, lambda n: "items" in n and any("g" in i for i in n["items"]))
    for i in n["items"]:
        i.pop("g", None)


r = tamper(p2, hide_shape)
check(not r.ok and r.why == "hidden", f"a shape on the edge kept back: refused ({r.why})")


def bigger(q):
    n = first(q, lambda n: "items" in n and any("g" in i for i in n["items"]))
    it = [i for i in n["items"] if "g" in i][0]
    it["n"][1] += 50000


r = tamper(p2, bigger)
check(not r.ok and r.why == "amount", f"a feature's area raised: refused ({r.why})")
r = tamper(p1, lambda q: q["area"].__setitem__("wkt", "POLYGON((0 0,400 0,400 400,0 400,0 0))"))
check(not r.ok and r.why in ("open", "hidden", "sums"), f"the area made larger after the proof: refused ({r.why})")
r = tamper(p1, lambda q: q["certificate"].__setitem__("code", "AAAA-AAAA-AAAA-AAAA"))
check(not r.ok and r.why == "code", "a code that is not the certificate's: refused")


def other(q):
    q["certificate"]["layers"][0]["sums"] = "f" * 64
    q["certificate"]["code"] = core.code(core.layer_codes(q["certificate"]["layers"]))


r = tamper(p1, other)
check(not r.ok and r.why == "sums", f"passed off under another certificate: refused ({r.why})")
r = tamper(p1, lambda q: q["sum_fields"].reverse())
check(not r.ok and r.why == "sums", "the field totals swapped: refused")

# ---- lines: a road crossing ----------------------------------------------------------------------------
roads = QgsVectorLayer("LineString?crs=EPSG:32638&field=name:string&field=lanes:integer", "Roads", "memory")
rf = []
for k, wkt in enumerate(["LINESTRING(-500 22.5,1200 22.5)", "LINESTRING(10 120,200 120)",
                         "LINESTRING(10 160,200 160,200 260)", "LINESTRING(900 900,950 950)"]):
    f = QgsFeature(roads.fields())
    f.setAttributes([f"road {k}", k + 1])
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    rf.append(f)
roads.dataProvider().addFeatures(rf)
pr = totals.prove_totals(roads, square, 0.05)
r = totals.verify_totals(pr)
check(r.ok and r.count == 2 and abs(r.amount - 480) < 0.01 and r.crossing == 1 and r.measure == "length"
      and r.fields["lanes"] == 5,
      f"roads: {r.count} inside, {r.amount} m long, the long road crossing ({r.why})")

# ---- geographic -----------------------------------------------------------------------------------------
geo = QgsVectorLayer("Polygon?crs=EPSG:4326&field=v:double", "Geo", "memory")
gf = []
for k in range(4):
    x = 46.7 + k * 0.001
    f = QgsFeature(geo.fields())
    f.setAttributes([k * 1.5])
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 24.7,{x + 0.0005} 24.7,{x + 0.0005} 24.7005,{x} 24.7005,{x} 24.7))"))
    gf.append(f)
geo.dataProvider().addFeatures(gf)
pr = totals.prove_totals(geo, QgsGeometry.fromWkt("POLYGON((46.69 24.69,46.7025 24.69,46.7025 24.71,46.69 24.71,46.69 24.69))"),
                         0.05 / 111320.0)
r = totals.verify_totals(pr)
check(r.ok and r.count == 3 and 2700 < r.amount / 3 < 2900 and r.fields["v"] == 4.5,
      f"a geographic layer: {r.count} parcels, {r.amount:.1f} m² on the ellipsoid ({r.why})")

# ---- against the certificate ------------------------------------------------------------------------
old = copy.deepcopy(cert)
del old["layers"][0]["sums"]
try:
    totals.prove_totals(lyr, square, 0.05, old, 0)
    check(False, "a proof against an old certificate")
except core.FingerprintError as ex:
    check(str(ex) == "old certificate", "an old certificate without sums: said so")
lyr.startEditing()
lyr.changeAttributeValue(next(lyr.getFeatures()).id(), lyr.fields().indexOf("price"), 1.0)
try:
    totals.prove_totals(lyr, square, 0.05, cert, 0)
    check(False, "a proof of a layer changed since its certificate")
except core.FingerprintError as ex:
    check(str(ex) == "changed", "the layer changed since the certificate: no proof made against it")
lyr.rollBack()
p5 = totals.prove_totals(lyr, square, 0.05)
check(totals.verify_totals(p5).ok and p5["certificate"]["code"] == cert["code"],
      "without a certificate: the same code as the layer's certificate")

# ---- the dialog --------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class Bar:
    def pushMessage(self, *a, **k):
        pass


class Canvas:
    def setExtent(self, *a):
        pass

    def refresh(self):
        pass

    def mapSettings(self):
        from qgis.core import QgsMapSettings
        return QgsMapSettings()


area_lyr = QgsVectorLayer("Polygon?crs=EPSG:32638", "Area", "memory")
af = QgsFeature()
af.setGeometry(circle)
area_lyr.dataProvider().addFeatures([af])
QgsProject.instance().addMapLayer(area_lyr)
area_lyr.selectAll()


class Iface:
    def __init__(self):
        self.w = QWidget()

    def mainWindow(self):
        return self.w

    def activeLayer(self):
        return area_lyr

    def messageBar(self):
        return Bar()

    def mapCanvas(self):
        return Canvas()


class Plugin:
    iface = Iface()


from biruni.fingerprint import totals_ui  # noqa: E402

plug = Plugin()
dlg = totals_ui.prove_ui(plug)
dlg.data.setCurrentIndex(dlg.data.findData(lyr.id()))
cpath = os.path.join(root, "parcels.biruni-proof.json")
core.write_json(cert, cpath)
dlg.pick_cert(cpath)
out = os.path.join(root, "circle")
pr = dlg.make(out)
check(pr is not None and os.path.exists(out + totals_ui.TOTALS) and cert["code"] in dlg.out.text(),
      "made from the dialog, with the certificate's code: " + dlg.out.text()[:100])
n0 = len(QgsProject.instance().mapLayers())
r = totals_ui.verify_ui(plug, out + totals_ui.TOTALS, quiet=True)
check(r.ok and cert["code"] in r.text and len(QgsProject.instance().mapLayers()) == n0 + 1 and str(r2.count) in r.text,
      "checked from the button, the area on the map: " + r.text[:110].replace("\n", " "))
r = totals_ui.verify_ui(plug, cpath, quiet=True)
check(not r.ok and r.why == "format", "a file that is not a totals proof: said so")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
