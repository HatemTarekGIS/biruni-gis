r"""Offscreen UI tests for DriveRoomDialog and TeamDock Drive/host-right controls (never calls exec() or show()).

Run:  engine\bin\python-qgis.bat tests\test_room_drive_ui.py
"""
import os
import shutil
import sys
import tempfile
import time
import unittest.mock as mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QCheckBox, QMainWindow  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.field_photos import drive  # noqa: E402
from biruni.team import code as codes, dock as td, drive_ui, folder as rf, rules, session  # noqa: E402
from biruni.team.node import Node  # noqa: E402
from biruni.team.store import Store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


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
f = QgsFeature(mem.fields())
f.setAttributes(["A"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))
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

host = session.open_room([hl], "X", approve=True)
fake_root = os.path.join(root, "My Drive")
os.makedirs(fake_root)

# 1. Host page with no Drive roots shows notice and Choose a Folder button.
with mock.patch.object(drive, "suggest_roots", return_value=[]):
    dlg_none = drive_ui.DriveRoomDialog(host)
    dlg_none.create_folder()
    check(not dlg_none.no_drive_label.isHidden() and not dlg_none.choose_btn.isHidden(),
          "no Drive roots shows 'not running' notice and Choose a Folder button")

# 2. Host page creates "Biruni Team - X" under a fake root.
with mock.patch.object(drive, "suggest_roots", return_value=[fake_root]):
    dlg = drive_ui.DriveRoomDialog(host)
    created = dlg.create_folder()
    expected = os.path.join(fake_root, "Biruni Team - X")
    check(created == expected and os.path.isdir(expected)
          and os.path.isdir(rf.room_dir(expected, host))
          and not dlg.host_steps.isHidden() and "Biruni Team - X" in dlg.host_steps.text(),
          f"host page created 'Biruni Team - X' under fake root ({created})")
    check(not dlg.explorer_btn.isHidden() and not dlg.copy_name_btn.isHidden() and not dlg.qr_btn.isHidden(),
          "host page shows Explorer, Copy name, and Room Code buttons")

# Stop background scan timer for deterministic status tests.
c_host = rf.current()
c_host.stop()

# 3. Warning shows when streamed=True and hides once offline_ok is checked.
c_host.status["streamed"] = True
dlg.refresh()
check(not dlg.warning_label.isHidden(), "warning shows on DriveRoomDialog when streamed=True")

# Check dock warning & host table + state texts.
plugin = _Plugin()
dock = td.TeamDock(plugin)
dock.attach(host)
dock.refresh()
check(not dock.folder_warning.isHidden(), "warning shows on TeamDock when streamed=True")

# Add member Mona to host roster and check owner sees host-right checkbox (default off).
mona_ident = {"id": "b" * 32, "seed": "1" * 64, "name": "Mona"}
mona_pub = "22" * 32
host.hub.store.save_member({
    "id": mona_ident["id"], "name": "Mona", "pub": mona_pub, "rank": 1,
    "rights": {"L1": "edit"}, "token": None, "joined": time.time(), "seen": None, "state": "in",
})
host.hub._members_changed()
host.roster = host.hub.roster()
dock.refresh()
check(not dock.members.isColumnHidden(3), "owner sees host-right column")
cbs = [dock.members.itemWidget(dock.members.topLevelItem(i), 3)
       for i in range(dock.members.topLevelItemCount())]
cbs = [w for w in cbs if isinstance(w, QCheckBox)]
check(len(cbs) == 1 and not cbs[0].isChecked(), "host-right checkbox default off for member")
check(dock.make_host_btn.isHidden(), "Make This Computer the Host button hidden on host")

# Toggling checkbox grants host right.
cbs[0].setChecked(True)
check(rules.may_host(host.store, host.lookup_member, mona_ident["id"]),
      "toggling host-right checkbox grants host right")

# Host table shows member progress.
c_host.status["heads"] = {mona_ident["id"]: time.time() - 60}
dock.refresh_folder()
check(not dock.folder_table.isHidden() and dock.folder_table.topLevelItemCount() == 1
      and "changes" in dock.folder_table.topLevelItem(0).text(1),
      "host folder table lists member progress")

# 4. Member view: checkbox hidden, Make Host button visible only when allowed, warning + offline checkbox.
mst = Store(os.path.join(root, "mona_room", ".biruni", "team", "room.sqlite"))
mst.put("role", "member")
mst.put("rank", 1)
mst.put("folder_carrier", expected)
mst.put("roster", host.roster)
mst.put("host_grants", host.store.get("host_grants") or [])
mona = Node(mst, codes.RoomKeys(session.room_code(host)), dict(mona_ident))
mona.role = "member"
mc = rf.FolderCarrier(mona, expected)
rf._CARRIER[0] = mc
mc.status["streamed"] = True

dock.attach(mona)
dock.refresh()
check(dock.members.isColumnHidden(3), "member does NOT see host-right column")
check(not dock.make_host_btn.isHidden(), "member allowed by owner DOES see Make This Computer the Host button")
check(not dock.folder_warning.isHidden(), "member sees offline warning when streamed=True and offline_ok=False")

# Member DriveRoomDialog: checking "I made the folder available offline" hides the warning.
mdlg = drive_ui.DriveRoomDialog(mona)
check(not mdlg.warning_label.isHidden() and not mdlg.offline_cb.isChecked(),
      "member DriveRoomDialog shows offline warning before checkbox ticked")
mdlg.offline_cb.setChecked(True)
check(bool(mona.store.get("folder_offline_ok")) and mdlg.warning_label.isHidden(),
      "checking 'available offline' stores folder_offline_ok and hides warning")

# Placeholders still trigger warning even if offline_ok is True.
mc.status["placeholders"] = 2
mdlg.refresh()
check(not mdlg.warning_label.isHidden(), "placeholders > 0 triggers offline warning")

# Revoking host right hides Make Host button on member.
mst.put("host_grants", [rules.make_host_grant(host.ident["seed"], host.me_id, mona_ident["id"], False)])
dock.refresh()
check(dock.make_host_btn.isHidden(), "Make This Computer the Host button hidden when host right is revoked")

# Verify B4 state texts on member dock.
for state_key, expected_sub in (
    ("no-drive", "Google Drive is not running"),
    ("missing", "gone from this computer"),
    ("no-marker", "has no room"),
    ("rejoin-needed", "Fetching a fresh copy"),
):
    mc.status["state"] = state_key
    dock.refresh_folder()
    check(expected_sub in dock.folder_state.text(), f"state '{state_key}' shows '{dock.folder_state.text()}'")

mc.status["state"] = "host-silent"
mc.status["host_made"] = time.time() - 9 * 86400
dock.refresh_folder()
check("9 days" in dock.folder_state.text(), f"state 'host-silent' shows days ({dock.folder_state.text()})")

# Verify JoinDialog has the Google Drive mode.
jd = td.JoinDialog(plugin.iface.mainWindow())
check("Google Drive" in jd.mode_drive.text(), "JoinDialog has Google Drive mode")

rf._CARRIER[0] = None
dock.attach(None)
dock.shutdown()
mona.shutdown()
mona.store.close()
session.close_room(host)
prj.clear()
shutil.rmtree(root, ignore_errors=True)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
