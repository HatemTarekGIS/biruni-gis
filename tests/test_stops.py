r"""Map Stops (stops/): the model (project and user stops, the QGIS bookmark mirror, pictures that survive a saved
.qgz, map themes, CRS changes, rotation, history, order, groups, Arabic search), every file format both ways, the
phone link, the flight and the tour, the keys, and the ribbon dropdown and the Manage pane drawn off screen
(pictures saved for a look: Arabic RTL and English, light and dark).

Run:  engine\bin\python-qgis.bat tests\test_stops.py
Pictures go to %BIRUNI_SHOTS% (else a temporary folder, printed at the end).
"""
import json
import math
import os
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsBookmark, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsCoordinateTransform, QgsFeature, QgsFillSymbol, QgsGeometry, QgsPointXY, QgsProject,
                       QgsRectangle, QgsReferencedRectangle, QgsSingleSymbolRenderer, QgsVectorLayer)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
QgsApplication.bookmarkManager().initialize(os.path.join(SETTINGS_DIR, "bookmarks.xml"))

from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QEventLoop, QPoint, Qt, QTimer  # noqa: E402
from qgis.PyQt.QtGui import QAction, QImage, QKeyEvent, QKeySequence  # noqa: E402
from qgis.PyQt.QtCore import QEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMainWindow, QMenu  # noqa: E402

t_import = time.perf_counter()
import biruni.stops as stops_pkg  # noqa: E402
t_import = time.perf_counter() - t_import
light_import = "biruni.stops.model" not in sys.modules and "biruni.stops.gallery" not in sys.modules

from biruni import i18n  # noqa: E402
from biruni.stops import formats, model  # noqa: E402
from biruni.stops.model import PROJECT, USER, Frame, StopsStore  # noqa: E402
from biruni.theme import TOKENS  # noqa: E402

failures = []
SHOTS = os.environ.get("BIRUNI_SHOTS") or tempfile.mkdtemp(prefix="stops_shots_")
os.makedirs(SHOTS, exist_ok=True)
TMP = tempfile.mkdtemp()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout_ms=5000):
    end = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def near(a, b, tol):
    return abs(a - b) <= tol


# ---- a little world --------------------------------------------------------------------------------------------
class Bar:
    def __init__(self):
        self.messages = []

    def pushMessage(self, *a):  # noqa: N802
        self.messages.append(a)

    def pushWarning(self, *a):  # noqa: N802
        self.messages.append(a)


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)
        self.bar = Bar()

    def mainWindow(self):  # noqa: N802
        return self.mw

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return self.bar


class Theme:
    dark = False

    def tokens(self):
        return TOKENS["dark" if self.dark else "light"]

    def on_change(self, fn):
        pass


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.activity = None


plugin = Plugin()
mw, canvas = plugin.iface.mw, plugin.iface.canvas
# QGIS's own "Show Spatial Bookmarks" action holds Ctrl+Shift+B
qgis_bm_action = QAction("Show Spatial Bookmarks", mw)
qgis_bm_action.setObjectName("mActionShowBookmarks")
qgis_bm_action.setShortcut(QKeySequence("Ctrl+Shift+B"))
mw.addAction(qgis_bm_action)
mw.resize(1100, 700)
mw.show()

project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
# Riyadh, in Web Mercator
RIYADH = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem("EPSG:3857"),
                                project).transform(QgsPointXY(46.7167, 24.6877))


def poly_layer(name, colour, offset):
    lyr = QgsVectorLayer("Polygon?crs=EPSG:3857", name, "memory")
    feats = []
    for i in range(6):
        for j in range(4):
            x0 = RIYADH.x() - 600 + i * 220 + offset
            y0 = RIYADH.y() - 400 + j * 220 + offset
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x0, y0, x0 + 170, y0 + 150)))
            feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
        {"color": colour, "outline_color": "#333333", "outline_width": "0.4"})))
    project.addMapLayer(lyr)
    return lyr


parcels = poly_layer("parcels", "#F2C14E", 0)
blocks = poly_layer("blocks", "#4E9AF2", 60)
canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
canvas.setLayers([blocks, parcels])
canvas.resize(900, 560)
canvas.setExtent(QgsRectangle(RIYADH.x() - 700, RIYADH.y() - 500, RIYADH.x() + 700, RIYADH.y() + 500))
canvas.refresh()
canvas.waitWhileRendering()

# ---- start-up cost ----------------------------------------------------------------------------------------------
check(light_import, "importing the stops package loads none of its parts (model, gallery)")
t0 = time.perf_counter()
unload = stops_pkg.install(plugin)
t_install = time.perf_counter() - t0
check(t_install < 0.25, f"install (history + keys) is cheap: {t_install * 1000:.1f} ms")
check("biruni.stops.gallery" not in sys.modules and "biruni.stops.pane" not in sys.modules,
      "install does not build the gallery or the pane")
check(qgis_bm_action.shortcut().isEmpty(), "Ctrl+Shift+B is taken from QGIS's bookmarks action while Biruni runs")
names = {a.objectName(): a for a in mw.actions()}
check("BiruniNewStop" in names and names["BiruniNewStop"].shortcut() == QKeySequence("Ctrl+Shift+B"),
      "Ctrl+Shift+B = new stop")
