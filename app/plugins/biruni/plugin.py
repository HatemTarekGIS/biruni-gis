"""Biruni GIS plugin entry: wires theme, branding, ribbon shell and start page."""
import os

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsDataSourceUri, QgsProject,
                       QgsRasterLayer, QgsRectangle, QgsSettings)
from qgis.PyQt.QtCore import QObject, QProcess, QSize, Qt, QTimer
from qgis.PyQt.QtGui import QAction, QKeySequence
from qgis.PyQt.QtWidgets import (QApplication, QDockWidget, QLabel, QMenu, QMessageBox, QToolBar,
                                 QToolButton, QWidgetAction)

from . import i18n, icons
from .activity import ActivityLogger
from .algorithms import BiruniProvider
from .branding import Branding
from .commands import CATALOG, CommandRunner
from .i18n import tr
from .project import ProjectManager
from .ribbon import CORE_TOOLBARS, PLUGIN_HOST_TOOLBARS
from .shell import Shell
from .startpage import StartPage
from .theme import MODES, ThemeManager
from .toolbox import ToolboxDock
from .tracking import TrackingDock
from .history import ProjectHistory
from .support import Support
from .version import APP_NAME

BASEMAPS = [
    ("osm", "OpenStreetMap", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", 19,
     "© OpenStreetMap contributors"),
    ("opentopomap", "OpenTopoMap", "https://tile.opentopomap.org/{z}/{x}/{y}.png", 17,
     "© OpenStreetMap contributors, SRTM | Map style © OpenTopoMap (CC-BY-SA)"),
]


