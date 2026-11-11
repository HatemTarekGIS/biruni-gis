r"""Team room security (review of 2026-11-03): each attack that worked is tried again and must fail.

* joining as another member with only its id and public key (they are in every member's roster);
* a "replay" change that skipped every check: forged author, view-only layer, frozen room, no signature;
* a host naming the files of its copy "../../anything" to write outside the room folder;
* a host's map pointing layers at the member's own files, network shares or /vsicurl, with Python form code and
  actions;
* feature ids: worked out from each member's id, never handed out twice after a change of host.

Run:  engine\bin\python-qgis.bat tests\test_team_security.py
"""
import os
import sys
import tempfile
import threading
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsAction, QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsLayerDefinition, QgsProject, QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from biruni.secure import crypto  # noqa: E402
from biruni.team import code as codes, node as nodemod, session, snapshot, wire  # noqa: E402
from biruni.team.hub import own_block, sign_body  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return until()


def in_thread(fn):
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run)
    t.start()
    pump(60, lambda: not t.is_alive())
    if "e" in box:
        raise box["e"]
    return box.get("r")


root = tempfile.mkdtemp()
office = os.path.join(root, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "P", "memory")
for i in range(3):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"o{i}"])
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({i * 20} 0,{i * 20 + 10} 0,{i * 20 + 10} 10,{i * 20} 10,{i * 20} 0))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(office, "office.qgz"))
host = session.open_room([hl], "Security", approve=True)
code = session.room_code(host)
keys = codes.RoomKeys(code)
addr = f"http://127.0.0.1:{host.hub.port}"
host.joinRequested.connect(lambda req: host.hub.decide(req["id"], True))
host_id = host.member["id"]

mona = {"id": "b" * 32, "seed": "1" * 64, "name": "Mona"}
real_identity = session.identity
session.identity = lambda: dict(mona)
res = in_thread(lambda: session.join_steps(code, os.path.join(root, "rooms"), lambda *a: None, address=addr))
session.identity = real_identity
check(res["hello"]["status"] == "ok", "a real member joins with the proof of its key")
token = res["hello"]["member"]["token"]
entry = next(m for m in res["hello"]["room"]["members"] if m["id"] == mona["id"])
mona_pub = crypto.public_key(bytes.fromhex(mona["seed"])).hex()

# ---- 1. joining as Mona with what every member knows ------------------------------------------------------
atk = wire.Client(wire.Sealer(keys.key), addr)
r1 = in_thread(lambda: atk.call("hello", {"id": entry["id"], "name": "Mona", "pub": entry["pub"]}))
check(r1.get("status") == "prove" and "member" not in r1, f"id + public key alone get no token, only a nonce ({r1.get('status')})")
r2 = in_thread(lambda: atk.call("hello", {"id": entry["id"], "name": "Mona", "pub": entry["pub"], "nonce": r1["nonce"],
                                          "proof": "00" * 64}))
check("member" not in r2, f"a wrong proof gets no token ({r2.get('status')})")
attacker_seed = "2" * 64
r3 = in_thread(lambda: wire.say_hello(atk, {"id": entry["id"], "name": "Mona", "pub": entry["pub"]}, attacker_seed,
                                      keys.tag))
check("member" not in r3, f"a proof signed with another key gets no token ({r3.get('status')})")
r4 = in_thread(lambda: wire.say_hello(atk, {"id": mona["id"], "name": "Mona", "pub": mona_pub}, mona["seed"], keys.tag))
check(r4.get("status") == "ok" and r4["member"]["token"] != token, "the real key gets in, with a new token each time")
try:
    in_thread(lambda: atk.call("pull", {"token": token, "since": 0, "wait": 0}, timeout=10))
    check(False, "an old token stops working")
except wire.WireError:
    check(True, "an old token stops working")
token = r4["member"]["token"]


# ---- 2. forged replays and forged authors -----------------------------------------------------------------------
def op(kind="del", fid=1, author=None, seed=None, replay=False, **extra):
    o = {"uid": uuid.uuid4().hex, "layer": "L1", "fid": fid, "kind": kind, "base_ver": 0, "author": author or mona["id"],
         "ts": time.time(), "state": None if kind == "del" else {"g": None, "a": {"owner": "x"}}}
    o["sig"] = crypto.sign(bytes.fromhex(seed), sign_body(o)).hex() if seed else "00"
    if replay:
        o["replay"] = True
    o.update(extra)
    return o


