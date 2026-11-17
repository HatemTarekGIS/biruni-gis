r"""Tests for team/folder.py under DriveSim: delays, faults, trash, unmounted drives, expiry,
rejoin-needed, and background scan thread.

Run:  engine\bin\python-qgis.bat tests\test_room_drive_sim.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest.mock as mock

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

from biruni.field_photos import drive  # noqa: E402
from biruni.pigeon import core as pigeon  # noqa: E402
from biruni.team import code as codes, features, folder as rf, session  # noqa: E402
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

fids = {f["owner"]: f.id() for f in hl.getFeatures()}
f1, f2, f3, f4 = (fids[f"owner {i}"] for i in (1, 2, 3, 4))


def owner(lyr, fid):
    return features.stored_state(lyr, fid)["a"]["owner"]


def set_owner(node, lyr, fid, value):
    st = features.stored_state(lyr, fid)
    st["a"]["owner"] = value
    return node.submit("L1", fid, st)


v_host = os.path.join(root, "views", "host")
v_a = os.path.join(root, "views", "A")
v_b = os.path.join(root, "views", "B")
v_c = os.path.join(root, "views", "C")
for v in (v_host, v_a, v_b, v_c):
    os.makedirs(v)

sim = DriveSim({"host": v_host, "A": v_a, "B": v_b, "C": v_c})

# ---- 1. Member pointed at a folder with no room gets FolderError, nothing created ------------------------------
empty = os.path.join(root, "empty_folder")
os.makedirs(empty)
got_err = None
try:
    rf.use_folder(A, empty)
except rf.FolderError as e:
    got_err = e.args[0] if e.args else None
check(got_err == "no-room-here" and os.listdir(empty) == [] and not A.store.get("folder_carrier"),
      f"1. member on empty folder gets FolderError('no-room-here') and creates nothing ({got_err})")

# ---- 2. Member sync before host marker arrives writes nothing -------------------------------------------------
rf.create_room_folder(host, v_host)
hc = rf.FolderCarrier(host, v_host)
ac = rf.FolderCarrier(A, v_a)
bc = rf.FolderCarrier(B, v_b)
set_owner(A, al, f1, "Mona early")
ac.sync()
check(os.listdir(v_a) == [], "2. member sync before host marker arrives writes nothing")
sim.step()

# ---- 3. Edits converge through delayed steps -----------------------------------------------------------------
sim.delay("A", "host", 2)
sim.delay("host", "B", 2)
set_owner(B, bl, f2, "Sami 1")
set_owner(host, hl, f3, "Office 1")
ac.sync(force=True)
bc.sync(force=True)
hc.sync(force=True)
for _ in range(7):
    sim.step()
    hc.sync(force=True)
    ac.sync(force=True)
    bc.sync(force=True)
sim.delay("A", "host", 0)
sim.delay("host", "B", 0)
check(owner(hl, f1) == owner(al, f1) == owner(bl, f1) == "Mona early"
      and owner(hl, f2) == owner(al, f2) == owner(bl, f2) == "Sami 1"
      and owner(hl, f3) == owner(al, f3) == owner(bl, f3) == "Office 1",
      "3a. edits converge through delayed steps")
check(A.since == B.since == host.store.head() and A.store.outbox_count() == 0 and B.store.outbox_count() == 0,
      f"3b. everyone at head ({A.since}/{B.since}/{host.store.head()}) and outboxes empty")

# ---- 4. Head before pack stays pending, then applied; pack before head ignored, then applied -----------------
sim.heads_first(True)
set_owner(B, bl, f2, "Sami heads-first")
bc.sync(force=True)
sim.step()
rep = hc.sync()
check(rep["pending"] >= 1 and owner(hl, f2) == "Sami 1",
      f"4a. head before pack stays pending ({rep['pending']})")
sim.step()
rep = hc.sync(force=True)
check(owner(hl, f2) == "Sami heads-first", "4b. once pack arrives, edit is applied")
sim.heads_first(False)

sim.hold(r"\.bph$")
set_owner(A, al, f1, "Mona pack-first")
ac.sync(force=True)
sim.step()
hc.sync()
check(owner(hl, f1) == "Mona early", "4c. pack before head is ignored")
sim.hold(None)
sim.step()
hc.sync(force=True)
check(owner(hl, f1) == "Mona pack-first", "4d. once head arrives, edit is applied")

# ---- 5. Truncated file pending then complete; unreadable file counted then used ------------------------------
sim.truncate(r"\.bpf$")
set_owner(A, al, f4, "Mona trunc")
ac.sync(force=True)
sim.step()
rep = hc.sync()
check(rep["pending"] >= 1 and owner(hl, f4) == "owner 4",
      f"5a. truncated file stays pending ({rep['pending']})")
sim.truncate(None)
sim.step()
hc.sync(force=True)
check(owner(hl, f4) == "Mona trunc", "5b. truncated file used once complete")

b_next_seq = 1 + max([int(k[:6]) for k in bc._my_files() if rf._NAME.match(k)] + [0])
sim.unreadable(rf"d-bbbb.*{b_next_seq:06d}-.*\.bpf$")
set_owner(B, bl, f3, "Sami unreadable")
bc.sync(force=True)
sim.step()
rep = hc.sync()
check(hc.status["unreadable"] >= 1 and owner(hl, f3) == "Office 1",
      f"5c. unreadable file is counted ({hc.status['unreadable']}) and not applied")
sim.unreadable(None)
hc.sync(force=True)
check(owner(hl, f3) == "Sami unreadable", "5d. unreadable file used once readable")

# ---- 6. Duplicated head head-NNNNNN (1).bph with stale head present ------------------------------------------
b_head_next = int(B.store.get("folder_head_n") or 0) + 1
sim.duplicate(rf"d-bbbb.*head-{b_head_next:06d}\.bph$")
set_owner(B, bl, f2, "Sami dup-head")
bc.sync(force=True)
sim.step()
hc.sync()
b_in_host = os.path.join(rf.room_dir(v_host, host), "d-" + B.me_id)
dup_file = os.path.join(b_in_host, f"head-{b_head_next:06d} (1).bph")
stale_file = os.path.join(b_in_host, f"head-{b_head_next - 1:06d}.bph")
check(os.path.isfile(dup_file) and os.path.isfile(stale_file) and owner(hl, f2) == "Sami heads-first",
      f"6a. duplicated head ignored with stale head present ({os.path.basename(dup_file)})")
sim.duplicate(None)
bc.sync(force=True)
# Force a new head from B by touching a new edit so head-(next+1) is written
set_owner(B, bl, f2, "Sami recovered")
bc.sync(force=True)
sim.step()
hc.sync(force=True)
check(owner(hl, f2) == "Sami recovered", "6b. next head recovers")

# ---- 7. Old head replayed into a view (lower n) is ignored ---------------------------------------------------
cur_heads = sorted(x for x in os.listdir(b_in_host) if rf._HEAD_RE.match(x))
old_head_name = cur_heads[0]
new_head_name = cur_heads[-1]
new_head_bytes = open(os.path.join(b_in_host, new_head_name), "rb").read()
old_n = int(rf._HEAD_RE.match(old_head_name).group(1))
seen_before = (host.store.get("folder_head_seen") or {}).get(B.me_id)
os.remove(os.path.join(b_in_host, new_head_name))
rep = hc.sync()
seen_after = (host.store.get("folder_head_seen") or {}).get(B.me_id)
with open(os.path.join(b_in_host, new_head_name), "wb") as f:
    f.write(new_head_bytes)
check(old_n < seen_before and seen_after == seen_before and not rep["refused"],
      f"7. old head (n={old_n} < seen={seen_before}) ignored")

# ---- 15. Sealed: no plaintext owner name in any file ---------------------------------------------------------
plain_found = False
for dirpath, _dirs, filenames in os.walk(rf.room_dir(v_host, host)):
    for fn in filenames:
        raw = open(os.path.join(dirpath, fn), "rb").read()
        if any(tok in raw for tok in (b"Mona", b"Sami", b"Office")):
            plain_found = True
check(not plain_found, "15. sealed: no plaintext owner name in any file")

# ---- 11, 12, 13, 14 with mocked clock ------------------------------------------------------------------------
clock = [time.time()]
with mock.patch("time.time", lambda: clock[0]):
    # Sync everyone up to date (except C who stays away)
    sim.step()
    ac.sync(force=True)
    bc.sync(force=True)

    # 11. 40 host edits inside one WRITE_EVERY write at most 2 host files
    clock[0] += rf.WRITE_EVERY + 5
    before_h = set(os.listdir(hc.my_dir()))
    for i in range(40):
        set_owner(host, hl, f1, f"Batch {i}")
        hc.sync()
    new_h = set(os.listdir(hc.my_dir())) - before_h
    check(len(new_h) <= 2, f"11a. 40 host edits inside one WRITE_EVERY wrote {len(new_h)} files (<= 2)")
    clock[0] += rf.WRITE_EVERY + 5
    hc.sync()
    incr1 = sorted(x for x in os.listdir(hc.my_dir()) if rf._NAME.match(x))[-1]
    sz_39 = os.path.getsize(os.path.join(hc.my_dir(), incr1))
    clock[0] += rf.WRITE_EVERY + 5
    set_owner(host, hl, f1, "Single edit")
    hc.sync()
    incr2 = sorted(x for x in os.listdir(hc.my_dir()) if rf._NAME.match(x))[-1]
    sz_1 = os.path.getsize(os.path.join(hc.my_dir(), incr2))
    check(sz_1 < sz_39 and sz_1 < 4096,
          f"11b. incremental holds only new run (1-edit {sz_1}B < 39-edit {sz_39}B), C away")

    # 12. 31 days with member edit not yet sent to absent host: member rewrites, host accepts
    sim.absent("host")
    set_owner(A, al, f4, "Mona 31d")
    ac.sync(force=True)
    sim.step()
    clock[0] += 31 * 86400
    ac.sync()
    sim.present("host")
    sim.step()
    hc.sync(force=True)
    check(owner(hl, f4) == "Mona 31d", "12. after 31 days member rewrites outbox and host accepts")

    # 13. 31 days with idle host: late member B catches up via refreshed snapshot
    clock[0] += 31 * 86400
    hc.sync()
    sim.step()
    bc.sync()
    check(B.since == host.store.head() and owner(bl, f4) == "Mona 31d",
          f"13. after 31 days idle host refreshed snapshot; late member caught up ({B.since}/{host.store.head()})")

# ---- 9. absent(view) at on_room -> no-drive; present() resumes without restart --------------------------------
sim.absent("B")
rf.on_room(None)
B.store.put("folder_carrier", v_b)
rf.on_room(B)
pump(1.5, lambda: rf.current() is not None and rf.current().status["state"] == "no-drive")
check(rf.current() is not None and rf.current().status["state"] == "no-drive",
      f"9a. absent(view) at on_room gives 'no-drive' ({rf.current().status['state']})")
rf.current()._interval = 0.2
sim.present("B")
pump(2.0, lambda: rf.current().status["state"] == "ok")
check(rf.current().status["state"] == "ok",
      f"9b. after present(), syncing resumes without restart ({rf.current().status['state']})")
rf.use_folder(B, None)

# ---- 10. Folder moved to another root is adopted through find_room -------------------------------------------
moved_root = os.path.join(root, "MovedDrive")
moved_parent = os.path.join(moved_root, "Shared")
os.makedirs(moved_parent)
room_sub = "biruni-room-" + B.keys.tag[:16]
shutil.move(rf.room_dir(v_b, B), os.path.join(moved_parent, room_sub))
c10 = rf.FolderCarrier(B, v_b)
with mock.patch.object(drive, "suggest_roots", return_value=[moved_root]):
    for _ in range(4):
        c10.sync()
    c10.sync()
expected_moved = os.path.normcase(os.path.abspath(moved_parent))
check(B.store.get("folder_carrier") == expected_moved and c10.status["state"] == "ok",
      f"10. moved folder adopted via find_room ({B.store.get('folder_carrier')}, state={c10.status['state']})")
shutil.move(os.path.join(moved_parent, room_sub), rf.room_dir(v_b, B))

# ---- 14. DOWN_KEEP=50, 60 edits, C away -> C is rejoin-needed and applies nothing ----------------------------
with mock.patch("time.time", lambda: clock[0]):
    with mock.patch.object(pigeon, "DOWN_KEEP", 50):
        clock[0] += rf.WRITE_EVERY + 5
        for i in range(60):
            set_owner(host, hl, f3, f"Drop {i}")
        hc.sync()
        sim.step()
        c_since_before = C.since
        cc = rf.FolderCarrier(C, v_c)
        cc.sync()
        check(cc.status["state"] == "rejoin-needed" and C.since == c_since_before and owner(cl, f3) == "owner 3",
              f"14. DOWN_KEEP=50, 60 edits: C is rejoin-needed ({cc.status['state']}) and applied nothing")

# ---- 8. trash(view) -> missing, nothing recreated ------------------------------------------------------------
sim.trash("A")
ac.sync()
check(ac.status["state"] == "missing" and not os.path.exists(rf.room_dir(v_a, A)),
      "8a. trash(A) gives state 'missing' and recreates nothing")
sim.trash("host")
hc.sync()
check(hc.status["state"] == "missing" and not os.path.exists(rf.room_dir(v_host, host)),
      "8b. trash(host) gives state 'missing' and recreates nothing")

# ---- 16. Scan thread keeps app.processEvents() responsive while _read sleeps 3 s -----------------------------
real_read = rf._read
rf._read = lambda path: (time.sleep(3.0), real_read(path))[1]
c16 = rf.FolderCarrier(B, v_b)
max_dt = 0.0
try:
    c16.start(interval=60)
    t_end = time.monotonic() + 1.0
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        app.processEvents()
        max_dt = max(max_dt, time.monotonic() - t0)
        time.sleep(0.02)
finally:
    rf._read = real_read
    c16.stop()
check(max_dt < 0.3, f"16. scan thread non-blocking: max processEvents() = {max_dt:.4f} s (< 0.3 s)")

# ---- 17. A room carried through a folder before markers existed: its host writes the marker once ----------------
legacy = os.path.join(root, "legacy")
lbase = rf.room_dir(legacy, host)
os.makedirs(os.path.join(lbase, "d-" + host.me_id))
host.store.put("folder_made", "")
rf.use_folder(host, legacy)
check(rf.read_marker(lbase, host.keys.key, host.keys.tag) is not None,
      "17. a legacy room folder (the host's own d- folder, no marker) gets its marker from the host")
stranger = os.path.join(root, "stranger")
os.makedirs(rf.room_dir(stranger, host))
host.store.put("folder_made", "")
rf.use_folder(host, stranger)
check(rf.read_marker(rf.room_dir(stranger, host), host.keys.key, host.keys.tag) is None,
      "17b. a room folder without the host's own d- folder is not claimed")
rf.use_folder(host, None)

for n in (A, B, C):
    n.shutdown()
host.shutdown()
prj.clear()
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
