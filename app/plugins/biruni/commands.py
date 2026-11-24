"""Command catalogue: every button of the Biruni interface is a Command that is
backed by a QGIS action (by objectName), a dock panel, a Processing algorithm or
a Python callable. Buttons never duplicate QGIS logic - they drive QGIS."""
import time

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
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
    _c("field_photos", "Field\nPhotos", "camera", run=lambda p: _mod("field_photos.dock").open_dock(p)),
    _c("live_share", "See My\nMap", "antenna", run=lambda p: _mod("live_share.dock").open_dock(p)),
    _c("team_room", "Team\nRoom", "users", run=lambda p: _mod("team.dock").open_dock(p)),
    _c("dashboard", "Live\nDashboard", "presentation-analytics", run=lambda p: _mod("dashboard.dock").open_ui(p),
       tip="A live dashboard of your layers on phones, tablets and big screens - straight from this computer, no "
           "server: each person gets a signed card with what they may see, and every number carries a seal."),
    _c("dashboard_builder", "Dashboard\nBuilder", "layout-dashboard", run=lambda p: _mod("dashboard.dock").builder_ui(p),
       tip="Design a dashboard: numbers, gauges, charts, tables, maps, team activity, filters and themes, with a "
           "live preview on a phone."),
    _c("dashboard_seal_check", "Check a Dashboard Seal", "clipboard-check",
       run=lambda p: _mod("dashboard.dock").seal_check_ui(p),
       tip="Check that a number seen on a dashboard (or a screenshot of it) came from this data at that time."),
    _c("map_service", "Map\nService", "world-share", run=lambda p: _mod("mapservice.dialog").open_dialog(p),
       tip="Publish layers as a full map service with no server: the map in one file, identify and queries read in "
           "pieces, an OGC API other GIS programs can add, and a signature that shows any change. Put the folder on "
           "GitHub Pages, a shared folder or any web host."),
    _c("map_service_check", "Check a Map Service", "map-check", run=lambda p: _mod("mapservice.dialog").check_ui(p),
       tip="Check that a published map service is signed and that no file was changed after signing."),
    _c("portal", "Signed\nPortal", "world-upload", run=lambda p: _mod("portal").open_dialog(p),
       tip="The office catalogue with no server: maps, files and dashboards, each signed; groups are keys, so only "
           "members open what is shared with them; follow other offices' portals; items can expire."),
    _c("portal_viewer", "Hosted Portal Viewer", "browser", run=lambda p: _mod("portal").build_hosted_viewer(),
       tip="Build the portal viewer page for the public bridge site, so any portal folder opens from one address."),
    _c("team_vault", "Team Vault", "shield-check", run=lambda p: _mod("team_vault").open_dialog(p),
       tip="Keep encrypted copies of your work spread over the team's computers: any 3 of 5 bring it back, no one "
           "computer can read it, and repeated versions take almost no extra space."),
    _c("scrub_file", "Clean a File for Sending", "shield-check", run=lambda p: _mod("scrub").open_dialog(p),
       tip="Writes a clean copy of a file before it leaves the office: user names, paths on this computer, hidden "
           "fields, embedded projects and document properties are removed, with a receipt of what was removed."),
    _c("team_pigeon", "Carrier\nPigeon", "send", run=lambda p: _mod("pigeon.ui").open_ui(p),
       tip="Team room changes carried by hand where there is no network: a pack (a file or codes on the screen) "
           "passes from person to person until each change reaches the host and back. Encrypted and signed."),
    _c("light_send", "Send by\nLight", "bulb", run=lambda p: _mod("lightsync.ui").send_file_ui(p),
       tip="A file to a computer with no network and no USB allowed: this screen shows it as a moving code that the "
           "other computer's camera reads (optionally sealed with a passphrase)."),
    _c("light_receive", "Receive by Light", "camera", run=lambda p: _mod("lightsync.ui").receive_file_ui(p),
       tip="Read with this computer's camera a file shown as a moving code on another screen (Send by Light there)."),
    _c("team_compute", "Team\nComputing", "cpu", run=lambda p: _mod("team_compute.ui").run_ui(p),
       tip="Runs a heavy tool (overlaps and gaps, a dissolved buffer, slope, hillshade…) on the idle computers of the "
           "team room, in tiles; the answer is the same as on one computer."),
    _c("team_compute_share", "Share My Computer", "share-2", run=lambda p: _mod("team_compute.ui").share_ui(p),
       tip="Lets the members of the room run heavy tools on this computer while you are not editing (off until you "
           "turn it on)."),
    _c("review_draw", "Peer Review\nLottery", "dice-5", run=lambda p: _mod("peer_review.ui").draw_ui(p),
       tip="Draw a fair, checkable random sample of each person's work for blind review by someone else; the error "
           "rate of the whole delivery follows with a stated confidence."),
    _c("review_mine", "Review my Share", "clipboard-check", run=lambda p: _mod("peer_review.ui").review_ui(p),
       tip="Review the features the lottery gave you, one at a time, without seeing whose work they are."),
    _c("review_results", "Review Results", "chart-bar", run=lambda p: _mod("peer_review.ui").results_ui(p),
       tip="Error rate per person and for the whole delivery (exact 95% bounds), accept or reject per the plan, "
           "check the draw, save the report."),
    _c("diwan", "Diwan", "history", run=lambda p: _mod("diwan.dock").open_dock(p),
       tip="A GeoPackage that remembers: every feature's identity and history, the layer as it was at any moment, "
           "and the round trip through other formats"),
    _c("phone_hub", "Phone", "device-mobile", run=lambda p: _mod("phone_hub.dock").open_dock(p),
       tip="One code for the phone: it opens the phone GPS, field photos and your live map on the phone, without "
           "scanning another code."),
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
    # ---- map stops (stops/): saved views with pictures, history, tours ----
    _c("map_stops", "Map\nStops", "stops/map-route", menu=lambda p, m: _mod("stops").fill_menu(p, m)),
    _c("new_stop", "New Stop", "map-pin-plus", run=lambda p: _mod("stops").new_stop(p)),
    _c("manage_stops", "Manage Stops", "list-details", run=lambda p: _mod("stops").open_manager(p)),
    _c("stops_tour", "Play Stops as a Tour", "stops/player-play", run=lambda p: _mod("stops").play_tour(p)),
    _c("view_back", "Previous View", "arrow-back", run=lambda p: _mod("stops").go_back(p)),
    _c("view_forward", "Next View", "arrow-forward", run=lambda p: _mod("stops").go_forward(p)),
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
    _c("astrolabe", "Astrolabe", "compass", run=lambda p: _mod("astrolabe").toggle(p),
       tip="Turn three rings - on what, what to do, how much - and see the result on the map before it happens. "
           "Also F9, or right-click the map."),
    _c("astrolabe_repeat", "Repeat Last Command", "history-toggle", run=lambda p: _mod("astrolabe").repeat(p),
       tip="The astrolabe's last command again, on what is selected now."),
    _c("measure", "Measure", "ruler-measure", action="mActionMeasure",
       menu=["measure", "measure_area", "measure_angle", "measure_bearing"]),
    _c("measure_area", "Measure Area", "ruler-2", action="mActionMeasureArea"),
    _c("measure_angle", "Measure Angle", "angle", action="mActionMeasureAngle"),
    _c("measure_bearing", "Measure Bearing", "compass", action="mActionMeasureBearing"),
    _c("statistics", "Statistics", "chart-bar", action="mActionStatisticalSummary"),
    _c("attribute_table", "Attribute\nTable", "table", action="mActionOpenTable"),
    _c("map_tips", "Map Tips", "message-2", action="mActionMapTips"),
    # ---- data ----------------------------------------------------------
    _c("add_data", "Add\nData", "database-plus", run=lambda p: _mod("add_data").open_dialog(p)),
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
    _c("review_save", "Review\n& Save", "list-check", run=lambda p: _mod("editing.review_save").open_review(p)),
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
    _c("qgis_toolbox", "All Processing Tools", "list-tree", dock="ProcessingToolbox"),
    _c("overlaps_gaps", "Overlaps\n& Gaps", "layers-intersect", alg="biruni:overlapsgaps"),
    _c("coverage_doctor", "Parcel Coverage\nDoctor", "stethoscope", run=lambda p: _mod("coverage.ui").open_ui(p),
       tip="Checks a parcel layer as one fabric (narrow gaps, overlaps, shared sides drawn twice, slivers) and mends "
           "it in its edits: one Undo takes it back."),
    _c("wbt_watershed", "Watershed above Points", "map-pin", alg="biruni:wbtwatershed",
       tip="The area that drains to each chosen point (a culvert, a bridge, a dam site), with its area."),
    _c("wbt_ponding", "Where Water Ponds", "droplet", alg="biruni:wbtponding",
       tip="The low spots where rain water collects with no way out, with depth, area and volume."),
    _c("wbt_floodzone", "Flood-prone Land", "wave-sine", alg="biruni:wbtfloodzone",
       tip="The land along wadis that floods first (height above the nearest stream), with the streams."),
    _c("dwg_open", "Open DWG", "file-import", run=lambda p: _mod("dwg.ui").open_ui(p),
       tip="Open an AutoCAD DWG (R13 to 2018) as layers: arcs, blocks and Arabic texts kept."),
    _c("dwg_save", "Save as DWG", "file-export", run=lambda p: _mod("dwg.ui").save_ui(p),
       tip="Save the layers selected in the Layers panel (or a DXF) as an AutoCAD 2000 DWG."),
    _c("geoai_digitise", "Smart\nDigitise", "wand", run=lambda p: _mod("geoai.ui").open_ui(p),
       tip="Click a building, a field or a pool on an orthophoto and its outline is drawn (Segment Anything, on this "
           "computer, offline; the model is downloaded once with consent)."),
    _c("geoai_model", "Smart Digitise Model", "download", run=lambda p: _mod("geoai.ui").model_ui(p),
       tip="Download, import from a folder or remove the local model of Smart Digitise."),
    _c("offline_basemap", "Offline\nBasemap", "world-download", run=lambda p: _mod("share.offline_maps_ui").basemap_ui(p),
       tip="A basemap kept in one file (PMTiles), drawn without the internet with place names in Arabic: open one, or "
           "download only the area on the map from the daily OpenStreetMap build."),
    _c("basemap_pack", "Basemap Pack", "stack-push", run=lambda p: _mod("share.offline_maps_ui").pack_ui(p),
       tip="The chosen layers as one PMTiles file of vector tiles, for a website or another computer."),
    _c("site_preview", "Preview Website", "browser", run=lambda p: _mod("share.offline_maps_ui").preview_ui(p),
       tip="Open a published website from a preview server on this computer (PMTiles needs http)."),
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
    _c("shp_arcs_export", "Shapefile\nwith Arcs", "file-export", run=lambda p: _mod("shparc.ui").export_ui(p),
       tip="A shapefile every program opens, with the arcs as short lines that keep the area exactly and the "
           "length to a tenth of a millimetre - and the arcs themselves kept beside it for Biruni."),
    _c("shp_arcs_open", "Open Shapefile with Arcs", "folder-open", run=lambda p: _mod("shparc.ui").open_ui(p),
       tip="Open a shapefile written with its arcs: the arcs come back as arcs."),
    _c("copies_compare", "Compare\nCopies", "fingerprint", run=lambda p: _mod("fingerprint.ui").compare_ui(p),
       tip="Two copies of a layer compared square by square by their fingerprints: only the places that differ are "
           "opened, shown on the map, and the changes of one brought into the other."),
    _c("copies_certificate", "Delivery Certificate", "certificate", run=lambda p: _mod("fingerprint.ui").certificate_ui(p),
       tip="The fingerprints of the selected layers in one file with a short code: whoever receives the data proves "
           "it is what was delivered, and if anything changed it shows where."),
    _c("copies_check", "Check Certificate", "rosette-discount-check", run=lambda p: _mod("fingerprint.ui").check_ui(p),
       tip="Check the layers on the map against a delivery certificate."),
    _c("copies_package", "Changes Package", "package-export", run=lambda p: _mod("fingerprint.ui").package_ui(p),
       tip="Only the squares where this layer differs from the other copy (from its certificate), in a small file to "
           "carry: on a flash drive, by e-mail, without a server."),
    _c("copies_apply", "Apply Package", "package-import", run=lambda p: _mod("fingerprint.ui").apply_ui(p),
       tip="Bring a changes package into this layer. A square changed on both sides is left for you to decide."),
    _c("copies_area_proof", "Prove an Area", "map-search", run=lambda p: _mod("fingerprint.proof_ui").prove_ui(p),
       tip="A proof that a layer has nothing in an area, or exactly these features and no others: checked by anyone "
           "from the file alone, against the code of the delivery certificate, with the values around it hidden."),
    _c("copies_area_check", "Check Area Proof", "map-check", run=lambda p: _mod("fingerprint.proof_ui").verify_ui(p),
       tip="Check an area proof: what it says is in the area is all there is, in the data of its certificate's code."),
    _c("copies_share_hidden", "Share with Fields Hidden", "eye-off",
       run=lambda p: _mod("fingerprint.disclose_ui").share_ui(p),
       tip="A copy without the fields you choose (owners, prices…) that still proves every shape and value it shows "
           "is what the delivery certificate sealed; the values left out cannot be found from it."),
    _c("copies_shared_check", "Check Shared Copy", "file-text-shield",
       run=lambda p: _mod("fingerprint.disclose_ui").verify_ui(p),
       tip="Check a shared copy: all it shows is as certified, nothing added and nothing taken out."),
    _c("copies_totals_proof", "Prove Totals", "sum", run=lambda p: _mod("fingerprint.totals_ui").prove_ui(p),
       tip="Prove how many features an area has, their area or length and the totals of their numeric fields, "
           "without the features: checked by anyone from the file alone, against the code of the delivery certificate."),
    _c("copies_totals_check", "Check Totals Proof", "calculator", run=lambda p: _mod("fingerprint.totals_ui").verify_ui(p),
       tip="Check a totals proof: the count and the sums it gives are those of the certified data in its area."),
    _c("copies_mark", "Marked Copy", "fingerprint", run=lambda p: _mod("watermark.ui").mark_ui(p),
       tip="A copy marked with whom it is given to, hidden in its coordinates (a fortieth of a millimetre): still the "
           "certified data, but if it turns up where it should not, it tells whose copy it was."),
    _c("copies_whose", "Whose Copy?", "id-badge-2", run=lambda p: _mod("watermark.ui").who_ui(p),
       tip="Find whom a copy was given to, from its coordinates alone, even from a few features cut out of it."),
    _c("copies_stamp", "Stamp the Date", "calendar", run=lambda p: _mod("fingerprint.stamp_ui").stamp_ui(p),
       tip="Prove when a certificate existed, without trusting anyone: only its fingerprint is sent to the free "
           "OpenTimestamps calendars, which put it in a Bitcoin block within hours."),
    _c("copies_stamp_check", "Check the Date", "calendar-repeat", run=lambda p: _mod("fingerprint.stamp_ui").check_ui(p),
       tip="Check when a stamped file existed (completing its stamp first when it is still pending)."),
    _c("copies_private_match", "Private Match", "lock-search", run=lambda p: _mod("psi.ui").open_ui(p),
       tip="Find the features you and another party both have (the same parcel number, the same place) without "
           "either seeing the other's list: you trade small files, no server."),
    _c("conflate_match", "Match Two Layers", "arrows-join-2", run=lambda p: _mod("conflate.ui").open_ui(p),
       tip="Find the same features in two sources that share no id (another agency, another coordinate system, a "
           "shift of a metre) by their shapes, see which field of one is which field of the other by their values, "
           "even in other units, and copy the ticked values across."),
    # ---- view ----------------------------------------------------------
    _c("panel_layers", "Layers", "stack-2", dock="Layers"),
    _c("panel_browser", "Classic Browser", "folders", dock="Browser"),
    _c("panel_styling", "Styling", "palette", dock="LayerStyling"),
    _c("panel_log", "Messages", "message-2", dock="MessageLog"),
    _c("panel_overview", "Overview", "map", dock="Overview"),
    _c("panel_toolbox", "Toolbox", "tools", dock="BiruniToolbox"),
    _c("vault", "Vault", "vault/vault", run=lambda p: _mod("vault").toggle_dock(p)),
    _c("panel_vault", "Vault", "vault/vault", run=lambda p: _mod("vault").toggle_dock(p)),
    _c("standard_panels", "Standard Panel Layout", "layout-sidebar", run=lambda p: standard_panels(p)),
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
    _c("migrate_scan", "Readiness\nScan", "list-search", run=lambda p: _mod("migrate").open_dialog(p)),
    _c("export_aprx", "Save for\nArcGIS Pro", "file-export", run=lambda p: _mod("arcgis.export_ui").export_aprx(p)),
    _c("export_mapx", "Save Map for ArcGIS Pro (.mapx)", "map-2", run=lambda p: _mod("arcgis.export_ui").export_mapx(p)),
    _c("export_lyrx", "Save Layer for ArcGIS Pro (.lyrx)", "stack-2",
       run=lambda p: _mod("arcgis.export_ui").export_lyrx(p)),
    _c("import_pagx", "Open ArcGIS Pro Layout", "layout", run=lambda p: _mod("arcgis.export_ui").import_pagx(p)),
    _c("arcgis_services", "ArcGIS\nServices", "world-cog", run=lambda p: _mod("arcgis_services.ui").open_browser(p)),
    _c("arcgis_sync", "Sync Offline Layer", "world-upload", run=lambda p: _mod("arcgis_services.ui").sync_active_layer(p)),
    _c("arcgis_signin", "Sign In to Services Again", "key",
       run=lambda p: _mod("arcgis_services.ui").refresh_service_tokens(p)),
    _c("run_arcpy", "Run arcpy\nScript", "script", alg="biruni:runarcpyscript"),
    _c("import_models", "Import\nModelBuilder", "schema", run=lambda p: _mod("migrate").import_models_dialog(p)),
    _c("open_data", "Open\nData", "world-download",
       menu=["osm_download", "overture", "s2_download", "dem_download", "landcover_download",
             "boundaries_download", "sample_data"]),
    _c("osm_download", "OpenStreetMap…", "map-2", alg="biruni:downloadosm"),
    _c("s2_download", "Satellite Image (Sentinel-2)…", "satellite", alg="biruni:downloadsentinel2"),
    _c("dem_download", "Elevation (DEM 30 m)…", "mountain", alg="biruni:downloaddem"),
    _c("landcover_download", "Land Cover (10 m)…", "plant-2", alg="biruni:downloadlandcover"),
    _c("boundaries_download", "Administrative Boundaries…", "border-outer", alg="biruni:downloadboundaries"),
    _c("sample_data", "Sample World Data", "world", run=lambda p: _samples(p)),
    _c("coords", "Coordinates", "current-location", dock="BiruniCoords"),
    _c("cad_to_gis", "CAD to\nGIS", "file-vector", alg="biruni:cadtogis"),
    _c("indoor_map", "Floor Plan to Indoor Map", "building", alg="biruni:floorplantoindoor"),
    _c("indoor_route", "Route Between Rooms", "route", alg="biruni:indoorroute"),
    _c("cad_georef", "Move to Real Coordinates", "arrows-move-vertical", alg="biruni:similaritytransform"),
    _c("cogo", "COGO", "compass", dock="BiruniCogo"),
    _c("subdivide", "Subdivide Parcel", "layout-grid", alg="biruni:subdividepolygon"),
    _c("traverse_table", "Traverse from Table", "table", alg="biruni:traverse"),
    _c("attribute_rules", "Attribute\nRules", "list-check", run=lambda p: _rules(p)),
    # ---- the locked safe (secure/): password-locked databases and projects, offline users and rights ----
    _c("lock_database", "Lock\nDatabase", "lock", run=lambda p: _mod("secure.runtime").lock_database(p)),
    _c("lock_users", "Users &\nPermissions", "shield-check", run=lambda p: _mod("secure.runtime").manage_users(p)),
    _c("lock_project", "Lock Project", "file-text-shield", run=lambda p: _mod("secure.runtime").lock_project(p)),
    _c("open_locked", "Open Locked File", "key", run=lambda p: _mod("secure.runtime").open_locked(p)),
    _c("remove_lock", "Remove Lock", "lock-search", run=lambda p: _mod("secure.runtime").remove_lock(p)),
    # ---- the data charter (charter/): fields, lists, kinds, rules, linked choices ----
    _c("data_charter", "Data\nCharter", "license", run=lambda p: _mod("charter.dialog").open_charter(p)),
    _c("charter_fields", "Fields", "columns-3", run=lambda p: _mod("charter.dialog").open_charter(p, "fields")),
    _c("charter_lists", "Value Lists", "list-details", run=lambda p: _mod("charter.dialog").open_charter(p, "lists")),
    _c("charter_kinds", "Kinds", "category", run=lambda p: _mod("charter.dialog").open_charter(p, "kinds")),
    _c("charter_rules", "Rules", "list-check", run=lambda p: _mod("charter.dialog").open_charter(p, "rules")),
    _c("charter_links", "Linked Choices", "list-tree", run=lambda p: _mod("charter.dialog").open_charter(p, "links")),
    _c("charter_learn", "Learn from\nData", "sparkles", run=lambda p: _mod("charter.dialog").open_charter(p, "learn")),
    _c("geo_rules", "Spatial\nRules", "shield-check", run=lambda p: _mod("georules.ui").open_ui(p),
       tip="The rules the shapes keep - no overlaps, a smallest area, the area field is the area, inside the "
           "parcels - learned from the data; a save that breaks one does not happen. Kept in the data itself."),
    _c("charter_health", "Data\nHealth", "heart-rate-monitor", run=lambda p: _mod("charter.dialog").open_charter(p, "health")),
    _c("charter_dictionary", "Data\nDictionary", "book", run=lambda p: _mod("charter.dialog").open_charter(p, "share")),
    _c("check_data", "Check My\nData", "stethoscope", run=lambda p: _health(p)),
    _c("spatial_spell", "Spatial\nSpell Check", "map-search",
       run=lambda p: _mod("insight.spell_dialog").open_spellcheck(p),
       tip="Check each value against where its feature lies: the district around it, the nearest street, its "
           "neighbours, its measured area"),
    _c("learned_rules", "Rules from\nYour Data", "bulb", run=lambda p: _mod("insight.rules_dialog").open_rules(p),
       tip="Discover the rules your data follows without anyone writing them, and the features that break them"),
    _c("topology_rules", "Topology\nRules", "topology-ring", alg="biruni:topologycheck"),
    _c("closest_facility", "Closest\nFacility", "building-hospital", alg="biruni:closestfacility"),
    _c("od_matrix", "OD Matrix", "table", alg="biruni:odmatrix"),
    _c("service_area", "Service Area", "circles-relation", alg="native:serviceareafrompoint"),
    _c("shortest_path", "Shortest Path", "route", alg="native:shortestpathpointtopoint"),
    _c("map_sheet", "Map\nSheet", "file-certificate", run=lambda p: _mapsheet(p)),
    _c("map_book", "Map\nBook", "mapbook/map-book", run=lambda p: _mod("mapbook").open_panel(p)),
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
    _c("from_pro", "Coming from\nArcGIS Pro?", "arrows-exchange", run=lambda p: _mod("from_pro").open_dialog(p)),
    _c("report_problem", "Report a Problem", "bug", run=lambda p: p.support.report_problem()),
    _c("check_updates", "Check for Updates", "refresh-alert", run=lambda p: p.support.check(manual=True)),
    # ---- added in 0.3 ------------------------------------------------------
    _c("office_tools", "Engineering\nOffice", "briefcase", run=lambda p: _toolbox_category(p, "office")),
    _c("guess_crs", "Which Coordinate\nSystem?", "zoom-question", alg="biruni:guesscrs"),
    _c("delivery_check", "Check\nDelivery", "clipboard-check", alg="biruni:deliverycheck"),
    _c("seal_files", "Seal\nFiles", "file-text-shield", alg="biruni:sealfiles"),
    _c("check_seal", "Check a\nSealed File", "shield-check", alg="biruni:checkseal"),
    _c("delivery_spec", "Delivery Spec Template", "file-spreadsheet", alg="biruni:deliveryspectemplate"),
    _c("compare_layers", "Compare\nVersions", "git-compare", alg="biruni:comparelayers"),
    _c("quantities", "Bill of\nQuantities", "receipt", alg="biruni:quantities"),
    _c("deed_parcel", "Parcel from Deed", "file-text", alg="biruni:deedtoparcel"),
    _c("deed_survey", "Deed vs Survey", "file-diff", alg="biruni:deedvssurvey"),
    _c("setbacks", "Setbacks Check", "building-community", alg="biruni:setbackcheck"),
    _c("scan_parcels", "Parcels from Scanned Plan", "scan", alg="biruni:scantoparcels"),
    _c("arabic_join", "Join by Similar Names", "link", alg="biruni:arabicjoin"),
    _c("whatsapp_points", "WhatsApp Locations", "brand-whatsapp", alg="biruni:whatsapplocations"),
    _c("review_send", "Send for\nReview", "message-2-share",
       run=lambda p: _dialog(p, "share.review_dialog", "SendForReviewDialog")),
    _c("review_back", "Bring Back Review Comments", "message-2-down",
       run=lambda p: _dialog(p, "share.review_dialog", "BringBackReviewDialog")),
    _c("clean_table", "Clean My\nTable", "wash-dry", alg="biruni:cleantable"),
    _c("spell_values", "Spelling\nVariants", "text-spellcheck", alg="biruni:spellvalues"),
    _c("export_photos", "Export\nPhotos", "photo-down", alg="biruni:exportphotos"),
    _c("georef_pdf", "Georeference\nPDF", "file-type-pdf", alg="biruni:georefpdf"),
    _c("spatial_filter", "Show Only\nInside Area", "filter-cog", run=lambda p: _spatial_filter(p, True)),
    _c("spatial_filter_clear", "Show Everything", "filter-off", run=lambda p: _spatial_filter(p, False)),
    _c("feature_counts", "Feature Counts", "list-numbers", run=lambda p: _feature_counts()),
    _c("four_colors", "4-Colour\nMap", "color-swatch", alg="biruni:fourcolors"),
    _c("light_mode", "Light\nMode", "feather", run=lambda p: _light_mode(p)),
    _c("free_memory", "Free Memory", "memory", run=lambda p: _mod("performance").free_memory_command(p)),
    _c("autosave", "Autosave", "clock-play", menu=lambda p, m: _autosave_menu(m)),
    _c("cad_more", "More\nTools", "dots-circle-horizontal",
       menu=["guess_crs", "wrong_place", "ortho_tiles", "landxml_in", "landxml_out", "field_to_finish",
             "ground_grid", "localization", "s2_download", "landcover_download", "boundaries_download"]),
    _c("overture", "Overture Maps (buildings, places…)…", "building-skyscraper", alg="biruni:downloadoverture"),
    _c("gnss_quality", "GNSS\nQuality", "satellite", alg="biruni:gnssquality"),
    _c("crs_doctor", "Why Don't My\nLayers Line Up?", "map-question", run=lambda p: _mod("trends_ui").crs_doctor(p)),
    _c("earth_tour", "Google Earth\nTour", "plane-departure", run=lambda p: _mod("trends_ui").earth_tour(p)),
    _c("import_kml", "Open KML / KMZ (with colours)", "file-import", run=lambda p: _mod("trends_ui").import_kml(p)),
    _c("publish_site", "Publish as\nWebsite", "world-upload", run=lambda p: _mod("trends_ui").publish_site(p)),
    _c("who_locks", "Who Is Using\nThis File?", "lock-search", run=lambda p: _mod("file_locks").open_dialog(p)),
    _c("project_doctor", "Project\nDoctor", "heart-rate-monitor",
       run=lambda p: _mod("project_doctor_dialog").open_dialog(p)),
    _c("system_health", "System\nHealth", "shield-check",
       run=lambda p: _mod("health").open_dialog(p)),
    _c("backup_sync", "Backup\nand Sync", "database-export",
       run=lambda p: _mod("backup.dialog").open_dialog(p),
       tip="Back up the office's photos and keys to a disk of its own, check the backup and test a restore."),
    _c("where_used", "Where Is This Data Used?", "file-search",
       run=lambda p: _mod("project_doctor_dialog").open_dialog(p, "used")),
    _c("rename_project", "Rename Project", "cursor-text",
       run=lambda p: _mod("project_doctor_dialog").open_dialog(p, "rename")),
    _c("session_script", "Steps to\nScript", "file-code", run=lambda p: _mod("session_script").open_dialog(p)),
    _c("quick_align", "Place Image\non Map", "photo-pin", run=lambda p: _mod("quick_align").start(p)),
    _c("lines_to_plots", "Plots from\nLines", "polygon", alg="biruni:linestoplots"),
    _c("sales_map", "Sales\nMap", "map-dollar", run=lambda p: _dialog(p, "share.sales_map_dialog", "SalesMapDialog")),
    _c("sales_back", "Bring Back Plot Statuses", "home-dollar",
       run=lambda p: _dialog(p, "share.sales_map_dialog", "BringBackSalesDialog")),
    _c("label_cluster", "Group\nLabels", "tags", alg="biruni:labelcluster"),
    _c("map_index", "Map Book\nIndex", "list-letters", alg="biruni:mapindex"),
    _c("tabular_labels", "Table Labels", "table", run=lambda p: _tabular_labels(p)),
    _c("contour_style", "Contour\nLabels", "line", run=lambda p: _contour_style(p)),
    _c("movable_labels", "Movable Labels", "hand-move", run=lambda p: _movable_labels(p)),
    _c("ground_grid", "Ground ↔\nGrid", "scale", alg="biruni:groundgrid"),
    _c("localization", "Site\nCalibration", "target", alg="biruni:localization"),
    _c("field_to_finish", "Field to\nFinish", "route-2", alg="biruni:fieldtofinish"),
    _c("landxml_in", "Open LandXML", "file-import", alg="biruni:landxmlimport"),
    _c("landxml_out", "Save as LandXML", "file-export", alg="biruni:landxmlexport"),
    _c("wrong_place", "Why in the\nWrong Place?", "map-question", alg="biruni:wrongplace"),
    _c("ortho_tiles", "Imagery for CAD", "grid-pattern", alg="biruni:orthotiles"),
    _c("parcel_description", "Boundaries\nText", "file-description", alg="biruni:parceldescription"),
    _c("parcel_bundle", "Parcel\nFiles", "package-export", alg="biruni:parcelbundle"),
    _c("table_tools", "Table\nTools", "table-options", run=lambda p: _table_tools(p, 0)),
    _c("field_order", "Field Order", "columns-3", run=lambda p: _table_tools(p, 0)),
    _c("highlight_table", "Highlight Cells", "highlight", run=lambda p: _table_tools(p, 1)),
    _c("natural_sort", "Sort Naturally", "sort-ascending-numbers", run=lambda p: _table_tools(p, 2)),
    _c("multi_join", "Join by Several Fields", "arrows-join-2", alg="biruni:multijoin"),
    _c("select_previous", "Select Previous", "arrow-back", run=lambda p: _selsets().select_previous(p)),
    _c("select_next", "Select Next", "arrow-forward", run=lambda p: _selsets().select_next(p)),
    _c("saved_selections", "Saved\nSelections", "bookmarks", menu=lambda p, m: _selsets().fill_menu(p, m)),
    _c("arabic_labels_fix", "Fix Arabic\nLabels", "text-direction-rtl", run=lambda p: _arabic_fix(p)),
    _c("arabic_text_doctor", "Fix Arabic\nText", "language", alg="biruni:arabictextdoctor"),
    _c("hijri_dates", "Hijri Dates", "calendar", alg="biruni:hijridates"),
    _c("map_studio", "Map\nStudio", "wand", run=lambda p: _mod("map_studio").open_studio(p)),
    _c("thematic", "Thematic\nMap", "chart-dots-3", run=lambda p: _mod("map_studio.thematic_dialog").open_dialog(p)),
    _c("color_preview", "Check Map Colours", "color-filter",
       run=lambda p: _mod("map_studio.color_dialog").open_color_preview(p)),
    _c("terrain_style", "Terrain Colours and Hillshade", "mountain", alg="biruni:terrainstyle"),
    _c("map_products", "Ready\nMaps", "files", menu=lambda p, m: _mod("map_studio.tools_ui").products_menu(p, m)),
    _c("place_icons", "Icons from Names", "map-pins", run=lambda p: _mod("map_studio.tools_ui").place_icons(p)),
    _c("symbols_by_scale", "Symbols by Scale", "zoom-scan",
       run=lambda p: _mod("map_studio.tools_ui").symbols_by_scale(p)),
    _c("install_symbols", "Add Biruni Symbols to Library", "category",
       run=lambda p: _mod("map_studio.tools_ui").install_symbols(p)),
    _c("bilingual_labels", "Labels in Two Languages", "language-hiragana",
       run=lambda p: _mod("map_studio.tools_ui").bilingual_labels(p)),
    _c("label_priorities", "Label Importance", "sort-descending",
       run=lambda p: _mod("map_studio.tools_ui").label_priorities(p)),
    _c("hidden_labels", "Hidden Labels Report", "eye-off", run=lambda p: _mod("map_studio.tools_ui").hidden_labels(p)),
    _c("map_caption", "Map Caption", "text-caption", run=lambda p: _mod("map_studio.tools_ui").caption(p)),
    _c("live_qr", "Live QR\nCode", "device-mobile-pin", alg="biruni:liveqr"),
    _c("create_features", "Create\nFeatures", "template", run=lambda p: _mod("editing.create_pane").open_pane(p),
       tip="Draw new features from templates - typed lengths and directions, arcs, rectangles, circles, fill and "
           "auto-complete"),
    _c("modify_features", "Modify\nFeatures", "edit/modify-features",
       run=lambda p: _mod("editing.modify_pane").open_pane(p),
       tip="Change the selected features - align, move / rotate / scale, repeat copy, copy parallel, fillet, "
           "split, two-point cut, reshape, shared edges, merge, clip, fill gaps, transfer attributes and clean-up"),
    # ---- spatial statistics (spatial_stats/) ------------------------------
    _c("ss_advisor", "Spatial Statistics\nAdvisor", "zoom-question",
       run=lambda p: _mod("spatial_stats.advisor_dialog").open_dialog(p)),
    _c("ss_hotspots", "Hot Spots", "flame", alg="biruni:sshotspots"),
    _c("ss_clusters", "Clusters and Outliers", "chart-dots-3", alg="biruni:sslisa"),
    _c("ss_moran", "Similar Values Together?", "chart-dots-3", alg="biruni:ssmoran"),
    _c("ss_points", "Clustered or Spread Out?", "circle-dot", alg="biruni:ssnearest"),
    _c("ss_kde", "Density Map", "circle-dot", alg="biruni:sskde"),
    _c("ss_centre", "Centre and Spread", "circle-dot", alg="biruni:sscentre"),
    _c("ss_ols", "What Explains\nThis Value?", "chart-dots-3", alg="biruni:ssols"),
    _c("ss_gwr", "Local Regression (GWR)", "chart-dots-3", alg="biruni:ssgwr"),
    _c("ss_emerging", "Hot Spots Over Time", "flame", alg="biruni:ssemerging"),
    # ---- graduation projects (thesis/) ----------------------------------
    _c("thesis_starter", "Study Area\nStarter Pack", "package", run=lambda p: _mod("thesis.starter_dialog").show(p)),
    _c("thesis_methodology", "Methodology\nChapter", "list-details",
       run=lambda p: setattr(p, "_methodology_dialog", _mod("thesis.methodology_dialog").show_methodology(p))),
    _c("thesis_figures", "Thesis\nFigures", "list-numbers", run=lambda p: _mod("thesis.figures_dialog").open_dialog(p)),
    _c("thesis_defense", "Defence\nPresentation", "presentation",
       run=lambda p: _dialog(p, "thesis.defense_dialog", "DefenseDialog")),
    _c("parcel_atlas", "Route Parcels\nAtlas", "road", alg="biruni:parcelatlas"),
    _c("eudr_check", "EUDR\nCheck", "leaf", alg="biruni:eudrcheck"),
    _c("my_country", "My\nCountry", "world-pin", menu=lambda p, m: _mod("regions").ribbon_menu(p, m)),
    _c("choose_country", "Choose My Country…", "world-pin", run=lambda p: _mod("regions").open_dialog(p)),
    _c("cadastre_lookup", "Find Parcel in\nCadastre", "map-search", alg="biruni:cadastrelookup"),
    _c("open_national", "Open National\nFile", "file-import", alg="biruni:opennationalformat"),
    _c("export_national", "Save in a National Format", "file-export", alg="biruni:exportnationalformat"),
    _c("convert_dates", "Convert Dates (Calendars)", "calendar-repeat", alg="biruni:convertdates"),
    _c("fix_text_encoding", "Fix Text\nEncoding", "file-text-shield", alg="biruni:fixtextencoding"),
    _c("las_ground", "Ground from\nPoint Cloud", "drone", alg="biruni:lasground"),
    _c("las_volume", "Volume\n(Cut / Fill)", "cone", alg="biruni:lasvolume"),
    _c("las_profile", "Elevation Profile", "chart-area-line", alg="biruni:lasprofile"),
]}


