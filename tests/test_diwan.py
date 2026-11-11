r"""Diwan (a GeoPackage that remembers): the file stays a valid GeoPackage for every program, every feature keeps
its identity, edits made by another program are found and logged, the map as it was at any moment, putting a
feature back, and a round trip through other formats.

Run:  engine\bin\python-qgis.bat tests\test_diwan.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from osgeo import gdal, ogr, osr  # noqa: E402

gdal.UseExceptions()
from biruni.diwan.store import Diwan, DiwanError, UID_FIELD  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def square(x, y, s=10):
    return ogr.CreateGeometryFromWkt(f"POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))")


def make(path, n=50, layer="parcels"):
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32638)
    lyr = ds.CreateLayer(layer, srs, ogr.wkbPolygon)
    for name, t in (("owner", ogr.OFTString), ("area", ogr.OFTReal), ("n", ogr.OFTInteger)):
        lyr.CreateField(ogr.FieldDefn(name, t))
    for i in range(n):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(square((i % 10) * 20, (i // 10) * 20))
        f.SetField("owner", f"owner {i}")
        f.SetField("area", 100.0)
        f.SetField("n", i)
        lyr.CreateFeature(f)
    ds = None


def edit(path, layer="parcels"):
    """Another program edits the file (plain GDAL, no Diwan): (layer, dataset - keep it while editing)."""
    ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
    return ds.GetLayerByName(layer), ds


work = tempfile.mkdtemp()
path = os.path.join(work, "project.gpkg")
make(path)

# ---- turning it on, and the file stays a GeoPackage ----------------------------------------------------
with Diwan(path) as d:
    n = d.enable(who="Hatem")
check(n == 50 and Diwan.is_diwan(path), f"every feature gets its identity and first version ({n})")
from osgeo_utils.samples import validate_gpkg  # noqa: E402

errors = validate_gpkg.check(path, abort_at_first_error=False, warning_msg=False, myprint=lambda *a: None)
check(not errors, f"the official GeoPackage validator finds no error ({errors[:3]})")
ds = gdal.OpenEx(path, gdal.OF_VECTOR)
names = [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]
check(names == ["parcels", "diwan_history"] and ds.GetLayerByName("parcels").GetFeatureCount() == 50,
      f"other programs see the layer and the history as a table, nothing else ({names})")
check(ds.GetLayerByName("diwan_history").GetFeatureCount() == 50, "… and can read the history")
ds = None
import sqlite3  # noqa: E402

ext = sqlite3.connect(path).execute("SELECT count(*) FROM sqlite_master WHERE name='gpkg_extensions'").fetchone()[0]
check(not ext or not sqlite3.connect(path).execute(
    "SELECT 1 FROM gpkg_extensions WHERE extension_name LIKE '%diwan%'").fetchone(),
      "no extension row that would make other programs warn against editing")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
from qgis.core import QgsApplication, QgsVectorLayer  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
ql = QgsVectorLayer(path + "|layername=parcels", "p", "ogr")
check(ql.isValid() and ql.featureCount() == 50, "the engine opens it as an ordinary layer")
del ql

with Diwan(path) as d:
    uid3, uid5, uid7 = d.uid_of("parcels", 3), d.uid_of("parcels", 5), d.uid_of("parcels", 7)
    check(len({uid3, uid5, uid7}) == 3 and all(len(u) == 36 for u in (uid3, uid5, uid7)), "identities are UUIDs")
    before = d.history("parcels", uid3)[-1]["at"]
time.sleep(1.1)

# ---- edits made by another program are found ------------------------------------------------------------
lyr, keep = edit(path)
f = lyr.GetFeature(3)
f.SetField("owner", "new owner")
lyr.SetFeature(f)
f = lyr.GetFeature(5)
f.SetGeometry(square(500, 500))
lyr.SetFeature(f)
lyr.DeleteFeature(7)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(square(900, 900))
f.SetField("owner", "added elsewhere")
lyr.CreateFeature(f)
f = lyr.GetFeature(10)                                   # renumbered: deleted and written again, same content
g, owner, area, n10 = f.GetGeometryRef().Clone(), f.GetField("owner"), f.GetField("area"), f.GetField("n")
uid10 = Diwan(path).uid_of("parcels", 10)
lyr.DeleteFeature(10)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(g)
f.SetField("owner", owner)
f.SetField("area", area)
f.SetField("n", n10)
lyr.CreateFeature(f)
new10 = f.GetFID()
lyr = keep = None

with Diwan(path) as d:
    found = d.reconcile(who="")
    check(found == {"add": 1, "change": 2, "delete": 1, "moved": 1},
          f"another program's edits are found: 2 changed, 1 deleted, 1 added, 1 renumbered ({found})")
    h = d.history("parcels", uid3)
    check(len(h) == 2 and h[0]["attrs"]["owner"] == "owner 2" and h[1]["attrs"]["owner"] == "new owner"
          and h[1]["how"] == "external", "the feature's history holds both versions, the new one marked external")
    check(d.uid_of("parcels", new10) == uid10, "a renumbered feature keeps its identity")
    check(d.history("parcels", uid7)[-1]["op"] == "delete" and d.fid_of("parcels", uid7) is None,
          "a deleted feature keeps its history")
    check(d.reconcile() == {"add": 0, "change": 0, "delete": 0, "moved": 0}, "nothing more to find the next time")

    # ---- the map as it was ----------------------------------------------------------------------------
    past = d.state_at("parcels", before)
    check(len(past) == 50 and past[uid3]["attrs"]["owner"] == "owner 2" and uid7 in past,
          "the layer as it was before the edits: 50 features, the old owner, the deleted one still there")

    # ---- putting one feature back ---------------------------------------------------------------------
    fid = d.restore(h[0]["seq"], who="Hatem")
    check(d.history("parcels", uid3)[-1]["how"] == "restore" and fid == 3, "a feature is put back as it was")
rds = gdal.OpenEx(path, gdal.OF_VECTOR)
row = rds.GetLayerByName("parcels").GetFeature(3)
check(row.GetField("owner") == "owner 2", "… in the table itself, the others untouched")

# ---- converting and bringing back -------------------------------------------------------------------------
for ext_, label in ((".shp", "shapefile"), (".geojson", "GeoJSON")):
    out = os.path.join(work, "copy" + ext_)
    with Diwan(path) as d:
        n_out = d.export("parcels", out)
        same = d.bring_back("parcels", out, who="Hatem")
    check(n_out == 50 and same["same"] == 50 and same["change"] == 0,
          f"{label}: exported with the identities; an untouched copy changes nothing ({same})")

out = os.path.join(work, "field.shp")
with Diwan(path) as d:
    d.export("parcels", out)
    uid_a = d.uid_of("parcels", 2)      # fids start at 1: the feature with n = 1
ds = gdal.OpenEx(out, gdal.OF_VECTOR | gdal.OF_UPDATE)
cl = ds.GetLayer(0)
for f in cl:
    if f.GetField("n") == 1:
        f.SetField("owner", "changed in the field")
        cl.SetFeature(f)
    elif f.GetField("n") == 2:
        f.SetGeometry(square(40, -100))
        cl.SetFeature(f)
    elif f.GetField("n") == 4:
        cl.DeleteFeature(f.GetFID())
    elif f.GetField("n") == 6:
        f.SetFieldNull(UID_FIELD)        # the field lost on the way: matched by its content
        cl.SetFeature(f)
f = ogr.Feature(cl.GetLayerDefn())
f.SetGeometry(square(700, 700))
f.SetField("owner", "new in the field")
cl.CreateFeature(f)
ds = None
with Diwan(path) as d:
    got = d.bring_back("parcels", out, who="Field team")
    check(got["change"] == 2 and got["delete"] == 1 and got["add"] == 1,
          f"the field copy comes back: 2 changed, 1 deleted, 1 added ({got})")
    h = d.history("parcels", uid_a)
    check(h[-1]["attrs"]["owner"] == "changed in the field" and h[-1]["how"].startswith("import:")
          and h[-1]["who"] == "Field team", "the change is logged with who brought it and from which format")
other = os.path.join(work, "other.gpkg")
make(other)
with Diwan(other) as d2:
    d2.enable()
    d2.export("parcels", os.path.join(work, "other.geojson"))
with Diwan(path) as d:
    try:
        d.bring_back("parcels", os.path.join(work, "other.geojson"))
        check(False, "a copy of another file is refused")
    except DiwanError:
        check(True, "a copy of another file is refused")
errors = validate_gpkg.check(path, abort_at_first_error=False, warning_msg=False, myprint=lambda *a: None)
check(not errors, f"after all this the file is still a valid GeoPackage ({errors[:3]})")

# ---- inside Biruni: saves are logged; the panel ------------------------------------------------------------------
from qgis.core import QgsGeometry, QgsProject  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QObject, pyqtSignal  # noqa: E402

from biruni import background  # noqa: E402
from biruni.diwan import dock as ddock, live  # noqa: E402


def settle():
    for _ in range(300):
        app.processEvents()
        if not background.busy():
            return
        time.sleep(0.02)


ql = QgsVectorLayer(path + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(ql)
check(live.diwan_of(ql) == (path, "parcels"), "a layer of a Diwan file is recognised")
live.watch(ql)
with Diwan(path) as d:
    uid9 = d.uid_of("parcels", 9)
ql.startEditing()
ql.changeAttributeValue(9, ql.fields().indexOf("owner"), "saved in Biruni")
ql.commitChanges()
settle()
with Diwan(path) as d:
    h9 = d.history("parcels", uid9)
check(h9[-1]["how"] == "biruni" and h9[-1]["attrs"]["owner"] == "saved in Biruni" and h9[-1]["who"],
      f"a save in Biruni is logged with its author ({h9[-1]['how']}, {h9[-1]['who']!r})")


class _Bar:
    said = []

    def pushSuccess(self, *a):
        self.said.append(a)

    pushInfo = pushWarning = pushSuccess


class _Iface(QObject):
    currentLayerChanged = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.canvas = QgsMapCanvas()
        self.bar = _Bar()

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def activeLayer(self):  # noqa: N802
        return ql

    def messageBar(self):  # noqa: N802
        return self.bar

    def mainWindow(self):  # noqa: N802
        return None


class _Plugin:
    iface = _Iface()


panel = ddock.DiwanDock(_Plugin())
check("Diwan" in panel.state.text() or "ديوان" in panel.state.text(), f"the panel says the layer is kept in a Diwan "
      f"({panel.state.text()})")
ql.selectByIds([9])
app.processEvents()
check(panel.versions.count() == 2, f"the selected feature's versions are listed ({panel.versions.count()})")
panel.versions.setCurrentRow(1)                         # the older one
check(panel.restore_btn.isEnabled(), "an older version can be put back")
panel.restore()
check(ql.isEditable() and ql.getFeature(9)["owner"] == "owner 8" and ql.isModified(),
      "it comes back as an edit of the layer (undoable, not saved yet)")
ql.undoStack().undo()
check(ql.getFeature(9)["owner"] == "saved in Biruni", "… and Ctrl+Z takes it away")
ql.rollBack()
n_layers = len(QgsProject.instance().mapLayers())
panel.show_past()
past_layers = [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr is not ql]
check(len(QgsProject.instance().mapLayers()) == n_layers + 1 and past_layers[-1].featureCount() == ql.featureCount()
      and past_layers[-1].readOnly(), "the layer as it was at a moment is added as a read-only layer")
panel.shutdown()

# ---- speed ---------------------------------------------------------------------------------------------------
big = os.path.join(work, "big.gpkg")
make(big, 20000)
t0 = time.time()
with Diwan(big) as d:
    d.enable()
t1 = time.time()
lyr, keep = edit(big)
for fid in range(1, 201):
    f = lyr.GetFeature(fid)
    f.SetField("n", -fid)
    lyr.SetFeature(f)
lyr = keep = None
t2 = time.time()
with Diwan(big) as d:
    found = d.reconcile()
t3 = time.time()
print(f"20,000 features: turned on in {t1 - t0:.1f} s, 200 outside edits found in {t3 - t2:.1f} s")
check(found["change"] == 200 and t1 - t0 < 20 and t3 - t2 < 10, "a big file is quick enough")

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
