r"""The friendlier Layers panel, off screen: our icons on the panel's buttons, live search, the empty-map card, layer
health badges (each problem appears and clears), solo peek (exact restore), smart arrange (preview, apply, undo),
"made with a tool" and a clean unload.

Run:  engine\bin\python-qgis.bat tests\test_layers_panel.py
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsLayerTreeModel,  # noqa: E402
                       QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.gui import (QgsLayerTreeMapCanvasBridge, QgsLayerTreeView, QgsLegendFilterButton,  # noqa: E402
                      QgsMapCanvas)
from qgis.PyQt.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer  # noqa: E402
from qgis.PyQt.QtGui import QAction, QKeyEvent, QMouseEvent  # noqa: E402
from qgis.PyQt.QtWidgets import (QDockWidget, QMainWindow, QMenu, QToolBar, QToolButton, QVBoxLayout,  # noqa: E402
                                 QWidget)

from biruni import icons  # noqa: E402
from biruni import layers_panel  # noqa: E402
from biruni.layers_panel import arrange, search, toolbar  # noqa: E402
from biruni.layers_panel.health import SLOW_SECONDS, nice_scale  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=450):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class Bar:
    notices = []

    def _push(self, title, text, *a):
        Bar.notices.append(text)

    pushInfo = pushSuccess = pushWarning = _push


def stock_toolbar():
    """The panel's toolbar as the engine builds it (same texts, same kinds of items, same order)."""
    tb = QToolBar()
    style = QAction("Layer Styling", tb)
    style.setToolTip("Open the Layer Styling panel")
    style.setCheckable(True)
    style.setShortcut("F7")
    add_group = QAction("Add Group", tb)
    themes = QToolButton()
    themes.setToolTip("Manage Map Themes")
    themes.setMenu(QMenu(themes))
    filt = QAction("Filter Legend by Map Content", tb)
    filt.setCheckable(True)
    legend = QgsLegendFilterButton()
    expand = QAction("Expand All", tb)
    collapse = QAction("Collapse All", tb)
    remove = QAction("Remove Layer/Group…", tb)
    remove.setObjectName("mActionRemoveLayer")
    tb.addAction(style)
    tb.addAction(add_group)
    tb.addWidget(themes)
    tb.addAction(filt)
    tb.addWidget(legend)
    tb.addAction(expand)
    tb.addAction(collapse)
    tb.addAction(remove)
    return tb, {"style": style, "add_group": add_group, "themes": themes, "filter_map": filt, "filter_expr": legend,
                "expand": expand, "collapse": collapse, "remove": remove}


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.canvas.resize(500, 400)
        self.mw.setCentralWidget(self.canvas)
        self.dock = QDockWidget("Layers", self.mw)
        self.dock.setObjectName("Layers")
        body = QWidget()
        lay = QVBoxLayout(body)
        self.toolbar, self.buttons = stock_toolbar()
        self.view = QgsLayerTreeView()
        root = QgsProject.instance().layerTreeRoot()
        model = QgsLayerTreeModel(root, self.view)
        model.setFlag(QgsLayerTreeModel.Flag.AllowNodeChangeVisibility)
        model.setFlag(QgsLayerTreeModel.Flag.AllowNodeReorder)
        self.view.setModel(model)
        lay.addWidget(self.toolbar)
        lay.addWidget(self.view)
        self.dock.setWidget(body)
        self.mw.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock)
        self.bridge = QgsLayerTreeMapCanvasBridge(root, self.canvas)
        self.active = None
        self.add_feature = QAction("add feature", self.mw)

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas

    def layerTreeView(self):
        return self.view

    def messageBar(self):
        return Bar()

    def setActiveLayer(self, layer):
        self.active = layer
        return True

    def actionAddFeature(self):
        return self.add_feature


class Theme:
    def __init__(self):
        self._listeners = []
        self.colour = "#33414C"

    def tokens(self):
        return {"icon": self.colour, "text": "#1E252B", "text_dim": "#5B6670", "accent": "#0E7C74",
                "accent_text": "#FFFFFF", "accent_hover": "#0A635D", "border": "#DDE2E7", "hover": "#EDF2F5"}

    def on_change(self, fn):
        self._listeners.append(fn)

    def switch(self, colour):
        self.colour = colour
        for fn in list(self._listeners):
            fn()


class Runner:
    def __init__(self):
        self.ran = []
        self.algs = []

    def execute(self, cmd):
        self.ran.append(cmd.id)

    def open_algorithm(self, alg_id):
        self.algs.append(alg_id)


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()
        self.runner = Runner()


