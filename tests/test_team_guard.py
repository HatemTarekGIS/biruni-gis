r"""Team room safeguards: a member who may edit but not delete; the guard that holds a member deleting many
features at once until the host says yes or no; taking back everything a member did in a period, keeping what
others changed afterwards.

Run:  engine\bin\python-qgis.bat tests\test_team_guard.py
"""
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from biruni.team import code as codes, session  # noqa: E402
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
for i in range(30):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"o{i + 1}", "home"])
    x = (i % 10) * 20
    y = (i // 10) * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 10} {y},{x + 10} {y + 10},{x} {y + 10},{x} {y}))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(office, "office.qgz"))
host = session.open_room([hl], "Guard", approve=False)
code = session.room_code(host)
held_signals = []
host.deletesHeld.connect(held_signals.append)

ident = {"id": "b" * 32, "seed": "1" * 64, "name": "Mona"}
real = session.identity
session.identity = lambda: dict(ident)
box = {}
t = threading.Thread(target=lambda: box.update(session.join_steps(code, os.path.join(root, "mona"), lambda *a: None)))
t.start()
pump(60, lambda: not t.is_alive())
session.identity = real
snap = box["snap"]
ml = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", "Mona", "ogr")
st = Store(os.path.join(box["folder"], ".biruni", "team", "room.sqlite"))
st.put("layers", box["hello"]["room"]["layers"])
st.put("since", snap["head"])
st.set_versions([tuple(v) for v in snap["versions"]])
mona = Node(st, codes.RoomKeys(code), dict(ident))
mona.roster = box["hello"]["room"]["members"]
mona.attach_layers({"L1": ml})
mona.start_member(box["address"], box["hello"]["member"])
check(pump(20, lambda: mona.state == "connected"), "Mona is in the room")
fld = ml.fields().indexOf


def delete(fids):
    ml.startEditing()
    ml.deleteFeatures(list(fids))
    mona.commit_layer("L1")


# ---- 1. edit, not delete -------------------------------------------------------------------------------------------
host.hub.set_rights(mona.me_id, "L1", "nodelete")
pump(4)
check(not ml.readOnly(), "a member who may edit but not delete can still edit")
delete([1])
check(pump(20, lambda: ml.getFeature(1).isValid()) and hl.getFeature(1).isValid(),
      "her deletion is refused and the feature comes back on her copy")
ml.startEditing()
ml.changeAttributeValue(2, fld("owner"), "edited, not deleted")
mona.commit_layer("L1")
check(pump(20, lambda: hl.getFeature(2)["owner"] == "edited, not deleted"), "her edits still reach the host")
host.hub.set_rights(mona.me_id, "L1", "edit")
pump(4)

# ---- 2. deleting a lot at once -----------------------------------------------------------------------------------------
host.store.put("delete_limit", 5)
n0 = hl.dataProvider().featureCount()
delete(range(10, 22))                                       # 12 deletions in one save
check(pump(20, lambda: held_signals and hl.dataProvider().featureCount() == n0 - 5),
      f"the first 5 go through, then the host is asked ({n0 - hl.dataProvider().featureCount()} deleted)")
check(len(host.hub.held.get(mona.me_id, [])) == 7, f"7 deletions wait ({len(host.hub.held.get(mona.me_id, []))})")
host.hub.release(mona.me_id, allow=False)
check(pump(20, lambda: ml.dataProvider().featureCount() == hl.dataProvider().featureCount()),
      "refused: the 7 features come back on Mona's copy, the two copies agree")
held_signals.clear()
n1 = hl.dataProvider().featureCount()
delete(range(22, 30))                                       # 8 more
check(pump(20, lambda: held_signals), "deleting a lot again is held again")
host.hub.release(mona.me_id, allow=True)
check(pump(20, lambda: hl.dataProvider().featureCount() == n1 - 8 and
           ml.dataProvider().featureCount() == hl.dataProvider().featureCount()),
      "allowed: all 8 are deleted everywhere")
host.store.put("delete_limit", 20)

# ---- 3. taking back a member's work -----------------------------------------------------------------------------------
pump(2)
since = time.time()
time.sleep(1.1)
ml.startEditing()
for fid in (3, 4, 5):
    ml.changeAttributeValue(fid, fld("owner"), "Mona's mistake")
mona.commit_layer("L1")
check(pump(20, lambda: all(hl.getFeature(f)["owner"] == "Mona's mistake" for f in (3, 4, 5))), "Mona changes 3 owners")
hl.startEditing()
hl.changeAttributeValue(4, hl.fields().indexOf("use"), "shop")
host.commit_layer("L1")
pump(2)
done, left = host.revert_member(mona.me_id, since)
check(done == 3 and left == 0, f"the host takes back her work of the period ({done} taken back, {left} left)")
check([hl.getFeature(f)["owner"] for f in (3, 4, 5)] == ["o3", "o4", "o5"] and hl.getFeature(4)["use"] == "shop",
      "the owners are back, the host's own later change is kept")
check(pump(20, lambda: [ml.getFeature(f)["owner"] for f in (3, 4, 5)] == ["o3", "o4", "o5"]),
      "and Mona's copy follows")
check(host.revert_member(mona.me_id, time.time()) == (0, 0), "nothing done in a period without her changes")

mona.shutdown()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
