r"""Folder rekey tests: removed member cannot read the new folder, remaining members switch through the
folder alone, a member away 40 days switches on return, and a member without a box key is listed in dock.

Run:  engine\bin\python-qgis.bat tests\test_room_folder_rekey.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest.mock as mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
sys.path.insert(0, HERE)

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.team import code as codes, dock as td, features, folder as rf, session, wire  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402
from drivesim import DriveSim  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


class _Bar:
    def pushMessage(self, *a, **k):  # noqa: N802
        pass


class _Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)

    def mainWindow(self):  # noqa: N802
        return self.mw

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return _Bar()

    def activeLayer(self):  # noqa: N802
        return None


class _Theme:
    def tokens(self):
        return {k: "#888888" for k in ("text", "text_dim", "accent", "panel", "border", "accent_text",
                                       "accent_hover", "mid", "icon")}


class _Plugin:
    def __init__(self):
        self.iface = _Iface()
        self.theme = _Theme()
        self.toolbox = None


root = tempfile.mkdtemp()
office = os.path.join(root, "office")
os.makedirs(office)
gpkg = os.path.join(office, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "Parcels", "memory")
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
prj = QgsProject.instance()
prj.addMapLayer(hl)
prj.setCrs(hl.crs())
prj.write(os.path.join(office, "office.qgz"))

host = session.open_room([hl], "Folder Rekey", approve=False)
old_code = session.room_code(host)
old_keys = codes.RoomKeys(old_code)
real_identity = session.identity


def join(ident):
    session.identity = lambda: dict(ident)
    box = {}
    t = threading.Thread(target=lambda: box.update(session.join_steps(old_code, os.path.join(root, ident["name"]),
                                                                      lambda *a: None)))
    t.start()
    pump(60, lambda: not t.is_alive())
    session.identity = real_identity
    snap = box["snap"]
    lyr = QgsVectorLayer(os.path.join(snap["folder"], snap["layer_files"]["L1"]) + "|layername=parcels",
                         ident["name"], "ogr")
    st = Store(os.path.join(box["folder"], ".biruni", "team", "room.sqlite"))
    st.put("layers", box["hello"]["room"]["layers"])
    st.put("since", snap["head"])
    st.put("block", box["hello"]["block"])
    st.put("sid", box["hello"]["room"].get("sid"))
    st.put("roster", box["hello"]["room"].get("members") or [])
    st.set_versions([tuple(v) for v in snap["versions"]])
    n = Node(st, codes.RoomKeys(old_code), dict(ident))
    n.roster = st.get("roster")
    n.attach_layers({"L1": lyr})
    n.start_member(box["address"], box["hello"]["member"])
    return n, lyr


mona, ml = join({"id": "b" * 32, "seed": "1" * 64, "name": "Mona"})
sami, sl = join({"id": "c" * 32, "seed": "2" * 64, "name": "Sami"})
hala, hal = join({"id": "d" * 32, "seed": "3" * 64, "name": "Hala"})
pump(8, lambda: len(host.hub.roster()) == 4 and len(mona.roster) == 4 and len(hala.roster) == 4)

# Add a member without a box key and check dock lists them.
host.hub.store.save_member({
    "id": "e" * 32, "name": "NoBox", "pub": "11" * 32, "rank": 9,
    "rights": {"L1": "edit"}, "token": None, "joined": time.time(), "seen": None, "state": "in",
})
plugin = _Plugin()
dock = td.TeamDock(plugin)
dock.attach(host)
dock.refresh()
check(not dock.no_box_label.isHidden() and "NoBox" in dock.no_box_label.text()
      and "Give these members the new code yourself" in dock.no_box_label.text(),
      f"member without a box key is listed ({dock.no_box_label.text()})")
dock.attach(None)

# Cut the network for good.
for n in (mona, sami, hala):
    n.stop_network()
    n._tick.stop()
    n._pres_timer.stop()
    n.state = "reconnecting"

views = {n: os.path.join(root, "views", n) for n in ("host", "mona", "sami", "hala")}
for p in views.values():
    os.makedirs(p)
sim = DriveSim(views)
rf.create_room_folder(host, views["host"])
host.store.put("folder_carrier", views["host"])
mona.store.put("folder_carrier", views["mona"])
sami.store.put("folder_carrier", views["sami"])
hala.store.put("folder_carrier", views["hala"])
sim.step()
hc = rf.FolderCarrier(host, views["host"])
mc = rf.FolderCarrier(mona, views["mona"])
sc = rf.FolderCarrier(sami, views["sami"])
hac = rf.FolderCarrier(hala, views["hala"])

# Remove Sami and sync once before changing code.
host.hub.remove_member(sami.me_id)
hc.sync(force=True)
sim.step()
mc.sync()

# Host changes code; rekey goes into old folder, then host creates & writes new folder.
new_code = host.change_code(wait=0.2)
new_keys = codes.RoomKeys(new_code)
pump(2, lambda: codes.normalize(host.keys.code) == codes.normalize(new_code))
check(codes.normalize(host.keys.code) == codes.normalize(new_code)
      and os.path.isdir(os.path.join(views["host"], "biruni-room-" + new_keys.tag[:16])),
      "host switched code and created the new room folder")

# Step DriveSim so Mona & Sami see the rekey file and new room folder (Hala stays absent/offline!).
sim.absent("hala")
sim.step()
sim.present("hala")

# Mona switches through the folder alone and sends an edit under the new code.
mc.sync()
pump(2, lambda: codes.normalize(mona.keys.code) == codes.normalize(new_code))
check(codes.normalize(mona.keys.code) == codes.normalize(new_code),
      "remaining member Mona switched code through the folder alone")
st1 = features.stored_state(ml, 1)
st1["a"]["owner"] = "after folder rekey"
mona.submit("L1", 1, st1)
mc.sync(force=True)
sim.step()
hc.sync(force=True)
check(features.stored_state(hl, 1)["a"]["owner"] == "after folder rekey",
      "Mona's edit reaches host under the new code")

# Removed member Sami cannot switch and cannot read the new folder.
sc.sync()
pump(1)
check(codes.normalize(sami.keys.code) == codes.normalize(old_code),
      "removed member Sami did not get the new code")
new_dir_sami = os.path.join(views["sami"], "biruni-room-" + new_keys.tag[:16])
check(rf.read_marker(new_dir_sami, sami.keys.key, new_keys.tag) is None,
      "removed member Sami cannot read the new folder with old key")

# Hala returns after 40 days and switches on return via the 90-day rekey file.
base_t = time.time()
clock_40d = [base_t + 40 * 86400]
with mock.patch("time.time", side_effect=lambda: clock_40d[0]):
    hc.sync(force=True)
    sim.step()
    hac.sync()
    pump(2, lambda: codes.normalize(hala.keys.code) == codes.normalize(new_code))
    check(codes.normalize(hala.keys.code) == codes.normalize(new_code),
          "member Hala away 40 days switches on return through the folder")
    hac.sync(force=True)
    check(features.stored_state(hal, 1)["a"]["owner"] == "after folder rekey",
          "Hala catches up in the new folder after switching")

# Member action "Enter the Room's New Code..." rejects a code with no valid marker in folder.
bogus = codes.new_code()
check(not mona.apply_new_code(bogus), "apply_new_code rejects a code with no valid marker in the folder")

dock.shutdown()
for n in (mona, sami, hala):
    n.shutdown()
session.close_room(host)
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