def push(ops):
    return in_thread(lambda: atk.call("push", {"token": token, "ops": ops}, timeout=60))["results"]


n0 = hl.dataProvider().featureCount()
r = push([op(author=host_id, replay=True, force=True, name="HOST (forged)")])
check(r[0]["status"] == "refused" and hl.dataProvider().featureCount() == n0,
      f"a replay in the host's name without the host's signature is refused ({r[0]})")
r = push([op(author=host_id, seed=mona["seed"], replay=True)])
check(r[0]["status"] == "refused", f"a replay in the host's name signed by someone else is refused ({r[0].get('why')})")
r = push([op(author="z" * 32, seed=mona["seed"], replay=True)])
check(r[0]["status"] == "refused", f"a replay of an unknown author is refused ({r[0].get('why')})")
host.hub.set_rights(mona["id"], "L1", "view")
r = push([op(seed=mona["seed"], replay=True)])
check(r[0]["status"] == "refused" and "view" in r[0].get("why", ""), f"view-only holds for replays too ({r[0]})")
host.hub.set_rights(mona["id"], "L1", "edit")
host.hub.frozen = True
r = push([op(seed=mona["seed"], replay=True)])
check(r[0]["status"] == "refused" and r[0].get("why") == "frozen", f"a frozen room refuses replays too ({r[0]})")
host.hub.frozen = False
r = push([op(fid=2)])
check(r[0]["status"] == "refused" and hl.dataProvider().featureCount() == n0, "an unsigned change is refused")
r = push([op(fid=2, seed=mona["seed"], replay=True)])
pump(5, lambda: hl.dataProvider().featureCount() == n0 - 1)
check(r[0]["status"] == "ok" and hl.dataProvider().featureCount() == n0 - 1,
      f"a replay correctly signed by its member still goes through ({r[0]})")

# ---- 3. file names chosen by a host ---------------------------------------------------------------------------
for name in ("../../evil.txt", "C:/Windows/evil.txt", "C:\\x.txt", "\\\\server\\share\\x", "/etc/x", "a/../../b",
             "a//b", " x"):
    check(snapshot.inside(root, name) is None, f"unsafe name refused: {name!r}")
check(snapshot.inside(root, "L1.gpkg") and snapshot.inside(root, "extra/ab12/قطع.shp"),
      "plain names (Arabic ones too) are accepted")


class FakeHost:
    def call(self, path, obj, timeout=None):
        return {"files": {"../../../OUTSIDE_pwned.txt": 5, "versions.json": 10}}

    def download(self, *a, **k):
        open(a[2], "w").write("x")


dest = os.path.join(root, "member")
os.makedirs(dest)
try:
    nodemod.download_snapshot(FakeHost(), "t", dest, lambda *a: None)
    check(False, "a copy with an unsafe file name is refused")
except wire.WireError:
    check(not os.path.exists(os.path.join(root, "OUTSIDE_pwned.txt")) and
          not os.path.exists(os.path.join(os.path.dirname(root), "OUTSIDE_pwned.txt")),
          "a copy with an unsafe file name is refused, nothing written outside")

# ---- 4. the host's map ---------------------------------------------------------------------------------------------
QgsProject.instance().removeAllMapLayers()
mapdir = os.path.join(root, "copy")
os.makedirs(mapdir)
import shutil  # noqa: E402

shutil.copy(gpkg, os.path.join(mapdir, "L1.gpkg"))
shared = QgsVectorLayer(gpkg + "|layername=parcels", "Shared", "ogr")
cfg = shared.editFormConfig()
from qgis.core import Qgis  # noqa: E402

cfg.setInitCodeSource(Qgis.AttributeFormPythonInitCodeSource.Dialog)
cfg.setInitCode("import os\ndef go(d, l, f):\n    os.system('calc')\n")
cfg.setInitFunction("go")
shared.setEditFormConfig(cfg)
shared.actions().addAction(QgsAction(Qgis.AttributeActionType.GenericPython, "run", "import os; os.system('calc')"))
layers = [shared,
          QgsVectorLayer("/vsicurl/http://127.0.0.1:9/x.gpkg", "Remote file", "ogr"),
          QgsVectorLayer("\\\\127.0.0.1\\share\\x.shp", "Network share", "ogr"),
          QgsVectorLayer("Point?crs=EPSG:4326", "Scratch", "memory")]
