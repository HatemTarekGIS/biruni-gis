r"""Team room: the time machine. A shared layer at any moment since the room opened - a value as it was, a
feature deleted since there again, one drawn since not yet there - stepped and played change by change, and
features picked from the past brought back as a change of their own.

Run:  engine\bin\python-qgis.bat tests\test_team_timemachine.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.team import session, timemachine  # noqa: E402

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
gpkg = os.path.join(root, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "P", "memory")
for i in range(5):
    f = QgsFeature(mem.fields())
    f.setAttributes([f"o{i + 1}"])
    x = i * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 10} 0,{x + 10} 10,{x} 10,{x} 0))"))
    mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
hl = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(hl)
QgsProject.instance().write(os.path.join(root, "map.qgz"))
host = session.open_room([hl], "Time", approve=False)
store = host.store


def owners(lyr):
    return {f["fid"]: f["owner"] for f in lyr.getFeatures()}


def tick():
    time.sleep(1.05)
    t = time.time()
    time.sleep(1.05)
    return t


t_a = tick()
hl.startEditing()
hl.changeAttributeValue(1, hl.fields().indexOf("owner"), "sold")
host.commit_layer("L1")
t_b = tick()
hl.startEditing()
hl.deleteFeature(2)
host.commit_layer("L1")
t_c = tick()
hl.startEditing()
nf = QgsFeature(hl.fields())
nf.setGeometry(QgsGeometry.fromWkt("POLYGON((200 0,210 0,210 10,200 10,200 0))"))
nf["owner"] = "new"
hl.addFeature(nf)
host.commit_layer("L1")
pump(2)
check(len(timemachine.moments(store, "L1")) == 3, "three changes recorded")
now = owners(hl)
check(now.get(1) == "sold" and 2 not in now and "new" in now.values(), "now: parcel 1 sold, parcel 2 gone, a new one")

past, _m, c = timemachine.layer_at(hl, store, "L1", t_a, "then")
o = owners(past)
check(o.get(1) == "o1" and o.get(2) == "o2" and "new" not in o.values() and len(o) == 5,
      f"before the first change: as it was ({o})")
check(c == {"changed": 1, "deleted_since": 1, "added_since": 1}, f"… and how it differs from now {c}")
o = owners(timemachine.layer_at(hl, store, "L1", t_b, "then")[0])
check(o.get(1) == "sold" and o.get(2) == "o2", "after the first change, before the deletion")
o = owners(timemachine.layer_at(hl, store, "L1", t_c, "then")[0])
check(2 not in o and "new" not in o.values(), "after the deletion, before the new parcel")


class Bar:
    said = []

    def pushMessage(self, title, text, level=None, duration=0):
        Bar.said.append(text)


canvas = QgsMapCanvas()


class Iface:
    def messageBar(self):
        return Bar()

    def mapCanvas(self):
        return canvas


tm = timemachine.TimeMachine(None, host, Iface())
check(tm.ghost is None and "3 changes" in tm.when.text(), "the machine opens on now: " + tm.when.text())
tm.set_ts(t_a)
tree = QgsProject.instance().layerTreeRoot()
check(tm.ghost is not None and owners(tm.ghost).get(2) == "o2" and not tree.findLayer(hl.id()).isVisible(),
      "moved back: the past layer is on the map in place of the shared one")
check("3 changes after this moment" in tm.when.text(), tm.when.text())
tm.step(1)
check(owners(tm.ghost).get(1) == "sold" and owners(tm.ghost).get(2) == "o2", "one step forward: the first change")
tm.step(1)
check(2 not in owners(tm.ghost), "another: the deletion")
tm.step(-1)
check(owners(tm.ghost).get(2) == "o2", "and back")

tm.set_ts(t_a)
tm.play()
check(pump(10, lambda: tm.ghost is None), "played forward change by change up to now")
check(tree.findLayer(hl.id()).isVisible(), "… and the shared layer is shown again")

tm.set_ts(t_a)
ids = [mid for mid, (fid, _st) in tm.mapping.items() if fid in (1, 2)]
tm.ghost.selectByIds(ids)
n = tm.bring_back()
check(n == 2 and pump(5, lambda: owners(hl).get(1) == "o1" and owners(hl).get(2) == "o2"),
      f"parcel 1's owner and the deleted parcel 2 brought back ({n})")
hist = store.feature_history("L1", 2)
check(hist[-1].get("note") == "restore" and hist[-1]["kind"] == "put", "… as a change of their own, in the record")
check(len(timemachine.moments(store, "L1")) == 5, "… which the machine can take back too")
tm.done(0)
check(tm.ghost is None and tree.findLayer(hl.id()).isVisible() and len(QgsProject.instance().mapLayers()) == 1,
      "closing leaves the map as it was")

session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
