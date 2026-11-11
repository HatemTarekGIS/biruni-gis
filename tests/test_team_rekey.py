r"""Changing a team room's code: the new code is sealed to each member still in the room, everyone switches at the
same moment and goes on working; a removed member keeps an old code that opens nothing.

Run:  engine\bin\python-qgis.bat tests\test_team_rekey.py
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
from biruni.team import code as codes, session, wire  # noqa: E402
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
host = session.open_room([hl], "Rekey", approve=True)
old_code = session.room_code(host)
host.joinRequested.connect(lambda req: host.hub.decide(req["id"], True))


def join(ident):
    real = session.identity
    session.identity = lambda: dict(ident)
    box = {}
    t = threading.Thread(target=lambda: box.update(session.join_steps(old_code, os.path.join(root, ident["name"]),
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
    n = Node(st, codes.RoomKeys(old_code), dict(ident))
    n.roster = box["hello"]["room"]["members"]
    n.attach_layers({"L1": lyr})
    n.start_member(box["address"], box["hello"]["member"])
    return n, lyr


mona, ml = join({"id": "b" * 32, "seed": "1" * 64, "name": "Mona"})
sami, sl = join({"id": "c" * 32, "seed": "2" * 64, "name": "Sami"})
check(pump(20, lambda: mona.state == "connected" and sami.state == "connected"), "two members in the room")
host.hub.remove_member(sami.me_id)

new = host.change_code(wait=3)
check(new and codes.normalize(new) != codes.normalize(old_code), f"the host made a new code ({new})")
check(pump(40, lambda: mona.keys.code == codes.RoomKeys(new).code and mona.state == "connected"),
      f"Mona got the new code sealed for her, switched and is connected again ({mona.state})")
check(host.keys.code == codes.RoomKeys(new).code and session.room_code(host) and
      codes.normalize(session.room_code(host)) == codes.normalize(new), "the host works under the new code")
ml.startEditing()
ml.changeAttributeValue(1, ml.fields().indexOf("owner"), "after the new code")
check(pump(30, lambda: hl.getFeature(1)["owner"] == "after the new code"),
      "Mona's edits reach the host under the new code")
check(sami.keys.code == codes.RoomKeys(old_code).code, "Sami, removed, got no new code")
found = wire.find_on_network(codes.RoomKeys(old_code), wire.Sealer(codes.RoomKeys(old_code).key), wait=2)
check(not found, f"with the old code the room cannot be found any more ({len(found)})")
try:
    wire.Client(wire.Sealer(codes.RoomKeys(old_code).key), f"http://127.0.0.1:{host.hub.port}").call(
        "hello", {"id": "d" * 32, "name": "x", "pub": "00" * 32}, timeout=10)
    check(False, "the host does not answer the old code")
except wire.WireError:
    check(True, "the host does not answer the old code")

for n in (mona, sami):
    n.shutdown()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