class BiruniPlugin(QObject):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.mw = iface.mainWindow()
        self.theme = None
        self.runner = None
        self.branding = None
        self.shell = None
        self.start = None
        self.project = None
        self.activity = None
        self.provider = None
        self.toolbox = None
        self.tracking = None
        self.history = None
        self.support = None
        self.docks = []
        self.locator_filter = None
        self.drop_handler = None
        self.arcgis_handlers = None
        self._restart_cmd = None

    # ---- lifecycle -----------------------------------------------------
    def initGui(self):  # noqa: N802
        i18n.init()
        self.runner = CommandRunner(self)
        self.theme = ThemeManager(self.mw)
        self.branding = Branding(self)
        self.shell = Shell(self)
        self.start = StartPage(self)

        self.project = ProjectManager(self)
        self.activity = ActivityLogger(self)
        # Keep a Python reference: the provider's virtual methods live in Python.
        self.provider = BiruniProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        self.toolbox = ToolboxDock(self)
        self.mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.toolbox)
        self.toolbox.hide()
        self.tracking = TrackingDock(self)
        self.mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.tracking)
        self.mw.tabifyDockWidget(self.toolbox, self.tracking)
        self.tracking.hide()
        self.history = ProjectHistory(self)
        self.support = Support(self)
        self._add_docks()

        self.theme.apply()
        self.branding.install()
        self.shell.install()
        self.start.install()
        self.project.install()
        self.activity.install()
        self.history.install()
        self.support.install()
        self._install_handlers()
        self._install_shortcuts()
        self.theme.on_change(self._on_theme_changed)
        self._on_theme_changed()
        self.iface.initializationCompleted.connect(self._on_initialized)

        if os.environ.get("BIRUNI_SELFTEST"):
            from . import devtools
            devtools.schedule(self)

    def _on_initialized(self):
        # Other plugins (e.g. Processing) are loaded after Biruni: rebuild the
        # ribbon so their actions and panels get buttons, and re-apply the
        # theme because QGIS resets the application stylesheet during startup.
        self.build_ribbon(self.shell.ribbon)
        self.theme.apply()
        self.shell.apply()
        self.start.sync()
        self.toolbox.load()
        self.support.auto_check()
        s = QgsSettings()
        if not s.value("biruni/first-run-done", False, type=bool):
            s.setValue("biruni/first-run-done", True)
            self._first_run_layout()
            self.mw.showMaximized()

    def _add_docks(self):
        """Panels added in 0.2; each is optional so one failing import never
        takes the program down."""
        def coords():
            from .coords_dock import CoordinatesDock

            return CoordinatesDock(self)

        def cogo():
            from .survey.cogo_dock import CogoDock

            return CogoDock(self)

        def learn():
            from .learn.lessons import LearnDock

            return LearnDock(self)

        for key, make in (("coords", coords), ("cogo", cogo), ("learn", learn)):
            try:
                dock = make()
            except Exception as e:  # noqa: BLE001
                from qgis.core import QgsMessageLog

                QgsMessageLog.logMessage(f"{key}: {e}", APP_NAME)
                continue
            self.mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            self.mw.tabifyDockWidget(self.toolbox, dock)  # a tab beside the toolbox, not a stacked panel
            dock.hide()
            self.docks.append(dock)

    def _install_handlers(self):
        """Drag and drop of ArcGIS Pro files and of spreadsheets with
        coordinates, and ArcGIS tool names in the search box."""
        try:
            from .arcgis import ui as arcgis_ui

            self.arcgis_handlers = arcgis_ui.install(self)
        except Exception as e:  # noqa: BLE001
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(f"arcgis: {e}", APP_NAME)
        from .smart_table_dialog import TableDropHandler

        self.drop_handler = TableDropHandler(self)
        self.iface.registerCustomDropHandler(self.drop_handler)
        from .learn.locator import ArcGISNamesFilter

        self.locator_filter = ArcGISNamesFilter(self)
        self.iface.registerLocatorFilter(self.locator_filter)

    def _first_run_layout(self):
        """Contents (layers) on one side, catalogue (browser) on the other -
        the arrangement most desktop-GIS users already know."""
        layers = self.mw.findChild(QDockWidget, "Layers")
        browser = self.mw.findChild(QDockWidget, "Browser")
        if layers is None or browser is None:
            return
        self.mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, browser)
        self.mw.tabifyDockWidget(browser, self.toolbox)
        layers.show()
        browser.show()
        self.toolbox.show()
        browser.raise_()
        self.mw.resizeDocks([layers, browser], [320, 320], Qt.Orientation.Horizontal)

    def _install_shortcuts(self):
        """Ctrl+N / Ctrl+S go through Biruni so new and temporary maps become
        project folders (the QGIS actions keep working from the menus)."""
        self._shortcut_backup = {}
        self._own_actions = []
        for qgis_name, key, fn in (("mActionNewProject", "Ctrl+N", self.project.new_project),
                                   ("mActionSaveProject", "Ctrl+S", self.project.save)):
            qa = self.runner.qaction(qgis_name)
            if qa is not None:
                self._shortcut_backup[qa] = qa.shortcut()
                qa.setShortcut(QKeySequence())
            own = QAction(self.mw)
            own.setShortcut(QKeySequence(key))
            own.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            own.triggered.connect(lambda _=False, f=fn: f())
            self.mw.addAction(own)
            self._own_actions.append(own)

    def unload(self):
        try:
            self.iface.initializationCompleted.disconnect(self._on_initialized)
        except TypeError:
            pass
        for qa, seq in getattr(self, "_shortcut_backup", {}).items():
            qa.setShortcut(seq)
        for a in getattr(self, "_own_actions", []):
            self.mw.removeAction(a)
        for part in (self.activity, self.start, self.shell, self.branding, self.project, self.history,
                     self.support):
            if part is not None:
                part.unload()
        if self.drop_handler is not None:
            self.iface.unregisterCustomDropHandler(self.drop_handler)
        if self.locator_filter is not None:
            self.iface.deregisterLocatorFilter(self.locator_filter)
        if self.arcgis_handlers is not None:
            from .arcgis import ui as arcgis_ui

            arcgis_ui.uninstall(self, self.arcgis_handlers)
        for dock in self.docks:
            if hasattr(dock, "shutdown"):
                dock.shutdown()
            self.mw.removeDockWidget(dock)
            dock.deleteLater()
        if self.tracking is not None:
            self.tracking.shutdown()
            self.mw.removeDockWidget(self.tracking)
            self.tracking.deleteLater()
        if self.toolbox is not None:
            self.mw.removeDockWidget(self.toolbox)
            self.toolbox.deleteLater()
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)

    def _on_theme_changed(self):
        if self.shell and self.shell.ribbon:
            self.shell.ribbon.refresh_theme()
        if self.start:
            self.start.refresh_theme()
        if self.toolbox:
            self.toolbox.refresh_theme()
        if self.tracking:
            self.tracking.refresh_theme()
        for dock in self.docks:
            if hasattr(dock, "refresh_theme"):
                dock.refresh_theme()

    # ---- ribbon layout -------------------------------------------------
    def build_ribbon(self, r):
        home = r.add_tab("home", "Home")
        g = r.group(home, "Project")
        g.large("new_project")
        g.smalls("open", "save", "project_folder")
        g.smalls("history_versions", "activity_report")
        g = r.group(home, "Navigate")
        for cid in ("pan", "zoom_in", "zoom_out", "full_extent", "zoom_layer"):
            g.large(cid)
        g.smalls("zoom_selection", "zoom_prev", "zoom_next")
        g.smalls("refresh", "new_bookmark", "bookmarks")
        g = r.group(home, "Layers")
        g.large("add_data")
        g.large("basemap")
        g = r.group(home, "Selection")
        g.large("select")
        g.smalls("select_expr", "select_location", "select_all")
        g.smalls("invert_selection", "clear_selection")
        g = r.group(home, "Inquiry")
        g.large("identify")
        g.large("measure")
        g.large("attribute_table")
        g.smalls("statistics", "map_tips", "coords")
        g = r.group(home, "Learn")
        g.large("learn")

        data = r.add_tab("data", "Data")
        g = r.group(data, "Add")
        g.large("add_data")
        g.large("smart_table")
        g.smalls("add_vector", "add_raster", "add_csv")
        g.smalls("add_postgis", "add_wms", "add_wfs")
        g.smalls("add_xyz", "open_arcgis")
        g = r.group(data, "Basemaps")
        g.large("basemap")
        g = r.group(data, "Open Data")
        g.large("open_data")
        g.smalls("osm_download", "s2_download", "dem_download")
        g = r.group(data, "Create")
        g.large("new_layer")
        g = r.group(data, "CAD & Imagery")
        g.large("cad_to_gis")
        g.large("georeferencer")
        g.smalls("dwg_import", "cad_georef")
        g = r.group(data, "Export")
        g.large("export_layers")
        g.smalls("export_layer")
        g = r.group(data, "GPS")
        g.large("phone_gps")
        g.smalls("import_track")
        g = r.group(data, "Project Data")
        g.smalls("store_temporary", "consolidate", "package")

        edit = r.add_tab("edit", "Edit")
        g = r.group(edit, "Session")
        g.large("toggle_edit")
        g.large("save_edits")
        g.smalls("undo", "redo")
        g = r.group(edit, "Features")
        g.large("add_feature")
        g.large("vertex_tool")
        g.smalls("move", "rotate", "delete")
        g.smalls("cut", "copy", "paste")
        g = r.group(edit, "Geometry")
        g.smalls("split", "merge", "reshape")
        g.smalls("offset", "trim_extend", "simplify")
        g.smalls("add_ring", "add_part")
        g = r.group(edit, "Snapping")
        g.large("snapping")
        g.smalls("topological", "tracing", "snapping_options")
        g = r.group(edit, "Precision")
        g.large("cad_tools")
        g.smalls("vertex_editor")
        g = r.group(edit, "Attributes")
        g.large("field_calc")
        g.large("attribute_rules")
        g.smalls("multi_edit")
        g = r.group(edit, "Survey")
        g.large("cogo")
        g.smalls("subdivide", "traverse_table")

        ana = r.add_tab("analysis", "Analysis")
        g = r.group(ana, "Geoprocessing")
        g.large("toolbox")
        g.smalls("buffer", "clip", "dissolve")
        g.smalls("intersection", "union", "difference")
        g.smalls("qgis_toolbox")
        g = r.group(ana, "Data Management")
        g.smalls("spatial_join", "reproject", "merge_layers")
        g.smalls("centroids")
        g = r.group(ana, "Measure")
        g.large("geometry_attributes")
        g.large("number_features")
        g = r.group(ana, "Networks")
        g.large("closest_facility")
        g.smalls("od_matrix", "service_area", "shortest_path")
        g = r.group(ana, "Raster")
        g.large("raster_calc")
        g = r.group(ana, "Automation")
        g.large("model_designer")
        g.smalls("history")

        qa = r.add_tab("quality", "Quality")
        g = r.group(qa, "Check")
        g.large("check_data")
        g = r.group(qa, "Coverage")
        g.large("overlaps_gaps")
        g = r.group(qa, "Geometry")
        g.large("check_validity")
        g.large("fix_geometries")
        g.large("remove_duplicates")
        g.smalls("remove_null")
        g = r.group(qa, "Topology")
        g.large("topology_rules")
        g.large("validation_panel")
        g.smalls("topological", "snapping")

        share = r.add_tab("share", "Share")
        g = r.group(share, "Print Layout")
        g.large("map_sheet")
        g.large("new_layout")
        g.large("layout_manager")
        g.smalls("new_report")
        g = r.group(share, "Export Map")
        g.large("export_image")
        g.large("export_pdf")
        g.large("export_dxf")
        g = r.group(share, "Export Data")
        g.large("export_layer")
        g = r.group(share, "Publish")
        g.large("web_map")
        g.smalls("export_kmz", "portfolio")
        g = r.group(share, "Field work")
        g.large("qfield_send")
        g.smalls("qfield_back", "phone_gps")
        g = r.group(share, "Reports")
        g.large("activity_report")
        g.smalls("timesheet")

        view = r.add_tab("view", "View")
        g = r.group(view, "Panels")
        g.smalls("panel_layers", "panel_browser", "panel_toolbox")
        g.smalls("panel_styling", "panel_log", "panel_overview")
        g.smalls("learn", "coords")
        g = r.group(view, "Display")
        g.large("full_screen")
        g.large("map_only")
        g = r.group(view, "Appearance")
        g.large("theme")
        g.large("language")
        g = r.group(view, "Interface")
        g.large("classic")
        g.large("options")
        g.smalls("plugins", "python", "shortcuts")
        g = r.group(view, "Help")
        g.large("learn")
        g.smalls("report_problem", "check_updates", "about")

        r.add_tab("plugins", "Plugins")
        self.build_plugins_tab(r)

    def build_plugins_tab(self, r=None):
        r = r or self.shell.ribbon
        r.clear_page("plugins")
        page = r.pages["plugins"]
        g = r.group(page, "Manage")
        g.large("plugins")
        g.large("python")
        known = {CATALOG[c].action for c in CATALOG if CATALOG[c].action}
        found = False
        for tb in [t for t in self.mw.findChildren(QToolBar) if t.parent() is self.mw]:
            name = tb.objectName()
            if name == "BiruniRibbonBar" or (name in CORE_TOOLBARS and name not in PLUGIN_HOST_TOOLBARS):
                continue
            # Core QGIS actions are named "mAction…"; plugin actions are not.
            acts = [a for a in tb.actions()
                    if not a.isSeparator() and not isinstance(a, QWidgetAction)
                    and a.objectName() not in known and not a.objectName().startswith("mAction")]
            if not acts:
                continue
            found = True
            grp = r.group(page, tb.windowTitle() or name)
            for a in acts:
                b = QToolButton()
                b.setObjectName("RibbonLarge")
                b.setDefaultAction(a)
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                b.setIconSize(QSize(24, 24))
                b.setMaximumWidth(96)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                grp.custom(b)
        if not found:
            grp = r.group(page, "Installed")
            hint = QLabel(tr("Tools from plugins you install will appear here."))
            hint.setObjectName("RibbonCaption")
            hint.setContentsMargins(8, 0, 8, 0)
            grp.custom(hint)
        r.refresh_theme()

    def on_tab_shown(self, key):
        if key == "plugins":
            self.build_plugins_tab()

    # ---- menus ---------------------------------------------------------
    def _cmd_action(self, menu, cid):
        cmd = CATALOG[cid]
        if not self.runner.available(cmd):
            return None
        t = self.theme.tokens()
        text = tr(cmd.label).replace("\n", " ")
        qa = self.runner.qaction(cmd.action) if cmd.action else None
        if qa is not None and not qa.shortcut().isEmpty():
            text += "\t" + qa.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        act = menu.addAction(icons.icon(cmd.icon, t["icon"], t["icon_disabled"]), text)
        if qa is not None:
            act.setEnabled(qa.isEnabled())
        act.triggered.connect(lambda _=False, c=cmd: self.runner.execute(c))
        return act

    def _qgis_submenu(self, menu, object_name, label, icon_name):
        sub = self.mw.findChild(QMenu, object_name)
        if sub is None:
            return
        t = self.theme.tokens()
        a = menu.addMenu(sub)
        a.setText(label)
        a.setIcon(icons.icon(icon_name, t["icon"]))

    def build_file_menu(self):
        m = QMenu(self.mw)
        m.aboutToShow.connect(lambda: (m.clear(), self._fill_file_menu(m)))
        return m

    def _fill_file_menu(self, m):
        a = self._cmd_action(m, "new_project")
        if a is not None:
            a.setText(a.text() + "\tCtrl+N")
        self._qgis_submenu(m, "mProjectFromTemplateMenu", tr("New from Template"), "template")
        self._cmd_action(m, "open")
        self._qgis_submenu(m, "mRecentProjectsMenu", tr("Open Recent"), "clock-hour-4")
        a = self._cmd_action(m, "save")
        if a is not None:
            a.setText(a.text() + "\tCtrl+S")
        self._cmd_action(m, "save_as_folder")
        self._cmd_action(m, "save_as")
        self._cmd_action(m, "close")
        m.addSeparator()
        for cid in ("project_folder", "history_versions", "store_temporary", "consolidate", "package",
                    "activity_report", "timesheet"):
            self._cmd_action(m, cid)
        rec = m.addAction(tr("Record project activity"))
        rec.setCheckable(True)
        rec.setChecked(self.activity.enabled())
        rec.toggled.connect(self.activity.set_enabled)
        m.addSeparator()
        self._cmd_action(m, "project_props")
        self._cmd_action(m, "open_arcgis")
        self._cmd_action(m, "smart_table")
        self._qgis_submenu(m, "menuImport_Export", tr("Import / Export"), "transfer")
        self._qgis_submenu(m, "mLayoutsMenu", tr("Print Layouts"), "layout-board")
        m.addSeparator()
        self._cmd_action(m, "options")
        self._cmd_action(m, "plugins")
        m.addSeparator()
        self._cmd_action(m, "about")
        ex = self.runner.qaction("mActionExit")
        if ex is not None:
            t = self.theme.tokens()
            a = m.addAction(icons.icon("logout", t["icon"]), tr("Exit") + "\t" +
                            ex.shortcut().toString(QKeySequence.SequenceFormat.NativeText))
            a.triggered.connect(ex.trigger)

    def fill_theme_menu(self, m):
        t = self.theme.tokens()
        labels = {"system": (tr("Follow Windows"), "device-desktop"), "light": (tr("Light"), "sun"),
                  "dark": (tr("Dark"), "moon")}
        cur = self.theme.mode()
        for mode in MODES:
            text, ic = labels[mode]
            a = m.addAction(icons.icon(ic, t["icon"]), text)
            a.setCheckable(True)
            a.setChecked(mode == cur)
            a.triggered.connect(lambda _=False, md=mode: self.theme.set_mode(md))

    def fill_language_menu(self, m):
        cur = i18n.current_code()
        for code, name in i18n.LANGUAGES:
            a = m.addAction(name)
            a.setCheckable(True)
            a.setChecked(code == cur)
            a.triggered.connect(lambda _=False, c=code: self.change_language(c))

    def fill_help_menu(self, m):
        self._cmd_action(m, "learn")
        self._cmd_action(m, "report_problem")
        self._cmd_action(m, "check_updates")
        m.addSeparator()
        self._cmd_action(m, "about")
        t = self.theme.tokens()
        for name, label, ic in (("mActionHelpContents", tr("QGIS User Guide"), "book"),
                                ("mActionConfigureShortcuts", tr("Keyboard Shortcuts"), "keyboard"),
                                ("mActionAbout", tr("About QGIS"), "info-square")):
            qa = self.runner.qaction(name)
            if qa is not None:
                a = m.addAction(icons.icon(ic, t["icon"]), label)
                a.triggered.connect(qa.trigger)

    def fill_all_menus(self, m):
        for a in self.mw.menuBar().actions():
            if a.menu() is not None:
                m.addAction(a)
        m.addSeparator()
        t = self.theme.tokens()
        c = m.addAction(icons.icon("layout-navbar", t["icon"]), tr("Switch to Classic Menus"))
        c.triggered.connect(lambda: self.shell.set_classic(True))

    def fill_basemap_menu(self, m):
        t = self.theme.tokens()
        for key, name, *_ in BASEMAPS:
            a = m.addAction(icons.icon("map-2", t["icon"]), name)
            a.triggered.connect(lambda _=False, k=key: self.add_basemap(k))
        m.addSeparator()
        xyz = CATALOG["add_xyz"]
        if self.runner.available(xyz):
            a = m.addAction(icons.icon(xyz.icon, t["icon"]), tr("Other XYZ / WMTS service…"))
            a.triggered.connect(lambda: self.runner.execute(xyz))

    # ---- basemaps ------------------------------------------------------
    def add_basemap(self, key, zoom_world=False):
        spec = next((b for b in BASEMAPS if b[0] == key), None)
        if spec is None:
            return None
        _, name, url, zmax, attribution = spec
        uri = QgsDataSourceUri()
        uri.setParam("type", "xyz")
        uri.setParam("url", url)
        uri.setParam("zmin", "0")
        uri.setParam("zmax", str(zmax))
        layer = QgsRasterLayer(bytes(uri.encodedUri()).decode(), name, "wms")
        if not layer.isValid():
            self.iface.messageBar().pushWarning(APP_NAME, tr("Could not load the basemap {n}.").format(n=name))
            return None
        try:
            layer.serverProperties().setAttribution(attribution)
        except AttributeError:
            pass
        prj = QgsProject.instance()
        first = not prj.mapLayers()
        if first:
            prj.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        prj.addMapLayer(layer, False)
        prj.layerTreeRoot().addLayer(layer)  # bottom of the layer list
        if zoom_world or first:
            canvas = self.iface.mapCanvas()
            canvas.setExtent(QgsRectangle(-17000000, -7000000, 17000000, 11500000))
            canvas.refresh()
        return layer

    # ---- language / restart --------------------------------------------
    def change_language(self, code):
        if code == i18n.current_code():
            return
        i18n.set_language(code)
        if self.start:
            for b, (c, _) in zip(self.start.lang_buttons, i18n.LANGUAGES):
                b.setChecked(c == code)
        r = QMessageBox.question(
            self.mw, APP_NAME, tr("The new language will be used after Biruni GIS restarts. Restart now?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.restart()

    def restart(self):
        launcher = os.environ.get("BIRUNI_LAUNCHER")
        if not launcher or not os.path.exists(launcher):
            return
        self._restart_cmd = launcher
        app = QApplication.instance()
        app.aboutToQuit.connect(self._spawn_launcher)
        ex = self.runner.qaction("mActionExit")
        if ex is not None:
            ex.trigger()
        # If the user cancelled the exit (e.g. at the save prompt), forget the restart.
        QTimer.singleShot(2000, self._cancel_restart)

    def _spawn_launcher(self):
        if self._restart_cmd:
            QProcess.startDetached(self._restart_cmd, [])

    def _cancel_restart(self):
        try:
            QApplication.instance().aboutToQuit.disconnect(self._spawn_launcher)
        except TypeError:
            pass
        self._restart_cmd = None
