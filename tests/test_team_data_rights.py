r"""Data-entry right and owner-locked fields (Biruni GIS team rooms), off screen.

Tests the data right (network, client guard, forged ops, same_geometry) and field locks (network,
editFormConfig readOnly, number-grant carve-out on new features, pigeon packs, folder+DriveSim, and UI).

Run:  engine\bin\python-qgis.bat tests\test_team_data_rights.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
sys.path.insert(0, HERE)

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import Qt  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.pigeon import core  # noqa: E402
from biruni.team import code as codes, dock as td, features, folder as rf, rules, rules_ui, session  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402
from drivesim import DriveSim  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


root = tempfile.mkdtemp()
hostdir = os.path.join(root, "office")
os.makedirs(hostdir)
gpkg = os.path.join(hostdir, "parcels.gpkg")
mem = QgsVectorLayer(
    "Polygon?crs=EPSG:32638&field=owner:string&field=parcel_no:integer&field=area:double",
    "Parcels",
    "memory",
)
# Features 1 and 2 share vertex (10, 0)-(10, 10) for shared-node propagation tests.
polys = (
    ("owner 1", 10, 100.0, "POLYGON((0 0,10 0,10 10,0 10,0 0))"),
    ("owner 2", 20, 100.0, "POLYGON((10 0,20 0,20 10,10 10,10 0))"),
    ("owner 3", 30, 100.0, "POLYGON((40 0,50 0,50 10,40 10,40 0))"),
)
for ow, pno, ar, wkt in polys:
    f = QgsFeature(mem.fields())
    f.setAttributes([ow, pno, ar])
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
prj = QgsProject.instance()
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
prj.addMapLayer(hl)
prj.setCrs(hl.crs())
prj.write(os.path.join(hostdir, "office.qgz"))

host = session.open_room([hl], "Field team", approve=False)
code = session.room_code(host)
check(host.role == "host" and host.hub.port, "a room is open")
real_identity = session.identity


def join(ident, where):
    session.identity = lambda: dict(ident)
    result, err = {}, []

    def worker():
        try:
            result.update(session.join_steps(code, os.path.join(root, where), lambda s, a, b: None))
        except Exception as e:  # noqa: BLE001
            err.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    pump(60, lambda: not t.is_alive())
    session.identity = real_identity
    check(not err and result.get("snap"), f"{ident['name']} joined the room {err}")
    snap = result["snap"]
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels",
                         "Parcels", "ogr")
    st = Store(os.path.join(result["folder"], ".biruni", "team", "room.sqlite"))
    st.put("layers", result["hello"]["room"]["layers"])
    st.put("since", snap["head"])
    st.put("block", result["hello"]["block"])
    st.put("sid", result["hello"]["room"].get("sid"))
    st.put("roster", result["hello"]["room"].get("members") or [])
    st.set_versions([tuple(v) for v in snap["versions"]])
    n = Node(st, codes.RoomKeys(code), dict(ident))
    n.roster = st.get("roster")
    n.attach_layers({"L1": lyr})
    n.start_member(result["address"], result["hello"]["member"])
    return n, lyr


A, al = join({"id": "a" * 32, "seed": "1" * 64, "name": "Mona"}, "mona")
B, bl = join({"id": "b" * 32, "seed": "2" * 64, "name": "Sami"}, "sami")
pump(5, lambda: len(host.hub.roster()) == 3 and len(A.roster) == 3 and len(B.roster) == 3)

a_statuses = []
b_statuses = []
A.status.connect(lambda lvl, txt: a_statuses.append((lvl, txt)))
B.status.connect(lambda lvl, txt: b_statuses.append((lvl, txt)))

fid1 = next(f.id() for f in hl.getFeatures() if f["owner"] == "owner 1")
fid2 = next(f.id() for f in hl.getFeatures() if f["owner"] == "owner 2")


def _hex(wkt):
    g = QgsGeometry.fromWkt(wkt)
    return bytes(g.asWkb()).hex()


# ---- 1. Network path, data right -----------------------------------------------------------------------------
host.hub.set_rights(A.me_id, "L1", "data")
check(pump(25, lambda: ((A.member or {}).get("rights") or {}).get("L1") == "data"),
      "1. data right reaches Mona")

st1 = features.stored_state(al, fid1)
st1["a"]["owner"] = "Mona data edit"
A.submit("L1", fid1, st1)
check(pump(5, lambda: features.stored_state(hl, fid1)["a"]["owner"] == "Mona data edit"
           and features.stored_state(bl, fid1)["a"]["owner"] == "Mona data edit"),
      "1. Mona's owner change reaches the host and Sami")

# ---- 2. Moved geometry under data right ----------------------------------------------------------------------
a_statuses.clear()
orig_g1 = features.stored_state(hl, fid1)["g"]
moved_st = features.stored_state(al, fid1)
moved_st["g"] = _hex("POLYGON((1 1,11 1,11 11,1 11,1 1))")
A.submit("L1", fid1, moved_st)
check(pump(5, lambda: ("warn", "refused:data entry only: the shape cannot change") in a_statuses
           and A.store.outbox_count() == 0),
      "2. moved geometry refused with expected status")
check(features.stored_state(hl, fid1)["g"] == orig_g1
      and features.stored_state(bl, fid1)["g"] == orig_g1
      and features.stored_state(al, fid1)["g"] == orig_g1,
      "2. host and Sami unchanged, Mona's copy put back")

# ---- 3. Add and delete under data right ----------------------------------------------------------------------
a_statuses.clear()
new_fid = A.next_fid(al)
A.submit("L1", new_fid, {"g": orig_g1, "a": {"owner": "Added", "parcel_no": 99, "area": 10.0}})
check(pump(5, lambda: ("warn", "refused:data entry only: cannot add features") in a_statuses
           and A.store.outbox_count() == 0),
      "3. add refused under data right")
check(features.stored_state(al, new_fid) is None and features.stored_state(hl, new_fid) is None,
      "3. new feature reverted on Mona and absent on host")

a_statuses.clear()
A.submit("L1", fid1, None)
check(pump(5, lambda: ("warn", "refused:data entry only: cannot delete") in a_statuses
           and A.store.outbox_count() == 0),
      "3. delete refused under data right")
check(features.stored_state(al, fid1) is not None and features.stored_state(hl, fid1) is not None,
      "3. deleted feature restored on Mona and intact on host")

# ---- 4. same_geometry unit tests -----------------------------------------------------------------------------
h_single = _hex("POLYGON((0 0,10 0,10 10,0 10,0 0))")
h_multi_rot = _hex("MULTIPOLYGON(((10 0,10 10,0 10,0 0,10 0)))")
h_extra_v = _hex("POLYGON((0 0,5 0,10 0,10 10,0 10,0 0))")
h_shifted = _hex("POLYGON((1 0,11 0,11 10,1 10,1 0))")
check(features.same_geometry(h_single, h_multi_rot),
      "4. same_geometry: single vs multi with rotated ring start is True")
check(features.same_geometry(None, "") and features.same_geometry(None, None),
      "4. same_geometry: null and empty are True")
check(not features.same_geometry(h_single, h_extra_v),
      "4. same_geometry: extra vertex on an edge is False")
check(not features.same_geometry(h_single, h_shifted),
      "4. same_geometry: translated shape is False")
check(not features.same_geometry(None, h_single),
      "4. same_geometry: None vs shape is False")

# ---- 5. Forged ops -------------------------------------------------------------------------------------------
fop = A._op("L1", 0, "field", None, {"name": "hack"})
r_field = host.hub.apply_remote(fop, host.hub._member(A.me_id))
check(isinstance(r_field, dict) and r_field.get("status") == "refused"
      and r_field.get("why") == "only the host changes fields",
      f"5. forged field op refused ({r_field})")

cur1 = features.stored_state(hl, fid1)
nb_before = features.stored_state(hl, fid2)
forged_base = {"g": _hex("POLYGON((0 0,12 0,10 10,0 10,0 0))"), "a": dict(cur1["a"])}
unmoved_state = {"g": cur1["g"], "a": dict(cur1["a"], owner="Mona forged base")}
pop = A._op("L1", fid1, "put", forged_base, unmoved_state)
pop["base_ver"] = host.store.version("L1", fid1)
from biruni.secure import crypto  # noqa: E402
from biruni.team.hub import sign_body  # noqa: E402
pop["sig"] = crypto.sign(bytes.fromhex(A.ident["seed"]), sign_body(pop)).hex()
r_pop = host.hub.apply_remote(pop, host.hub._member(A.me_id))
check(isinstance(r_pop, dict) and r_pop.get("status") == "ok"
      and features.stored_state(hl, fid2)["g"] == nb_before["g"],
      "5. forged base with unmoved state under data right leaves neighbours untouched")
pump(5, lambda: features.stored_state(al, fid1)["a"]["owner"] == "Mona forged base"
     and features.stored_state(bl, fid1)["a"]["owner"] == "Mona forged base")

# ---- 6. Client guard -----------------------------------------------------------------------------------------
a_statuses.clear()
al.startEditing()
al.changeAttributeValue(fid1, al.fields().indexOf("owner"), "Mona guard attr")
al.changeGeometry(fid1, QgsGeometry.fromWkt("POLYGON((1 1,11 1,11 11,1 11,1 1))"))
check(pump(1.0, lambda: ("warn", "data-only") in a_statuses),
      "6. client guard emitted data-only")
buf = al.editBuffer()
check(buf is not None and not buf.changedGeometries() and fid1 in buf.changedAttributeValues(),
      "6. buffer has no geometry change and attribute change is kept")
al.rollBack()

# ---- Locks (set Mona back to edit first) ---------------------------------------------------------------------
host.hub.set_rights(A.me_id, "L1", "edit")
check(pump(25, lambda: ((A.member or {}).get("rights") or {}).get("L1") == "edit"),
      "Mona set back to edit")

# ---- 7. Forged / wrong-layer locks and non-owner set_locked_fields -------------------------------------------
host.store.put(rules.FKEY, {"L1": rules.make_field_lock(A.ident["seed"], A.me_id, "L1", ["parcel_no"], 1)})
check(rules.locked_fields(host.store, host.lookup_member, "L1") == [],
      "7. lock signed with Mona's seed gives locked_fields == []")
st_b = features.stored_state(bl, fid1)
st_b["a"]["parcel_no"] = 101
B.submit("L1", fid1, st_b)
check(pump(5, lambda: features.stored_state(hl, fid1)["a"]["parcel_no"] == 101),
      "7. Sami's parcel_no change accepted when lock signature is not rank 0")

raised = False
try:
    A.set_locked_fields("L1", ["parcel_no"])
except PermissionError:
    raised = True
check(raised, "7. A.set_locked_fields raises PermissionError")

other_layer_doc = rules.make_field_lock(host.ident["seed"], host.me_id, "L2", ["parcel_no"], 1)
host.store.put(rules.FKEY, {"L1": other_layer_doc})
check(rules.locked_fields(host.store, host.lookup_member, "L1") == [],
      "7. valid doc stored under another layer key is ignored")
host.store.put(rules.FKEY, {})

# ---- 8. set_locked_fields, sync, readOnly, and unlock --------------------------------------------------------
host.set_locked_fields("L1", ["parcel_no", "area"])
check(pump(25, lambda: rules.locked_fields(A.store, A.lookup_member, "L1") == ["area", "parcel_no"]
           and rules.locked_fields(B.store, B.lookup_member, "L1") == ["area", "parcel_no"]),
      "8. set_locked_fields reaches members' stores")
check(al.editFormConfig().readOnly(al.fields().indexOf("parcel_no"))
      and al.editFormConfig().readOnly(al.fields().indexOf("area"))
      and not al.editFormConfig().readOnly(al.fields().indexOf("owner"))
      and not hl.editFormConfig().readOnly(hl.fields().indexOf("parcel_no")),
      "8. editFormConfig readOnly is True on Mona for locked fields, False on host and for owner")

host.set_locked_fields("L1", [])
check(pump(25, lambda: not al.editFormConfig().readOnly(al.fields().indexOf("parcel_no"))
           and not al.editFormConfig().readOnly(al.fields().indexOf("area"))),
      "8. unlocking clears readOnly on Mona")

host.set_locked_fields("L1", ["parcel_no", "area"])
pump(25, lambda: al.editFormConfig().readOnly(al.fields().indexOf("parcel_no"))
     and rules.locked_fields(B.store, B.lookup_member, "L1") == ["area", "parcel_no"])

# ---- 9. Sami locked field refused, owner allowed, host allowed -----------------------------------------------
b_statuses.clear()
old_pno = features.stored_state(hl, fid1)["a"]["parcel_no"]
st_bad = features.stored_state(bl, fid1)
st_bad["a"]["parcel_no"] = 999
B.submit("L1", fid1, st_bad)
check(pump(5, lambda: ("warn", "refused:locked field: parcel_no") in b_statuses
           and B.store.outbox_count() == 0),
      "9. Sami's parcel_no change refused with locked field: parcel_no")
check(features.stored_state(bl, fid1)["a"]["parcel_no"] == old_pno
      and features.stored_state(hl, fid1)["a"]["parcel_no"] == old_pno
      and features.stored_state(al, fid1)["a"]["parcel_no"] == old_pno,
      "9. Sami's copy put back and others untouched")

st_ok = features.stored_state(bl, fid1)
st_ok["a"]["owner"] = "Sami unlocked edit"
B.submit("L1", fid1, st_ok)
check(pump(5, lambda: features.stored_state(hl, fid1)["a"]["owner"] == "Sami unlocked edit"
           and features.stored_state(al, fid1)["a"]["owner"] == "Sami unlocked edit"),
      "9. Sami's owner change accepted")

st_host = features.stored_state(hl, fid1)
st_host["a"]["parcel_no"] = 777
host.submit("L1", fid1, st_host)
check(pump(5, lambda: features.stored_state(al, fid1)["a"]["parcel_no"] == 777
           and features.stored_state(bl, fid1)["a"]["parcel_no"] == 777),
      "9. host's parcel_no change reaches everyone")

# ---- 10. New feature by Sami with area=5 ---------------------------------------------------------------------
nfid = B.next_fid(bl)
B.submit("L1", nfid, {"g": orig_g1, "a": {"owner": "New Sami", "parcel_no": None, "area": 5.0}})
check(pump(5, lambda: features.stored_state(hl, nfid) is not None
           and features.stored_state(bl, nfid) is not None
           and features.stored_state(bl, nfid)["a"]["area"] is None),
      "10. new feature kept and area cleared to NULL on host and Sami")
check(features.stored_state(hl, nfid)["a"]["area"] is None
      and features.stored_state(hl, nfid)["a"]["owner"] == "New Sami",
      "10. host has area=NULL and owner kept")
pump(5, lambda: features.stored_state(al, nfid) is not None)

# 10b. Locked numbering field inside author's granted range is kept on new feature; rank!=0 local edit reverted.
host.store.put(rules.GKEY, [rules.make_grant(host.ident["seed"], host.me_id, "L1", "parcel_no", B.me_id, 500, 505)])
host.hub._settings = (0.0, {})
nfid2 = B.next_fid(bl)
B.submit("L1", nfid2, {"g": orig_g1, "a": {"owner": "New Sami 2", "parcel_no": 502, "area": 5.0}})
check(pump(5, lambda: features.stored_state(hl, nfid2) is not None
           and features.stored_state(bl, nfid2) is not None),
      "10b. new feature with granted parcel_no arrives")
check(features.stored_state(hl, nfid2)["a"]["parcel_no"] == 502
      and features.stored_state(hl, nfid2)["a"]["area"] is None,
      "10b. granted parcel_no=502 kept while ungranted area=NULL")

orig_rank = host.hub.me["rank"]
host.hub.me["rank"] = 1
try:
    bad_local = features.stored_state(hl, fid1)
    bad_local["a"]["parcel_no"] = 333
    features.write_state(hl, fid1, bad_local)
    lop = host._op("L1", fid1, "put", features.stored_state(hl, fid1), bad_local)
    res_loc = host.hub.local(lop, st_host, bad_local)
    check(res_loc is None and features.stored_state(hl, fid1)["a"]["parcel_no"] == 777
          and any("locked field: parcel_no" in e.get("text", "") for e in (host.store.get("room_errors") or [])),
          "10b. host.hub.local with rank != 0 reverts locked field and logs error")
finally:
    host.hub.me["rank"] = orig_rank
    features.write_state(hl, fid1, st_host)

# ---- 11. Pigeon path -----------------------------------------------------------------------------------------
for n in (A, B):
    n.stop_network()
    n._tick.stop()
    n._pres_timer.stop()
    n.state = "reconnecting"

st_pig = features.stored_state(bl, fid1)
st_pig["a"]["parcel_no"] = 555
B.submit("L1", fid1, st_pig)
rep_up = core.open_data(host, core.make(B)[0])
check("locked field: parcel_no" in rep_up["refused"]
      and features.stored_state(hl, fid1)["a"]["parcel_no"] == 777,
      f"11. pigeon up refuses locked field and leaves host unchanged ({rep_up['refused']})")

core.open_data(B, core.make(host)[0])
check(features.stored_state(bl, fid1)["a"]["parcel_no"] == 777 and B.store.outbox_count() == 0,
      "11. pigeon down puts Sami's copy back and empties his outbox")

# ---- 12. FolderCarrier + DriveSim path -----------------------------------------------------------------------
views = {k: os.path.join(root, "views", k) for k in ("H", "A", "B")}
for p in views.values():
    os.makedirs(p)
sim = DriveSim(views)
rf.create_room_folder(host, views["H"])
host.store.put("folder_carrier", views["H"])
A.store.put("folder_carrier", views["A"])
B.store.put("folder_carrier", views["B"])
sim.step()
hc = rf.FolderCarrier(host, views["H"])
ac = rf.FolderCarrier(A, views["A"])
bc = rf.FolderCarrier(B, views["B"])
hc.sync(force=True)
sim.step()
ac.sync()
bc.sync()

st_drv = features.stored_state(bl, fid1)
st_drv["a"]["parcel_no"] = 888
B.submit("L1", fid1, st_drv)
bc.sync(force=True)
sim.step()
hc.sync(force=True)
check(features.stored_state(hl, fid1)["a"]["parcel_no"] == 777,
      "12. FolderCarrier+DriveSim refuses Sami's locked field change on host")
sim.step()
bc.sync(force=True)
check(features.stored_state(bl, fid1)["a"]["parcel_no"] == 777 and B.store.outbox_count() == 0,
      "12. FolderCarrier+DriveSim puts Sami's copy back and empties his outbox")

# ---- 13. UI checks -------------------------------------------------------------------------------------------
dlg = rules_ui.LocksDialog(host)
check(dlg.edit, "13. LocksDialog editable on owner host")
for i in range(dlg.list.count()):
    it = dlg.list.item(i)
    if it.text() == "owner":
        it.setCheckState(Qt.CheckState.Checked)
        break
dlg.save()
check("owner" in rules.locked_fields(host.store, host.lookup_member, "L1"),
      "13. LocksDialog save() locks the ticked field")
dlg.deleteLater()

dlg_m = rules_ui.LocksDialog(A)
check(not dlg_m.edit, "13. LocksDialog read-only on member")
dlg_m.deleteLater()

od = td.OpenDialog(None)
check(od.rights.findData("data") >= 0, "13. OpenDialog rights combo includes 'data'")
od.deleteLater()

for n in (A, B):
    n.shutdown()
host.shutdown()
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
