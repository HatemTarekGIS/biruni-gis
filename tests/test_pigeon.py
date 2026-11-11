r"""Carrier pigeon, off screen: a host and two members on this PC join a room over the network, then the network
is cut and the changes travel only in packs.

A member's change reaches the host through another member (two hand-overs) and the host's stream comes back
the same way; the author's outbox empties and the carrier drops what was delivered; a pack opened twice does
nothing; an altered pack, another room's pack, an expired pack, a change with a forged signature and a pack of
someone outside the room are refused; a pack that starts after a gap waits for the one before it.

Run:  engine\bin\python-qgis.bat tests\test_pigeon.py
"""
import os
import sys
import tempfile
import threading
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.pigeon import core  # noqa: E402
from biruni.secure import crypto  # noqa: E402
from biruni.team import code as codes, features, session  # noqa: E402
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
for i, wkt in enumerate(("POLYGON((0 0,10 0,10 10,0 10,0 0))", "POLYGON((20 0,30 0,30 10,20 10,20 0))",
                         "POLYGON((40 0,50 0,50 10,40 10,40 0))")):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"owner {i + 1}", 100.0])
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
pump(3, lambda: len(host.hub.roster()) == 3)
# both rosters complete (B joined after A): A's pull gets the roster
pump(8, lambda: len(A.roster) == 3 and len(B.roster) == 3)
print("rosters before the cut:", len(A.roster), len(B.roster))

# ---- the network is cut: the members work alone ------------------------------------------------------------------
for n in (A, B):
    n.stop_network()
    n._tick.stop()
    n._pres_timer.stop()
    n.state = "reconnecting"


def owner(lyr, fid):
    return features.stored_state(lyr, fid)["a"]["owner"]


def set_owner(node, lyr, fid, value):
    st = features.stored_state(lyr, fid)
    st["a"]["owner"] = value
    check(node.submit("L1", fid, st), f"{node.ident['name'] if node is not host else 'host'} sets parcel {fid} to "
                                      f"{value}")


fid1 = next(f.id() for f in al.getFeatures() if f["owner"] == "owner 1")
set_owner(A, al, fid1, "Mona's edit")
check(A.store.outbox_count() == 1, "Mona's change waits in her outbox")

# Mona -> Sami -> host
env_a = core.make(A)
check(len(env_a) == 1 and env_a[0].startswith(core.MAGIC), "Mona makes one pack file")
rep = core.open_data(B, env_a[0])
check(rep["carried"] == 1 and rep["applied"] == 0, f"Sami carries Mona's pack ({rep})")
rep = core.open_data(B, env_a[0])
check(rep["dups"] == 1 and rep["carried"] == 0, "the same pack opened twice does nothing")
env_b = core.make(B)
rep = core.open_data(host, b"".join(env_b[:1]))
hfid1 = fid1
check(rep["in"] == 1 and owner(hl, hfid1) == "Mona's edit", f"the host decided Mona's change, carried by Sami ({rep})")
check(rep["refused"] == [], "nothing refused")

# host -> Sami -> Mona
env_h = core.make(host)
rep = core.open_data(B, env_h[0])
check(rep["applied"] >= 1 and owner(bl, fid1) == "Mona's edit", f"Sami's map has the room's change ({rep})")
check(core.counts(B)["carried"] >= 1, "Sami carries the host's pack on")
up_left = core._q(B.store, "SELECT COUNT(*) FROM pigeon_carry WHERE kind='up'")[0][0]
check(up_left == 0, "Sami dropped Mona's pack once her change was in the stream")
env_b2 = core.make(B)
rep = core.open_data(A, env_b2[0])
check(owner(al, fid1) == "Mona's edit" and A.store.outbox_count() == 0,
      f"the host's answer reached Mona through Sami: her outbox is empty ({rep})")
check(len(A.roster) == 3, "the host's pack brought Mona the whole roster")
check(A.since == host.store.head(), f"Mona's place in the stream is the host's ({A.since} / {host.store.head()})")

# ---- refused ------------------------------------------------------------------------------------------------------
data = bytearray(env_a[0])
data[-40] ^= 1
try:
    rep = core.open_data(host, bytes(data))
    check(any("altered" in r for r in rep["refused"]) or rep["refused"], f"an altered pack is refused ({rep['refused']})")