work = tempfile.mkdtemp()
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))

# ---- folding for the search ----------------------------------------------------------------------------------------
check(search.normalize("أَحْمَد") == "احمد", "diacritics and hamza forms are ignored")
check(search.normalize("مدرسة") == search.normalize("مدرسه"), "ta marbuta = ha")
check(search.normalize("مـــدرسة") == search.normalize("مدرسة"), "tatweel ignored")
check(search.normalize("Café ١٢") == "cafe 12", "accents and Arabic-Indic digits")
check(search.matches("Roads - main", ["main", "road"]) and not search.matches("Roads", ["rail"]), "all terms must match")
check(nice_scale(37412) == 20000 and nice_scale(9000) == 5000 and nice_scale(0) == 0, "round scales")

# ---- install on an empty map ---------------------------------------------------------------------------------------
plugin = Plugin()
iface = plugin.iface
iface.mw.show()  # offscreen platform: nothing appears on a screen
original_tips = {k: (b.toolTip()) for k, b in iface.buttons.items()}
undo = layers_panel.install(plugin, defer=False)
panel = layers_panel.live_panel(plugin)
check(panel is not None, "the panel additions are built")
colour = plugin.theme.colour
for key, target in iface.buttons.items():
    expected = icons.icon("layers/" + toolbar._ICON_FILES[key], colour)
    check(target.icon().cacheKey() == expected.cacheKey() and target.toolTip().startswith("<b>"),
          f"'{key}' has our icon and a clear tooltip")
plugin.theme.switch("#D3D9DF")
check(iface.buttons["expand"].icon().cacheKey() == icons.icon("layers/expand", "#D3D9DF").cacheKey(),
      "icons follow a theme change")
check(panel.solo_action.icon().cacheKey() == icons.icon("layers/solo", "#D3D9DF").cacheKey(),
      "our own buttons follow it too")
own = [a.objectName() for a in iface.toolbar.actions() if a.objectName().startswith("biruniLayers")]
check(own[1:] == ["biruniLayersSolo", "biruniLayersArrange", "biruniLayersArrangeUndo"], f"our buttons added: {own}")
check(not panel.undo_action.isVisible(), "Undo arrange is hidden until there is something to undo")

# positional fallback: a translated build whose texts are unknown
tb2 = QToolBar()
items = []
for i in range(8):
    if i in (2, 4):
        w = QToolButton()
        tb2.addWidget(w)
        items.append(w)
    else:
        a = QAction(f"؟{i}", tb2)
        tb2.addAction(a)
        items.append(a)
found = toolbar.identify(tb2)
check(len(found) == 8 and found["collapse"][0] is items[6] and found["themes"][1] is items[2],
      "buttons with unknown texts are recognised by their place")

check(not panel.empty.isHidden() and panel.view.isHidden(), "empty map: the friendly card instead of the tree")
check(panel.search_edit.isHidden(), "no search box on an empty map")
panel.empty.add_button.click()
panel.empty.vault_button.click()
check(plugin.runner.ran == ["add_data", "vault"], f"Add data and Open the Vault run their commands: {plugin.runner.ran}")
check("drag" in panel.empty.hint.text().lower(), "the card says files can be dropped")