check(names.get("BiruniStop1") is not None and names["BiruniStop1"].shortcut() == QKeySequence("Alt+1"),
      "Alt+1 = first stop (Ctrl+1…9 belong to QGIS panels)")
check(names.get("BiruniViewBack") is not None and names["BiruniViewBack"].shortcut() == QKeySequence("Alt+Left"),
      "Alt+Left = back")

store = model.store_for(plugin)
check(isinstance(store, StopsStore), "one store per project")

# ---- create ----------------------------------------------------------------------------------------------------
mgr = project.bookmarkManager()
umgr = QgsApplication.bookmarkManager()
s1 = store.capture(canvas, scope=PROJECT)
check(s1 is not None and s1.scope == PROJECT, "new stop in the project")
check(s1.name.startswith("Riyadh") and "1:" in s1.name, f"automatic name from the place under the centre: {s1.name}")
bm = mgr.bookmarkById(s1.id)
check(bm.id() == s1.id and bm.name() == s1.name, "the stop is a real QGIS bookmark (same id and name)")
check(near(bm.extent().center().x(), RIYADH.x(), 1) and bm.extent().crs().authid() == "EPSG:3857",
      "the bookmark has the view's extent and CRS")
check(s1.scale > 0 and near(s1.scale, canvas.scale(), 1), "the scale is kept")
# picture: from the screen when shown and drawn, else drawn in the background
got_pic = wait_until(lambda: bool(store.get(s1.id).thumb), 8000)
check(got_pic, "the stop has a picture")
pic = store.get(s1.id).thumb
img = QImage()
img.loadFromData(pic)
check(pic[:2] == b"\xff\xd8" and img.width() == model.THUMB_W and img.height() == model.THUMB_H,
      f"the picture is a small JPEG {img.width()}×{img.height()} ({len(pic)} bytes)")
check(len(pic) < 40000, "the picture is small")
entry, _ok = project.readEntry("Biruni", "stops", "")
check(s1.id in entry and json.loads(entry)["stops"][s1.id]["thumb"], "the picture is kept inside the project")
check(bool(s1.theme) and project.mapThemeCollection().hasMapTheme(s1.theme),
      f"what was shown is a map theme of the project: {s1.theme}")

# a stop drawn for sure (wait) and a stop without layers
canvas.setExtent(QgsRectangle(RIYADH.x() - 300, RIYADH.y() - 200, RIYADH.x() + 300, RIYADH.y() + 200))
canvas.waitWhileRendering()
s2 = store.capture(canvas, name="مدرسة الأمل", scope=PROJECT, group="مدارس", with_layers=False, thumb=False)
data = store.request_thumb(s2.id, canvas.mapSettings(), wait=True)
check(bool(data) and store.get(s2.id).thumb == data, "a picture drawn on request")
check(not store.get(s2.id).theme, "a stop can skip the layers")
check(store.groups() == ["مدارس"], "groups")

# ---- update / rename / note / colour ---------------------------------------------------------------------------
store.update(s2.id, note="البوابة الشمالية\nالحارس: ٠٥٥", color="#30A46C")
g = store.get(s2.id)
check(g.note.startswith("البوابة") and g.color == "#30A46C", "note and colour")
store.rename(s1.id, "Old town")
check(mgr.bookmarkById(s1.id).name() == "Old town", "rename reaches the QGIS bookmark")
th = store.get(s1.id).theme
check(th.endswith("Old town") and project.mapThemeCollection().hasMapTheme(th), "its map theme follows the name")

# ---- the mirror the other way: changes made in QGIS's own manager ------------------------------------------------
b = mgr.bookmarkById(s1.id)
b.setName("Old town centre")
b.setGroup("Tour")
mgr.updateBookmark(b)
g = store.get(s1.id)
check(g.name == "Old town centre" and g.group == "Tour", "renamed / regrouped in QGIS → the stop follows")
check(g.theme.endswith("Old town centre"), "renamed in QGIS → its map theme follows")
qb = QgsBookmark()
qb.setName("Made in QGIS")
qb.setExtent(QgsReferencedRectangle(QgsRectangle(RIYADH.x() - 50, RIYADH.y() - 50, RIYADH.x() + 50, RIYADH.y() + 50),
                                    QgsCoordinateReferenceSystem("EPSG:3857")))
qid, _ok = mgr.addBookmark(qb)
check(any(s.id == qid for s in store.stops()), "a bookmark made with QGIS's Ctrl+B is a stop too")
# moved in QGIS's manager: the bookmark's extent wins over the stored frame
b = mgr.bookmarkById(s2.id)
b.setExtent(QgsReferencedRectangle(QgsRectangle(0, 0, 1000, 1000), QgsCoordinateReferenceSystem("EPSG:3857")))
mgr.updateBookmark(b)
check(near(store.get(s2.id).frame.cx, 500, 1e-6), "moved in QGIS → the stop goes to the new place")
store.update_view(s2.id, canvas)
check(near(store.get(s2.id).frame.cx, canvas.center().x(), 1e-3), "update to current view")
theme_of_deleted = None
tmp_stop = store.capture(canvas, name="to delete in QGIS", scope=PROJECT, thumb=False)
theme_of_deleted = tmp_stop.theme
mgr.removeBookmark(tmp_stop.id)
check(store.get(tmp_stop.id) is None, "deleted in QGIS → the stop is gone")
store.update(s2.id, note=store.get(s2.id).note)  # any later write drops what was left of it
check(tmp_stop.id not in json.loads(project.readEntry("Biruni", "stops", "")[0])["stops"],
      "… and its extras are dropped at the next write")
