r"""Spatial fingerprints: two copies of a layer (a GeoPackage and the shapefile written from it) are found the same,
one parcel changed among thousands is found by a few fingerprints, the change is brought over and the copies are
the same again; a certificate proves a delivery and shows where it differs; a package carries only the squares
that changed, and does not overwrite a square both sides changed.

Run:  engine\bin\python-qgis.bat tests\test_fingerprint.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsField, QgsGeometry,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QVariant  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.fingerprint import core  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()


def write(layer, path, driver):
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = driver
    err = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsCoordinateTransformContext(), o)
    assert err[0] == QgsVectorFileWriter.NoError, err
    return QgsVectorLayer(path, os.path.basename(path), "ogr")


# ---- 3600 parcels in a UTM zone ------------------------------------------------------------------------------------
mem = QgsVectorLayer("Polygon?crs=EPSG:32638", "parcels", "memory")
mem.dataProvider().addAttributes([QgsField("PARCEL_ID", QVariant.Int), QgsField("OWNER", QVariant.String),
                                  QgsField("AREA_M2", QVariant.Double)])
mem.updateFields()
feats = []
X0, Y0 = 670000.0, 2730000.0
for i in range(60):
    for j in range(60):
        x, y = X0 + i * 30.0, Y0 + j * 30.0
        f = QgsFeature(mem.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 25} {y},{x + 25} {y + 20},{x} {y + 20},{x} {y}))"))
        f.setAttributes([i * 100 + j, f"owner {i}-{j}", 500.0])
        feats.append(f)
mem.dataProvider().addFeatures(feats)
gpkg = write(mem, os.path.join(work, "office.gpkg"), "GPKG")
shp = write(gpkg, os.path.join(work, "delivered.shp"), "ESRI Shapefile")
check(gpkg.featureCount() == shp.featureCount() == 3600, "a GeoPackage of 3600 parcels and the shapefile written from it")
check(core.field_names(gpkg) == ["parcel_id", "owner", "area_m2"], "the GeoPackage's own fid is not data")

fields = core.common_fields(gpkg, shp)
ident = core.guess_identity(fields)
check(ident == "parcel_id", "the field that names a parcel is guessed (without it, a change is a feature gone and one come)")
ta = core.build(gpkg, fields)
tb = core.build(shp, fields)
check(ta.root == tb.root and ta.count == 3600,
      "the two files are found the same, though the shapefile writes multipolygons turned the other way")
d = core.compare(ta, tb)
check(d.same and d.compared == 1, "… with one fingerprint compared")

# ---- one parcel changed in the delivered copy --------------------------------------------------------------------
target = next(f for f in shp.getFeatures() if f["PARCEL_ID"] == 3017)
shp.startEditing()
g = QgsGeometry(target.geometry())
g.moveVertex(target.geometry().vertexAt(1).x() + 0.40, target.geometry().vertexAt(1).y(), 1)
shp.changeGeometry(target.id(), g)
shp.commitChanges()
tb = core.build(shp, fields)
d = core.compare(ta, tb)
check(not d.same and len(d.cells) == 1 and len(d.only_a) == 1 and len(d.only_b) == 1,
      f"one corner moved 40 cm: found in one square of {len(ta.leaf)}, one feature each side")
check(d.compared < 100, f"… by {d.compared} fingerprints compared, not 3600 features")
fa = next(gpkg.getFeatures(core.QgsFeatureRequest(d.only_a[0])))
check(fa["PARCEL_ID"] == 3017, "… and it is that parcel")

tiny = next(f for f in shp.getFeatures() if f["PARCEL_ID"] == 10)
shp.startEditing()
g = QgsGeometry(tiny.geometry())
g.moveVertex(tiny.geometry().vertexAt(2).x() + 0.00003, tiny.geometry().vertexAt(2).y(), 2)
shp.changeGeometry(tiny.id(), g)
shp.commitChanges()
check(core.compare(ta, core.build(shp, fields)).cells == d.cells, "a move of 0.03 mm is not a change")

# ---- with an identity field: changed, added, removed --------------------------------------------------------------
fields_id = core.common_fields(gpkg, shp)
ta = core.build(gpkg, fields_id, identity="parcel_id")
shp.startEditing()
gone = next(f for f in shp.getFeatures() if f["PARCEL_ID"] == 1205)
shp.deleteFeature(gone.id())
other = next(f for f in shp.getFeatures() if f["PARCEL_ID"] == 4444)
shp.changeAttributeValue(other.id(), shp.fields().indexOf("OWNER"), "someone else")
nf = QgsFeature(shp.fields())
nf.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({X0 - 100} {Y0},{X0 - 90} {Y0},{X0 - 90} {Y0 + 10},{X0 - 100} {Y0}))"))
for name, v in (("PARCEL_ID", 99999), ("OWNER", "a new parcel"), ("AREA_M2", 50.0)):
    nf[name] = v
shp.addFeature(nf)
check(shp.commitChanges(), "the delivered copy is edited: one parcel gone, one owner changed, one parcel added")
tb = core.build(shp, fields_id, identity="parcel_id")
d = core.compare(ta, tb)
check(len(d.changed) == 2 and len(d.only_a) == 1 and len(d.only_b) == 1,
      f"by PARCEL_ID: {len(d.changed)} changed (the corner, the owner), 1 only in the office copy, 1 only in the delivered one")

# ---- bring the office copy's version into the delivered one -------------------------------------------------------
r = core.bring(d, gpkg, shp)
check(shp.isEditable() and r == {"added": 1, "removed": 1, "changed": 2}, f"the changes are brought, in the layer's edits ({r})")
check(core.build(shp, fields_id, identity="parcel_id").root == ta.root, "… and the two are the same again, before saving")
shp.commitChanges()
check(core.build(shp, fields_id, identity="parcel_id").root == ta.root, "… and after saving")

# ---- the certificate ---------------------------------------------------------------------------------------------
cert = core.certificate([("parcels", core.build(gpkg, fields))])
path = os.path.join(work, "delivery.biruni-proof.json")
core.write_json(cert, path)
cert = core.read_json(path)
check(len(cert["code"]) == 19 and cert["layers"][0]["features"] == 3600, f"a certificate with a short code {cert['code']}")
res = core.check(shp, cert["layers"][0])
check(res["same"], "the delivered copy is what the certificate says")
shp.startEditing()
f = next(f for f in shp.getFeatures() if f["PARCEL_ID"] == 2222)
shp.changeAttributeValue(f.id(), shp.fields().indexOf("AREA_M2"), 501.0)
shp.commitChanges()
res = core.check(shp, cert["layers"][0])
want = core.build(gpkg, fields).cell_of(f.geometry())
check(not res["same"] and res["cells"] == [want], "a changed area is found, and where: the one square")
check(core.check(QgsVectorLayer("Polygon?crs=EPSG:32638&field=x:int", "x", "memory"), cert["layers"][0])["fields"],
      "a layer without the certificate's fields is not the delivery")

# ---- a package of changes: the office copy is the newer one, the field copy has only its certificate ----------
field = write(gpkg, os.path.join(work, "field.gpkg"), "GPKG")
field_cert = core.certificate([("parcels", core.build(field, fields))])["layers"][0]
gpkg.startEditing()
for f in gpkg.getFeatures():
    if f["PARCEL_ID"] in (100, 101, 5959):
        gpkg.changeAttributeValue(f.id(), gpkg.fields().indexOf("OWNER"), "sold")
gpkg.commitChanges()
pkg = core.make_package(gpkg, field_cert)
p_path = os.path.join(work, "changes.biruni-changes")
core.write_json(pkg, p_path)
size = os.path.getsize(p_path)
sold = {core.build(gpkg, fields).cell_of(f.geometry()) for f in gpkg.getFeatures() if f["OWNER"] == "sold"}
check(len(sold) == 2 and {core._key(k) for k in pkg["cells"]} == sold and size < 4000,
      f"the package carries the {len(sold)} squares that changed only ({size} bytes)")
pkg = core.read_json(p_path)
field.startEditing()
f = next(f for f in field.getFeatures() if f["PARCEL_ID"] == 5959)
field.changeAttributeValue(f.id(), field.fields().indexOf("OWNER"), "changed in the field too")
field.commitChanges()
r = core.apply_package(field, pkg)
check(r["added"] == 2 and r["removed"] == 2 and len(r["left"]) == 1,  # 100 and 101 share a square
      f"applied: 2 squares brought, the square both copies changed is left for someone to decide ({r})")
field.commitChanges()
owners = {f["PARCEL_ID"]: f["OWNER"] for f in field.getFeatures() if f["PARCEL_ID"] in (100, 101, 5959)}
check(owners == {100: "sold", 101: "sold", 5959: "changed in the field too"}, f"… and nothing written in the field is lost {owners}")
check(core.make_package(gpkg, core.certificate([("parcels", core.build(gpkg, fields))])["layers"][0]) is None,
      "a copy against its own certificate: nothing to carry")

# ---- copies that cannot be compared ------------------------------------------------------------------------------
geo = QgsVectorLayer("Polygon?crs=EPSG:4326", "geo", "memory")
try:
    core.compare(ta, core.build(geo, []))
    check(False, "copies in different coordinate systems are not compared")
except core.FingerprintError:
    check(True, "copies in different coordinate systems are not compared")
check(core.grid_for(geo) == (2.0 ** -11, 1e-9), "a map in degrees has its own grid")

# ---- in the program: the dialog, the certificate and its check -------------------------------------------------
from qgis.core import QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402

from biruni.fingerprint import ui  # noqa: E402


class Bar:
    said = []

    def pushMessage(self, title, text, level=None, duration=0):
        Bar.said.append(text)


canvas = QgsMapCanvas()


class Tree:
    def selectedLayers(self):
        return [office]


class Iface:
    active = None

    def messageBar(self):
        return Bar()

    def mainWindow(self):
        return None

    def activeLayer(self):
        return Iface.active

    def mapCanvas(self):
        return canvas

    def layerTreeView(self):
        return Tree()


def flat(text):
    return text.replace("\n", " ")


plugin = type("P", (), {"iface": Iface()})()
said = []
ui.QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
ui.QMessageBox.information = staticmethod(lambda parent, title, text: said.append(text))
office = write(gpkg, os.path.join(work, "office2.gpkg"), "GPKG")
office.setName("office")
copy = write(gpkg, os.path.join(work, "copy2.gpkg"), "GPKG")
copy.setName("copy")
QgsProject.instance().addMapLayers([office, copy])
copy.startEditing()
f = next(f for f in copy.getFeatures() if f["PARCEL_ID"] == 707)
copy.changeAttributeValue(f.id(), copy.fields().indexOf("OWNER"), "edited in the copy")
copy.commitChanges()
Iface.active = office
dlg = ui.compare_ui(plugin)
dlg.b.setCurrentIndex(dlg.b.findData(copy.id()))
check(dlg.key.currentData() == "parcel_id", "the dialog finds the field that names a parcel")
dlg.compare()
names = [l.name() for l in QgsProject.instance().mapLayers().values()]
check("1 squares" in dlg.out.text() and "Changed: 1" in dlg.out.text() and len(names) == 4,
      "compared: one square, one parcel changed, both shown on the map: " + dlg.out.text())
dlg.bring()
check("now the same" in dlg.out.text() and copy.isEditable(), "brought: " + dlg.out.text())
copy.commitChanges()
cpath = os.path.join(work, "office" + ui.PROOF)
cert = ui.certificate_ui(plugin, cpath)
check(cert is not None and os.path.isfile(cpath) and cert["code"] in Bar.said[-1],
      "the certificate from the selected layers: " + Bar.said[-1])
check(ui.check_ui(plugin, cpath) is True and "the same as delivered" in said[-1], "checked: " + flat(said[-1]))
office.startEditing()
office.deleteFeature(next(office.getFeatures()).id())
check(ui.check_ui(plugin, cpath) is False and "differs in 1 squares" in said[-1],
      "a parcel deleted before saving: the check says so and where: " + flat(said[-1]))
office.rollBack()

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