def _toolbox_category(p, key):
    dock = p.toolbox
    dock.show()
    dock.raise_()
    if hasattr(dock, "expand_category"):
        dock.expand_category(key)


def _spatial_filter(p, on):
    from . import spatial_filter

    if on:
        spatial_filter.apply(p.iface)
    else:
        spatial_filter.clear(p.iface)


def _mod(name):
    """A Biruni module imported on first use (keeps start-up fast)."""
    import importlib

    return importlib.import_module(f"{__package__}.{name}")


def _active_vector(p, geometry=None):
    """The active vector layer (optionally of one geometry type), or None after telling the user."""
    from qgis.core import QgsVectorLayer

    from .i18n import tr
    from .version import APP_NAME

    lyr = p.iface.activeLayer()
    if isinstance(lyr, QgsVectorLayer) and (geometry is None or lyr.geometryType() == geometry):
        return lyr
    p.iface.messageBar().pushWarning(APP_NAME, tr("Choose a vector layer in the Layers panel first."))
    return None


def _tabular_labels(p):
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                     QVBoxLayout)

    from . import cartography
    from .i18n import tr

    lyr = _active_vector(p)
    if lyr is None:
        return
    dlg = QDialog(p.mw)
    dlg.setWindowTitle(tr("Table Labels"))
    v = QVBoxLayout(dlg)
    v.addWidget(QLabel(tr("Fields to show in the label table:")))
    fields = QListWidget()
    for f in lyr.fields():
        it = QListWidgetItem(lyr.attributeDisplayName(lyr.fields().indexOf(f.name())))
        it.setData(Qt.ItemDataRole.UserRole, f.name())
        it.setCheckState(Qt.CheckState.Unchecked)
        fields.addItem(it)
    v.addWidget(fields)
    v.addWidget(QLabel(tr("Header (optional, an expression such as \"parcel_no\"):")))
    header = QLineEdit()
    v.addWidget(header)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    v.addWidget(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    chosen = [fields.item(i).data(Qt.ItemDataRole.UserRole) for i in range(fields.count())
              if fields.item(i).checkState() == Qt.CheckState.Checked]
    if chosen:
        cartography.tabular_label(lyr, chosen, header.text().strip() or None)
        lyr.triggerRepaint()


def _contour_style(p):
    from qgis.core import Qgis, QgsProject, QgsRasterLayer

    from . import cartography
    from .i18n import tr
    from .version import APP_NAME

    lyr = _active_vector(p, Qgis.GeometryType.Line)
    if lyr is None:
        return
    dems = [l for l in QgsProject.instance().mapLayers().values()
            if isinstance(l, QgsRasterLayer) and l in p.iface.layerTreeView().selectedLayers()]
    info = cartography.style_contours(lyr, dem=dems[0] if dems else None)
    lyr.triggerRepaint()
    p.iface.messageBar().pushSuccess(APP_NAME, tr("Contours styled: interval {i}, labelled index contours every {s}.")
                                     .format(i=f"{info['interval']:g}", s=f"{info['index_step']:g}"))


def _movable_labels(p):
    from . import cartography
    from .version import APP_NAME

    lyr = _active_vector(p)
    if lyr is None:
        return
    ok, msg = cartography.enable_manual_labels(lyr)
    bar = p.iface.messageBar()
    (bar.pushSuccess if ok else bar.pushWarning)(APP_NAME, msg)


def _table_tools(p, tab):
    from .table_tools_dialog import open_table_tools

    open_table_tools(p, tab)


def _selsets():
    from . import selection_sets

    return selection_sets


def _arabic_fix(p):
    from . import arabic_labels
    from .i18n import tr
    from .version import APP_NAME

    report = arabic_labels.fix_project()
    bar = p.iface.messageBar()
    if not report:
        bar.pushInfo(APP_NAME, tr("The Arabic labels of this project already draw correctly."))
        return
    lines = [f"{name}: {', '.join(changes)}" for name, changes in report.items()]
    bar.pushSuccess(APP_NAME, tr("Arabic labels fixed:") + "\n" + "\n".join(lines))


def _light_mode(p):
    from . import performance

    performance.toggle(p)


def _autosave_menu(m):
    from . import autosave

    autosave.fill_menu(m)


def _feature_counts():
    from . import layer_info

    layer_info.set_feature_counts(not layer_info.feature_counts_shown())


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


def dock_beside_map(mw, dock):
    """Put a pane back at the right of the map as a tab beside the Browser (the ArcGIS Pro arrangement:
    Catalog / Geoprocessing tabs on the right), or alone at the right when there is no Browser."""
    tab_beside(mw, dock)


def tab_beside(mw, dock, preferred=None):
    """Open a pane at the right of the map, as a tab beside a pane that is really there: the preferred one
    (usually the Toolbox), else the Browser, else alone. A pane that is floating or hidden is never used -
    tabifying with it made new panes float with it as a tiny window in the screen's corner."""
    dock.setFloating(False)
    if mw.dockWidgetArea(dock) == Qt.DockWidgetArea.NoDockWidgetArea:
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    for other in (preferred, mw.findChild(QDockWidget, "Browser")):
        if (other is not None and other is not dock and not other.isFloating() and other.isVisible()
                and mw.dockWidgetArea(other) != Qt.DockWidgetArea.NoDockWidgetArea):
            mw.tabifyDockWidget(other, dock)
            return
    if mw.dockWidgetArea(dock) != Qt.DockWidgetArea.RightDockWidgetArea:
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)


