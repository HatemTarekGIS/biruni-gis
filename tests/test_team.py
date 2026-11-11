r"""Team rooms, off screen, two computers in one process (a host and a member on this PC, over real HTTP
and the network discovery):

codes and keys; sealed messages refuse other keys and replays; the vertex merge (different edges joined,
same vertex refused, attributes field by field); a room opened on a saved map; a member finds it by the
code, waits for approval and gets the map; live editing on both sides reaches the other; new features take
ids from separate blocks; the north and south edges of one parcel edited at once are joined; the same field
edited at once is a conflict kept for the author, who keeps theirs; delete; undo; view-only rights;
the stream is signed; the record survives.

Run:  engine\bin\python-qgis.bat tests\test_team.py
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

from biruni.team import code as codes, merge, session, wire  # noqa: E402
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


# ---- codes, sealing, merge ----------------------------------------------------------------------------
c = codes.new_code()
check(len(c) == 19 and c.count("-") == 3, f"a room code looks like {c}")
check(codes.normalize(c.lower().replace("-", " ")) == c.replace("-", ""), "codes are read in any case and spacing")
check(codes.normalize("OIL" + c.replace("-", "")[3:]) is not None, "look-alike letters are read")
k1, k2 = codes.RoomKeys(c), codes.RoomKeys(codes.new_code())
s1, s2 = wire.Sealer(k1.key), wire.Sealer(k2.key)
blob = s1.seal({"x": 1})
check(s1.open(blob) == {"x": 1}, "a sealed message opens with its key")
try:
    s1.open(blob)
    check(False, "a repeated message is refused")
except wire.WireError:
    check(True, "a repeated message is refused")
try:
    s2.open(s1.seal({"x": 1}))
    check(False, "another room's key cannot open it")
except wire.WireError:
    check(True, "another room's key cannot open it")


def geo(h):
    g = QgsGeometry()
    g.fromWkb(bytes.fromhex(h))
    return g


def hexw(wkt):
    return bytes(QgsGeometry.fromWkt(wkt).asWkb()).hex()


base = hexw("POLYGON((0 0,10 0,10 10,0 10,0 0))")
north = hexw("POLYGON((0 0,10 0,10 12,0 12,0 0))")          # moves the two top vertices
south = hexw("POLYGON((0 -2,10 -2,10 10,0 10,0 -2))")        # moves the two bottom vertices
g, ok = merge.merge_geometry(base, north, south)
check(ok and geo(g).isGeosEqual(QgsGeometry.fromWkt("POLYGON((0 -2,10 -2,10 12,0 12,0 -2))")),
      "north and south edges edited at once are joined")
same = hexw("POLYGON((0 0,10 0,11 11,0 10,0 0))")
other = hexw("POLYGON((0 0,10 0,9 9,0 10,0 0))")
g, ok = merge.merge_geometry(base, same, other)
check(not ok and g == other, "the same vertex moved two ways is a conflict (the current one kept)")
ins = hexw("POLYGON((0 0,5 -1,10 0,10 10,0 10,0 0))")
g, ok = merge.merge_geometry(base, ins, north)
check(ok and geo(g).isGeosEqual(QgsGeometry.fromWkt("POLYGON((0 0,5 -1,10 0,10 12,0 12,0 0))")),
      "an added vertex joins a moved edge")
st, conf = merge.merge_feature({"g": base, "a": {"a": 1, "b": 1}}, {"g": base, "a": {"a": 2, "b": 1}},
                               {"g": base, "a": {"a": 1, "b": 3}})
check(st["a"] == {"a": 2, "b": 3} and not conf, "different fields are joined")
st, conf = merge.merge_feature({"g": base, "a": {"a": 1}}, {"g": base, "a": {"a": 2}}, {"g": base, "a": {"a": 3}})
check(st["a"]["a"] == 3 and conf and conf[0]["field"] == "a", "the same field changed two ways is a conflict")

# ---- a room ------------------------------------------------------------------------------------------------
root = tempfile.mkdtemp()
hostdir = os.path.join(root, "office")
os.makedirs(hostdir)
gpkg = os.path.join(hostdir, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string&field=area:double", "Parcels", "memory")
for i, wkt in enumerate(("POLYGON((0 0,10 0,10 10,0 10,0 0))", "POLYGON((20 0,30 0,30 10,20 10,20 0))")):
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

host = session.open_room([hl], "Survey team", approve=True)
code = session.room_code(host)
check(host.role == "host" and host.hub.port, f"a room is open on port {host.hub.port}")
asked = []
host.joinRequested.connect(lambda req: (asked.append(req), host.hub.decide(req["id"], True)))

# the member is another identity on this PC
real_identity = session.identity
member_ident = {"id": "b" * 32, "seed": "1" * 64, "name": "Mona"}
session.identity = lambda: dict(member_ident)
steps, result, err = [], {}, []


def worker():
    try:
        result.update(session.join_steps(code, os.path.join(root, "rooms"), lambda s, a, b: steps.append(s)))
    except Exception as e:  # noqa: BLE001
        err.append(repr(e))


t = threading.Thread(target=worker)
t.start()
pump(40, lambda: not t.is_alive())
check(not err and result.get("snap"), f"a member found the room by its code and got its files {err}")
check(asked and asked[0]["name"] == "Mona" and "waiting" in steps, "the host approved the new member first")
snap = result["snap"]
ml = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", "Parcels", "ogr")
check(ml.isValid() and ml.featureCount() == 2, "the member's copy has the parcels")
mst = Store(os.path.join(result["folder"], ".biruni", "team", "room.sqlite"))
mst.put("layers", result["hello"]["room"]["layers"])
mst.put("since", snap["head"])
mst.put("block", result["hello"]["block"])
mst.set_versions([tuple(v) for v in snap["versions"]])
member = Node(mst, codes.RoomKeys(code), dict(member_ident))
member.attach_layers({"L1": ml})
member.start_member(result["address"], result["hello"]["member"])
session.identity = real_identity


def host_feat(fid):
    return hl.getFeature(fid)


def mem_feat(fid):
    return ml.getFeature(fid)


# member adds a parcel (live editing: saved by itself)
ml.startEditing()
nf = QgsFeature(ml.fields())
nf.setAttribute("owner", "new by Mona")
nf.setGeometry(QgsGeometry.fromWkt("POLYGON((40 0,50 0,50 10,40 10,40 0))"))
ml.addFeature(nf)
ok = pump(15, lambda: any(f["owner"] == "new by Mona" for f in hl.getFeatures()))
check(ok, "a parcel the member drew reached the host by itself (live editing)")
new_ids = [f.id() for f in hl.getFeatures() if f["owner"] == "new by Mona"]
check(new_ids and new_ids[0] >= 1_000_000 and new_ids[0] != host.store.get("block")[0],
      f"the new parcel took an id from the member's block ({new_ids})")

# host edits the owner of parcel 2; the member sees it
hl.startEditing()
hl.changeAttributeValue(2, hl.fields().indexOf("owner"), "changed at the office")
ok = pump(15, lambda: mem_feat(2)["owner"] == "changed at the office")
check(ok, "an edit at the host reached the member")

# both edit parcel 1 at once: the member the north edge, the host the south edge
pump(3)
ml.changeGeometry(1, QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 12,0 12,0 0))"))
hl.changeGeometry(1, QgsGeometry.fromWkt("POLYGON((0 -2,10 -2,10 10,0 10,0 -2))"))
hl.commitChanges(False)   # the host saves first; the member's change arrives on an older version
want = QgsGeometry.fromWkt("POLYGON((0 -2,10 -2,10 12,0 12,0 -2))")
ok = pump(20, lambda: host_feat(1).geometry().isGeosEqual(want) and mem_feat(1).geometry().isGeosEqual(want))
check(ok, "north and south edges of one parcel, edited at once on two computers, are both kept everywhere")
check(any(e.get("note") == "merged" for e in host.store.last(20)), "the stream says the two edits were joined")

# both change the same field: a conflict for the member, who keeps theirs
pump(3)
ml.changeAttributeValue(1, ml.fields().indexOf("owner"), "Mona's owner")
hl.changeAttributeValue(1, hl.fields().indexOf("owner"), "office owner")
hl.commitChanges(False)
ok = pump(20, lambda: bool(member.store.open_conflicts()))
check(ok, "the same field changed on both computers is a conflict kept for the member")
pump(5, lambda: mem_feat(1)["owner"] == "office owner")
check(host_feat(1)["owner"] == "office owner" and mem_feat(1)["owner"] == "office owner",
      "nothing was overwritten: the first saved value stays until someone chooses")
member.resolve(member.store.open_conflicts()[0], keep_mine=True)
ok = pump(15, lambda: host_feat(1)["owner"] == "Mona's owner")
check(ok, "keep mine: the member's value is now everyone's")

# delete and undo
ml.deleteFeature(2)
ok = pump(15, lambda: not host_feat(2).isValid())
check(ok, "a delete reaches the host")
pump(3)
member.undo_last()
ok = pump(15, lambda: host_feat(2).isValid() and host_feat(2)["owner"] == "changed at the office")
check(ok, "undo my last change brings the parcel back everywhere")

# view-only rights
host.hub.set_rights("b" * 32, "L1", "view")
ok = pump(25, lambda: ml.readOnly())
check(ok, "a member made view-only cannot edit the layer any more")

# the record
hist = host.store.feature_history("L1", 1)
check(len(hist) >= 3 and all(e["sig"] for e in hist if e["author"] == "b" * 32),
      "the history of a parcel keeps every change with the member's signature")
check(host.store.outbox_count() == 0 and member.store.outbox_count() == 0, "nothing is left waiting to be sent")

member.shutdown()
session.close_room(host)
if ml.isEditable():
    ml.rollBack()
mst.close()
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
