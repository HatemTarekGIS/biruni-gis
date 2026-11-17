r"""Host right tests: default off, non-owner denied, rogue marker ignored with refused_host,
owner grant + takeover + replay + convergence, and returning owner takes back the room.

Run:  engine\bin\python-qgis.bat tests\test_room_host_right.py
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

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.team import code as codes, features, folder as rf, rules, session  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402
from drivesim import DriveSim  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


root = tempfile.mkdtemp()
office = os.path.join(root, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "Parcels", "memory")
for i in range(4):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"owner {i + 1}"])
    x = i * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 10} 0,{x + 10} 10,{x} 10,{x} 0))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
prj = QgsProject.instance()
prj.addMapLayer(hl)
prj.setCrs(hl.crs())
prj.write(os.path.join(office, "office.qgz"))

A = session.open_room([hl], "Host Right", approve=False)
code = session.room_code(A)
real_identity = session.identity


def join(ident, where):
    session.identity = lambda: dict(ident)
    box = {}
    t = threading.Thread(target=lambda: box.update(session.join_steps(code, os.path.join(root, where),
                                                                      lambda *a: None)))
    t.start()
    pump(60, lambda: not t.is_alive())
    session.identity = real_identity
    snap = box["snap"]
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels",
                         ident["name"], "ogr")
    st = Store(os.path.join(box["folder"], ".biruni", "team", "room.sqlite"))
    st.put("layers", box["hello"]["room"]["layers"])
    st.put("since", snap["head"])
    st.put("block", box["hello"]["block"])
    st.put("sid", box["hello"]["room"].get("sid"))
    st.put("roster", box["hello"]["room"].get("members") or [])
    st.set_versions([tuple(v) for v in snap["versions"]])
    n = Node(st, codes.RoomKeys(code), dict(ident))
    n.roster = st.get("roster")
    n.attach_layers({"L1": lyr})
    n.start_member(box["address"], box["hello"]["member"])
    return n, lyr


B, bl = join({"id": "b" * 32, "seed": "1" * 64, "name": "Badr"}, "badr")
C, cl = join({"id": "c" * 32, "seed": "2" * 64, "name": "Celine"}, "celine")
pump(8, lambda: len(A.hub.roster()) == 3 and len(B.roster) == 3 and len(C.roster) == 3)

for n in (B, C):
    n.stop_network()
    n._tick.stop()
    n._pres_timer.stop()
    n.state = "reconnecting"

views = {n: os.path.join(root, "views", n) for n in ("A", "B", "C")}
for p in views.values():
    os.makedirs(p)
sim = DriveSim(views)
rf.create_room_folder(A, views["A"])
A.store.put("folder_carrier", views["A"])
B.store.put("folder_carrier", views["B"])
C.store.put("folder_carrier", views["C"])
sim.step()
ac = rf.FolderCarrier(A, views["A"])
bc = rf.FolderCarrier(B, views["B"])
cc = rf.FolderCarrier(C, views["C"])
# Ensure everyone has the full 3-member roster via the folder.
ac.sync(force=True)
sim.step()
bc.sync()
cc.sync()

# 1. Default off.
check(not B.can_make_host() and B.make_me_host(bc) == "not-allowed",
      "default off: make_me_host() returns 'not-allowed'")

# 2. set_host_right by non-owner raises; forged grant ignored.
raised = False
try:
    B.set_host_right(C.me_id, True)
except PermissionError:
    raised = True
check(raised, "set_host_right by non-owner raises PermissionError")

forged = rules.make_host_grant(B.ident["seed"], B.me_id, C.me_id, True)
tmp_st = rf._MemStore({rules.HKEY: [forged]})
check(not rules.may_host(tmp_st, B.lookup_member, C.me_id), "host grant signed by non-owner is ignored")

# 3. Forced start_host by unallowed C: others ignore marker and set refused_host.
C.start_host(3)
cc.write_marker(at=C.since, prev_sid=C.store.get("sid"))
sim.step()
ac.sync()
bc.sync()
check(A.epoch == 1 and B.epoch == 1 and ac.status.get("refused_host") == C.me_id
      and bc.status.get("refused_host") == C.me_id,
      f"forced start_host by unallowed member ignored; refused_host={bc.status.get('refused_host')}")
# Restore C as member at epoch 1 so C keeps participating.
C.stop_network()
C.role = "member"
C.store.put("role", "member")
C.epoch = 1
C.store.put("epoch", 1)
C.store.put("sid", A.store.get("sid"))

# 4. Owner grants host right to B, syncs, then A goes offline while making an unshared edit.
A.set_host_right(B.me_id, True)
ac.sync(force=True)
sim.step()
bc.sync()
cc.sync()
check(B.can_make_host() and not C.can_make_host(), "owner grant gives B host right (C still False)")

# A makes an edit that has NOT reached B yet, and C has an edit in outbox.
st1 = features.stored_state(hl, 1)
st1["a"]["owner"] = "A before takeover"
A.submit("L1", 1, st1)

st2 = features.stored_state(cl, 2)
st2["a"]["owner"] = "C before takeover"
C.submit("L1", 2, st2)

# B takes over!
res_take = B.make_me_host(bc)
check(res_take == "ok" and B.role == "host" and B.epoch == 2, f"B took over via make_me_host ({res_take})")
sim.step()

# C syncs and rebases onto B, sending C's outbox.
cc.sync(force=True)
check(C.epoch == 2 and C.role == "member", f"C accepted B's takeover marker (epoch={C.epoch})")
sim.step()

# Returning owner A syncs: becomes member, writes replay+outbox up pack, rebases onto B.
ac.sync(force=True)
check(A.role == "member" and A.hub is None and A.epoch == 2,
      f"returning owner A stepped down to member (role={A.role}, epoch={A.epoch})")
sim.step()

# B applies A's replay and C's outbox, then writes down packs; A and C sync to converge.
bc.sync(force=True)
sim.step()
ac.sync(force=True)
cc.sync(force=True)
check(all(features.stored_state(lyr, 1)["a"]["owner"] == "A before takeover" for lyr in (hl, bl, cl))
      and all(features.stored_state(lyr, 2)["a"]["owner"] == "C before takeover" for lyr in (hl, bl, cl)),
      "replayed entries and outbox converge across A, B, and C after takeover")

# 5. Returning owner A (rank 0) takes the room back.
check(A.can_make_host(), "returning owner A (rank 0) has can_make_host() == True")
res_back = A.make_me_host(ac)
check(res_back == "ok" and A.role == "host" and A.epoch == 3, f"owner took room back (epoch={A.epoch})")
sim.step()
bc.sync(force=True)
cc.sync(force=True)
check(B.role == "member" and B.hub is None and B.epoch == 3 and C.epoch == 3,
      "B stepped down to member and B+C followed owner back to epoch 3")

st3 = features.stored_state(bl, 3)
st3["a"]["owner"] = "after take-back"
B.submit("L1", 3, st3)
bc.sync(force=True)
sim.step()
ac.sync(force=True)
sim.step()
cc.sync(force=True)
check(all(features.stored_state(lyr, 3)["a"]["owner"] == "after take-back" for lyr in (hl, bl, cl)),
      "edits converge after owner take-back")

for n in (A, B, C):
    n.shutdown()
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