def standard_panels(plugin):
    """Panels as people know them from ArcGIS Pro: Layers on the left, Browser and Toolbox as tabs on the
    right, nothing floating."""
    mw = plugin.iface.mainWindow()
    layers = mw.findChild(QDockWidget, "Layers")
    if layers is not None:
        layers.setFloating(False)
        mw.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, layers)
        layers.show()
    browser = mw.findChild(QDockWidget, "Browser")
    if browser is not None:
        browser.setFloating(False)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, browser)
        browser.show()
    for d in mw.findChildren(QDockWidget):
        if d.isFloating() and d.isVisible():
            dock_beside_map(mw, d)
    toolbox = mw.findChild(QDockWidget, "BiruniToolbox")
    if toolbox is not None:
        dock_beside_map(mw, toolbox)
        toolbox.show()
    vault = vault_for_browser(plugin, force=True)
    if vault is not None:
        vault.raise_()
    elif browser is not None:
        browser.raise_()
    sizes = [d for d in (layers, vault or browser) if d is not None]
    if sizes:
        mw.resizeDocks(sizes, [320] * len(sizes), Qt.Orientation.Horizontal)


def vault_for_browser(plugin, force=False):
    """The Vault takes the place of the classic Browser (the ArcGIS Pro Catalog arrangement): the Browser is
    hidden and the Vault opens as a tab at the right. At start-up (force=False) only when the Browser is showing,
    so someone who closed both keeps a clean screen. The classic Browser stays in View > Panels."""
    mw = plugin.iface.mainWindow()
    browser = mw.findChild(QDockWidget, "Browser")
    if not force and (browser is None or not browser.isVisible()):
        return None
    try:
        vault = _mod("vault").open_dock(plugin)
    except Exception as e:  # noqa: BLE001 - the classic Browser stays as it is
        from qgis.core import QgsMessageLog

        QgsMessageLog.logMessage(f"vault: {e}", "Biruni")
        return None
    if browser is not None:
        if not browser.isFloating() and mw.dockWidgetArea(browser) == mw.dockWidgetArea(vault):
            mw.tabifyDockWidget(browser, vault)  # same spot as the Browser had
        browser.hide()
    vault.setFloating(False)
    vault.show()
    vault.raise_()
    return vault


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
        self._indexed = -1e9  # when the actions were last indexed (time.monotonic)
        self._dialogs = []

    def qaction(self, name):
        """The QGIS action with this object name. All actions are indexed in one pass (searching the
        whole main window once per name took seconds at start-up); a name not found makes a new pass
        only when the last one is more than a second old - plugins add actions while QGIS starts."""
        a = self._actions.get(name)
        if a is not None:
            return a
        now = time.monotonic()
        if now - self._indexed > 1.0:
            self._indexed = now
            for act in self.mw.findChildren(QAction):
                n = act.objectName()
                if n and n not in self._actions:
                    self._actions[n] = act
        return self._actions.get(name)

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
                if show and d.isFloating():  # panels live beside the map: a pane left floating comes back
                    dock_beside_map(self.mw, d)
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
        try:  # forget the dialog when it closes (some dialogs cannot be watched: keep them, no harm)
            dlg.finished.connect(lambda *_: self._dialogs.remove(dlg) if dlg in self._dialogs else None)
        except (AttributeError, TypeError, RuntimeError) as e:
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(f"{alg_id}: dialog not watched ({e})", "Biruni")
        dlg.show()
        dlg.raise_()