from qgis.core import QgsRasterLayer  # noqa: E402

layers.append(QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
                             "Basemap", "wms"))
QgsProject.instance().addMapLayers(layers, False)
for lyr in layers:
    QgsProject.instance().layerTreeRoot().addLayer(lyr)
ok, err = QgsLayerDefinition.exportLayerDefinition(os.path.join(mapdir, "map.qlr"),
                                                   QgsProject.instance().layerTreeRoot().children())
sid = shared.id()
QgsProject.instance().removeAllMapLayers()
gone = snapshot.load_room_map(mapdir, {sid: "L1.gpkg"}, {sid: "parcels"}, {})
names = sorted(lyr.name() for lyr in QgsProject.instance().mapLayers().values())
check("Remote file" in gone and "Network share" in gone and "Remote file" not in names and "Network share" not in names,
      f"layers on /vsicurl or a network share are not opened ({names}, gone {gone})")
check("Basemap" in names and "Scratch" in names, "web basemaps and temporary layers are kept")
loaded = QgsProject.instance().mapLayersByName("Shared")[0]
check(os.path.realpath(loaded.source().split("|")[0]) == os.path.realpath(os.path.join(mapdir, "L1.gpkg")),
      "the shared layer reads the room's copy")
check(loaded.editFormConfig().initCode() == "" and
      loaded.editFormConfig().initCodeSource() == Qgis.AttributeFormPythonInitCodeSource.NoSource and
      not loaded.actions().actions(), "Python form code and actions of the host's map are taken away")

# ---- 4b. who may say "I am the host" -------------------------------------------------------------------------------
roster = [{"id": host_id, "pub": host.pub, "state": "in"}, {"id": mona["id"], "pub": mona_pub, "state": "in"},
          {"id": "c" * 32, "pub": crypto.public_key(bytes.fromhex("3" * 64)).hex(), "state": "out"}]
claim = {"port": 47213, "epoch": 999, "on": 999, "rank": 0, "since": time.time(), "head": 1, "name": "x", "host": "x"}
check(wire.announce_ok(keys.tag, host.hub.where(), roster), "the real host's announcement is signed and accepted")
check(not wire.announce_ok(keys.tag, dict(claim, hid=host_id), roster), "an unsigned claim to be the host is refused")
check(not wire.announce_ok(keys.tag, wire.sign_announce("2" * 64, keys.tag, dict(claim, hid=host_id)), roster),
      "a claim in the host's name signed with another key is refused")
check(not wire.announce_ok(keys.tag, wire.sign_announce("2" * 64, keys.tag, dict(claim, hid="d" * 32)), roster),
      "a code holder who is not a member cannot become the host")
check(not wire.announce_ok(keys.tag, wire.sign_announce("3" * 64, keys.tag, dict(claim, hid="c" * 32)), roster),
      "a removed member cannot become the host")
signed = wire.sign_announce(mona["seed"], keys.tag, dict(claim, hid=mona["id"]))
check(wire.announce_ok(keys.tag, signed, roster) and not wire.announce_ok(keys.tag, dict(signed, epoch=1000), roster),
      "a member's real announcement is accepted, and changing any word of it breaks the signature")

# ---- 5. feature ids ------------------------------------------------------------------------------------------------
a0, a1, b0 = own_block("a" * 32, 0), own_block("a" * 32, 1), own_block("b" * 32, 0)
check(a0 == own_block("a" * 32, 0) and len({a0[0], a1[0], b0[0]}) == 3 and a0[1] - a0[0] == 1_000_000
      and b0[1] < 2 ** 63, "each member's blocks come from its own id: the same on every host, never shared")
blocks = {own_block(uuid.uuid4().hex, 0)[0] for _ in range(1000)}
check(len(blocks) == 1000, "1000 members: 1000 different blocks")
lyr = QgsVectorLayer(gpkg + "|layername=parcels", "again", "ogr")
taken = host.store.get("block")[0]
f = QgsFeature(lyr.fields())
f.setAttributes([taken, "already here"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 0))"))
lyr.dataProvider().addFeatures([f])
check(host.next_fid(lyr) != taken, "an id a tool already used in the file is skipped")

session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