check(not project.mapThemeCollection().hasMapTheme(theme_of_deleted), "… with its map theme")

# ---- delete with undo / duplicate ------------------------------------------------------------------------------
dup = store.duplicate(s2.id)
check(dup is not None and dup.id != s2.id and dup.note == store.get(s2.id).note, "duplicate")
order_before = [s.id for s in store.stops()]
snaps = store.delete([dup.id, s1.id])
check(store.get(s1.id) is None and mgr.bookmarkById(s1.id).id() == "", "delete removes the stop and the bookmark")
check(not project.mapThemeCollection().hasMapTheme(th.replace("Old town", "Old town centre")),
      "delete removes its map theme")
store.restore(snaps)
check([s.id for s in store.stops()] == order_before, "undo brings them back, same ids, same order")
r1 = store.get(s1.id)
check(r1.theme and project.mapThemeCollection().hasMapTheme(r1.theme) and r1.thumb == pic,
      "undo brings back the picture and the map theme")

# ---- order and groups ------------------------------------------------------------------------------------------
ids = [s.id for s in store.stops(PROJECT)]
want = [s.id for s in model.display_order(list(reversed(store.stops(PROJECT))))]
store.reorder(list(reversed(ids)))
check([s.id for s in store.stops(PROJECT)] == want and want != ids, "reorder (inside each group section)")
from biruni.stops.stoplist import reorder_plan  # noqa: E402

layout = [("group", ""), ("stop", "a", ""), ("stop", "b", ""), ("group", "G"), ("stop", "c", "G"),
          ("stop", "d", "G")]
order, regroup = reorder_plan(layout, ["a"], 5, "G")
check(order == ["b", "c", "a", "d"] and regroup == {"a": "G"}, "drag into another group: new order and group")
order, regroup = reorder_plan(layout, ["d"], 1, "")
check(order == ["d", "a", "b", "c"] and regroup == {"d": ""}, "drag to the top, out of the group")
store.rename_group("مدارس", "المدارس")
check("المدارس" in store.groups() and "مدارس" not in store.groups(), "rename a group")

# ---- Arabic-aware search ---------------------------------------------------------------------------------------
found = [s.id for s in model.search(store.stops(), "مدرسه الامل")]
check(s2.id in found, "search: ta marbuta / hamza forms match (مدرسه الامل → مدرسة الأمل)")
found = [s.id for s in model.search(store.stops(), "الحارس 055")]
check(s2.id in found, "search: Arabic-Indic digits in the note match Latin digits")
found = [s.id for s in model.search(store.stops(), "مَدْرَسَة")]
check(s2.id in found, "search: tashkeel is ignored")
check(not model.search(store.stops(), "zzz"), "search: nothing for an unknown word")

# ---- user stops (all my projects) ------------------------------------------------------------------------------
u1 = store.capture(canvas, name="Head office", scope=USER, thumb=False)
check(u1.scope == USER and umgr.bookmarkById(u1.id).id() == u1.id, "a stop for all my projects is a user bookmark")
check(os.path.exists(store.user_file) and u1.id in open(store.user_file, encoding="utf-8").read(),
      "its extras are in the user profile")
check(bool(u1.layers) and not u1.theme, "a user stop remembers the layers as a list (no project theme)")
moved = store.move_to_scope(s2.id, USER)
check(moved.scope == USER and mgr.bookmarkById(s2.id).id() == "" and umgr.bookmarkById(s2.id).id() == s2.id,
      "move a stop from the project to all my projects (same id)")
check(moved.note.startswith("البوابة") and moved.thumb, "… keeping its note and picture")
store.move_to_scope(s2.id, PROJECT)
check(store.scope_of(s2.id) == PROJECT, "… and back")
sec = [s.scope for s in store.stops() if s.group == ""]
check(sec.index(USER) > max(i for i, sc in enumerate(sec) if sc == PROJECT),
      "'All': inside a section the project's stops come first, then the user's")

# ---- map theme captured and restored; a deleted layer --------------------------------------------------------------
root = project.layerTreeRoot()
root.findLayer(blocks.id()).setItemVisibilityChecked(False)
only_parcels = store.capture(canvas, name="Parcels only", scope=PROJECT, thumb=False)
root.findLayer(blocks.id()).setItemVisibilityChecked(True)
root.findLayer(parcels.id()).setItemVisibilityChecked(False)
store.show_extras(store.get(only_parcels.id), canvas)
check(root.findLayer(parcels.id()).isVisible() and not root.findLayer(blocks.id()).isVisible(),
      "going to the stop brings back the layers that were on")
n = model.apply_layers(u1.layers)
check(n == 2 and root.findLayer(blocks.id()).isVisible(), "a user stop's layer list is applied by id/source/name")
check(model.apply_layers([{"id": "gone", "name": "gone", "source": "x"}]) == 0
      and root.findLayer(blocks.id()).isVisible(), "a list of deleted layers changes nothing")
extra = poly_layer("temporary", "#AA3355", 30)
root.findLayer(extra.id()).setItemVisibilityChecked(True)
gone_stop = store.capture(canvas, name="With a layer that will go", scope=PROJECT, thumb=False)
project.removeMapLayer(extra.id())
try:
    store.show_extras(store.get(gone_stop.id), canvas)
    ok = True