# ---- some layers ---------------------------------------------------------------------------------------------------
def memory(uri, name, n=0, crs="EPSG:4326"):
    lyr = QgsVectorLayer(f"{uri}?crs={crs}" if uri != "None" else "None", name, "memory")
    feats = []
    for i in range(n):
        f = QgsFeature(lyr.fields())
        if uri == "Point":
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(31 + i * 0.01, 30)))
        elif uri == "LineString":
            f.setGeometry(QgsGeometry.fromWkt(f"LINESTRING({31 + i * 0.01} 30, {31.005 + i * 0.01} 30.01)"))
        elif uri == "Polygon":
            f.setGeometry(QgsGeometry.fromWkt("POLYGON((31 30, 31.1 30, 31.1 30.1, 31 30))"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


points = memory("Point", "مَدْرَسَة الأمل", 5)
lines = memory("LineString", "Main roads", 3)
polys = memory("Polygon", "Parcels", 2)
table = memory("None", "Owners table", 0)
from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

tif = os.path.join(work, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(tif, 10, 10, 1, gdal.GDT_Byte)
ds.SetGeoTransform([31, 0.01, 0, 30.1, 0, -0.01])
ds.SetProjection(QgsCoordinateReferenceSystem("EPSG:4326").toWkt())
ds.GetRasterBand(1).Fill(7)
ds = None
raster = QgsRasterLayer(tif, "Elevation")
basemap = QgsRasterLayer("type=xyz&url=http://127.0.0.1:9/{z}/{x}/{y}.png&zmax=19&zmin=0", "Street map", "wms")
project.addMapLayers([raster, polys, table, points])
root = project.layerTreeRoot()
roads = root.insertGroup(0, "Roads group")
project.addMapLayer(lines, False)
roads.addLayer(lines)
project.addMapLayer(basemap, False)
root.addLayer(basemap).setItemVisibilityChecked(False)  # no tiles fetched while testing
pump()
check(panel.empty.isHidden() and not panel.view.isHidden() and not panel.search_edit.isHidden(),
      "with layers: the tree and the search box are back")

# ---- search ---------------------------------------------------------------------------------------------------------
view = panel.view
model = view.model()


def shown_names(parent=None):
    from qgis.PyQt.QtCore import QModelIndex

    parent = parent or QModelIndex()
    out = []
    for r in range(model.rowCount(parent)):
        idx = model.index(r, 0, parent)
        node = view.index2node(idx)
        if node is None or view.isRowHidden(r, parent):
            continue
        out.append(node.name())
        out += shown_names(idx)
    return out


all_names = shown_names()
from biruni.layers_panel.solo import visibility_state  # noqa: E402

ticks_before_search = visibility_state(root)["layers"]
panel.search_edit.setText("الامل")
pump(300)
check(shown_names() == ["مَدْرَسَة الأمل"], f"Arabic search without diacritics finds the layer: {shown_names()}")
panel.search_edit.setText("ROADS")
pump(300)
check(shown_names() == ["Roads group", "Main roads"], f"a group and a layer inside it match: {shown_names()}")
panel.search_edit.setText("main")
pump(300)
check(shown_names() == ["Roads group", "Main roads"], "a match inside a group shows its group")
panel.search_edit.setText("zzz")
pump(300)
check(shown_names() == [] and not panel.no_match.isHidden(), "no match: nothing shown and a short note")
check(visibility_state(root)["layers"] == ticks_before_search, "searching does not touch visibility")
app.sendEvent(panel.search_edit, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
pump(300)
check(panel.search_edit.text() == "" and shown_names() == all_names and panel.no_match.isHidden(),
      "Esc clears the search and every row is back")

# ---- health badges ---------------------------------------------------------------------------------------------------
health = panel.health


def badge(layer):
    node = root.findLayer(layer.id())
    return next((i for i in view.indicators(node) if i.objectName() == "biruniLayerHealth"), None) if node else None


def settle():
    pump(400)
    health.flush()


settle()
check(health.problems.get(table.id()) == ["empty"] and badge(table) is not None, "an empty layer gets a badge")
check(badge(points) is None and badge(polys) is None and badge(raster) is None, "healthy layers get none")
check(badge(basemap) is None, "a web basemap in another system is not flagged")

missing = os.path.join(work, "gone", "parcels.geojson")
broken = QgsVectorLayer(missing, "Old parcels", "ogr")
project.addMapLayer(broken)
settle()
check(health.problems.get(broken.id()) == ["broken"] and badge(broken) is not None, "a missing source gets a badge")
check("not found" in badge(broken).toolTip(), "its tooltip says what is wrong")
check([label for label, _fn in health.fixes(broken, ["broken"])] == ["Repair data source…"], "and offers the repair")
os.makedirs(os.path.dirname(missing))
with open(missing, "w", encoding="utf-8") as f:
    json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"a": 1},
                                                          "geometry": {"type": "Point", "coordinates": [31, 30]}}]}, f)
broken.setDataSource(missing, "Old parcels", "ogr")
settle()
check(broken.isValid() and badge(broken) is None and broken.id() not in health.problems,
      "fixed source: the badge goes away")

check(table.startEditing(), "edit the empty table")
f = QgsFeature(table.fields())
table.addFeature(f)
settle()
check(health.problems.get(table.id()) == ["edits"], f"unsaved edits get a badge (no longer empty): "
      f"{health.problems.get(table.id())}")
check(table.commitChanges(), "save")
settle()
check(badge(table) is None and table.id() not in health.problems, "saved edits: the badge goes away")

