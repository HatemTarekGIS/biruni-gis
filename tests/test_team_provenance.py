r"""Team room: where a feature came from. Every point of a parcel's shape says who placed it and when, the
corners that were there before the room say so; every value says who gave it; each change keeps what was on its
author's screen (the images, the scale), signed with it, so it cannot be changed afterwards; field photos taken
near the parcel before the change are its evidence; the history window shows all of it.

Run:  engine\bin\python-qgis.bat tests\test_team_provenance.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProject, QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from biruni.secure import crypto  # noqa: E402
from biruni.team import code as codes, provenance, session  # noqa: E402
from biruni.team.hub import sign_body  # noqa: E402
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


# ---- an old room's record gets the new column --------------------------------------------------------------------
old = os.path.join(tempfile.mkdtemp(), "room.sqlite")
con = sqlite3.connect(old)
con.execute("CREATE TABLE ops (seq INTEGER PRIMARY KEY, uid TEXT UNIQUE, epoch INTEGER, author TEXT, name TEXT, ts REAL, "
            "layer TEXT, fid INTEGER, kind TEXT, state TEXT, prev TEXT, ver INTEGER, sig TEXT, note TEXT)")
con.execute("INSERT INTO ops (uid, layer, fid, kind, ver) VALUES ('u', 'L1', 1, 'put', 1)")
con.commit()
con.close()
st_old = Store(old)
rows = st_old.since(0)
check(len(rows) == 1 and "src" not in rows[0], "a room recorded before this version opens; its changes have no source")
st_old.close()

op = {"uid": "x", "layer": "L1", "fid": 1, "kind": "put", "state": {"g": None, "a": {}}, "base_ver": 0, "author": "a",
      "ts": 1.0}
check(sign_body(op) == crypto.canonical({k: op.get(k) for k in ("uid", "layer", "fid", "kind", "state", "base_ver",
                                                                   "author", "ts")}),
      "a change without a source is signed as before (members with the older Biruni still agree)")

# ---- a room with Mona -------------------------------------------------------------------------------------------
root = tempfile.mkdtemp()
office = os.path.join(root, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string&field=use:string", "P", "memory")
for i in range(4):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"o{i + 1}", "home"])
    x = i * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 10} 0,{x + 10} 10,{x} 10,{x} 0))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(office, "office.qgz"))
host = session.open_room([hl], "Origin", approve=False)
code = session.room_code(host)
host.source_of_change = lambda: {"img": ["Ortho 2024"], "scale": 500}

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
mona.source_of_change = lambda: {"img": ["Drone 11-07", "x" * 200], "scale": 250.7}
check(pump(20, lambda: mona.state == "connected"), "Mona is in the room")
fld = ml.fields().indexOf


def move_vertex(lyr, node, fid, i, dx):
    g = QgsGeometry(lyr.getFeature(fid).geometry())
    p = g.vertexAt(i)
    g.moveVertex(p.x() + dx, p.y(), i)
    if i == 0:   # the ring's closing point goes with the first one
        n = len(list(g.vertices())) - 1
        g.moveVertex(p.x() + dx, p.y(), n)
    lyr.startEditing()
    lyr.changeGeometry(fid, g)
    node.commit_layer("L1")


# Mona moves the second corner of parcel 2 and changes its owner; the host then moves its third corner and its use
move_vertex(ml, mona, 2, 1, 0.5)
ml.startEditing()
ml.changeAttributeValue(2, fld("owner"), "Mona's owner")
mona.commit_layer("L1")
check(pump(20, lambda: hl.getFeature(2)["owner"] == "Mona's owner"), "Mona's changes reach the host")
time.sleep(1.1)
move_vertex(hl, host, 2, 2, 0.7)
hl.startEditing()
hl.changeAttributeValue(2, hl.fields().indexOf("use"), "shop")
host.commit_layer("L1")
check(pump(20, lambda: ml.getFeature(2)["use"] == "shop"), "the host's changes reach Mona")

hist = host.store.feature_history("L1", 2)
src = [e.get("src") for e in hist]
check(src[0] == {"img": ["Drone 11-07", "x" * 80], "scale": 250} and src[-1] == {"img": ["Ortho 2024"], "scale": 500},
      f"each change keeps what was on its author's screen, kept small ({src[0]})")
mona_pub = next(m["pub"] for m in host.store.members() if m["id"] == mona.me_id)
first = dict(hist[0], base_ver=0)
check(crypto.verify(bytes.fromhex(mona_pub), bytes.fromhex(first["sig"]), sign_body(first)),
      "… signed by Mona with it")
forged = dict(first, src={"img": ["Another image"], "scale": 250})
check(not crypto.verify(bytes.fromhex(mona_pub), bytes.fromhex(first["sig"]), sign_body(forged)),
      "… so nobody can say afterwards it was drawn on another image")
check(any(e.get("src") for e in mona.store.feature_history("L1", 2)), "Mona's copy of the record has the sources too")

pts = provenance.vertices(hist)
who = {i: (e or {}).get("name") for _p, _r, i, _xy, e in pts}
host_name = host.ident["name"]
check(len(pts) == 4 and who[1] == "Mona" and who[2] == host_name and who[0] is None and who[3] is None,
      f"corner by corner: the 2nd is Mona's, the 3rd the host's, the other two were there before the room ({who})")
vals = {k: e.get("name") for k, e in provenance.fields(hist).items()}
check(vals == {"owner": "Mona", "use": host_name}, f"value by value: the owner is Mona's, the use the host's ({vals})")
check(dict(provenance.authors(hist)) == {"Mona": 1, host_name: 1, "": 2}, "how many points each placed")

# Mona redraws the whole shape: every point is hers
ml.startEditing()
ml.changeGeometry(2, QgsGeometry.fromWkt("POLYGON((20 0, 31 0, 31 12, 25 15, 20 12, 20 0))"))
mona.commit_layer("L1")
check(pump(20, lambda: len(list(hl.getFeature(2).geometry().vertices())) == 6), "Mona redraws parcel 2 with five corners")
pts = provenance.vertices(host.store.feature_history("L1", 2))
names = [(e or {}).get("name") for *_r, e in pts]
check(len(pts) == 5 and names.count("Mona") == 4 and names[0] is None,
      "… the four corners she placed are hers; the one she left where it was is still from before the room")

# ---- field photos as evidence ---------------------------------------------------------------------------------------
photos = QgsVectorLayer("Point?crs=EPSG:32638&field=photo:string&field=taken:string&field=kind:string", "Field photos",
                        "memory")
feats = []
for wkt, path, taken in (("POINT(26 5)", "near_before.jpg", "2020-01-01T10:00:00"),
                         ("POINT(26 5)", "near_after.jpg", "2099-01-01T10:00:00"),
                         ("POINT(500 500)", "far.jpg", "2020-01-01T10:00:00")):
    f = QgsFeature(photos.fields())
    f.setGeometry(QgsGeometry.fromWkt(wkt))
    f.setAttributes([path, taken, "parcel"])
    feats.append(f)
photos.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(photos)
hist = host.store.feature_history("L1", 2)
near = provenance.photos_near(hl.getFeature(2).geometry(), hl.crs(), hist[-1]["ts"])
check([p[1] for p in near] == ["near_before.jpg"], f"the photo taken near it before the change is its evidence ({near})")

# ---- the history window ---------------------------------------------------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.team import dock as td  # noqa: E402

canvas = QgsMapCanvas()
h = td.HistoryDialog(None, host, "L1", 2, canvas)
texts = [h.tree.topLevelItem(i).text(3) for i in range(h.tree.topLevelItemCount())]
check(any("Drone 11-07" in t and "1:250" in t for t in texts) and any("Ortho 2024" in t for t in texts),
      f"each change shows what it was drawn on ({texts[:2]})")
check(len(h.markers) == 4 and "Mona 4" in h.origin.text() and "owner" in h.origin.text(),
      "the points are on the map in their author's colour, with who placed how many: " + h.origin.text())
check(h.photos.count() == 1, "the photo near it is listed, to open")
h.done(0)
check(not h.markers, "closing the window takes the points off the map")

mona.shutdown()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