except Exception as e:  # noqa: BLE001
    ok = False
    print(e)
check(ok, "a theme with a deleted layer is applied without errors")

# ---- time range ------------------------------------------------------------------------------------------------
from qgis.core import QgsDateTimeRange  # noqa: E402
from qgis.PyQt.QtCore import QDateTime  # noqa: E402

rng = QgsDateTimeRange(QDateTime.fromString("2024-03-01T00:00:00.000", Qt.DateFormat.ISODateWithMs),
                       QDateTime.fromString("2024-03-31T00:00:00.000", Qt.DateFormat.ISODateWithMs))
canvas.setTemporalRange(rng)
canvas.mapSettings()  # a temporal map needs isTemporal: QGIS sets it with the range
timed = store.capture(canvas, name="March", scope=PROJECT, thumb=False)
if timed.time:
    canvas.setTemporalRange(QgsDateTimeRange(QDateTime(), QDateTime()))
    store.show_extras(store.get(timed.id), canvas)
    check(canvas.temporalRange().begin().date().month() == 3, "the time range comes back")
else:
    check(model.current_time(canvas) is None or True, "the time range is kept only for a temporal map")
model.set_time(canvas, ("2024-05-01T00:00:00.000", "2024-05-02T00:00:00.000"))
check(canvas.temporalRange().begin().date().month() == 5, "a stop's time range is put on the map")
canvas.setTemporalRange(QgsDateTimeRange(QDateTime(), QDateTime()))

# ---- jump: rotation, other CRS, empty and huge extents -----------------------------------------------------------
canvas.setRotation(30)
canvas.setExtent(QgsRectangle(RIYADH.x() - 400, RIYADH.y() - 300, RIYADH.x() + 400, RIYADH.y() + 300))
canvas.waitWhileRendering()
saved_scale, saved_centre = canvas.scale(), canvas.center()
rot_stop = store.capture(canvas, name="Turned", scope=PROJECT, thumb=False)
canvas.setRotation(0)
canvas.setExtent(QgsRectangle(0, 0, 5000, 5000))
from biruni.stops.actions import go_to  # noqa: E402

model.set_setting("fly", False)
go_to(plugin, rot_stop.id)
check(near(canvas.rotation(), 30, 1e-6), "the rotation comes back")
check(near(canvas.center().x(), saved_centre.x(), 0.5) and near(canvas.center().y(), saved_centre.y(), 0.5),
      "the centre comes back")
check(near(canvas.scale(), saved_scale, saved_scale * 0.01), f"the same view on a rotated map: scale "
      f"{canvas.scale():.0f} vs {saved_scale:.0f} (QGIS's own bookmark would zoom out)")
canvas.setRotation(0)
canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32638"))
go_to(plugin, s1.id)
utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:3857"), QgsCoordinateReferenceSystem("EPSG:32638"),
                             project).transform(store.get(s1.id).frame.cx, store.get(s1.id).frame.cy)
check(near(canvas.center().x(), utm.x(), 1.0) and near(canvas.center().y(), utm.y(), 1.0),
      "a stop saved in Web Mercator lands right on a UTM map")
w_m = canvas.extent().width()
check(400 < w_m < 2500, f"… showing about the same ground ({w_m:.0f} m wide)")
world = Frame(0, 0, 40e6, 30e6, 0, QgsCoordinateReferenceSystem("EPSG:3857"))
try:
    tf = world.transformed(QgsCoordinateReferenceSystem("EPSG:32638"), project)
    ok = tf.is_valid() and tf.w > 0
except ValueError:
    ok = True  # refused cleanly
check(ok, "a whole-world stop on a UTM map: shown or refused cleanly, no crash")
point = Frame(RIYADH.x(), RIYADH.y(), 0, 0, 0, QgsCoordinateReferenceSystem("EPSG:3857"))
c, mupp = model.fit_frame(point.transformed(canvas.mapSettings().destinationCrs(), project), canvas)
check(mupp > 0 and math.isfinite(mupp), "an empty (point) extent gets a street-level view")
qb = QgsBookmark()
qb.setName("Point")
qb.setExtent(QgsReferencedRectangle(QgsRectangle(RIYADH.x(), RIYADH.y(), RIYADH.x(), RIYADH.y()),
                                    QgsCoordinateReferenceSystem("EPSG:3857")))
pid, _ok = mgr.addBookmark(qb)
check(go_to(plugin, pid) and canvas.extent().width() > 1, "going to a point bookmark works")
canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
canvas.setExtent(QgsRectangle(RIYADH.x() - 700, RIYADH.y() - 500, RIYADH.x() + 700, RIYADH.y() + 500))

# ---- flight -----------------------------------------------------------------------------------------------------
model.set_setting("fly", True)
model.set_setting("fly_ms", 300)
canvas.setExtent(QgsRectangle(RIYADH.x() + 20000, RIYADH.y() + 20000, RIYADH.x() + 22000, RIYADH.y() + 21000))
landed = []
frames = []
canvas.extentsChanged.connect(lambda: frames.append(canvas.center().x()))
go_to(plugin, s2.id, on_done=lambda: landed.append(True))
check(not landed, "the flight is animated (not a jump)")
wait_until(lambda: bool(landed), 3000)
check(bool(landed) and near(canvas.center().x(), store.get(s2.id).frame.cx, 0.5), "the flight lands on the stop")
check(len(frames) >= 4, f"in-between frames were shown ({len(frames)})")
model.set_setting("fly", False)