except core.PigeonError as e:
    check(True, f"an altered pack is refused ({e})")

other = types.SimpleNamespace(keys=codes.RoomKeys(codes.new_code()))
try:
    core.open_data(host, core.MAGIC + crypto.canonical({"room": other.keys.tag, "packs": []}))
    check(False, "another room's pack is refused")
except core.PigeonError as e:
    check(str(e) == "another room", "another room's pack is refused")

fid2 = next(f.id() for f in al.getFeatures() if f["owner"] == "owner 2")
st2 = features.stored_state(al, fid2)
st2["a"]["owner"] = "forged"
forged = A._op("L1", fid2, "put", features.stored_state(al, fid2), st2)
forged["sig"] = "00" * 64
body = core._base(A, "up")
body.update(ops=[forged], since=A.since)
env = core.MAGIC + crypto.canonical({"room": A.keys.tag, "packs": [crypto.b64(core.seal(A, body))]})
rep = core.open_data(host, env)
check(owner(hl, fid2) == "owner 2" and "bad signature" in rep["refused"], f"a forged change is refused ({rep['refused']})")

stranger = types.SimpleNamespace(keys=A.keys, ident={"id": "c" * 32, "seed": "3" * 64, "name": "X"},
                                 pub=crypto.public_key(bytes.fromhex("3" * 64)).hex(), store=A.store, epoch=1,
                                 me_id="c" * 32)
body = core._base(stranger, "up")
body.update(ops=[], since=0)
env = core.MAGIC + crypto.canonical({"room": A.keys.tag, "packs": [crypto.b64(core.seal(stranger, body))]})
rep = core.open_data(host, env)
check("not a member" in rep["refused"], "a pack of someone outside the room is refused (the room code is not enough)")

body = core._base(B, "up")
body.update(ops=[forged], since=0)      # Sami's pack carrying a change in Mona's name
env = core.MAGIC + crypto.canonical({"room": A.keys.tag, "packs": [crypto.b64(core.seal(B, body))]})
rep = core.open_data(host, env)
check("a change of someone else" in rep["refused"], "a member cannot carry a change in another member's name")

old = core.EXPIRY
core.EXPIRY = -10
env_old = core.make(A)
core.EXPIRY = old
rep = core.open_data(host, env_old[0])
check("expired" in rep["refused"], "an expired pack is refused")

# ---- a gap ----------------------------------------------------------------------------------------------------------
fid3 = next(f.id() for f in hl.getFeatures() if f["owner"] == "owner 3")
set_owner(host, hl, fid3, "host first")
d1 = core.make(host)
seen = host.store.get("pigeon_seen_map") or {}
head1 = host.store.head()
host.store.put("pigeon_seen_map", {k: head1 for k in ("a" * 32, "b" * 32)})   # both said they have it
set_owner(host, hl, fid1, "host second")
d2 = core.make(host)
host.store.put("pigeon_seen_map", seen)
rep = core.open_data(A, d2[0])
check(rep["waiting"] >= 1 and owner(al, fid1) == "Mona's edit", f"a pack after a gap waits ({rep})")
rep = core.open_data(A, d1[0])
check(owner(al, fid3) == "host first" and owner(al, fid1) == "host second" and rep["waiting"] == 0,
      f"the earlier pack fills the gap and the waiting one follows ({rep})")
check(A.since == host.store.head(), "Mona is at the head of the stream")

# ---- the window ---------------------------------------------------------------------------------------------------
from biruni.pigeon import ui  # noqa: E402

plugin = types.SimpleNamespace(iface=types.SimpleNamespace(mainWindow=lambda: None))
dlg = ui.PigeonDialog(plugin, A)
check("Packs carried for others" in dlg.state.text(), "the window shows what this computer carries")
lines = ui.report_lines(dict(rep, refused=["bad signature", "bad signature"]))
check(any("(2)" in ln for ln in lines), "the report counts the refused packs by reason")
dlg.deleteLater()

for n in (A, B):
    n.shutdown()
host.shutdown()
print("\n%d failure(s)" % len(failures))
sys.exit(1 if failures else 0)