web = memory("Point", "Survey points (web mercator)", 2, "EPSG:3857")
project.addMapLayer(web)
settle()
check(health.problems.get(web.id()) == ["crs"] and badge(web) is not None, "another coordinate system gets a badge")
check("EPSG:3857" in badge(web).toolTip(), "the tooltip names both systems")
project.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
settle()
check(badge(web) is None, "same system as the map: the badge goes away")
check(health.problems.get(points.id()) == ["crs"], "and the others now carry it")
project.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
settle()

health.note_render_times({polys.id(): SLOW_SECONDS + 1.5})
settle()
check(health.problems.get(polys.id()) == ["slow"] and "2.5" in badge(polys).toolTip(), "a slow layer gets a badge")
iface.canvas.setExtent(polys.extent())
labels = [label for label, _fn in health.fixes(polys, ["slow"])]
check(len(labels) == 2 and "1:" in labels[0], f"slow layer fixes: {labels}")
health.fixes(polys, ["slow"])[0][1]()
check(polys.hasScaleBasedVisibility() and polys.minimumScale() > 0, "one click limits it to closer zooms")
polys.setScaleBasedVisibility(False)
health.note_render_times({polys.id(): 0.02})
settle()
check(badge(polys) is None, "drawn fast again: the badge goes away")
raster_fixes = [label for label, _fn in health.fixes(raster, ["slow"])]
check(any("overview" in x.lower() for x in raster_fixes), "a slow image is offered overviews")

iface.canvas.setExtent(polys.extent())
loop = QEventLoop()
iface.canvas.mapCanvasRefreshed.connect(loop.quit)
QTimer.singleShot(8000, loop.quit)
iface.canvas.refreshAllLayers()
loop.exec()
check(points.id() in health.render_times and polys.id() in health.render_times,
      f"real per-layer draw times are read after the map is drawn: {sorted(health.render_times.values())[:3]}")

# ---- solo peek -------------------------------------------------------------------------------------------------------
root.findLayer(raster.id()).setItemVisibilityChecked(False)
group2 = root.addGroup("Hidden group")
project.addMapLayer(memory("Point", "In hidden group", 1), False)
hidden_layer = project.mapLayersByName("In hidden group")[0]
group2.addLayer(hidden_layer)
group2.setItemVisibilityChecked(False)
pump(100)


def ticks():
    st = visibility_state(root)
    return st["layers"], [(g.name(), c) for g, c in st["groups"]]


before = ticks()
project.setDirty(False)
view.setCurrentLayer(lines)
check(panel.toggle_solo() and panel.solo_action.isChecked(), "Solo peek starts")
visible = {n.layerId() for n in root.findLayers() if n.isVisible()}
check(visible == {lines.id()}, f"only the chosen layer is on the map: {visible}")
check(not project.isDirty(), "peeking does not mark the project as changed")
panel.toggle_solo()
check(ticks() == before and not panel.solo_action.isChecked(), "click again: every tick is back exactly")
check(not project.isDirty(), "still unchanged")

view.setCurrentLayer(hidden_layer)
panel.toggle_solo()
check({n.layerId() for n in root.findLayers() if n.isVisible()} == {hidden_layer.id()},
      "a layer in a hidden group is shown by opening its group")
panel.toggle_solo()
check(ticks() == before, "and the group is hidden again afterwards")

view.expandAll()
pump(50)
rect = view.visualRect(view.node2index(root.findLayer(points.id())))
press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(rect.center()), QPointF(rect.center()),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier)
eaten = panel.alt_click.eventFilter(view.viewport(), press)
check(eaten and {n.layerId() for n in root.findLayers() if n.isVisible()} == {points.id()}, "Alt+click solos a layer")
panel.alt_click.eventFilter(view.viewport(), press)
check(ticks() == before, "Alt+click again restores")

# ---- smart arrange ---------------------------------------------------------------------------------------------------
root.setHasCustomLayerOrder(True)
root.setCustomLayerOrder(list(reversed(root.layerOrder())))
tree_before = root.dump()
order_before = [lyr.id() for lyr in root.customLayerOrder()]
layer_count = len(project.mapLayers())
steps = arrange.plan()
kinds = [k for k, _n in steps]
check(kinds == ["tables", "points", "lines", "polygons", "rasters", "basemaps"], f"kinds in drawing order: {kinds}")
points_group = dict(steps)["points"]
tree_order = [n.layerId() for n in root.findLayers()]
point_ids = {web.id(), points.id(), hidden_layer.id(), broken.id()}
point_order = [n.layerId() for n in points_group]
check(point_order == [i for i in tree_order if i in point_ids] and len(point_order) == 4,
      "the current order is kept inside a kind")