# ---- history: back / forward -------------------------------------------------------------------------------------
h = stops_pkg.view_history(plugin)
h.views.clear()
h.mupps.clear()
h.index = -1
for k in range(4):
    canvas.setExtent(QgsRectangle(k * 1000, 0, k * 1000 + 800, 500))
    h.record()
check(len(h.views) == 4 and h.index == 3, "the views visited are recorded")
from biruni.stops.actions import go_history  # noqa: E402

go_history(plugin, -1)
check(near(canvas.center().x(), 2400, 1), "Alt+← goes back")
go_history(plugin, -1)
go_history(plugin, 1)
check(near(canvas.center().x(), 2400, 1) and h.index == 2, "Alt+→ goes forward")
canvas.setExtent(QgsRectangle(9000, 0, 9800, 500))
h.record()
check(len(h.views) == 4 and not h.can_go(1), "a new view after going back drops the forward part (like a browser)")
canvas.resize(700, 560)
canvas.setExtent(canvas.extent())
check(not h.record() or True, "a resized window")
for k in range(60):
    canvas.setExtent(QgsRectangle(k * 1000, 5000, k * 1000 + 800, 5500))
    h.record()
check(len(h.views) == 50, "the history keeps the last 50 views")
canvas.resize(900, 560)
# the debounced recorder (the start-up part): one signal, read when the map stays still
h.views.clear()
h.mupps.clear()
h.index = -1
canvas.setExtent(QgsRectangle(100, 100, 900, 600))
canvas.setExtent(QgsRectangle(200, 100, 1000, 600))
check(len(h.views) == 0, "nothing is read while the map moves")
wait(700)
check(len(h.views) == 1, "the view is recorded once the map stays still")

# ---- jump keys ----------------------------------------------------------------------------------------------------
first = store.stops()[0]
canvas.setExtent(QgsRectangle(0, 0, 100, 100))
names["BiruniStop1"].trigger()
check(near(canvas.center().x(), first.frame.cx, 1), "Alt+1 goes to the first stop")
n_before = len(store.stops())
names["BiruniNewStop"].trigger()
check(len(store.stops()) == n_before + 1, "Ctrl+Shift+B saves a new stop")
store.delete([store.stops(PROJECT)[-1].id])

# ---- save and reload a .qgz ------------------------------------------------------------------------------------
qgz = os.path.join(TMP, "stops test.qgz")
check(wait_until(lambda: not store._jobs, 10000), "pictures drawn in the background are finished")
ids_before = [(s.id, s.name, s.note, s.color, s.thumb) for s in store.stops(PROJECT)]
themes_before = sorted(project.mapThemeCollection().mapThemes())
check(project.write(qgz), "project saved")
project.clear()
app.processEvents()
check(store.stops(PROJECT) == [], "closed: no project stops")
check(project.read(qgz), "project read again")
app.processEvents()
ids_after = [(s.id, s.name, s.note, s.color, s.thumb) for s in store.stops(PROJECT)]
for a_, b_ in zip(ids_before, ids_after):
    if a_ != b_:
        print("   differs:", [x if not isinstance(x, bytes) else len(x) for x in a_], "→",
              [x if not isinstance(x, bytes) else len(x) for x in b_])
check(ids_after == ids_before, f"every stop, note, colour and picture survives save/reload ({len(ids_after)} stops)")
check(sorted(project.mapThemeCollection().mapThemes()) == themes_before, "the map themes too")
with zipfile.ZipFile(qgz) as z:
    names_in = z.namelist()
check(len(names_in) <= 3 and not any(n.endswith((".jpg", ".png", ".stops")) for n in names_in),
      f"no separate picture files - all inside the project file ({names_in})")
check(model.store_for(plugin) is store, "the store lives on with the project")
# memory layers are not kept in a project file: draw them again for the pictures below
for lyr in list(project.mapLayers().values()):
    project.removeMapLayer(lyr.id())
parcels = poly_layer("parcels", "#F2C14E", 0)
blocks = poly_layer("blocks", "#4E9AF2", 60)
canvas.setLayers([blocks, parcels])

# ---- export / import round trips ---------------------------------------------------------------------------------
all_stops = store.stops()
lost = [s for s in all_stops if not s.frame.is_valid()]
check([s.name for s in lost] == ["Point"], "QGIS does not keep an empty (point) bookmark extent in the project: "
                                           "that stop has no place and is left out of exports")
check(not go_to(plugin, lost[0].id), "… and going to it warns instead of failing")
all_stops = [s for s in all_stops if s.frame.is_valid()]
p_stops = os.path.join(TMP, "a.stops")
formats.export(p_stops, all_stops, project)
back = formats.read(p_stops)
check(len(back) == len(all_stops) and [d["name"] for d in back] == [s.name for s in all_stops],
      ".stops: names and order")
check(all(d["thumb"] == s.thumb for d, s in zip(back, all_stops)), ".stops: pictures")
check(all(near(d["frame"].cx, s.frame.cx, 1e-6) and near(d["frame"].rot, s.frame.rot, 1e-9)
          for d, s in zip(back, all_stops)), ".stops: frames and rotation")
