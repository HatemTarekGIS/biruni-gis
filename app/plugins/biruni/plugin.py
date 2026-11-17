"""Biruni GIS plugin entry: wires theme, branding, ribbon shell and start page."""
import os

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsDataSourceUri, QgsProject,
                       QgsRasterLayer, QgsRectangle, QgsSettings)
from qgis.PyQt.QtCore import QObject, QProcess, QSize, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QAction, QDesktopServices, QKeySequence
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
        if os.environ.get("BIRUNI_PROFILE_START"):  # developers: where the start-up time goes
            from . import devtools

            devtools.profile_start(self)
        self._crashed_before = self._optional("crash log", self._start_crash_log)
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
        from .thesis.methodology import install_recorder  # every tool run, with its settings, for the methodology

        install_recorder()
        self.history.install()
        self.support.install()
        self._install_handlers()
        self._install_shortcuts()
        self._install_extras()
        self.theme.on_change(self._on_theme_changed)
        self._on_theme_changed()
        self.iface.initializationCompleted.connect(self._on_initialized)
        if getattr(self, "_profile_done", None):
            self._profile_done()

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
        if self.toolbox.isFloating():  # a toolbox left floating comes back beside the map
            from .commands import dock_beside_map

            dock_beside_map(self.mw, self.toolbox)
        self.support.auto_check()
        if getattr(self, "_crashed_before", None):
            from . import crashlog

            crashlog.offer(self.iface, self._crashed_before)
        self._optional("start time", lambda: __import__("biruni.from_pro", fromlist=["x"]).record_start())
        from qgis.PyQt.QtCore import QTimer

        # the Vault replaces the classic Browser; built a moment after start so start-up stays quick
        QTimer.singleShot(1500, lambda: self._optional(
            "vault", lambda: __import__("biruni.commands", fromlist=["x"]).vault_for_browser(self)))
        self._optional("light mode", lambda: __import__("biruni.performance", fromlist=["x"])
                       .suggest_on_first_run(self))
        self._optional("light mode", self._light_mode_at_start)
        s = QgsSettings()
        if not s.value("biruni/first-run-done", False, type=bool):
            s.setValue("biruni/first-run-done", True)
            self._first_run_layout()
            self.mw.showMaximized()

    def _light_mode_at_start(self):
        from . import performance

        if performance.is_on():
            performance.apply_runtime(self, True)

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

    @staticmethod
    def _optional(what, fn):
        """Run a part added in 0.3; one failing part must never take the program down."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(f"{what}: {e}", APP_NAME)
            return None

    def _install_extras(self):
        """Layer facts in the Layers panel, autosave, the area filter banner and error explanations."""
        from qgis.core import QgsProject

        def layer_facts():
            from . import layer_info

            layer_info.install(self.iface)
            return layer_info.uninstall

        def autosave():
            from .autosave import AutoSaver

            self.autosave = AutoSaver(self)
            self.autosave.install()
            return self.autosave.uninstall

        def area_filter():
            from . import spatial_filter

            def on_read(*_):
                spatial_filter.check_on_open(self.iface)

            QgsProject.instance().readProject.connect(on_read)
            return lambda: QgsProject.instance().readProject.disconnect(on_read)

        def arabic():
            from . import arabic_labels

            arabic_labels.install(self.iface)
            return arabic_labels.uninstall

        def selections():
            from . import selection_sets

            selection_sets.install(self.iface)
            return selection_sets.uninstall

        def table_functions():
            from . import table_tools

            table_tools.register_functions()
            return table_tools.unregister_functions

        def edit_guard():
            from . import edit_guard as guard

            guard.install(self)
            return guard.uninstall

        def spatial_rules():
            from .georules import guard as rules_guard

            rules_guard.install(self)
            return rules_guard.uninstall

        def explain():
            from . import explain as explainer

            explainer.install(self)
            return explainer.uninstall

        def charter_menu():
            import importlib

            from qgis.core import Qgis
            from qgis.PyQt.QtGui import QAction

            from . import icons

            act = QAction(icons.icon("license", "#0E7C74"), i18n.tr("Data Charter…"), self.iface.mainWindow())
            act.triggered.connect(lambda: importlib.import_module(f"{__package__}.charter.dialog")
                                  .open_charter(self, None, self.iface.activeLayer()))
            self.iface.addCustomActionForLayerType(act, "", Qgis.LayerType.Vector, True)
            return lambda: self.iface.removeCustomActionForLayerType(act)

        def centred_windows():
            from . import centering

            return centering.install(self.iface)

        def vault():
            from . import vault as _vault

            return _vault.install(self)

        def locked_files():  # opens .bgpkg / .bproj (drag and drop, Open Project); the rest loads on first use
            from . import secure

            handlers = secure.install(self)
            return lambda: secure.uninstall(self, handlers)

        def layers_panel():
            from . import layers_panel as _layers

            return _layers.install(self)

        def map_stops():
            from . import stops

            return stops.install(self)

        def charter_language():
            def on_read(*_):
                if any(lyr.customProperty("biruni/charter", "") for lyr in QgsProject.instance().mapLayers().values()):
                    from .charter.apply import sync_language

                    sync_language()

            QgsProject.instance().readProject.connect(on_read)
            return lambda: QgsProject.instance().readProject.disconnect(on_read)

        def service_sign_in():
            def on_read(*_):
                from qgis.PyQt.QtCore import QTimer

                if any(lyr.customProperty("biruni/arcgis-connection", "") for lyr in QgsProject.instance().mapLayers().values()):
                    QTimer.singleShot(0, lambda: __import__("biruni.arcgis_services.layers", fromlist=["x"])
                                      .refresh_tokens(background=True))

            QgsProject.instance().readProject.connect(on_read)
            return lambda: QgsProject.instance().readProject.disconnect(on_read)

        def team_rooms():
            def on_read(*_):
                from qgis.PyQt.QtCore import QTimer

                if any(lyr.customProperty("biruni/team-layer", "") for lyr in QgsProject.instance().mapLayers().values()):
                    QTimer.singleShot(0, lambda: __import__("biruni.team.dock", fromlist=["x"]).on_project_read(self))

            def on_clear(*_):
                import sys

                team = sys.modules.get(f"{__package__}.team")
                if team is not None and team.current() is not None:
                    dock = sys.modules.get(f"{__package__}.team.dock")
                    if dock is not None and id(self) in dock._DOCK:
                        dock._DOCK[id(self)].attach(None)
                    from .team import session

                    session.close_room(team.current())

            QgsProject.instance().readProject.connect(on_read)
            # before the layers are deleted ("cleared" comes after: the room then let go of deleted layers and the
            # program crashed when it was closed with a room open)
            QgsProject.instance().aboutToBeCleared.connect(on_clear)

            def undo():
                QgsProject.instance().readProject.disconnect(on_read)
                QgsProject.instance().aboutToBeCleared.disconnect(on_clear)
            return undo

        def diwan():   # saves logged, and edits made by other programs found, in Diwan files
            from .diwan import live

            return live.install(self)

        def shapefile_arcs():   # a shapefile added with its arcs kept beside it: offer to bring them back
            from .shparc import ui

            return ui.install(self)

        def astrolabe():   # the disc of three rings: the map's right-click menu and F9
            from . import astrolabe as mod

            return mod.install(self)

        def field_photos_drive():
            def on_change(*_):
                import sys

                m = sys.modules.get(f"{__package__}.field_photos.dock")
                if m is not None:
                    m.on_project_changed(self)

            proj = QgsProject.instance()
            proj.readProject.connect(on_change)
            proj.cleared.connect(on_change)

            def undo():
                proj.readProject.disconnect(on_change)
                proj.cleared.disconnect(on_change)

            return undo

        skip = set(filter(None, os.environ.get("BIRUNI_SKIP_EXTRAS", "").split(",")))  # for tracking down issues
        parts = (("layer facts", layer_facts), ("autosave", autosave), ("area filter", area_filter),
                 ("explain", explain), ("arabic labels", arabic), ("selection sets", selections),
                 ("table functions", table_functions), ("edit guard", edit_guard), ("spatial rules", spatial_rules),
                 ("data charter", charter_menu), ("charter language", charter_language),
                 ("map stops", map_stops), ("centred windows", centred_windows), ("vault", vault),
                 ("layers panel", layers_panel), ("locked files", locked_files),
                 ("service sign-in", service_sign_in), ("team rooms", team_rooms), ("diwan", diwan),
                 ("astrolabe", astrolabe), ("shapefile arcs", shapefile_arcs),
                 ("field photos drive", field_photos_drive))
        self._extra_unloads = [u for u in (self._optional(name, fn) for name, fn in parts
                                           if name.replace(" ", "_") not in skip) if u]

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
        browser.show()  # the Vault takes its place a moment later (vault_for_browser)
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
        # F10 (as in Windows programs): the keyboard goes to the ribbon - ← / → tabs, Enter into the page,
        # Tab through its buttons, Esc back to the map
        f10 = QAction(self.mw)
        f10.setShortcut(QKeySequence("F10"))
        f10.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        f10.triggered.connect(lambda: self.shell.ribbon.focus_tabs() if self.shell and self.shell.ribbon else None)
        self.mw.addAction(f10)
        self._own_actions.append(f10)

    def _start_crash_log(self):
        from . import crashlog

        found = crashlog.start()
        QgsApplication.instance().aboutToQuit.connect(crashlog.stop)  # a normal exit, even without unload
        return found

    def unload(self):
        from . import crashlog
        from .thesis.methodology import uninstall_recorder

        crashlog.stop()

        uninstall_recorder()
        try:
            self.iface.initializationCompleted.disconnect(self._on_initialized)
        except TypeError:
            pass
        for qa, seq in getattr(self, "_shortcut_backup", {}).items():
            qa.setShortcut(seq)
        for a in getattr(self, "_own_actions", []):
            self.mw.removeAction(a)
        for undo in reversed(getattr(self, "_extra_unloads", [])):
            self._optional("unload", undo)
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
        import sys

        field_photos = sys.modules.get(f"{__package__}.field_photos.dock")  # only when it was opened
        if field_photos is not None:
            field_photos.close_dock(self)
        create_pane = sys.modules.get(f"{__package__}.editing.create_pane")
        if create_pane is not None:
            create_pane.close_pane(self)
        modify_pane = sys.modules.get(f"{__package__}.editing.modify_pane")
        if modify_pane is not None:
            modify_pane.close_pane(self)
        live_share = sys.modules.get(f"{__package__}.live_share.dock")
        if live_share is not None:
            live_share.close_dock(self)
        phone_hub = sys.modules.get(f"{__package__}.phone_hub.dock")
        if phone_hub is not None:
            phone_hub.close_dock(self)
        diwan_dock = sys.modules.get(f"{__package__}.diwan.dock")
        if diwan_dock is not None:
            diwan_dock.close_dock(self)
        team_dock = sys.modules.get(f"{__package__}.team.dock")
        if team_dock is not None:
            team_dock.close_dock(self)
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
        g.smalls("project_doctor", "who_locks", "rename_project")
        g = r.group(home, "Navigate")
        for cid in ("pan", "zoom_in", "zoom_out", "full_extent", "zoom_layer"):
            g.large(cid)
        g.smalls("zoom_selection", "zoom_prev", "zoom_next")
        g.large("map_stops")
        g.smalls("refresh", "new_stop", "manage_stops")
        g = r.group(home, "Layers")
        g.large("add_data")
        g.large("basemap")
        g.large("vault")
        g = r.group(home, "Selection")
        g.large("select")
        g.smalls("select_expr", "select_location", "select_all")
        g.smalls("invert_selection", "clear_selection")
        g.large("saved_selections")
        g.smalls("select_previous", "select_next")
        g = r.group(home, "Astrolabe")
        g.large("astrolabe")
        g.smalls("astrolabe_repeat")
        g = r.group(home, "Area Filter")
        g.large("spatial_filter")
        g.smalls("spatial_filter_clear", "feature_counts")
        g = r.group(home, "Inquiry")
        g.large("identify")
        g.large("measure")
        g.large("attribute_table")
        g.smalls("statistics", "map_tips", "coords")
        g = r.group(home, "My Country")
        g.large("my_country")
        g = r.group(home, "Learn")
        g.large("learn")
        g.large("from_pro")

        data = r.add_tab("data", "Data")
        g = r.group(data, "Add")
        g.large("add_data")
        g.large("smart_table")
        g.smalls("add_vector", "add_raster", "add_csv")
        g.smalls("add_postgis", "add_wms", "add_wfs")
        g.smalls("add_xyz", "open_arcgis", "import_kml")
        g = r.group(data, "From ArcGIS")
        for cid in ("from_pro", "migrate_scan", "import_models", "run_arcpy", "export_aprx", "arcgis_services"):
            if cid in CATALOG:  # parts still being added are left out
                g.large(cid)
        g.smalls("export_mapx", "export_lyrx", "import_pagx")
        g.smalls("arcgis_sync", "arcgis_signin")
        g = r.group(data, "Open Data")
        g.large("open_data")
        g.smalls("osm_download", "overture", "dem_download")
        g.smalls("cadastre_lookup")
        g = r.group(data, "Create")
        g.large("new_layer")
        g = r.group(data, "CAD & Imagery")
        g.large("cad_to_gis")
        g.large("georeferencer")
        g.smalls("dwg_import", "cad_georef", "georef_pdf")
        g.smalls("dwg_open", "dwg_save")
        g.smalls("quick_align", "lines_to_plots", "scan_parcels")
        g.smalls("indoor_map", "indoor_route")
        g.large("open_national")
        g.large("cad_more")
        g = r.group(data, "Point Clouds")
        g.large("las_ground")
        g.smalls("las_volume", "las_profile")
        g = r.group(data, "GPS")
        g.large("phone_hub")
        g.large("phone_gps")
        g.large("field_photos")
        g.smalls("import_track", "gnss_quality", "whatsapp_points")
        g = r.group(data, "Project Data")
        g.large("diwan")
        g.smalls("store_temporary", "consolidate", "package")

        edit = r.add_tab("edit", "Edit")
        g = r.group(edit, "Session")
        g.large("toggle_edit")
        g.large("team_room")
        g.large("save_edits")
        g.large("review_save")
        g.smalls("undo", "redo")
        g = r.group(edit, "Teamwork")
        g.smalls("team_pigeon", "light_send", "light_receive")
        g.smalls("team_compute", "team_compute_share", "team_vault")
        g = r.group(edit, "Enterprise")
        g.large("map_service")
        g.smalls("portal", "map_service_check", "portal_viewer")
        g = r.group(edit, "Features")
        g.large("create_features")
        g.large("modify_features")
        g.large("add_feature")
        g.large("vertex_tool")
        g.smalls("move", "rotate", "delete")
        g.smalls("cut", "copy", "paste")
        g = r.group(edit, "Smart Digitise")
        g.large("geoai_digitise")
        g.smalls("geoai_model")
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
        g.large("table_tools")
        g.smalls("multi_edit", "field_order", "highlight_table")
        g.smalls("natural_sort")
        g = r.group(edit, "Data Charter")
        g.large("data_charter")
        g.large("charter_learn")
        g.large("geo_rules")
        g.smalls("charter_fields", "charter_lists", "charter_kinds")
        g.smalls("charter_rules", "charter_links", "charter_dictionary")
        g = r.group(edit, "Protect")
        g.large("lock_database")
        g.large("lock_users")
        g.smalls("lock_project", "open_locked", "remove_lock")
        g = r.group(edit, "Survey")
        g.large("cogo")
        g.smalls("subdivide", "traverse_table", "field_to_finish")
        g.smalls("deed_parcel", "deed_survey", "parcel_description")
        g.smalls("ground_grid", "localization", "parcel_bundle")

        ana = r.add_tab("analysis", "Analysis")
        g = r.group(ana, "Geoprocessing")
        g.large("toolbox")
        g.smalls("buffer", "clip", "dissolve")
        g.smalls("intersection", "union", "difference")
        g.smalls("qgis_toolbox")
        g = r.group(ana, "Data Management")
        g.smalls("spatial_join", "reproject", "merge_layers")
        g.smalls("centroids", "multi_join")
        g = r.group(ana, "Measure")
        g.large("geometry_attributes")
        g.large("number_features")
        g = r.group(ana, "Spatial Statistics")
        g.large("ss_advisor")
        g.large("ss_hotspots")
        g.smalls("ss_clusters", "ss_moran", "ss_emerging")
        g.smalls("ss_points", "ss_kde", "ss_centre")
        g.large("ss_ols")
        g.smalls("ss_gwr")
        g = r.group(ana, "Engineering Office")
        g.large("office_tools")
        g.large("parcel_bundle")
        g.large("guess_crs")
        g.large("quantities")
        g.smalls("parcel_description", "lines_to_plots", "sales_map")
        g.smalls("delivery_check", "compare_layers", "setbacks")
        g.smalls("deed_parcel", "deed_survey", "scan_parcels")
        g.smalls("arabic_join", "whatsapp_points", "delivery_spec")
        g.smalls("seal_files", "check_seal")
        g.smalls("cadastre_lookup", "parcel_atlas", "eudr_check")
        g = r.group(ana, "Networks")
        g.large("closest_facility")
        g.smalls("od_matrix", "service_area", "shortest_path")
        g = r.group(ana, "Raster")
        g.large("raster_calc")
        g = r.group(ana, "Water")
        g.smalls("wbt_watershed", "wbt_ponding", "wbt_floodzone")
        g = r.group(ana, "Automation")
        g.large("model_designer")
        g.smalls("history", "session_script")

        qa = r.add_tab("quality", "Quality")
        g = r.group(qa, "Check")
        g.large("check_data")
        g.large("spatial_spell")
        g.large("learned_rules")
        g.large("charter_health")
        g.large("crs_doctor")
        g.large("clean_table")
        g.large("arabic_text_doctor")
        g.large("delivery_check")
        g.smalls("fix_text_encoding", "convert_dates", "hijri_dates")
        g.smalls("spell_values", "compare_layers", "delivery_spec")
        g.smalls("scrub_file")
        g = r.group(qa, "Peer Review")
        g.large("review_draw")
        g.smalls("review_mine", "review_results")
        g = r.group(qa, "Coverage")
        g.large("overlaps_gaps")
        g.large("coverage_doctor")
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
        g.large("map_studio")
        g.large("map_sheet")
        g.large("map_book")
        g.large("new_layout")
        g.large("layout_manager")
        g.smalls("new_report", "four_colors", "arabic_labels_fix")
        g.smalls("label_cluster", "map_index", "tabular_labels")
        g.smalls("contour_style", "movable_labels", "parcel_atlas")
        g = r.group(share, "Map Design")
        g.large("thematic")
        g.large("map_products")
        g.large("live_qr")
        g.smalls("color_preview", "terrain_style", "map_caption")
        g.smalls("place_icons", "symbols_by_scale", "install_symbols")
        g.smalls("bilingual_labels", "label_priorities", "hidden_labels")
        g = r.group(share, "Export Map")
        g.large("export_image")
        g.large("export_pdf")
        g.large("export_dxf")
        g = r.group(share, "Export Data")
        g.large("export_layers")
        g.smalls("export_layer", "export_photos", "landxml_out")
        g.smalls("shp_arcs_export", "shp_arcs_open")
        g.smalls("export_national")
        g = r.group(share, "Copies")
        g.large("copies_compare")
        g.smalls("copies_certificate", "copies_check", "copies_stamp")
        g.smalls("copies_package", "copies_apply", "copies_stamp_check")
        g.smalls("copies_mark", "copies_whose")
        g = r.group(share, "Proofs")
        g.smalls("copies_area_proof", "copies_totals_proof", "copies_share_hidden")
        g.smalls("copies_area_check", "copies_totals_check", "copies_shared_check")
        g = r.group(share, "Between Agencies")
        g.large("conflate_match")
        g.large("copies_private_match")
        g = r.group(share, "Publish")
        g.large("web_map")
        g.large("publish_site")
        g.large("live_share")
        g.large("dashboard")
        g.smalls("export_kmz", "portfolio", "earth_tour")
        g.smalls("basemap_pack", "site_preview", "dashboard_seal_check")
        g = r.group(share, "Review")
        g.large("review_send")
        g.large("sales_map")
        g.smalls("review_back", "sales_back")
        g = r.group(share, "Field work")
        g.large("phone_hub")
        g.large("qfield_send")
        g.smalls("qfield_back", "phone_gps")
        g = r.group(share, "Reports")
        g.large("activity_report")
        g.smalls("timesheet")

        thesis = r.add_tab("thesis", "Graduation Project")
        g = r.group(thesis, "Thesis")
        g.large("thesis_starter")
        g.large("thesis_methodology")
        g.large("thesis_figures")
        g.large("thesis_defense")

        view = r.add_tab("view", "View")
        g = r.group(view, "Panels")
        g.smalls("panel_layers", "panel_vault", "panel_browser")
        g.smalls("panel_toolbox", "panel_styling", "panel_log")
        g.smalls("panel_overview", "learn", "coords")
        g.smalls("standard_panels")
        g = r.group(view, "Display")
        g.large("full_screen")
        g.large("map_only")
        g = r.group(view, "Appearance")
        g.large("theme")
        g.large("language")
        g = r.group(view, "Performance")
        g.large("light_mode")
        g.smalls("autosave", "feature_counts", "free_memory")
        g = r.group(view, "Interface")
        g.large("classic")
        g.large("options")
        g.smalls("plugins", "python", "shortcuts")
        g = r.group(view, "Help")
        g.large("learn")
        g.large("from_pro")
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

    def fill_workbench_menu(self, m):
        """The workspaces (workbench.py): each shows only the part of the ribbon its kind of user needs."""
        from . import workbench

        t = self.theme.tokens()
        cur = self.shell.ribbon.workbench if self.shell and self.shell.ribbon else workbench.current()
        for name, wb in workbench.WORKBENCHES.items():
            a = m.addAction(icons.icon(wb.icon, t["icon"]), tr(wb.title))
            a.setToolTip(tr(wb.tip))
            a.setCheckable(True)
            a.setChecked(name == cur)
            a.triggered.connect(lambda _=False, n=name: self.choose_workbench(n))
        m.setToolTipsVisible(True)
        m.addSeparator()
        m.addAction(tr("Hidden buttons are still found by the search box")).setEnabled(False)

    def choose_workbench(self, name):
        """Rebuild the ribbon at once with the workspace ``name``, and remember it."""
        from . import workbench

        workbench.set_current(name)
        r = self.shell.ribbon
        r.use_workbench(name)
        self.build_ribbon(r)
        r.keep_shown_tab()
        r.refresh_theme()

    def fill_language_menu(self, m):
        cur = i18n.current_code()
        for code, name in i18n.LANGUAGES:
            a = m.addAction(name)
            a.setCheckable(True)
            a.setChecked(code == cur)
            a.triggered.connect(lambda _=False, c=code: self.change_language(c))
        m.addSeparator()
        m.addAction(tr("Help translate Biruni…")).triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl(i18n.HELP_TRANSLATE_URL)))

    def fill_help_menu(self, m):
        self._cmd_action(m, "learn")
        self._cmd_action(m, "report_problem")
        self._cmd_action(m, "check_updates")
        m.addSeparator()
        self._cmd_action(m, "about")
        t = self.theme.tokens()
        # the engine's own "About" stays one click away inside Biruni's About window (with the credit to QGIS)
        for name, label, ic in (("mActionHelpContents", tr("Full Reference Manual (online)"), "book"),
                                ("mActionConfigureShortcuts", tr("Keyboard Shortcuts"), "keyboard")):
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
        off = CATALOG["offline_basemap"]
        if self.runner.available(off):
            a = m.addAction(icons.icon(off.icon, t["icon"]), tr("Offline basemap (PMTiles)…"))
            a.triggered.connect(lambda: self.runner.execute(off))
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
        if self.start and getattr(self.start, "lang_combo", None) is not None:
            self.start.lang_combo.setCurrentIndex(max(0, self.start.lang_combo.findData(code)))
        if not i18n.has_qgis_translation(code):
            QMessageBox.information(self.mw, APP_NAME, tr("Some menus and dialogs of the engine have no translation in this "
                                                          "language and will stay in English."))
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
