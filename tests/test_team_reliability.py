r"""Team room reliability (review of 2026-11-03): changes are not lost or wrong in the rare cases.

* a member applies a pull of many changes at once (one by one, 1000 took 14 s and froze the window);
* changes waiting for an edit to be saved are kept on disk;
* a change the host refuses (frozen room) is undone on the member's copy instead of staying there alone;
* a save cut by a power failure between the file and the queue is sent at the next start;
* the team Undo takes back only my change and keeps what someone changed after me;
* the outbox is sent in the order the changes were made, not by the computer's clock.

Run:  engine\bin\python-qgis.bat tests\test_team_reliability.py
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

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from biruni.team import code as codes, features, session  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402

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
        time.sleep(0.03)
    return until()


root = tempfile.mkdtemp()
office = os.path.join(root, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string&field=use:string", "P", "memory")
for i in range(4):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"o{i}", "home"])
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({i * 20} 0,{i * 20 + 10} 0,{i * 20 + 10} 10,{i * 20} 10,{i * 20} 0))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(office, "office.qgz"))
host = session.open_room([hl], "Reliability", approve=False)
code = session.room_code(host)
stores = {}


def join(ident):
    real = session.identity
    session.identity = lambda: dict(ident)
    box = {}
    t = threading.Thread(target=lambda: box.update(session.join_steps(code, os.path.join(root, ident["name"]),
                                                                      lambda *a: None)))
    t.start()
    pump(60, lambda: not t.is_alive())
    session.identity = real
    snap = box["snap"]
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", ident["name"],
                         "ogr")
    st = Store(os.path.join(box["folder"], ".biruni", "team", "room.sqlite"))
    st.put("layers", box["hello"]["room"]["layers"])
    st.put("since", snap["head"])
    st.set_versions([tuple(v) for v in snap["versions"]])
    st.put("roster", box["hello"]["room"]["members"])
    n = Node(st, codes.RoomKeys(code), dict(ident))
    n.roster = box["hello"]["room"]["members"]
    n.attach_layers({"L1": lyr})
    n.start_member(box["address"], box["hello"]["member"])
    stores[ident["name"]] = (st, lyr, ident)
    return n, lyr


mona, ml = join({"id": "b" * 32, "seed": "1" * 64, "name": "Mona"})
check(pump(20, lambda: mona.state == "connected"), "Mona is in the room")
fld = ml.fields().indexOf

# ---- 1. many changes applied at once ---------------------------------------------------------------------------
entries = []
for i in range(1000):
    fid = 900_000 + i
    entries.append({"uid": uuid.uuid4().hex, "layer": "L1", "fid": fid, "kind": "put", "ver": 1, "seq": None,
                    "state": {"g": QgsGeometry.fromWkt(f"POINT({i} 50)").buffer(1, 2).asWkb().data().hex(),
                              "a": {"owner": f"batch {i}", "use": "x"}},
                    "prev": None, "author": "e" * 32, "name": "Someone", "ts": time.time(), "sig": "00"})
t0 = time.time()
mona.apply_entries(entries)
took = time.time() - t0
check(ml.dataProvider().featureCount() >= 1004 and ml.getFeature(900_500)["owner"] == "batch 500",
      f"a pull of 1000 changes is written at once ({took:.2f} s; one by one it took about 14 s)")
check(took < 3, "… in a few seconds at most")
check(all(mona.store.has(e["uid"]) for e in entries[:5] + entries[-5:]), "… and kept in the log")

# ---- 2. waiting changes on disk ------------------------------------------------------------------------------------
ml.startEditing()
ml.changeAttributeValue(2, fld("use"), "being edited")
wait_e = {"uid": uuid.uuid4().hex, "layer": "L1", "fid": 2, "kind": "put", "ver": 9, "seq": None,
          "state": {"g": features.stored_state(ml, 2)["g"], "a": {"owner": "from the stream", "use": "home"}},
          "prev": None, "author": "e" * 32, "name": "Someone", "ts": time.time(), "sig": "00"}
mona.apply_entries([wait_e])
check(any(e["uid"] == wait_e["uid"] for e in (mona.store.get("deferred") or {}).get("L1", [])),
      "a change for a feature being edited waits, written on disk")
ml.rollBack()
mona.deferred = {}
mona._save_deferred()

# ---- 3. a refused change is undone here -------------------------------------------------------------------------
pump(3)
host.hub.frozen = True
before = features.stored_state(ml, 3)["a"]["owner"]
ml.startEditing()
ml.changeAttributeValue(3, fld("owner"), "while frozen")
mona.commit_layer("L1")
check(pump(20, lambda: features.stored_state(ml, 3)["a"]["owner"] == before),
      "a change the frozen room refused goes back to the room's value on the member's copy")
host.hub.frozen = False
check(pump(10, lambda: not mona.store.outbox_count()), "and it leaves the outbox")

# ---- 4. a save cut by a power failure ------------------------------------------------------------------------------
st, _lyr, ident = stores["Mona"]
ml.startEditing()
ml.changeAttributeValue(4, fld("owner"), "saved just before the cut")
mona.detach_layers()                                 # (so the save below runs only as far as a power cut lets it)
mona._before("L1")                                   # the save begins…
ml.commitChanges(False)                              # …the file is written…
mona._touch.pop("L1", None)                          # …and the power goes before the change is queued
check("L1" in (st.get("pending_commit") or {}), "the save left its mark")
n_out = st.outbox_count()
mona.stop_network()
again = Node(st, codes.RoomKeys(code), dict(ident))
again.attach_layers({"L1": ml})
queued = [o for o in st.outbox(1000) if int(o["fid"]) == 4]
check(len(queued) == 1 and queued[0]["state"]["a"]["owner"] == "saved just before the cut" and not st.get("pending_commit"),
      f"at the next start the saved change is queued to be sent ({st.outbox_count() - n_out} queued)")
again.detach_layers()
mona.attach_layers({"L1": ml})
mona.start_member(mona.address, mona.member)
check(pump(20, lambda: hl.getFeature(4)["owner"] == "saved just before the cut"), "and it reaches the host")

# ---- 5. undo keeps what someone did after me -----------------------------------------------------------------------
ml.startEditing()
ml.changeAttributeValue(1, fld("owner"), "Mona's owner")
mona.commit_layer("L1")
check(pump(20, lambda: hl.getFeature(1)["owner"] == "Mona's owner"), "Mona changes the owner of parcel 1")
hl.startEditing()
hl.changeAttributeValue(1, hl.fields().indexOf("use"), "shop")
host.commit_layer("L1")
check(pump(20, lambda: features.stored_state(ml, 1)["a"]["use"] == "shop"), "then the host changes its use")
mona.undo_last()
check(pump(20, lambda: hl.getFeature(1)["owner"] == "o0" and hl.getFeature(1)["use"] == "shop"),
      f"Mona's Undo takes back her owner and keeps the host's use ({hl.getFeature(1)['owner']}, "
      f"{hl.getFeature(1)['use']})")

# ---- 6b. a tool that writes straight into the file (the GPS recorder, COGO) ---------------------------------------
from biruni import team  # noqa: E402

team.set_current(mona)
f = QgsFeature(ml.fields())
f.setAttributes([None, "from a tool", "gps"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((300 0,310 0,310 10,300 10,300 0))"))
ok, out = ml.dataProvider().addFeatures([f])
moved = team.notify_added(ml, out)
new_id = moved.get(out[0].id(), out[0].id())
check(new_id in moved.values() and pump(20, lambda: hl.getFeature(new_id).isValid()),
      f"a feature a tool wrote is moved into Mona's ids and reaches the host ({out[0].id()} -> {new_id})")
ml.dataProvider().changeAttributeValues({new_id: {fld("use"): "changed by the tool"}})
team.notify_changed(ml, [new_id])
check(pump(20, lambda: hl.getFeature(new_id)["use"] == "changed by the tool"), "and so does the tool's change")
team.set_current(None)

# ---- 6c. the host changes the fields ---------------------------------------------------------------------------------
from qgis.core import QgsField  # noqa: E402
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

hl.startEditing()
hl.addAttribute(QgsField("zone", QMetaType.Type.QString))
host.commit_layer("L1")
check(pump(20, lambda: ml.dataProvider().fields().indexOf("zone") >= 0), "a field the host adds reaches the member")
use_before = ml.getFeature(1)["use"]
hl.startEditing()
hl.renameAttribute(hl.fields().indexOf("use"), "landuse")
host.commit_layer("L1")
check(pump(20, lambda: ml.dataProvider().fields().indexOf("landuse") >= 0 and
           ml.dataProvider().fields().indexOf("use") < 0), "a field the host renames is renamed on the member's copy")
check(ml.getFeature(1)["landuse"] == use_before, "… with its values")
hl.startEditing()
hl.deleteAttribute(hl.fields().indexOf("zone"))
host.commit_layer("L1")
check(pump(20, lambda: ml.dataProvider().fields().indexOf("zone") < 0), "a field the host deletes is deleted there too")

# ---- 6. the order of the outbox ------------------------------------------------------------------------------------
s2 = Store(os.path.join(root, "order.sqlite"))
s2.queue({"uid": "first", "layer": "L1", "fid": 1})
real_time = time.time
time.time = lambda: real_time() - 3600              # the clock set back an hour
s2.queue({"uid": "second", "layer": "L1", "fid": 1})
time.time = real_time
check([o["uid"] for o in s2.outbox(10)] == ["first", "second"], "the outbox keeps the order the changes were made")

mona.shutdown()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