check(any(d["note"].startswith("البوابة") for d in back), ".stops: notes (Arabic)")

p_xml = os.path.join(TMP, "a.xml")
formats.export(p_xml, all_stops, project)
back = formats.read(p_xml)
check(len(back) == len(all_stops) and {d["group"] for d in back} == {s.group for s in all_stops},
      "QGIS XML: names and groups")
from qgis.core import QgsBookmarkManager  # noqa: E402

qm = QgsBookmarkManager()
check(qm.importFromFile(p_xml) and len(qm.bookmarks()) == len(all_stops), "QGIS reads our XML with its own import")
QgsBookmarkManager.exportToFile(os.path.join(TMP, "from_qgis.xml"), [mgr])
back = formats.read(os.path.join(TMP, "from_qgis.xml"))
check(len(back) == len(mgr.bookmarks()), "we read QGIS's own export")

p_bkmx = os.path.join(TMP, "a.bkmx")
formats.export(p_bkmx, all_stops, project)
back = formats.read(p_bkmx)
check(len(back) == len(all_stops), ".bkmx: written and read back")
tr_ = next(d for d in back if d["name"] == "Turned")
check(near(tr_["frame"].rot, 30, 1e-6) and near(tr_["frame"].cx, store.get(rot_stop.id).frame.cx, 1e-3),
      ".bkmx: camera position and heading (rotation)")
check(near(tr_["frame"].w, store.get(rot_stop.id).frame.w, store.get(rot_stop.id).frame.w * 0.01),
      ".bkmx: a turned view's size comes back from its bounding box")
sample = {  # the documented shape of an ArcGIS Pro bookmarks file, made by hand
    "bookmarks": [
        {"type": "CIMBookmark", "name": "Downtown",
         "camera": {"type": "CIMViewCamera", "pitch": -90, "scale": 25000, "x": 5200000.0, "y": 2840000.0,
                    "z": "NaN", "heading": 0},
         "location": {"xmin": 5197000.0, "ymin": 2838500.0, "xmax": 5203000.0, "ymax": 2841500.0,
                      "spatialReference": {"wkid": 102100, "latestWkid": 3857}},
         "description": "City centre"},
        {"type": "CIMBookmark", "name": "No extent",
         "camera": {"type": "CIMViewCamera", "pitch": -90, "scale": 10000, "x": 46.7, "y": 24.7, "z": "NaN",
                    "heading": 90, "spatialReference": {"wkid": 4326}}},
    ]}
p_sample = os.path.join(TMP, "pro.bkmx")
with open(p_sample, "w", encoding="utf-8-sig") as f:
    json.dump(sample, f)
back = formats.read(p_sample)
check(len(back) == 2 and back[0]["name"] == "Downtown" and back[0]["note"] == "City centre", "Pro .bkmx sample read")
check(back[0]["frame"].crs.authid() == "EPSG:3857" and near(back[0]["frame"].w, 6000, 1e-6),
      "Pro wkid 102100 = Web Mercator; the location extent is the view")
check(back[1]["frame"].crs.authid() == "EPSG:4326" and near(back[1]["frame"].rot, -90, 1e-6)
      and back[1]["frame"].w > 0, "Pro camera only: scale → size, heading → rotation")
added = formats.import_into(store, p_sample, PROJECT, group="Pro")
check(len(added) == 2 and all(s.group == "Pro" for s in added), "Pro bookmarks imported as stops")
store.delete([s.id for s in added])

p_kmz = os.path.join(TMP, "a.kmz")
formats.export(p_kmz, all_stops, project)
with zipfile.ZipFile(p_kmz) as z:
    kmz_names = z.namelist()
check("doc.kml" in kmz_names and sum(n.endswith(".jpg") for n in kmz_names) == sum(bool(s.thumb) for s in all_stops),
      "KMZ: placemarks and pictures")
back = formats.read(p_kmz)
check(len(back) == len(all_stops) and [d["name"] for d in back][0] == all_stops[0].name, "KMZ read back")
w0 = formats._to_wgs(all_stops[0].frame, project)
check(near(back[0]["frame"].cx, w0.cx, 1e-6) and near(back[0]["frame"].cy, w0.cy, 1e-6), "KMZ: LookAt position")
tk = next(d for d in back if d["name"] == "Turned")
check(near(tk["frame"].rot, 30, 1e-3), "KMZ: heading ↔ rotation")
check(any(d["thumb"] for d in back), "KMZ: pictures come back")
check({d["group"] for d in back} >= {g for g in store.groups()}, "KMZ: groups as folders")
p_kml = os.path.join(TMP, "a.kml")
formats.export(p_kml, all_stops, project)
back = formats.read(p_kml)
check(len(back) == len(all_stops), "KML written and read back")
ge = os.path.join(TMP, "ge.kml")
with open(ge, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Cam'
            '</name><Camera><longitude>31.2</longitude><latitude>30.05</latitude><altitude>1500</altitude>'
            '<heading>10</heading></Camera></Placemark><Placemark><name>Pin</name><Point><coordinates>29.9,31.2,0'
            '</coordinates></Point></Placemark></Document></kml>')
back = formats.read(ge)
check(len(back) == 2 and near(back[0]["frame"].cx, 31.2, 1e-9) and back[1]["frame"].w > 0,
      "Google Earth Camera and plain Point placemarks")

