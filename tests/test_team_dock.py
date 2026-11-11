r"""The Team Room panel, off screen: the start page; a room opened on a saved map shows its code, the team and the
host's control panel (tiles, changes per minute, changes by member, addresses); edits fill the activity feed;
another member's pointer and work area are drawn on the map; a conflict is listed and resolved; history of a
feature; today's work map; the change log as CSV; a backup; the network test; the command and ribbon buttons.

Run:  engine\bin\python-qgis.bat tests\test_team_dock.py
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.commands import CATALOG  # noqa: E402
from biruni.team import dock as td, session  # noqa: E402
from biruni.team.overlay import Pointer  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


class Bar:
    notices = []

    def pushMessage(self, title, text, level=0, duration=0):  # noqa: N802
        Bar.notices.append(text)


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.active = None

    def mainWindow(self):  # noqa: N802
        return self.mw

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return Bar()

    def activeLayer(self):  # noqa: N802
        return self.active


class Theme:
    def tokens(self):
        return {k: "#888888" for k in ("text", "text_dim", "accent", "panel", "border", "accent_text",
                                       "accent_hover", "mid", "icon")}


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.toolbox = None


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
lyr = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
prj = QgsProject.instance()
prj.addMapLayer(lyr)
prj.setCrs(lyr.crs())
prj.write(os.path.join(root, "map.qgz"))

check("team_room" in CATALOG, "the Team Room command exists")
plugin = Plugin()
d = td.open_dock(plugin)
check(d is td.open_dock(plugin) and d.pages.currentIndex() == 0, "one panel; it starts on the open/join page")

node = session.open_room([lyr], "Survey team")
d.attach(node)
check(d.pages.currentIndex() == 1 and d.code.text() == session.room_code(node), "the room's code is shown")
check(d.tabs.isTabVisible(1), "the host sees the control panel")
d.tabs.setCurrentIndex(1)

lyr.startEditing()
lyr.changeAttributeValue(1, lyr.fields().indexOf("owner"), "B")
check(pump(10, lambda: d.feed.count() >= 1), "a live edit appears in the activity feed")
d._panel_numbers()
check(d.tiles["today"].value.text() == "1" and d.tiles["per_min"].value.text() == "1",
      "the control panel counts today's changes and this minute's")
check("http://" in d.addresses.text() and str(node.hub.port) in d.addresses.text(), "the room's addresses are shown")
check(sum(d.spark.values) == 1 and d.bars.rows and d.bars.rows[0][1] == 1, "changes per minute and per member are drawn")
d.spark.grab()
d.bars.grab()

# another member: a pointer and a work area
node.hub.presence["m" * 32] = {"c": [5, 5], "e": [0, 0, 10, 10], "edit": [], "ts": time.time(), "name": "Mona"}
node.recent["m" * 32] = [(time.time(), lyr.extent(), "Mona")]
d._tick()
check(any(isinstance(i, Pointer) and i.name == "Mona" for i in d.canvas.scene().items()), "Mona's pointer is on the map")
check("m" * 32 in d.overlay.zones, "Mona's work area is shaded on the map")

# an area of work for Mona: the selected parcel
mona_rec = {"id": "m" * 32, "name": "Mona", "pub": "00", "rank": 1, "rights": {"L1": "edit"}, "token": "t" * 32,
            "joined": time.time(), "seen": None, "state": "in"}
node.store.save_member(mona_rec)
plugin.iface.active = lyr
lyr.removeSelection()
d._set_area(mona_rec)
check(not d._area_bands and "Select the features" in Bar.notices[-1], "no selection: the host is told to select the area first")
lyr.selectByIds([1])
d._set_area(mona_rec)
saved = next(m for m in node.store.members() if m["id"] == "m" * 32)
check("_area" in saved["rights"] and saved["rights"]["L1"] == "edit" and len(d._area_bands) == 1,
      "the selected parcel is Mona's area, kept with her rights and drawn on the map")
node.hub.set_area("m" * 32, None)
d._show_areas()
check(not d._area_bands, "«anywhere»: the area is gone from the map")
lyr.removeSelection()

# a conflict
node.store.add_conflict({"uid": "x", "layer": "L1", "fid": 1, "other": "Mona", "theirs": {"g": None, "a": {"owner": "C"}},
                         "mine": {"g": None, "a": {"owner": "B"}}, "applied": True,
                         "items": [{"what": "field", "field": "owner", "mine": "B", "theirs": "C"}]})
d.refresh()
check(d.conflicts.count() == 1 and "Mona" in d.conflicts.item(0).text() and "(1)" in d.tabs.tabText(2),
      "a conflict is listed with both values")
d.conflicts.setCurrentRow(0)
d._resolve(False)
check(not node.store.open_conflicts(), "keep theirs closes the conflict")

# history of a feature
lyr.selectByIds([1])
plugin.iface.active = lyr
h = td.HistoryDialog(plugin.iface.mainWindow(), node, "L1", 1, d.canvas)
check(h.tree.topLevelItemCount() >= 1, "the history lists the feature's changes")
h.tree.setCurrentItem(h.tree.topLevelItem(0))
h.reject()

# tools
n0 = len(prj.mapLayers())
d._heat()
check(len(prj.mapLayers()) == n0 + 1, "the map of today's work is added")
csvp = os.path.join(root, "log.csv")
td.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (csvp, ""))
d._export()
check(os.path.isfile(csvp) and "Parcels" in open(csvp, encoding="utf-8-sig").read(), "the change log is saved as CSV")
d._backup()
bk = os.path.join(os.path.dirname(node.store.path), "backups")
check(os.path.isdir(bk) and any(os.listdir(os.path.join(bk, x)) for x in os.listdir(bk)), "a backup is made")
d._probe()
check(any("answers on this network" in t for t in Bar.notices), "the network test finds the room")
d.freeze.setChecked(True)
check(node.hub.frozen, "freeze editing")
d.notice.setText("Lunch break")
d._send_notice()
check(node.hub.notice and "Lunch break" in node.hub.notice["text"], "a message to everyone")

td.close_dock(plugin)
if lyr.isEditable():
    lyr.rollBack()
prj.clear()
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
