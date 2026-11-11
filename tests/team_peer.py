r"""One computer of a team room, for test_team_takeover.py (its own process, its own settings and identity).

usage: python-qgis team_peer.py <folder> host|join|reopen [code]
It writes <folder>/status.json twice a second and runs the commands written to <folder>/cmd.json.
"""
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
folder, mode = sys.argv[1], sys.argv[2]
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(folder, "profile")
os.makedirs(os.path.join(folder, "profile"), exist_ok=True)

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

app = QgsApplication([], False)
app.initQgis()
from biruni import team  # noqa: E402
from biruni.team import node as node_mod, session  # noqa: E402

node_mod.ELECTION_S = 6
node_mod.ELECTION_STEP = 8
session.set_my_name(os.path.basename(folder))

if mode == "host":
    gpkg = os.path.join(folder, "parcels.gpkg")
    mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "Parcels", "memory")
    for i in range(5):
        f = QgsFeature(mem.fields())
        f.setAttributes([f"start {i + 1}"])
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({i * 20} 0,{i * 20 + 10} 0,{i * 20 + 10} 10,{i * 20} 10,{i * 20} 0))"))
        mem.dataProvider().addFeatures([f])
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = "GPKG"
    o.layerName = "parcels"
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
    lyr = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
    QgsProject.instance().addMapLayer(lyr)
    QgsProject.instance().setCrs(lyr.crs())
    QgsProject.instance().write(os.path.join(folder, "map.qgz"))
    node = session.open_room([lyr], "Takeover test", approve=False)
    with open(os.path.join(folder, "code.txt"), "w") as f:
        f.write(session.room_code(node))
elif mode == "join":
    res = session.join_steps(sys.argv[3], os.path.join(folder, "rooms"), lambda *a: None)
    node, _ = session.finish_join(res)
else:
    with open(os.path.join(folder, "project.txt")) as fh:
        QgsProject.instance().read(fh.read().strip())
    node = session.reopen()
with open(os.path.join(folder, "project.txt"), "w") as fh:
    fh.write(QgsProject.instance().fileName())

done = set()
last_write = 0.0
while True:
    app.processEvents()
    time.sleep(0.03)
    node = team.current()
    try:
        with open(os.path.join(folder, "cmd.json")) as fh:
            cmds = json.load(fh)
    except (OSError, ValueError):
        cmds = []
    for c in cmds:
        if c["id"] in done or node is None:
            continue
        done.add(c["id"])
        lyr = node.layers.get("L1")
        if c["op"] == "edit":
            if not lyr.isEditable():
                lyr.startEditing()
            lyr.changeAttributeValue(int(c["fid"]), lyr.fields().indexOf("owner"), c["owner"])
        elif c["op"] == "quit":
            node.shutdown()
            sys.exit(0)
    if node is not None and time.time() - last_write > 0.5:
        last_write = time.time()
        lyr = node.layers.get("L1")
        owners = {}
        if lyr is not None:
            for f in lyr.dataProvider().getFeatures():
                owners[str(f.id())] = f["owner"]
        st = {"role": node.role, "state": node.state, "epoch": node.epoch, "owners": owners, "t": time.time(),
              "outbox": node.store.outbox_count()}
        tmp = os.path.join(folder, "status.tmp")
        with open(tmp, "w") as f:
            json.dump(st, f)
        try:
            os.replace(tmp, os.path.join(folder, "status.json"))
        except OSError:  # the test is reading it
            pass