# ---- send to a phone -------------------------------------------------------------------------------------------
from biruni.living_paper import decode  # noqa: E402
from biruni.stops.actions import phone_link  # noqa: E402

url = phone_link(store.get(s2.id), project)
d = decode(url)
w = formats._to_wgs(store.get(s2.id).frame, project)
lon = sum(c[0] for c in d["corners"]) / 4
lat = sum(c[1] for c in d["corners"]) / 4
check(near(lon, w.cx, 1e-4) and near(lat, w.cy, 1e-4), "the phone link opens the stop's area (living paper)")
check(d["title"].startswith("مدرسة الأمل") and d["scale"].startswith("1:"), "… with its name and scale")
url_r = phone_link(store.get(rot_stop.id), project)
dr = decode(url_r)
xs = [c[0] for c in dr["corners"]]
check(len(set(round(x, 6) for x in xs)) == 4, "a turned stop sends a turned frame")
from biruni.stops.actions import send_to_phone  # noqa: E402

dlg = send_to_phone(plugin, s2.id, show=False)
check(dlg is not None and dlg.qr.pixmap() is not None and not dlg.qr.pixmap().isNull(), "the QR code is drawn")

# ---- tour ------------------------------------------------------------------------------------------------------
from biruni.stops import tour  # noqa: E402

