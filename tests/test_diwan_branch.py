r"""Diwan branches: a copy goes to the field, both sides edit, one sync brings each side the other's work -
fields and vertices joined, conflicts listed (the main file's value kept), deleted-vs-changed keeps the change,
and the next sync carries only what is new.

Run:  engine\bin\python-qgis.bat tests\test_diwan_branch.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()

from osgeo import gdal, ogr, osr  # noqa: E402
from qgis.core import QgsApplication  # noqa: E402

gdal.UseExceptions()
app = QgsApplication([], True)
app.initQgis()
from osgeo_utils.samples import validate_gpkg  # noqa: E402

from biruni.diwan import branch  # noqa: E402
from biruni.diwan.store import Diwan, DiwanError  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def ring(x, y, s=10, nx=None, sy=None):
    """A square with a middle vertex on its north and south edges (so the two edges can be moved apart)."""
    top = y + s if nx is None else y + s + nx
    bottom = y if sy is None else y - sy
    return ogr.CreateGeometryFromWkt(f"POLYGON(({x} {y},{x + s / 2} {bottom},{x + s} {y},{x + s} {y + s},"
                                     f"{x + s / 2} {top},{x} {y + s},{x} {y}))")


work = tempfile.mkdtemp()
main = os.path.join(work, "office.gpkg")
ds = ogr.GetDriverByName("GPKG").CreateDataSource(main)
srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
lyr = ds.CreateLayer("parcels", srs, ogr.wkbPolygon)
for name, t in (("owner", ogr.OFTString), ("area", ogr.OFTReal)):
    lyr.CreateField(ogr.FieldDefn(name, t))
for i in range(12):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ring(i * 20, 0))
    f.SetField("owner", f"owner {i + 1}")
    f.SetField("area", 100.0)
    lyr.CreateFeature(f)
ds = lyr = None
with Diwan(main) as d:
    d.enable(who="Office")
    uid = {fid: d.uid_of("parcels", fid) for fid in range(1, 13)}

field = os.path.join(work, "field laptop.gpkg")
bid = branch.make_branch(main, field, "Field team")
check(bool(bid) and Diwan.is_diwan(field) and [b["name"] for b in branch.branches(main)] == ["Field team"],
      "a branch is made and the main file lists it")
try:
    branch.make_branch(field, os.path.join(work, "x.gpkg"), "no")
    check(False, "a branch of a branch is refused")
except DiwanError:
    check(True, "a branch of a branch is refused")


def edit(path, who, fn):
    """Edit with plain GDAL (as any program), then log it as a save would."""
    ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
    fn(ds.GetLayerByName("parcels"))
    ds = None
    with Diwan(path) as d:
        d.reconcile(who=who, how="biruni")


def setf(lyr, fid, **kv):
    f = lyr.GetFeature(fid)
    for k, v in kv.items():
        if k == "geom":
            f.SetGeometry(v)
        else:
            f.SetField(k, v)
    lyr.SetFeature(f)


def add(lyr, x, owner):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ring(x, 100))
    f.SetField("owner", owner)
    f.SetField("area", 100.0)
    lyr.CreateFeature(f)


def office(lyr):
    setf(lyr, 1, owner="office owner")                        # 1: a field the field team leaves alone
    setf(lyr, 2, geom=ring(20, 0, nx=2))                       # 2: the north edge
    lyr.DeleteFeature(3)                                       # 3: deleted in the office
    setf(lyr, 6, owner="office says A")                        # 6: both change the owner: a conflict
    setf(lyr, 7, owner="office keeps 7")                       # 7: changed here, deleted in the field
    add(lyr, 0, "added in the office")


def fieldwork(lyr):
    setf(lyr, 1, area=120.0)                                   # 1: another field of the same feature
    setf(lyr, 2, geom=ring(20, 0, sy=3))                       # 2: the south edge of the same feature
    setf(lyr, 4, owner="field owner")                          # 4: only the field changes it
    lyr.DeleteFeature(5)                                       # 5: deleted in the field
    setf(lyr, 6, owner="field says B")
    lyr.DeleteFeature(7)
    add(lyr, 40, "added in the field")


edit(main, "Office", office)
edit(field, "Field team", fieldwork)
r = branch.sync(main, field, who="Hatem")
print("   ", {k: (v if k != "conflicts" else len(v)) for k, v in r.items()})


def table(path):
    """{uid: (owner, area, normalized geometry wkt)} of a file."""
    with Diwan(path) as d:
        ids = d.ids("parcels")
        out = {}
        for fid, g, a in d.rows("parcels"):
            from biruni.diwan.store import wkb_of
            geom = ogr.CreateGeometryFromWkb(wkb_of(g))
            geom.Normalize()
            out[ids[fid]] = (a["owner"], a["area"], geom.ExportToWkt())
        return out


tm, tf = table(main), table(field)
check(tm == tf, f"after the sync both files hold the same features ({len(tm)} / {len(tf)})")
check(tm[uid[1]][:2] == ("office owner", 120.0), "one feature, two fields changed on two sides: both kept")
g2 = ogr.CreateGeometryFromWkt(tm[uid[2]][2])
pts = [(round(g2.GetGeometryRef(0).GetX(i), 3), round(g2.GetGeometryRef(0).GetY(i), 3))
       for i in range(g2.GetGeometryRef(0).GetPointCount())]
check((25.0, 12.0) in pts and (25.0, -3.0) in pts,
      f"one feature, two edges moved on two sides: both moves kept, vertex by vertex ({pts})")
check(uid[3] not in tm and uid[5] not in tm, "a deletion on either side reaches the other")
check(tm[uid[4]][0] == "field owner", "the field's change reaches the office")
owners = sorted(v[0] for v in tm.values())
check("added in the office" in owners and "added in the field" in owners, "new features go both ways")
check(tm[uid[6]][0] == "office says A", "the same field changed differently: the office's value is kept…")
cs = branch.conflicts(main)
kinds = sorted((c["what"], c["uid"]) for c in cs)
check(("field", uid[6]) in kinds and ("deleted", uid[7]) in kinds and len(cs) == 2,
      f"… and both conflicts are listed in the main file ({[(c['what'], c['field']) for c in cs]})")
check(tm[uid[7]][0] == "office keeps 7", "changed on one side, deleted on the other: the change is kept")
with Diwan(main) as d:
    h = d.history("parcels", uid[4])
check(h[-1]["how"] == "sync:Field team" and h[-1]["who"] == "Hatem", "the main file logs where each change came from")

# nothing new: nothing moves
r2 = branch.sync(main, field, who="Hatem")
check(r2["to_main"] == 0 and r2["to_branch"] == 0 and not r2["conflicts"], f"a second sync moves nothing ({r2})")

# only what is new afterwards
edit(field, "Field team", lambda lyr: setf(lyr, 8, owner="later in the field"))
r3 = branch.sync(main, field, who="Hatem")
check(r3["to_main"] == 1 and r3["to_branch"] == 0 and table(main)[uid[8]][0] == "later in the field",
      f"the next sync carries only the new change ({r3})")

# turning a conflict around
c6 = next(c for c in cs if c["uid"] == uid[6])
branch.resolve(main, c6["id"], use_other=True, who="Hatem")
check(table(main)[uid[6]][0] == "field says B" and not any(c["id"] == c6["id"] for c in branch.conflicts(main)),
      "a conflict turned around takes the field's value in the office")
branch.sync(main, field, who="Hatem")
check(table(field)[uid[6]][0] == "field says B" and table(main) == table(field), "… and the files agree again")

# ---- the panel ---------------------------------------------------------------------------------------------------
from qgis.core import QgsProject, QgsVectorLayer  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QObject, pyqtSignal  # noqa: E402

from biruni.diwan import dock as ddock  # noqa: E402


class _Bar:
    def pushSuccess(self, *a):
        pass

    pushInfo = pushWarning = pushSuccess


class _Iface(QObject):
    currentLayerChanged = pyqtSignal(object)

    def __init__(self, layer):
        super().__init__()
        self.layer, self.canvas, self.bar = layer, QgsMapCanvas(), _Bar()

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def activeLayer(self):  # noqa: N802
        return self.layer

    def messageBar(self):  # noqa: N802
        return self.bar

    def mainWindow(self):  # noqa: N802
        return None


class _Plugin:
    pass


ml = QgsVectorLayer(main + "|layername=parcels", "Office parcels", "ogr")
fl = QgsVectorLayer(field + "|layername=parcels", "Field parcels", "ogr")
QgsProject.instance().addMapLayers([ml, fl])
pm = _Plugin()
pm.iface = _Iface(ml)
panel = ddock.DiwanDock(pm)
check("Field team" in panel.branch_state.text(), f"the main file's panel lists its branches ({panel.branch_state.text()})")
check(panel.conflict_list.count() == 1 and not panel.conflict_list.isHidden(),
      f"the open conflict is listed ({panel.conflict_list.count()})")
panel.conflict_list.setCurrentRow(0)
panel.resolve(False)
check(panel.conflict_list.count() == 0 and not branch.conflicts(main), "a conflict can be closed from the panel")
pf = _Plugin()
pf.iface = _Iface(fl)
bpanel = ddock.DiwanDock(pf)
check("Field team" in bpanel.branch_state.text() and not bpanel.branch_btn.isEnabled(),
      f"a branch's panel says whose branch it is ({bpanel.branch_state.text()})")
panel.shutdown()
bpanel.shutdown()
QgsProject.instance().removeAllMapLayers()

# safety
other = os.path.join(work, "other.gpkg")
import shutil  # noqa: E402

shutil.copy(main, other)
with Diwan(other) as d:
    d.con.execute("UPDATE diwan_meta SET value='someone else' WHERE key='file_id'")
    d.con.commit()
try:
    branch.sync(other, field)
    check(False, "a branch is refused by a file it does not come from")
except DiwanError:
    check(True, "a branch is refused by a file it does not come from")
for p, label in ((main, "main"), (field, "branch")):
    errors = validate_gpkg.check(p, abort_at_first_error=False, warning_msg=False, myprint=lambda *a: None)
    check(not errors, f"the {label} file is still a valid GeoPackage ({errors[:2]})")
ds = gdal.OpenEx(field, gdal.OF_VECTOR)
check([ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())] == ["parcels", "diwan_history"],
      "other programs still see only the layer and the history")
ds = None

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
