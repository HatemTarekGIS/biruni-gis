r"""The room through a shared folder (team/folder.py), off screen: a host and three members join a room over the
network, then the network is cut and every change travels only through one folder (as a OneDrive / NAS / USB
folder would carry it). Nobody is online with anybody else.

Edits of the host and of two members end the same everywhere; a sync service's conflict copies, temp files and a
half-synced file are never opened (the half file is used once it is complete); an altered file and an altered head
are refused; a member removed from the room is no longer heard; once the host compacts its folder, a member that
was away the whole time catches up from the snapshot.

Run:  engine\bin\python-qgis.bat tests\test_room_folder.py
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

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.team import code as codes, features, folder as rf, session  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402

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

# ---- the network is cut for good: only the folder carries ---------------------------------------------------------
for n in (A, B, C):
    n.stop_network()
    n._tick.stop()
    n._pres_timer.stop()
    n.state = "reconnecting"

shared = os.path.join(root, "OneDrive - Office", "Projects")
os.makedirs(shared)
rf.create_room_folder(host, shared)
hc, ac, bc = (rf.FolderCarrier(n, shared) for n in (host, A, B))


def owner(lyr, fid):
    return features.stored_state(lyr, fid)["a"]["owner"]


def set_owner(node, lyr, fid, value):
    st = features.stored_state(lyr, fid)
    st["a"]["owner"] = value
    check(node.submit("L1", fid, st), f"{node.ident['name'] if node is not host else 'the host'} sets parcel {fid} "
                                      f"to {value}")


fids = {f["owner"]: f.id() for f in hl.getFeatures()}
f1, f2, f3, f4 = (fids[f"owner {i}"] for i in (1, 2, 3, 4))

# ---- 1. three people, three edits, nobody online together -------------------------------------------------------
set_owner(A, al, f1, "Mona")
set_owner(B, bl, f2, "Sami")
set_owner(host, hl, f3, "Office")
ac.sync()
bc.sync()
rep = hc.sync()
check(rep is not None and rep["in"] == 2 and rep["devices"] == 2, f"the host decided both members' changes from the "
                                                                    f"folder ({rep and {k: rep[k] for k in ('in', 'devices', 'refused')}})")
ac.sync()
bc.sync()
want = {f1: "Mona", f2: "Sami", f3: "Office", f4: "owner 4"}
same = all(owner(lyr, fid) == v for lyr in (hl, al, bl) for fid, v in want.items())
check(same, "the host's map and both members' maps end the same: " + str([[owner(lyr, f) for f in want] for lyr in
                                                                         (hl, al, bl)]))
check(A.since == B.since == host.store.head(), f"everyone is at the head of the stream ({A.since}, {B.since}, "
                                                 f"{host.store.head()})")
check(A.store.outbox_count() == 0 and B.store.outbox_count() == 0, "the members' outboxes are empty")
names = sorted(x for x in os.listdir(rf.room_dir(shared, host)) if x.startswith("d-"))
check(names == sorted("d-" + x for x in ("a" * 32, "b" * 32, host.me_id)), f"one folder per computer ({names})")
ahead = sorted(x for x in os.listdir(ac.my_dir()) if rf._HEAD_RE.match(x))[-1]
with open(os.path.join(ac.my_dir(), ahead), "rb") as f:
    raw = f.read()
check(b"Mona" not in raw and all(b"Mona" not in open(os.path.join(ac.my_dir(), x), "rb").read()
                                 for x in os.listdir(ac.my_dir())), "the storage provider reads nothing (sealed)")

# ---- 2. what sync services leave behind is never opened -------------------------------------------------------------
adir = ac.my_dir()
anyfile = next(x for x in os.listdir(adir) if x.endswith(rf.EXT))
shutil.copy(os.path.join(adir, anyfile), os.path.join(adir, anyfile[:-4] + " (1)" + rf.EXT))
shutil.copy(os.path.join(adir, ahead), os.path.join(adir, ahead[:-4] + " (1).bph"))
with open(os.path.join(adir, "000099-0123456789ab.bpf.tmp"), "wb") as f:
    f.write(b"half")
set_owner(A, al, f4, "Mona again")
ac.sync(force=True)
newest = sorted(x for x in os.listdir(adir) if rf._NAME.match(x))[-1]
full = open(os.path.join(adir, newest), "rb").read()
with open(os.path.join(adir, newest), "wb") as f:
    f.write(full[:len(full) // 2])            # the sync service has brought half of it so far
rep = hc.sync()
check(rep["pending"] >= 1 and owner(hl, f4) == "owner 4", f"a half-synced file waits and is not used "
                                                           f"(pending {rep['pending']})")
check(rep["skipped"] >= 3, f"conflict copies and temp files are never opened (skipped {rep['skipped']})")
with open(os.path.join(adir, newest), "wb") as f:
    f.write(full)
rep = hc.sync(force=True)
check(owner(hl, f4) == "Mona again", f"once complete, the file is used ({rep['in']} in)")

# ---- 3. altered: refused --------------------------------------------------------------------------------------------
set_owner(B, bl, f2, "Sami again")
bc.sync(force=True)
bdir = bc.my_dir()
newest_b = sorted(x for x in os.listdir(bdir) if rf._NAME.match(x))[-1]
good = open(os.path.join(bdir, newest_b), "rb").read()
bad = bytearray(good)
bad[len(bad) // 2] ^= 1
open(os.path.join(bdir, newest_b), "wb").write(bytes(bad))
rep = hc.sync()
check(owner(hl, f2) == "Sami", f"an altered file is never used (pending {rep['pending']})")
open(os.path.join(bdir, newest_b), "wb").write(good)
heads_b = {h: open(os.path.join(bdir, h), "rb").read() for h in os.listdir(bdir) if rf._HEAD_RE.match(h)}
bhead = sorted(heads_b)[-1]
for h in list(heads_b):
    if h != bhead:
        os.remove(os.path.join(bdir, h))
head_good = heads_b[bhead]
hb = bytearray(head_good)
hb[-20] ^= 1
open(os.path.join(bdir, bhead), "wb").write(bytes(hb))
rep = hc.sync()
check(owner(hl, f2) == "Sami" and any("altered" in r for r in rep["refused"]),
      f"an altered head rejects that whole folder ({rep['refused']})")
ahead_new = sorted(x for x in os.listdir(adir) if rf._HEAD_RE.match(x))[-1]
os.remove(os.path.join(bdir, bhead))
shutil.copy(os.path.join(adir, ahead_new), os.path.join(bdir, ahead_new))    # Mona's valid head in Sami's folder
rep = hc.sync()
check(owner(hl, f2) == "Sami" and any("someone else" in r for r in rep["refused"]),
      f"a head moved into another member's folder is refused ({rep['refused']})")
os.remove(os.path.join(bdir, ahead_new))
for h, raw_h in heads_b.items():
    open(os.path.join(bdir, h), "wb").write(raw_h)
rep = hc.sync(force=True)
check(owner(hl, f2) == "Sami again", "the intact files are used again")

# ---- 4. a member removed from the room is no longer heard ----------------------------------------------------------
host.hub.remove_member("b" * 32)
check(any(m["id"] == "b" * 32 and m.get("state") == "out" for m in host.hub.roster()), "Sami is removed")
set_owner(B, bl, f3, "Sami after removal")
bc.sync(force=True)
rep = hc.sync(force=True)
check(owner(hl, f3) == "Office" and any("not a member" in r for r in rep["refused"]),
      f"a removed member's new files are ignored ({rep['refused']})")
ac.sync()
check(owner(al, f3) == "Office" and owner(al, f2) == "Sami again", "Mona never gets Sami's later change")

# ---- 5. compaction, and a member away the whole time catches up from the snapshot ----------------------------------
for i in range(5):
    set_owner(host, hl, f1, f"Office {i}")
    hc.sync(force=True)
hc.keep_days = 0
set_owner(host, hl, f4, "Office last")
hc.sync(force=True)
hdir = hc.my_dir()
left = sorted(os.listdir(hdir))
check(any(rf._SNAP_RE.match(x) for x in left) and sum(1 for x in left if rf._NAME.match(x)) <= 3,
      f"the host compacted its folder ({left})")
cc = rf.FolderCarrier(C, shared)
pump(0.1)
cc.sync()
cc.sync()
want = {fid: owner(hl, fid) for fid in (f1, f2, f3, f4)}
check(all(owner(cl, fid) == v for fid, v in want.items()) and C.since == host.store.head(),
      f"Hala, away the whole time, caught up from the snapshot ({[owner(cl, f) for f in want]} / {list(want.values())}, "
      f"{C.since}/{host.store.head()})")

# ---- 6. the room remembers its folder ------------------------------------------------------------------------------
c = rf.use_folder(A, shared)
check(rf.current() is c and A.store.get("folder_carrier") == shared and c.timer is not None,
      "a room set to use a folder carries through it every few seconds")
rf.on_room(None)
check(rf.current() is None and c.timer is None, "leaving the room stops the carrier")
rf.on_room(A)
check(rf.current() is not None and rf.current().folder == shared, "opening the room again starts it again")
rf.use_folder(A, None)
check(rf.current() is None and not A.store.get("folder_carrier"), "the folder can be given up")

for n in (A, B, C):
    n.shutdown()
host.shutdown()
print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