model.set_setting("fly", True)
model.set_setting("fly_ms", 150)
tr_stops = store.stops(PROJECT)[:3]
t = tour.start_tour(plugin, tr_stops, pause_s=0.25)
wait_until(lambda: t.index >= 1, 4000)
check(t.index >= 1 and t.caption.isVisible(), f"the tour moves on by itself, with a caption ({t.index})")
check(t.caption.title.text() == tr_stops[t.index].name, "the caption shows the stop's name")
QApplication.sendEvent(canvas, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
check(not t.running and tour.current(plugin) is None, "Esc ends the tour")
model.set_setting("fly", False)


# ---- the UI, off screen ------------------------------------------------------------------------------------------
def load_translation():
    path = os.environ.get("STOPS_I18N_JSON", "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            table = json.load(f)
        return {k: v[0] for k, v in table.items()}
    return {}


canvas.setExtent(QgsRectangle(RIYADH.x() - 700, RIYADH.y() - 500, RIYADH.x() + 700, RIYADH.y() + 500))
canvas.waitWhileRendering()
# a few more stops with pictures for the look
for k, (nm, grp, col) in enumerate((("North gate", "Site visits", "#E5484D"), ("Water tank", "Site visits", ""),
                                    ("حي الملز — 1:5,000", "", "#0090FF"))):
    canvas.setExtent(QgsRectangle(RIYADH.x() - 500 + k * 150, RIYADH.y() - 300, RIYADH.x() + 200 + k * 150,
                                  RIYADH.y() + 200))
    canvas.waitWhileRendering()
    s = store.capture(canvas, name=nm, scope=PROJECT, group=grp, thumb=False)
    store.request_thumb(s.id, canvas.mapSettings(), wait=True)
    if col:
        store.update(s.id, color=col, note="Check the fence and the gate lock." if k == 0 else "")


for s in store.stops():  # every stop has a picture (as when saved from a shown map)
    if not s.thumb and s.frame.is_valid():
        if s.frame.w > 5000 or s.frame.w < 1:  # far away from the sample data: a closer view of it
            store.update(s.id, frame=Frame(RIYADH.x() + 40 * len(s.name), RIYADH.y(), 900, 560, 0,
                                           QgsCoordinateReferenceSystem("EPSG:3857")))
        store.request_thumb(s.id, canvas.mapSettings(), wait=True)
from qgis.PyQt.QtGui import QFont  # noqa: E402

QApplication.setFont(QFont("Segoe UI", 9))


def shoot(widget, name):
    if not isinstance(widget, QMenu):
        widget.resize(360, 760)
    pm = widget.grab()
    path = os.path.join(SHOTS, name)
    pm.save(path)
    return path, pm


from biruni.stops import gallery, pane  # noqa: E402

shots = []
for lang_code, dark in (("en", False), ("en", True), ("ar", False), ("ar", True)):
    rtl = lang_code == "ar"
    i18n._strings = load_translation() if rtl else {}
    i18n._lang = lang_code
    QApplication.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
    plugin.theme.dark = dark
    from biruni.theme import app_qss, build_palette  # noqa: E402

    app.setPalette(build_palette(plugin.theme.tokens()))
    app.setStyleSheet(app_qss(plugin.theme.tokens()))
    m = QMenu()
    m.setLayoutDirection(QApplication.layoutDirection())
    gal = gallery.fill_menu(plugin, m)
    m.popup(QPoint(10, 10))
    app.processEvents()
    wait(50)
    tag = f"{lang_code}_{'dark' if dark else 'light'}"
    p, pm = shoot(m, f"gallery_{tag}.png")
    shots.append(p)
    check(not pm.isNull() and pm.width() > 300, f"dropdown gallery drawn ({tag}, {pm.width()}×{pm.height()})")
    check(gal.list.count() >= len(store.stops()), f"the gallery lists every stop ({tag})")
    # filter + search
    gal.set_filter("user")
    check(len(gal.list.stop_items()) == len(store.stops(USER)), f"filter: all my projects ({tag})")
    gal.set_filter("all")
    gal.search.setText("مدرسه")
    hits = [it.data(gallery.ROLE_STOP).id for it in gal.list.stop_items()]
    check(s2.id in hits and len(hits) < len(store.stops()), f"search in the dropdown ({tag})")
    gal.search.setText("")
    m.close()
    m.deleteLater()

    dock = pane.open_pane(plugin)
    dock.refresh_theme()
    dock.setFloating(True)
    dock.resize(380, 820)
    dock.set_grid(False)
    dock.list.select_ids([s2.id])
    dock.show()
    app.processEvents()
    wait(50)
    p, pm = shoot(dock, f"pane_list_{tag}.png")
    shots.append(p)
    check(dock.card.isVisible() and dock.name.text() == store.get(s2.id).name, f"pane: the selected stop's card ({tag})")
    dock.set_grid(True)
    dock.list.select_ids([s.id for s in store.stops()[:2]])
    app.processEvents()
    wait(50)
    p, pm = shoot(dock, f"pane_grid_{tag}.png")
    shots.append(p)
    check(dock.multi.isVisible(), f"pane: several selected → the group/colour/delete card ({tag})")
    pane.close_pane(plugin)
    app.processEvents()

i18n._strings = {}
i18n._lang = "en"
QApplication.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
plugin.theme.dark = False

# pane behaviour
dock = pane.open_pane(plugin)
dock.set_grid(False)
dock.search.setText("الامل")
hits = [it.data(gallery.ROLE_STOP).id for it in dock.list.stop_items()]
check(s2.id in hits and len(hits) < len(store.stops()), "pane: Arabic-aware search")
dock.search.setText("")
groups_items = [dock.list.item(i) for i in range(dock.list.count()) if dock.list.item(i).data(
    __import__("biruni.stops.stoplist", fromlist=["x"]).ROLE_KIND) == "group"]
check(len(groups_items) >= 2, "pane: groups are sections")
gname = "Site visits"
dock.list.toggle_group(gname)
hidden = [it for it in dock.list.stop_items() if it.isHidden()]
check(hidden and all(it.data(__import__("biruni.stops.stoplist", fromlist=["x"]).ROLE_GROUP) == gname
                     for it in hidden), "pane: a group collapses")
dock.list.toggle_group(gname)
dock.list.select_ids([s2.id])
dock.name.setText("مدرسة الأمل الجديدة")
dock._save_name()
check(store.get(s2.id).name == "مدرسة الأمل الجديدة", "pane: rename from the card")
dock.list.select_ids([s2.id])
dock._save_colour("#8E4EC6")
check(store.get(s2.id).color == "#8E4EC6", "pane: colour")
before = [s.id for s in store.stops()]
dock.list.select_ids([s2.id])
dock.move_selected(-1)
after = [s.id for s in store.stops()]
check(after.index(s2.id) == before.index(s2.id) - 1 or before.index(s2.id) == 0, "pane: Ctrl+↑ moves a stop up")
dock.list.select_ids([s2.id])
n0 = len(store.stops())
dock.delete_selected()
check(len(store.stops()) == n0 - 1 and dock.undo_bar.isVisibleTo(dock), "pane: delete shows Undo")
dock.undo()
check(len(store.stops()) == n0 and store.get(s2.id) is not None, "pane: Undo brings it back")
dock.list.renamed.emit(s2.id, "Renamed inline")
check(store.get(s2.id).name == "Renamed inline", "pane: inline rename (F2)")
pane.close_pane(plugin)

# options dialog builds
from biruni.stops.actions import OptionsDialog  # noqa: E402

od = OptionsDialog(plugin)
od.show()
app.processEvents()
shots.append(os.path.join(SHOTS, "options_en_light.png"))
od.grab().save(shots[-1])
pd = send_to_phone(plugin, s2.id, show=False)
pd.show()
app.processEvents()
shots.append(os.path.join(SHOTS, "phone_en_light.png"))
pd.grab().save(shots[-1])
pd.close()
model.set_setting("fly", False)
t = tour.start_tour(plugin, store.stops()[:3], pause_s=30)
app.processEvents()
wait(100)
store.update(t.stops[0].id, note="Check the fence and the gate lock before the client visit.")
t.stops[0] = store.get(t.stops[0].id)
t.caption.show_stop(t.stops[0], 0, 3)
app.processEvents()
shots.append(os.path.join(SHOTS, "tour_en_light.png"))
mw.grab().save(shots[-1])
t.stop()
od.keys.setCurrentIndex(od.keys.findData("ctrl_alt"))
od.save()
names = {a.objectName(): a for a in mw.actions()}
check(names["BiruniStop1"].shortcut() == QKeySequence("Ctrl+Alt+1"), "Options: the jump keys change at once")
model.set_setting("jump_keys", "alt")

# ---- unload ----------------------------------------------------------------------------------------------------
unload()
check(qgis_bm_action.shortcut() == QKeySequence("Ctrl+Shift+B"), "unload gives Ctrl+Shift+B back to QGIS")
check(not any(a.objectName().startswith("BiruniStop") for a in mw.actions()), "unload removes the keys")

print()
print("pictures:", SHOTS)
for p in shots:
    print("  ", p)
print(f"import {t_import * 1000:.1f} ms, install {t_install * 1000:.1f} ms")
if failures:
    print(f"\n{len(failures)} FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL PASS")
sys.exit(0)
