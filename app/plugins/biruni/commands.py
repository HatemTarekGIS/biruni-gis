"""Command catalogue: every button of the Biruni interface is a Command that is
backed by a QGIS action (by objectName), a dock panel, a Processing algorithm or
a Python callable. Buttons never duplicate QGIS logic - they drive QGIS."""
from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QDockWidget


class Command:
    def __init__(self, cid, label, icon, *, action=None, dock=None, alg=None, run=None,
                 menu=None, tip=None, checkable=False):
        self.id = cid
        self.label = label          # English; may contain "\n" for two-line large buttons
        self.icon = icon            # Tabler icon name
        self.action = action        # QGIS QAction objectName
        self.dock = dock            # QDockWidget objectName (toggle)
        self.alg = alg              # Processing algorithm id (opens its dialog)
        self.run = run              # callable(plugin)
        self.menu = menu            # list of command ids, or callable(plugin, QMenu)
        self.tip = tip
        self.checkable = checkable


def _c(*a, **k):
    return Command(*a, **k)


CATALOG = {c.id: c for c in [
    # ---- project -------------------------------------------------------
    _c("new", "New", "file-plus", action="mActionNewProject"),
    _c("new_project", "New\nProject", "folder-plus", run=lambda p: p.project.new_project()),
    _c("open", "Open", "folder-open", action="mActionOpenProject"),
    _c("save", "Save", "device-floppy", run=lambda p: p.project.save()),
    _c("save_as_folder", "Save as Project Folder…", "folder-down", run=lambda p: p.project.save_as_folder()),
    _c("save_as", "Save As", "file-export", action="mActionSaveProjectAs"),
    _c("project_folder", "Project Folder", "folder-search", run=lambda p: p.project.open_folder()),
    _c("consolidate", "Copy All Data into Project", "folder-down", run=lambda p: p.project.consolidate()),
    _c("store_temporary", "Save Temporary Layers", "database-import",
       run=lambda p: p.project.store_temporary_layers()),
    _c("package", "Package as ZIP", "file-zip", run=lambda p: p.project.package()),
    _c("phone_gps", "Phone\nGPS", "device-mobile-pin", dock="BiruniGps"),
    _c("import_track", "Import GPS Track", "route-2", alg="biruni:analysetrack"),
    _c("activity_report", "Activity Report", "report-analytics", run=lambda p: p.project.activity_report()),
    _c("close", "Close", "square-x", action="mActionCloseProject"),
    _c("project_props", "Properties", "adjustments-horizontal", action="mActionProjectProperties"),
    # ---- navigate ------------------------------------------------------
    _c("pan", "Pan", "hand-stop", action="mActionPan"),
    _c("zoom_in", "Zoom In", "zoom-in", action="mActionZoomIn"),
    _c("zoom_out", "Zoom Out", "zoom-out", action="mActionZoomOut"),
    _c("full_extent", "Full\nExtent", "world", action="mActionZoomFullExtent"),
    _c("zoom_layer", "Zoom to\nLayer", "focus-2", action="mActionZoomToLayers"),
    _c("zoom_selection", "Zoom to Selection", "zoom-in-area", action="mActionZoomToSelected"),
    _c("zoom_prev", "Previous Extent", "arrow-left", action="mActionZoomLast"),
    _c("zoom_next", "Next Extent", "arrow-right", action="mActionZoomNext"),
    _c("refresh", "Refresh", "refresh", action="mActionDraw"),
    _c("new_bookmark", "New Bookmark", "bookmark", action="mActionNewBookmark"),
    _c("bookmarks", "Bookmarks", "bookmarks", action="mActionShowBookmarks"),
    # ---- selection -----------------------------------------------------
    _c("select", "Select", "pointer", action="mActionSelectFeatures",
       menu=["select", "select_polygon", "select_freehand", "select_radius"]),
    _c("select_polygon", "Select by Polygon", "lasso-polygon", action="mActionSelectPolygon"),
    _c("select_freehand", "Select Freehand", "lasso", action="mActionSelectFreehand"),
    _c("select_radius", "Select by Radius", "circle-dashed", action="mActionSelectRadius"),
    _c("select_expr", "By Attributes", "list-search", action="mActionSelectByExpression"),
    _c("select_location", "By Location", "map-search", alg="native:selectbylocation"),
    _c("select_all", "Select All", "checks", action="mActionSelectAll"),
    _c("invert_selection", "Invert Selection", "arrows-exchange", action="mActionInvertSelection"),
    _c("clear_selection", "Clear", "deselect", action="mActionDeselectAll"),
    # ---- inquiry -------------------------------------------------------
    _c("identify", "Identify", "info-circle", action="mActionIdentify"),
    _c("measure", "Measure", "ruler-measure", action="mActionMeasure",
       menu=["measure", "measure_area", "measure_angle", "measure_bearing"]),
    _c("measure_area", "Measure Area", "ruler-2", action="mActionMeasureArea"),
    _c("measure_angle", "Measure Angle", "angle", action="mActionMeasureAngle"),
    _c("measure_bearing", "Measure Bearing", "compass", action="mActionMeasureBearing"),
    _c("statistics", "Statistics", "chart-bar", action="mActionStatisticalSummary"),
    _c("attribute_table", "Attribute\nTable", "table", action="mActionOpenTable"),
    _c("map_tips", "Map Tips", "message-2", action="mActionMapTips"),
    # ---- data ----------------------------------------------------------
    _c("add_data", "Add\nData", "database-plus", action="mActionDataSourceManager"),
    _c("add_vector", "Vector", "vector-triangle", action="mActionAddOgrLayer"),
    _c("add_raster", "Raster", "photo", action="mActionAddRasterLayer"),
    _c("add_csv", "CSV / XY", "file-type-csv", action="mActionAddDelimitedText"),
    _c("add_postgis", "PostGIS", "database", action="mActionAddPgLayer"),
    _c("add_wms", "WMS / WMTS", "world-download", action="mActionAddWmsLayer"),
    _c("add_xyz", "XYZ Tiles", "grid-3x3", action="mActionAddXyzLayer"),
    _c("add_wfs", "WFS / OGC API", "world-share", action="mActionAddWfsLayer"),
    _c("basemap", "Basemap", "map-2", menu=lambda p, m: p.fill_basemap_menu(m)),
    _c("new_layer", "New\nLayer", "square-rounded-plus",
       menu=["new_gpkg", "new_shp", "new_scratch", "new_spatialite", "new_virtual"]),
    _c("new_gpkg", "GeoPackage Layer…", "package", action="mActionNewGeoPackageLayer"),
    _c("new_shp", "Shapefile Layer…", "file-plus", action="mActionNewVectorLayer"),
    _c("new_scratch", "Temporary Scratch Layer…", "file-dots", action="mActionNewMemoryLayer"),
    _c("new_spatialite", "SpatiaLite Layer…", "database", action="mActionNewSpatiaLiteLayer"),
    _c("new_virtual", "Virtual Layer…", "layers-linked", action="mActionNewVirtualLayer"),
    _c("georeferencer", "Georeferencer", "map-pin-cog", action="mActionShowGeoreferencer"),
    _c("dwg_import", "DWG / DXF\nImport", "file-import", action="mActionDwgImport"),
    # ---- edit ----------------------------------------------------------
    _c("toggle_edit", "Edit", "pencil", action="mActionToggleEditing"),
    _c("save_edits", "Save\nEdits", "device-floppy", action="mActionSaveLayerEdits"),
    _c("add_feature", "Add\nFeature", "square-plus", action="mActionAddFeature"),
    _c("vertex_tool", "Vertex\nTool", "vector-bezier-2", action="mActionVertexTool"),
    _c("move", "Move", "arrows-move", action="mActionMoveFeature"),
    _c("rotate", "Rotate", "rotate", action="mActionRotateFeature"),
    _c("delete", "Delete", "trash", action="mActionDeleteSelected"),
    _c("cut", "Cut", "cut", action="mActionCutFeatures"),
    _c("copy", "Copy", "copy", action="mActionCopyFeatures"),
    _c("paste", "Paste", "clipboard", action="mActionPasteFeatures"),
    _c("undo", "Undo", "arrow-back-up", action="mActionUndo"),
    _c("redo", "Redo", "arrow-forward-up", action="mActionRedo"),
    _c("split", "Split", "slice", action="mActionSplitFeatures"),
    _c("merge", "Merge", "arrows-join", action="mActionMergeFeatures"),
    _c("reshape", "Reshape", "vector-spline", action="mActionReshapeFeatures"),
    _c("offset", "Offset", "line-dashed", action="mActionOffsetCurve"),
    _c("trim_extend", "Trim / Extend", "arrow-bar-to-right", action="mActionTrimExtendFeature"),
    _c("simplify", "Simplify", "wave-sine", action="mActionSimplifyFeature"),
    _c("add_ring", "Add Ring", "circle-dot", action="mActionAddRing"),
    _c("add_part", "Add Part", "square-plus-2", action="mActionAddPart"),
    _c("snapping", "Snapping", "magnet", action="EnableSnappingAction"),
    _c("snapping_options", "Snapping Options", "adjustments", action="mActionSnappingOptions"),
    _c("topological", "Topological Editing", "topology-ring", action="TopologicalEditingAction"),
    _c("tracing", "Tracing", "route", action="EnableTracingAction"),
    _c("cad_tools", "CAD\nTools", "ruler-3", dock="AdvancedDigitizingTools"),
    _c("vertex_editor", "Vertex Editor", "list-details", dock="VertexEditor"),
    _c("field_calc", "Field\nCalculator", "calculator", action="mActionOpenFieldCalc"),
    _c("multi_edit", "Edit Attributes", "forms", action="mActionMultiEditAttributes"),
    # ---- analysis ------------------------------------------------------
    _c("toolbox", "Toolbox", "tools", dock="BiruniToolbox"),
    _c("qgis_toolbox", "All Tools (QGIS)", "list-tree", dock="ProcessingToolbox"),
    _c("overlaps_gaps", "Overlaps\n& Gaps", "layers-intersect", alg="biruni:overlapsgaps"),
    _c("geometry_attributes", "Area, Length\n& XY", "ruler-measure", alg="biruni:geometryattributes"),
    _c("number_features", "Number\nFeatures", "list-numbers", alg="biruni:numberfeatures"),
    _c("export_layers", "Export\nLayers", "file-export", alg="biruni:exportlayers"),
    _c("buffer", "Buffer", "circles", alg="native:buffer"),
    _c("clip", "Clip", "crop", alg="native:clip"),
    _c("intersection", "Intersect", "layers-intersect-2", alg="native:intersection"),
    _c("union", "Union", "layers-union", alg="native:union"),
    _c("difference", "Difference", "layers-difference", alg="native:difference"),
    _c("dissolve", "Dissolve", "box-multiple", alg="native:dissolve"),
    _c("spatial_join", "Spatial Join", "link", alg="native:joinattributesbylocation"),
    _c("reproject", "Reproject", "world-cog", alg="native:reprojectlayer"),
    _c("centroids", "Centroids", "point", alg="native:centroids"),
    _c("merge_layers", "Merge Layers", "stack-push", alg="native:mergevectorlayers"),
    _c("model_designer", "Model\nDesigner", "schema", action="modelerAction"),
    _c("history", "History", "history", action="historyAction"),
    _c("raster_calc", "Raster\nCalculator", "calculator", action="mActionShowRasterCalculator"),
    # ---- quality -------------------------------------------------------
    _c("check_validity", "Check\nValidity", "shield-check", alg="native:checkvalidity"),
    _c("fix_geometries", "Fix\nGeometries", "first-aid-kit", alg="native:fixgeometries"),
    _c("remove_duplicates", "Remove\nDuplicates", "copy-off", alg="native:deleteduplicategeometries"),
    _c("remove_null", "Remove Empty Geometries", "polygon-off", alg="native:removenullgeometries"),
    _c("validation_panel", "Validation\nPanel", "alert-triangle", dock="QgsGeometryValidationDockBase"),
    # ---- share ---------------------------------------------------------
    _c("new_layout", "New\nLayout", "layout-board", action="mActionNewPrintLayout"),
    _c("layout_manager", "Layouts", "layout-list", action="mActionShowLayoutManager"),
    _c("new_report", "Report", "report", action="mActionNewReport"),
    _c("export_image", "Image", "photo-down", action="mActionSaveMapAsImage"),
    _c("export_pdf", "PDF", "file-type-pdf", action="mActionSaveMapAsPdf"),
    _c("export_dxf", "DXF", "file-export", action="mActionDxfExport"),
    _c("export_layer", "Export\nLayer", "database-export", action="mActionLayerSaveAs"),
    # ---- view ----------------------------------------------------------
    _c("panel_layers", "Layers", "stack-2", dock="Layers"),
    _c("panel_browser", "Browser", "folders", dock="Browser"),
    _c("panel_styling", "Styling", "palette", dock="LayerStyling"),
    _c("panel_log", "Messages", "message-2", dock="MessageLog"),
    _c("panel_overview", "Overview", "map", dock="Overview"),
    _c("panel_toolbox", "Toolbox", "tools", dock="BiruniToolbox"),
    _c("full_screen", "Full\nScreen", "maximize", action="mActionToggleFullScreen"),
    _c("map_only", "Map\nOnly", "layout-sidebar", action="mActionToggleMapOnly"),
    _c("options", "Options", "settings", action="mActionOptions"),
    _c("plugins", "Plugins", "puzzle", action="mActionManagePlugins"),
    _c("python", "Python\nConsole", "terminal-2", action="mActionShowPythonDialog"),
    _c("shortcuts", "Keyboard Shortcuts", "keyboard", action="mActionConfigureShortcuts"),
    _c("style_manager", "Style Manager", "brush", action="mActionStyleManager"),
    _c("theme", "Theme", "sun-moon", menu=lambda p, m: p.fill_theme_menu(m)),
    _c("language", "Language", "language", menu=lambda p, m: p.fill_language_menu(m)),
    _c("classic", "Classic\nMenus", "layout-navbar", run=lambda p: p.shell.set_classic(True)),
    _c("about", "About", "info-circle", run=lambda p: p.branding.show_about()),
    # ---- added in 0.2 ------------------------------------------------------
    _c("smart_table", "Excel / CSV\nto Points", "table-import", run=lambda p: _smart_table(p)),
    _c("open_arcgis", "Open ArcGIS Project", "file-arrow-right", run=lambda p: _arcgis(p)),
    _c("open_data", "Open\nData", "world-download",
       menu=["osm_download", "s2_download", "dem_download", "landcover_download", "boundaries_download",
             "sample_data"]),
    _c("osm_download", "OpenStreetMap…", "map-2", alg="biruni:downloadosm"),
    _c("s2_download", "Satellite Image (Sentinel-2)…", "satellite", alg="biruni:downloadsentinel2"),
    _c("dem_download", "Elevation (DEM 30 m)…", "mountain", alg="biruni:downloaddem"),
    _c("landcover_download", "Land Cover (10 m)…", "plant-2", alg="biruni:downloadlandcover"),
    _c("boundaries_download", "Administrative Boundaries…", "border-outer", alg="biruni:downloadboundaries"),
    _c("sample_data", "Sample World Data", "world", run=lambda p: _samples(p)),
    _c("coords", "Coordinates", "current-location", dock="BiruniCoords"),
    _c("cad_to_gis", "CAD to\nGIS", "file-vector", alg="biruni:cadtogis"),
    _c("cad_georef", "Move to Real Coordinates", "arrows-move-vertical", alg="biruni:similaritytransform"),
    _c("cogo", "COGO", "compass", dock="BiruniCogo"),
    _c("subdivide", "Subdivide Parcel", "layout-grid", alg="biruni:subdividepolygon"),
    _c("traverse_table", "Traverse from Table", "table", alg="biruni:traverse"),
    _c("attribute_rules", "Attribute\nRules", "list-check", run=lambda p: _rules(p)),
    _c("check_data", "Check My\nData", "stethoscope", run=lambda p: _health(p)),
    _c("topology_rules", "Topology\nRules", "topology-ring", alg="biruni:topologycheck"),
    _c("closest_facility", "Closest\nFacility", "building-hospital", alg="biruni:closestfacility"),
    _c("od_matrix", "OD Matrix", "table", alg="biruni:odmatrix"),
    _c("service_area", "Service Area", "circles-relation", alg="native:serviceareafrompoint"),
    _c("shortest_path", "Shortest Path", "route", alg="native:shortestpathpointtopoint"),
    _c("map_sheet", "Map\nSheet", "file-certificate", run=lambda p: _mapsheet(p)),
    _c("web_map", "Web\nMap", "world-share", run=lambda p: _dialog(p, "share.webmap_dialog", "WebMapDialog")),
    _c("export_kmz", "Google Earth (KMZ)", "world-pin",
       run=lambda p: _dialog(p, "share.webmap_dialog", "KmzDialog")),
    _c("qfield_send", "Send to\nField", "device-mobile-up",
       run=lambda p: _dialog(p, "share.qfield_dialog", "SendToFieldDialog")),
    _c("qfield_back", "Bring Back from Field", "device-mobile-down",
       run=lambda p: _dialog(p, "share.qfield_dialog", "BringBackDialog")),
    _c("portfolio", "Portfolio Page", "id-badge-2", run=lambda p: _dialog(p, "portfolio", "PortfolioDialog")),
    _c("timesheet", "Timesheet", "clock-dollar", run=lambda p: _timesheet(p)),
    _c("history_versions", "Project History", "history-toggle", run=lambda p: p.history.show_dialog()),
    _c("learn", "Learn", "school", dock="BiruniLearn"),
    _c("report_problem", "Report a Problem", "bug", run=lambda p: p.support.report_problem()),
    _c("check_updates", "Check for Updates", "refresh-alert", run=lambda p: p.support.check(manual=True)),
]}


