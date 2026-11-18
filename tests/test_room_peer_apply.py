r"""Tests for direct peer apply (LWW + HLC) under DriveSim.

Run:  engine\bin\python-qgis.bat tests\test_room_peer_apply.py
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

from biruni.pigeon import core as pigeon  # noqa: E402
from biruni.secure import crypto  # noqa: E402
from biruni.team import area, code as codes, features, folder as rf, peer, provenance, rules, session  # noqa: E402
from biruni.team.hub import sign_body  # noqa: E402
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
hostdir = os.path.join(root, "office")
os.makedirs(hostdir)
gpkg = os.path.join(hostdir, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string&field=area:double", "Parcels", "memory")
for i in range(4):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"owner {i + 1}", 100.0])
    x = i * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 10} 0,{x + 10} 10,{x} 10,{x} 0))"))
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
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", "Parcels",
                         "ogr")
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
C, cl = join({"id": "c" * 32, "seed": "3" * 64, "name": "Hala"}, "hala")
pump(3, lambda: len(host.hub.roster()) == 4)
pump(8, lambda: all(len(n.roster) == 4 for n in (A, B, C)))

for n in (A, B, C):
    n.stop_network()
    n._tick.stop()
    n._pres_timer.stop()
    n.state = "reconnecting"

clocks = {"H": 1_700_000_000.0, "A": 1_700_000_000.0, "B": 1_700_000_000.0, "C": 1_700_000_000.0}
host.wall = lambda: clocks["H"]
A.wall = lambda: clocks["A"]
B.wall = lambda: clocks["B"]
C.wall = lambda: clocks["C"]


def tick_all(dt=1.0):
    for k in clocks:
        clocks[k] += dt


fids = {f["owner"]: f.id() for f in hl.getFeatures()}
f1, f2, f3, f4 = (fids[f"owner {i}"] for i in (1, 2, 3, 4))


def owner(lyr, fid):
    st = features.stored_state(lyr, fid)
    return st["a"]["owner"] if st else None


def area_val(lyr, fid):
    st = features.stored_state(lyr, fid)
    return st["a"]["area"] if st else None


def set_fields(node, lyr, fid, **vals):
    st = features.stored_state(lyr, fid)
    st["a"].update(vals)
    return node.submit("L1", fid, st)


def snap_all(lyr):
    ids = sorted(f.id() for f in lyr.dataProvider().getFeatures())
    return {fid: features.stored_state(lyr, fid) for fid in ids}


views = {k: os.path.join(root, "views", k) for k in ("H", "A", "B", "C")}
for v in views.values():
    os.makedirs(v)
sim = DriveSim(views)
rf.create_room_folder(host, views["H"])
hc = rf.FolderCarrier(host, views["H"])
ac = rf.FolderCarrier(A, views["A"])
bc = rf.FolderCarrier(B, views["B"])
cc = rf.FolderCarrier(C, views["C"])

host.set_peer_apply(True)
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
check(all(rules.peer_apply(n.store, n.lookup_member) for n in (A, B, C)),
      "0. peer_apply setting reached A, B and C")

# ---- 1. Host offline: A edits owner, B has it in one step+sync, no new layers ---------------------------------
layers_before = set(prj.mapLayers())
tick_all(1)
set_fields(A, al, f1, owner="Mona 1")
ac.sync(force=True)
sim.step()
bc.sync(force=True)
check(owner(bl, f1) == "Mona 1" and owner(hl, f1) == "owner 1" and set(prj.mapLayers()) == layers_before,
      "1. host offline: B sees A's edit after one step+sync, no new layers")

# ---- 2. Older A vs newer B on same field; both arrival orders --------------------------------------------------
tick_all(10)
set_fields(A, al, f2, owner="A older")
clocks["B"] += 5
set_fields(B, bl, f2, owner="B newer")
ac.sync(force=True)
bc.sync(force=True)
sim.step()
cc.sync(force=True)
ac.sync(force=True)
bc.sync(force=True)
check(owner(cl, f2) == owner(al, f2) == owner(bl, f2) == "B newer",
      "2a. newer stamp B wins everywhere")

sim.delay("B", "C", 2)
tick_all(10)
clocks["A"] = clocks["B"] = clocks["C"] = clocks["H"]
set_fields(A, al, f3, owner="A older rev")
clocks["B"] += 5
set_fields(B, bl, f3, owner="B newer rev")
ac.sync(force=True)
bc.sync(force=True)
sim.step()
cc.sync(force=True)
check(owner(cl, f3) == "A older rev", "2b. C temporarily sees delayed A first")
sim.step()
sim.step()
cc.sync(force=True)
ac.sync(force=True)
bc.sync(force=True)
sim.delay("B", "C", 0)
check(owner(al, f3) == owner(bl, f3) == owner(cl, f3) == "B newer rev",
      "2b. reversed delivery order still converges to B")

# ---- 3. Different fields: both edits kept everywhere -----------------------------------------------------------
tick_all(10)
clocks["A"] = clocks["B"] = clocks["C"] = clocks["H"]
set_fields(A, al, f4, owner="Mona diff")
clocks["B"] += 2
set_fields(B, bl, f4, area=555.0)
ac.sync(force=True)
bc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
check(all(owner(l, f4) == "Mona diff" and area_val(l, f4) == 555.0 for l in (al, bl, cl)),
      "3. different fields kept everywhere")

# ---- 4. Delete vs newer edit, and edit vs newer delete, both arrival orders ------------------------------------
# 4a. Delete (t+10) then newer edit (t+20): delete arrives first at C
tick_all(10)
clocks["A"] = clocks["B"] = clocks["C"] = clocks["H"]
A.submit("L1", f4, None)
clocks["B"] += 5
set_fields(B, bl, f4, owner="Revived 4a")
sim.delay("B", "C", 1)
ac.sync(force=True)
bc.sync(force=True)
sim.step()
cc.sync(force=True)
check(features.stored_state(cl, f4) is None, "4a. C receives older delete first")
sim.step()
cc.sync(force=True)
ac.sync(force=True)
bc.sync(force=True)
sim.delay("B", "C", 0)
check(all(owner(l, f4) == "Revived 4a" and area_val(l, f4) == 555.0 for l in (al, bl, cl)),
      "4a. newer edit revives feature with its last values (delete first)")

# 4a reversed: edit arrives first at C, older delete arrives second
tick_all(10)
clocks["A"] = clocks["B"] = clocks["C"] = clocks["H"]
A.submit("L1", f3, None)
clocks["B"] += 5
set_fields(B, bl, f3, owner="Revived 4a rev")
sim.delay("A", "C", 1)
ac.sync(force=True)
bc.sync(force=True)
sim.step()
cc.sync(force=True)
sim.step()
cc.sync(force=True)
ac.sync(force=True)
bc.sync(force=True)
sim.delay("A", "C", 0)
check(all(owner(l, f3) == "Revived 4a rev" for l in (al, bl, cl)),
      "4a. newer edit wins over older delete (edit first)")

# 4b. Edit (t+10) then newer delete (t+20): both arrival orders
tick_all(10)
clocks["A"] = clocks["B"] = clocks["C"] = clocks["H"]
set_fields(A, al, f4, owner="Before del 4b")
clocks["B"] += 5
B.submit("L1", f4, None)
sim.delay("B", "C", 1)
ac.sync(force=True)
bc.sync(force=True)
sim.step()
cc.sync(force=True)
sim.step()
cc.sync(force=True)
ac.sync(force=True)
bc.sync(force=True)
sim.delay("B", "C", 0)
check(all(features.stored_state(l, f4) is None for l in (al, bl, cl)),
      "4b. newer delete removes feature (edit first)")

tick_all(10)
clocks["A"] = clocks["B"] = clocks["C"] = clocks["H"]
set_fields(A, al, f3, owner="Before del 4b rev")
clocks["B"] += 5
B.submit("L1", f3, None)
sim.delay("A", "C", 1)
ac.sync(force=True)
bc.sync(force=True)
sim.step()
cc.sync(force=True)
sim.step()
cc.sync(force=True)
ac.sync(force=True)
bc.sync(force=True)
sim.delay("A", "C", 0)
check(all(features.stored_state(l, f3) is None for l in (al, bl, cl)),
      "4b. newer delete removes feature (delete first)")

# ---- 5. A's clock +1 h: B holds op (clock_ahead, B's edit stands, hlc <= wall+SKEW), advance -> converges ------
base_clock = max(clocks.values()) + 10
for k in clocks:
    clocks[k] = base_clock
clocks["A"] = base_clock + 3600
set_fields(A, al, f2, owner="Future A")
ac.sync(force=True)
sim.step()
bc.sync(force=True)
hlc_b = (B.store.get("hlc") or [0, 0])[0]
check(bc.status["clock_ahead"] and bc.status["peer_waiting"] >= 1
      and owner(bl, f2) != "Future A"
      and hlc_b <= int(clocks["B"] * 1000) + peer.SKEW_MS,
      f"5a. B holds future op (clock_ahead={bc.status['clock_ahead']}, hlc_b={hlc_b})")

clocks["B"] += 10
set_fields(B, bl, f2, owner="B meanwhile")
bc.sync(force=True)
check(owner(bl, f2) == "B meanwhile", "5b. B's later edit stands while A's future op waits")

for k in ("H", "B", "C"):
    clocks[k] = base_clock + 3601
bc.sync(force=True)
sim.step()
cc.sync(force=True)
ac.sync(force=True)
check(not bc.status["clock_ahead"] and bc.status["peer_waiting"] == 0
      and owner(al, f2) == owner(bl, f2) == owner(cl, f2) == "Future A",
      "5c. after B's wall advances, future op applies and everyone converges")

# ---- 6. Forged ops in outbox: view-only, outside work area, geometry under data right ---------------------------
def forge_into_outbox(node, lyr, fid, state):
    cur = features.stored_state(lyr, fid)
    op = node._op("L1", fid, "put", cur, state)
    op["sig"] = crypto.sign(bytes.fromhex(node.ident["seed"]), sign_body(op)).hex()
    node.store.queue(op)


# 6a. view-only member C
host.hub.set_rights(C.me_id, "L1", "view")
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
st_f1 = features.stored_state(cl, f1)
forge_into_outbox(C, cl, f1, dict(st_f1, a=dict(st_f1["a"], owner="Forged view")))
cc.sync(force=True)
sim.step()
bc.sync(force=True)
hc.sync(force=True)
check(owner(bl, f1) != "Forged view" and owner(hl, f1) != "Forged view",
      "6a. view-only forged op refused by B and host")

# 6b. outside work area (area covers f1 only; forged op touches f2)
host.hub.set_rights(C.me_id, "L1", "edit")
host.hub.set_area(C.me_id, area.make([hl.getFeature(f1).geometry()], hl.crs()))
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
st_f2 = features.stored_state(cl, f2)
forge_into_outbox(C, cl, f2, dict(st_f2, a=dict(st_f2["a"], owner="Forged outside")))
cc.sync(force=True)
sim.step()
bc.sync(force=True)
hc.sync(force=True)
check(owner(bl, f2) != "Forged outside" and owner(hl, f2) != "Forged outside",
      "6b. outside-area forged op refused by B and host")

# 6c. geometry change by "data" member
host.hub.set_area(C.me_id, None)
host.hub.set_rights(C.me_id, "L1", "data")
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
st_f2_c = features.stored_state(cl, f2)
moved_g = bytes(QgsGeometry.fromWkt("POLYGON((5 5,15 5,15 15,5 15,5 5))").asWkb()).hex()
orig_g2 = st_f2_c["g"]
forge_into_outbox(C, cl, f2, {"g": moved_g, "a": dict(st_f2_c["a"])})
cc.sync(force=True)
sim.step()
bc.sync(force=True)
hc.sync(force=True)
check(features.stored_state(bl, f2)["g"] == orig_g2 and features.stored_state(hl, f2)["g"] == orig_g2,
      "6c. geometry change by data member refused by B and host")
host.hub.set_rights(C.me_id, "L1", "edit")
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)

# ---- 7. Right revoked at host while B's roster is stale -> B applies, then host corrects B --------------------
tick_all(5)
before_stale = owner(hl, f2)
host.hub.set_rights(A.me_id, "L1", "view")  # NOT synced to B yet
set_fields(A, al, f2, owner="A revoked")
ac.sync(force=True)
sim.step()
bc.sync(force=True)
check(owner(bl, f2) == "A revoked", "7a. with stale roster, B temporarily applies revoked A's op")
hc.sync(force=True)
sim.step()
bc.sync(force=True)
ac.sync(force=True)
cc.sync(force=True)
check(owner(hl, f2) == before_stale and owner(bl, f2) == before_stale and owner(al, f2) == before_stale,
      "7b. host correction brings B and A back to host's value")
host.hub.set_rights(A.me_id, "L1", "edit")
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)

# ---- 8. Host online: all states equal, second round changes nothing, outboxes empty ----------------------------
for _ in range(2):
    hc.sync(force=True)
    sim.step()
    ac.sync(force=True)
    bc.sync(force=True)
    cc.sync(force=True)
    sim.step()
    hc.sync(force=True)
    sim.step()
    ac.sync(force=True)
    bc.sync(force=True)
    cc.sync(force=True)

s_h, s_a, s_b, s_c = snap_all(hl), snap_all(al), snap_all(bl), snap_all(cl)
check(s_h == s_a == s_b == s_c, "8a. host, A, B and C layer states are identical")
# Second full round changes nothing (no flip-flop)
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
check(snap_all(hl) == s_h and snap_all(al) == s_a and snap_all(bl) == s_b and snap_all(cl) == s_c,
      "8b. second full round changes nothing (no flip-flop)")
check(all(n.store.outbox_count() == 0 for n in (host, A, B, C)),
      "8c. all outboxes are empty")

# ---- 9. Every op in B's up packs has author == B ---------------------------------------------------------------
all_b_ops_ok = True
pack_ops_count = 0
for fn in sorted(os.listdir(bc.my_dir())):
    if rf._NAME.match(fn):
        with open(os.path.join(bc.my_dir(), fn), "rb") as fp:
            _room, blobs = pigeon._envelope(fp.read())
        for blob in blobs:
            body = pigeon.unseal(B, blob)
            for op in body.get("ops") or []:
                pack_ops_count += 1
                if op.get("author") != B.me_id:
                    all_b_ops_ok = False
check(all_b_ops_ok, f"9. every op in B's up packs has author == B ({pack_ops_count} checked)")

# ---- 10. History on B names A for A's field --------------------------------------------------------------------
tick_all(5)
set_fields(A, al, f1, owner="Mona prov")
ac.sync(force=True)
sim.step()
bc.sync(force=True)
hist_b = B.store.feature_history("L1", f1)
prov_b = provenance.fields(hist_b)
check(bool(hist_b) and owner(bl, f1) == "Mona prov"
      and prov_b.get("owner", {}).get("name") == "Mona"
      and prov_b.get("owner", {}).get("author") == A.me_id
      and prov_b.get("owner", {}).get("state", {}).get("a", {}).get("owner") == "Mona prov",
      f"10. provenance on B names Mona for Mona prov ({owner(bl, f1)}, {prov_b.get('owner')})")
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)

# ---- 11. peer_apply off: B does not see A's edit until host syncs ----------------------------------------------
host.set_peer_apply(False)
hc.sync(force=True)
sim.step()
ac.sync(force=True)
bc.sync(force=True)
cc.sync(force=True)
check(not rules.peer_apply(B.store, B.lookup_member), "11a. peer_apply off reached B")
set_fields(A, al, f1, owner="No peer edit")
ac.sync(force=True)
sim.step()
bc.sync(force=True)
check(owner(bl, f1) == "Mona prov", "11b. with peer_apply off, B does not see A's edit while host is offline")
hc.sync(force=True)
sim.step()
bc.sync(force=True)
check(owner(bl, f1) == "No peer edit", "11c. once host syncs, B sees A's edit")

# ---- 12. With carrier.start(): every rf._read call is on thread 'biruni-room-folder' ---------------------------
threads = []
real_read = rf._read


def spy_read(path):
    threads.append(threading.current_thread().name)
    return real_read(path)


rf._read = spy_read
try:
    bc.start(interval=0.2)
    pump(2.0, lambda: len(threads) >= 1)
finally:
    bc.stop()
    rf._read = real_read
check(len(threads) >= 1 and all(t == "biruni-room-folder" for t in threads),
      f"12. all rf._read calls ran on biruni-room-folder ({len(threads)} calls: {set(threads)})")

for n in (A, B, C):
    n.shutdown()
host.shutdown()
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
