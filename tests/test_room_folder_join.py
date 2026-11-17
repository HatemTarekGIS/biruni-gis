r"""Folder join tests: approval, refusal, vectors-only base package, crypto isolation, tampered/unsafe zip,
pending join persistence, package reuse & cleanup, and rebase after rejoin-needed.

Run:  engine\bin\python-qgis.bat tests\test_room_folder_join.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
sys.path.insert(0, HERE)

from osgeo import gdal  # noqa: E402
from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.pigeon import core as pigeon  # noqa: E402
from biruni.secure import crypto  # noqa: E402
from biruni.team import code as codes, features, folder as rf, hub as thub, session, wire  # noqa: E402
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


def write_gpkg(path, layer_name, features_list):
    mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", layer_name, "memory")
    for idx, val in enumerate(features_list):
        f = QgsFeature(mem.fields())
        f.setAttributes([val])
        x = idx * 20
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 10} 0,{x + 10} 10,{x} 10,{x} 0))"))
        mem.dataProvider().addFeatures([f])
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = "GPKG"
    o.layerName = layer_name
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), o)
    return QgsVectorLayer(path + f"|layername={layer_name}", layer_name, "ogr")


def write_tiny_tif(path):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, 4, 4, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((0, 1, 0, 4, 0, -1))
    ds = None
    return QgsRasterLayer(path, "DEM")


root = tempfile.mkdtemp()
host_dir = os.path.join(root, "office")
os.makedirs(host_dir)
hl = write_gpkg(os.path.join(host_dir, "parcels.gpkg"), "parcels", ["owner 1", "owner 2", "owner 3"])
big_extra = write_gpkg(os.path.join(host_dir, "roads.gpkg"), "roads", [f"road {i}" for i in range(40)])
small_extra = write_gpkg(os.path.join(host_dir, "marks.gpkg"), "marks", ["m1"])
dem = write_tiny_tif(os.path.join(host_dir, "dem.tif"))

prj = QgsProject.instance()
for lyr in (hl, big_extra, small_extra, dem):
    prj.addMapLayer(lyr)
prj.setCrs(hl.crs())
prj.write(os.path.join(host_dir, "office.qgz"))

# Cut off all network discovery to prove joining uses the folder alone.
wire.find_on_network = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network discovery called"))
wire.find_published = lambda *a, **k: (_ for _ in ()).throw(AssertionError("ntfy discovery called"))
session.JOIN_POLL = 0.15
thub.MAX_BASE_EXTRA = os.path.getsize(os.path.join(host_dir, "marks.gpkg")) + 1024

host = session.open_room([hl], "Drive Join", approve=True)
code = session.room_code(host)
keys = codes.RoomKeys(code)

views = {n: os.path.join(root, "views", n) for n in ("host", "mona", "sami", "hala")}
for p in views.values():
    os.makedirs(p)
sim = DriveSim(views)
rf.create_room_folder(host, views["host"])
sim.step()
hc = rf.FolderCarrier(host, views["host"])

real_identity = session.identity
mona_id = {"id": "a" * 32, "seed": "1" * 64, "name": "Mona"}
sami_id = {"id": "b" * 32, "seed": "2" * 64, "name": "Sami"}
hala_id = {"id": "c" * 32, "seed": "3" * 64, "name": "Hala"}

# 1. Join with approval needed, then granted.
mona_res, mona_err = {}, []
session.identity = lambda: dict(mona_id)
steps_seen = []
t = threading.Thread(target=lambda: _run_join(mona_res, mona_err, views["mona"], os.path.join(root, "mona_rooms"),
                                              steps_seen))


def _run_join(res_box, err_box, drive_dir, rooms_dir, step_log=None):
    try:
        res_box.update(session.join_from_folder(
            code, rooms_dir, drive_folder=drive_dir,
            report=lambda s, a, b: step_log.append((s, a, b)) if step_log is not None else None,
        ))
    except Exception as e:  # noqa: BLE001
        err_box.append(e)


t.start()
pump(2, lambda: any(x[0] == "waiting" for x in steps_seen))
check(any(x[0] == "asked" for x in steps_seen) and any(x[0] == "waiting" for x in steps_seen),
      f"joiner reports asked and waiting ({steps_seen[:3]})")
check(any(p["tag"] == keys.tag for p in session.pending_joins()), "pending join saved while waiting")

sim.step()
hc.sync(force=True)
pump(0.5, lambda: mona_id["id"] in host.hub.waiting)
check(mona_id["id"] in host.hub.waiting, "host sees Mona's join request without network")

host.hub.decide(mona_id["id"], True)
hc.sync(force=True)
sim.step()
pump(10, lambda: not t.is_alive())
session.identity = real_identity
check(not mona_err and mona_res.get("address") is None and mona_res.get("snap"),
      f"Mona joined through the folder alone ({mona_err})")
check(any(s[0] == "downloading" for s in steps_seen), "joiner reports downloading a/b")

lo_map = {x["name"]: x["why"] for x in mona_res.get("left_out") or []}
check(lo_map.get("DEM") == "raster" and lo_map.get("roads") == "too large",
      f"raster and oversized vector appear in left_out ({lo_map})")

# Verify neither left-out layer is in any .bpb part.
host_d = os.path.join(views["host"], "biruni-room-" + keys.tag[:16], "d-" + host.me_id)
pkg_meta = host.store.get("folder_base_pkg")
bpb_names = [p["name"] for p in pkg_meta["parts"]]
check(bool(bpb_names), f"base package parts created ({bpb_names})")

# Someone with the code (room key) but not approved cannot unseal any .bpb.
bpb_blob = open(os.path.join(host_d, bpb_names[0]), "rb").read()
wrong_raised = False
try:
    crypto.unseal(keys.key, bpb_blob, f"biruni-room-base-1|{pkg_meta['id']}|0".encode("utf-8"))
except crypto.CryptoError:
    wrong_raised = True
check(wrong_raised, "someone with only the room code cannot unseal .bpb (CryptoError)")

# Unseal with pkg_key and confirm left_out files are absent.
pkg_key = crypto.unb64(pkg_meta["key"])
raw_zip = b"".join(
    crypto.unseal(pkg_key, open(os.path.join(host_d, p["name"]), "rb").read(),
                  f"biruni-room-base-1|{pkg_meta['id']}|{i}".encode("utf-8"))
    for i, p in enumerate(pkg_meta["parts"])
)
with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
    znames = zf.namelist()
check(not any("dem" in z.lower() or "roads" in z.lower() for z in znames),
      f"left_out files are in no .bpb ({znames})")

# Tampered part and unsafe zip entry '..\\x' are refused.
tamper_dir = tempfile.mkdtemp()
for p in pkg_meta["parts"]:
    data = bytearray(open(os.path.join(host_d, p["name"]), "rb").read())
    data[-1] ^= 0xFF
    with open(os.path.join(tamper_dir, p["name"]), "wb") as f:
        f.write(bytes(data))
tampered_refused = False
try:
    rf.extract_base(tamper_dir, pkg_meta, tempfile.mkdtemp())
except ValueError:
    tampered_refused = True
check(tampered_refused, "tampered .bpb part is refused")

evil_dir = tempfile.mkdtemp()
evil_buf = io.BytesIO()
with zipfile.ZipFile(evil_buf, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("..\\x", b"evil")
evil_sealed = crypto.seal(pkg_key, evil_buf.getvalue(), b"biruni-room-base-1|evilid|0")
with open(os.path.join(evil_dir, "base-evilid-0000.bpb"), "wb") as f:
    f.write(evil_sealed)
evil_info = {
    "id": "evilid",
    "key": pkg_meta["key"],
    "parts": [{"name": "base-evilid-0000.bpb", "sha": rf._sha(evil_sealed), "size": len(evil_sealed)}],
}
unsafe_refused = False
try:
    rf.extract_base(evil_dir, evil_info, tempfile.mkdtemp())
except ValueError:
    unsafe_refused = True
check(unsafe_refused, "zip entry '..\\x' is refused")
shutil.rmtree(tamper_dir, ignore_errors=True)
shutil.rmtree(evil_dir, ignore_errors=True)

def open_member_node(res, ident):
    snap = res["snap"]
    room = res["hello"]["room"]
    os.makedirs(res["folder"], exist_ok=True)
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels",
                         "Parcels", "ogr")
    st = Store(session.store_path(res["folder"]))
    st.put("code", session._protect(res["code"]))
    st.put("room_name", room.get("name", ""))
    st.put("layers", room.get("layers", {}))
    st.put("epoch", int(room.get("epoch", 1)))
    st.put("sid", room.get("sid"))
    for k, v in (room.get("settings") or {}).items():
        st.put(k, v)
    st.put("since", snap["head"])
    st.put("block", session.own_block(ident["id"], 0))
    st.put("block_n", 1)
    st.put("roster", room.get("members", []))
    st.set_versions([tuple(v) for v in snap["versions"]])
    st.put("pigeon_only", True)
    st.put("folder_carrier", res["drive_folder"])
    n = Node(st, codes.RoomKeys(res["code"]), dict(ident))
    n.role = "member"
    n.member = res["hello"]["member"]
    n.state = "reconnecting"
    n.attach_layers({"L1": lyr})
    return n, lyr


# Open Mona's node from join_from_folder output and verify map equals host's.
mona, ml = open_member_node(mona_res, mona_id)
check([f["owner"] for f in ml.getFeatures()] == [f["owner"] for f in hl.getFeatures()],
      "joiner's map equals the host's")
mc = rf.FolderCarrier(mona, views["mona"])

# 2. Refused join.
sami_res, sami_err = {}, []
session.identity = lambda: dict(sami_id)
ts = threading.Thread(target=lambda: _run_join(sami_res, sami_err, views["sami"], os.path.join(root, "sami_rooms")))
ts.start()
pump(2, lambda: os.path.isdir(os.path.join(views["sami"], "biruni-room-" + keys.tag[:16], "d-" + sami_id["id"])))
sim.step()
hc.sync(force=True)
pump(0.5, lambda: sami_id["id"] in host.hub.waiting)
host.hub.decide(sami_id["id"], False)
hc.sync(force=True)
sim.step()
pump(10, lambda: not ts.is_alive())
session.identity = real_identity
check(sami_err and "refused" in str(sami_err[0]), f"refused joiner receives WireError(refused) ({sami_err})")

# 3. Pending join survives restart, and second joiner reuses the package (before Mona checks in).
session.identity = lambda: dict(hala_id)
hala_rooms = os.path.join(root, "hala_rooms")
session.save_pending_join(code, hala_rooms, drive_folder=views["hala"], room_name="Drive Join")
pending_after = session.pending_joins()
check(len(pending_after) == 1 and codes.normalize(pending_after[0]["code"]) == codes.normalize(code),
      "pending join survives restart with decrypted code")

hala_res, hala_err = {}, []
th = threading.Thread(target=lambda: _run_join(hala_res, hala_err, views["hala"], hala_rooms))
th.start()
pump(2, lambda: os.path.isdir(os.path.join(views["hala"], "biruni-room-" + keys.tag[:16], "d-" + hala_id["id"])))
sim.step()
hc.sync(force=True)
pump(0.5, lambda: hala_id["id"] in host.hub.waiting)
host.hub.decide(hala_id["id"], True)
hc.sync(force=True)
sim.step()
pump(10, lambda: not th.is_alive())
session.identity = real_identity
check(not hala_err and hala_res.get("snap"), f"second joiner (Hala) joined ({hala_err})")
pkg_after_hala = host.store.get("folder_base_pkg")
check(pkg_after_hala["id"] == pkg_meta["id"], "second joiner reuses the same base package")

# Bidirectional edits flow both ways, and once both welcomed members check in, .bpb parts are deleted.
hala, _ = open_member_node(hala_res, hala_id)
hc2 = rf.FolderCarrier(hala, views["hala"])
st1 = features.stored_state(ml, 1)
st1["a"]["owner"] = "Mona edited"
mona.submit("L1", 1, st1)
st2 = features.stored_state(hl, 2)
st2["a"]["owner"] = "Host edited"
host.submit("L1", 2, st2)
mc.sync(force=True)
hc2.sync(force=True)
sim.step()
hc.sync(force=True)
sim.step()
mc.sync(force=True)
check(features.stored_state(hl, 1)["a"]["owner"] == "Mona edited"
      and features.stored_state(ml, 2)["a"]["owner"] == "Host edited",
      "edits flow both ways after folder join")
check(host.store.get("folder_base_pkg") is None
      and not any(f.endswith(".bpb") for f in os.listdir(host_d)),
      "base package parts are deleted once all welcomed members appear in pigeon_seen_map")

# 4. Rebase after rejoin-needed.
pigeon.DOWN_KEEP = 8
rf.WRITE_EVERY = 0
for i in range(12):
    st = features.stored_state(hl, 3)
    st["a"]["owner"] = f"bulk {i}"
    host.submit("L1", 3, st)
hc.keep_days = 0
hc.sync(force=True)
sim.step()
mc.sync(force=True)
check(mc.status["state"] == "rejoin-needed", f"Mona enters rejoin-needed ({mc.status['state']})")
mc.sync(force=True)  # writes join-*.bpj with rebase=True
sim.step()
hc.sync(force=True)  # responds with welcome + base without asking
sim.step()
mc.sync(force=True)  # rebases!
check(mc.status["state"] == "ok" and features.stored_state(mona.layers["L1"], 3)["a"]["owner"] == "bulk 11"
      and mona.since == host.store.head(),
      f"Mona rebased after rejoin-needed ({mc.status['state']}, since={mona.since}/{host.store.head()})")

mona.shutdown()
hala.shutdown()
session.close_room(host)

# 5. Verify session.finish_join with address=None sets folder carrier, pigeon_only, and returns left_out reasons.
session.identity = lambda: dict(hala_id)
fin_res = dict(hala_res, folder=os.path.join(root, "hala_finish"))
fin_node, fin_skipped = session.finish_join(fin_res)
session.identity = real_identity
check(fin_node.role == "member" and fin_node.state == "reconnecting"
      and fin_node.store.get("pigeon_only") is True and rf.current() is not None,
      "finish_join with address=None sets folder-only member state and starts carrier")
check(any("DEM" in s and "raster" in s for s in fin_skipped)
      and any("roads" in s and "too large" in s for s in fin_skipped),
      f"finish_join reports left_out with reasons ({fin_skipped})")
session.close_room(fin_node)
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