def _smart_table(p):
    from .smart_table_dialog import open_dialog

    open_dialog(p)


def _arcgis(p):
    from .arcgis import ui

    ui.open_arcgis_dialog(p)


def _samples(p):
    from .samples import load_world

    p.start.hide_page()
    load_world(p)


def _rules(p):
    from .rules import open_rules

    open_rules(p)


def _health(p):
    from .quality.health_dialog import show_health_dialog

    show_health_dialog(p)


def _mapsheet(p):
    from .mapsheet_dialog import open_mapsheet

    open_mapsheet(p)


def _timesheet(p):
    from .timesheet import open_timesheet

    open_timesheet(p)


def _dialog(p, module, cls):
    import importlib

    mod = importlib.import_module(f"{__package__}.{module}")
    getattr(mod, cls)(p).exec()


class CommandRunner:
    """Resolves Commands against the live QGIS main window and executes them."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.mw = plugin.iface.mainWindow()
        self._actions = {}
        self._dialogs = []

    def qaction(self, name):
        if name not in self._actions or self._actions[name] is None:
            found = [a for a in self.mw.findChildren(QAction, name)]
            self._actions[name] = found[0] if found else None
        return self._actions[name]

    def dock(self, name):
        return self.mw.findChild(QDockWidget, name)

    def available(self, cmd):
        if cmd.action:
            return self.qaction(cmd.action) is not None
        if cmd.dock:
            return self.dock(cmd.dock) is not None
        if cmd.alg:
            return QgsApplication.processingRegistry().algorithmById(cmd.alg) is not None
        return cmd.run is not None or cmd.menu is not None

    def execute(self, cmd):
        if cmd.action:
            a = self.qaction(cmd.action)
            if a is not None and a.isEnabled():
                a.trigger()
        elif cmd.dock:
            d = self.dock(cmd.dock)
            if d is not None:
                show = not d.isVisible()
                d.setVisible(show)
                if show:
                    d.raise_()
        elif cmd.alg:
            self.open_algorithm(cmd.alg)
        elif cmd.run:
            cmd.run(self.plugin)

    def open_algorithm(self, alg_id):
        import processing

        dlg = processing.createAlgorithmDialog(alg_id)
        if dlg is None:
            return
        self._dialogs.append(dlg)
        dlg.finished.connect(lambda *_: self._dialogs.remove(dlg) if dlg in self._dialogs else None)
        dlg.show()
        dlg.raise_()
