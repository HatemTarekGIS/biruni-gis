r"""A member joins a team room from outside the host's network, over the internet (needs internet; a few minutes):
the host opens a Cloudflare quick tunnel to the room and leaves its sealed address on ntfy.sh; the member, with
only the code and the local network search switched off, reads the address from ntfy, joins through the tunnel,
gets the map, and edits both ways. Nothing readable passes through ntfy or the tunnel: both see sealed bytes.

Run:  engine\bin\python-qgis.bat tests\test_team_outside.py
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
app = QgsApplication([], False)
app.initQgis()
from biruni.team import code as codes, session, wire  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until():
            return True
        time.sleep(0.05)
    return until()


root = tempfile.mkdtemp()
gpkg = os.path.join(root, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "Parcels", "memory")
f = QgsFeature(mem.fields())
f.setAttributes(["A"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))
mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(root, "map.qgz"))
host = session.open_room([hl], "Outside test", approve=False, external=True)
code = session.room_code(host)
host.status.connect(lambda level, text: print("host:", level, text, flush=True))
ok = pump(480, lambda: bool(host.external_url))
check(ok, f"the host opened an encrypted tunnel: {host.external_url}")

# the member: no local network search, only the code
wire.find_on_network = lambda *a, **k: []
session.identity = lambda: {"id": "c" * 32, "seed": "2" * 64, "name": "Far away"}
res, err = {}, []
t0 = time.time()


def worker():
    for _ in range(12):  # the address takes a moment to be readable on ntfy
        try:
            res.update(session.join_steps(code, os.path.join(root, "far"), lambda *a: None))
            return
        except wire.WireError as e:
            if "not-found" not in str(e):
                err.append(str(e))
                return
            time.sleep(5)
    err.append("not found on ntfy")


t = threading.Thread(target=worker)
t.start()
pump(240, lambda: not t.is_alive())
check(res.get("address", "").startswith("https://") and "trycloudflare.com" in res.get("address", ""),
      f"the member found the room from the code alone, through ntfy ({time.time() - t0:.0f} s) {err}")
if res:
    snap = res["snap"]
    ml = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels", "P", "ogr")
    check(ml.featureCount() == 1, "the map came through the tunnel")
    st = Store(os.path.join(res["folder"], ".biruni", "team", "room.sqlite"))
    st.put("layers", res["hello"]["room"]["layers"])
    st.put("since", snap["head"])
    st.put("block", res["hello"]["block"])
    st.set_versions([tuple(v) for v in snap["versions"]])
    member = Node(st, codes.RoomKeys(code), {"id": "c" * 32, "seed": "2" * 64, "name": "Far away"})
    member.attach_layers({"L1": ml})
    member.start_member(res["address"], res["hello"]["member"])
    ml.startEditing()
    ml.changeAttributeValue(1, ml.fields().indexOf("owner"), "from far away")
    t1 = time.time()
    check(pump(60, lambda: hl.getFeature(1)["owner"] == "from far away"),
          f"an edit from outside reached the host ({time.time() - t1:.1f} s with the 1.5 s save)")
    hl.startEditing()
    hl.changeAttributeValue(1, hl.fields().indexOf("owner"), "answer from the office")
    t1 = time.time()
    check(pump(60, lambda: ml.getFeature(1)["owner"] == "answer from the office"),
          f"the office's edit reached the member outside ({time.time() - t1:.1f} s)")
    member.shutdown()
host.stop_external()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}")
os._exit(1 if failures else 0)
