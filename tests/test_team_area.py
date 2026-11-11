r"""Team room: a member's area of work. The host gives a member an area; the member may change, add and delete
features only inside it. A change outside, a parcel moved out of it or in from outside, a deletion outside and a
feature drawn beyond it are refused and the member's copy goes back; the area reaches the member with the rights,
in another coordinate system too; "anywhere" lifts it.

Run:  enginein\python-qgis.bat tests	est_team_area.py
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
host = session.open_room([hl], "Area", approve=False)
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


from biruni.team import area  # noqa: E402

# the parcels are 10 m squares every 20 m, 10 by 3: fid = row * 10 + column + 1
AREA_WKT = "POLYGON((-5 -5, 55 -5, 55 55, -5 55, -5 -5))"     # columns 0-2 of every row: fids 1-3, 11-13, 21-23
crs = hl.crs()


def owner(lyr, fid):
    return lyr.getFeature(fid)["owner"]


def edit_owner(fid, value):
    ml.startEditing()
    ml.changeAttributeValue(fid, fld("owner"), value)
    mona.commit_layer("L1")


ml.startEditing()
ml.changeAttributeValue(5, fld("owner"), "anywhere at first")
mona.commit_layer("L1")
check(pump(20, lambda: owner(hl, 5) == "anywhere at first"), "without an area Mona edits anywhere")

a = area.make([QgsGeometry.fromWkt(AREA_WKT)], crs)
check(a is not None and a["crs"] == "EPSG:32638", "an area is made from shapes on the map")
host.hub.set_area(mona.me_id, a)
check(pump(20, lambda: area.of((mona.member or {}).get("rights")) is not None),
      "the area reaches Mona with her rights")

edit_owner(2, "inside")
check(pump(20, lambda: owner(hl, 2) == "inside"), "a change inside her area reaches the host")

edit_owner(5, "outside")
check(pump(20, lambda: owner(ml, 5) == "anywhere at first") and owner(hl, 5) == "anywhere at first",
      "a change outside is refused and her copy goes back")

g3 = QgsGeometry(ml.getFeature(3).geometry())
moved = QgsGeometry(g3)
moved.translate(100, 0)
ml.startEditing()
ml.changeGeometry(3, moved)
mona.commit_layer("L1")
check(pump(20, lambda: ml.getFeature(3).geometry().isGeosEqual(g3)) and hl.getFeature(3).geometry().isGeosEqual(g3),
      "a parcel taken out of her area is refused: it stays where it was")

g15 = QgsGeometry(ml.getFeature(15).geometry())
inward = QgsGeometry(g15)
inward.translate(-80, 0)       # column 4 -> inside
ml.startEditing()
ml.changeGeometry(15, inward)
mona.commit_layer("L1")
check(pump(20, lambda: ml.getFeature(15).geometry().isGeosEqual(g15)) and hl.getFeature(15).geometry().isGeosEqual(g15),
      "a parcel brought in from outside is refused too")

ml.startEditing()
ml.deleteFeature(10)
mona.commit_layer("L1")
check(pump(20, lambda: ml.getFeature(10).isValid()) and hl.getFeature(10).isValid(),
      "a deletion outside is refused: the parcel comes back")

ml.startEditing()
ml.deleteFeature(12)
mona.commit_layer("L1")
check(pump(20, lambda: not hl.getFeature(12).isValid()), "a deletion inside goes through")


def add(wkt, who):
    ml.startEditing()
    f = QgsFeature(ml.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    f["owner"] = who
    ml.addFeature(f)
    mona.commit_layer("L1")


def host_has(who):
    return any(f["owner"] == who for f in hl.getFeatures())


def mona_has(who):
    return any(f["owner"] == who for f in ml.dataProvider().getFeatures())


add("POLYGON((1 41, 9 41, 9 49, 1 49, 1 41))", "new inside")
check(pump(20, lambda: host_has("new inside")), "a parcel drawn inside reaches the host")
add("POLYGON((50 41, 70 41, 70 49, 50 49, 50 41))", "new across the line")
check(pump(20, lambda: not mona_has("new across the line")) and not host_has("new across the line"),
      "a parcel drawn across the area's line is refused and leaves her copy")

# the same area given in degrees: the host checks it in the layer's own system
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform  # noqa: E402

geo = QgsGeometry.fromWkt(AREA_WKT)
geo.transform(QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance()))
host.hub.set_area(mona.me_id, area.make([geo], QgsCoordinateReferenceSystem("EPSG:4326")))
pump(3)
edit_owner(1, "in degrees")
check(pump(20, lambda: owner(hl, 1) == "in degrees"), "an area kept in degrees: inside still goes through")
edit_owner(6, "in degrees, outside")
check(pump(20, lambda: owner(ml, 6) != "in degrees, outside") and owner(hl, 6) != "in degrees, outside",
      "… and outside is still refused")

host.hub.set_area(mona.me_id, None)
check(pump(20, lambda: area.of((mona.member or {}).get("rights")) is None), "«anywhere»: the area is lifted on her computer")
edit_owner(5, "free again")
check(pump(20, lambda: owner(hl, 5) == "free again"), "… and she edits anywhere again")
check(area.make([QgsGeometry.fromWkt("LINESTRING(0 0, 100 0)")], crs) is not None and
      area.make([], crs) is None, "lines make an area with a metre around them; nothing makes none")

mona.shutdown()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