dlg = arrange.preview_dialog(steps)
check(dlg.tree.topLevelItemCount() == len(steps) and dlg.tree.topLevelItem(1).childCount() == 4,
      "the preview lists each group and its layers")
from qgis.gui import QgsLayerTreeViewIndicator  # noqa: E402

foreign = QgsLayerTreeViewIndicator()  # another part's badge (e.g. a locked layer)
foreign.setObjectName("someoneElsesBadge")
view.addIndicator(root.findLayer(points.id()), foreign)
check(panel.smart_arrange(ask=False), "arranged")
check(foreign in view.indicators(root.findLayer(points.id())), "other parts' badges move with the layer")
check([c.name() for c in root.children()] == [arrange.kind_title(k) for k in kinds], "the tree is grouped by kind")
pump(200)  # the project removes layers left without a node from a queued call: wait for it
check(len(project.mapLayers()) == layer_count, "no layer was removed from the project")
check(not root.findLayer(hidden_layer.id()).isVisible() and not root.findLayer(basemap.id()).isVisible(),
      "what was hidden stays hidden")
check(root.findLayer(points.id()).isVisible(), "what was shown stays shown")
check(not root.hasCustomLayerOrder() and [lyr.id() for lyr in root.layerOrder()[:4]]
      == point_order, "drawing order follows the tree: points on top")
check(panel.undo_action.isVisible(), "Undo arrange is offered")
check(arrange.same_as_plan(arrange.plan()), "arranging again has nothing to do")
check(not panel.smart_arrange(ask=False), "and says so")
settle()
check(badge(web) is not None, "the badge of a moved layer is back on its new node")
check(panel.undo_arrange(), "undo")
pump(200)
check(root.dump() == tree_before, "Undo arrange brings back the exact tree")
check(foreign in view.indicators(root.findLayer(points.id())), "and come back with undo")
check(root.hasCustomLayerOrder() and [lyr.id() for lyr in root.customLayerOrder()] == order_before,
      "and the custom drawing order")
check(len(project.mapLayers()) == layer_count and not panel.undo_action.isVisible(), "nothing lost; undo used up")
root.setHasCustomLayerOrder(False)

# ---- where did it come from ------------------------------------------------------------------------------------------
made = memory("Polygon", "Buffered", 1)
panel.origin.history_updated(7, {"algorithm_id": "native:buffer", "parameters": {},
                                 "results": {"OUTPUT": made.id()}}, 0)
project.addMapLayer(made)
pump(400)
panel.origin.flush()
from biruni.layers_panel.origin import origin_of  # noqa: E402

data = origin_of(made)
check(data is not None and data["tool"] in ("Buffer", "native:buffer"), f"a tool's output remembers the tool: {data}")
node = root.findLayer(made.id())
ind = next((i for i in view.indicators(node) if i.objectName() == "biruniLayerOrigin"), None)
check(ind is not None and data["tool"] in ind.toolTip(), "and shows it beside the layer")
plain = memory("Point", "Hand made", 1)
project.addMapLayer(plain)
check(origin_of(plain) is None, "a layer added by hand gets no origin")

# ---- the card comes back when the map is emptied, then unload ---------------------------------------------------------
project.removeAllMapLayers()
root.removeAllChildren()
pump(100)
check(not panel.empty.isHidden() and panel.view.isHidden(), "removing every layer brings the card back")
project.addMapLayer(memory("Point", "Again", 1))
pump(100)
check(panel.empty.isHidden(), "and a new layer hides it")

undo()
check(layers_panel.live_panel(plugin) is None, "unloaded")
check(all(b.toolTip() == original_tips[k] for k, b in iface.buttons.items()), "the stock icons and tips are back")
check(not [a for a in iface.toolbar.actions() if a.objectName().startswith("biruniLayers")], "our buttons are gone")
pump(50)
from qgis.PyQt.QtWidgets import QLineEdit  # noqa: E402

check(iface.dock.findChild(QLineEdit, "biruniLayersSearch") is None, "the search box is gone")
check(not view.isHidden(), "the tree is shown")
check(not any(i.objectName().startswith("biruniLayer") for n in root.findLayers() for i in view.indicators(n)),
      "no badges left")
check(plugin.theme._listeners == [], "the theme listener is removed")

project.clear()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
