"""Workspaces: a smaller ribbon for each kind of user.

The full ribbon has more than 300 buttons; a surveyor, an engineering office, a student and a GIS analyst
each use a different part of it. A workspace hides the tabs, groups and buttons its user does not need, so
every page fits on the screen. Nothing is taken away: every command is still found by the ribbon's search
(learn/locator.py search_commands), and "Everything" brings the whole ribbon back.

A workspace names what it hides, by the keys and English titles used in plugin.build_ribbon:

* ``tabs``     a tab key                                  "thesis"
* ``groups``   "tab/Group title"                          "data/From ArcGIS"
* ``buttons``  a command id (hidden on every tab)         "add_postgis"
               or "tab/Group title/command id" (one place) "analysis/Engineering Office/whatsapp_points"

A new button added to the ribbon shows in every workspace until a workspace hides it. The CORE commands are
never hidden, whatever a workspace says.
"""
from collections import namedtuple

KEY = "biruni/workbench"
DEFAULT = "all"

# never hidden: the project, moving around the map, adding data, selecting, identifying, the edit session,
# and help (the search box sits in the ribbon's header and is always there)
CORE = frozenset({
    "new_project", "open", "save", "project_folder",
    "pan", "zoom_in", "zoom_out", "full_extent", "zoom_layer", "zoom_selection", "zoom_prev", "zoom_next",
    "refresh",
    "add_data",
    "select", "select_expr", "select_location", "select_all", "invert_selection", "clear_selection",
    "identify", "measure", "attribute_table",
    "toggle_edit", "save_edits", "review_save", "undo", "redo",
    "learn", "report_problem", "check_updates", "about", "options",
})

Workbench = namedtuple("Workbench", "title icon tip tabs groups buttons")

# hidden in every workspace but "Everything": niche helpers that are a search away, and the View tab's
# settings that the ribbon's header (Theme, Language) or Options already give
_LEAN_GROUPS = ("view/Appearance", "view/Performance")
_LEAN = (
    "map_stops", "new_stop", "manage_stops", "rename_project", "select_previous", "select_next", "map_tips",
    "view/Panels/coords", "view/Panels/learn", "panel_overview", "panel_browser",
    "classic", "shortcuts", "view/Interface/plugins", "view/Help/from_pro",
)

WORKBENCHES = {
    "all": Workbench(
        "Everything", "layout-grid", "Every tool of Biruni (the full ribbon)",
        tabs=(), groups=(), buttons=()),

    "surveyor": Workbench(
        "Surveyor", "ruler-measure", "CAD and DXF, traverse and COGO, parcels, coordinates, field work and GPS, "
        "deeds, editing and delivery to the authorities",
        tabs=("thesis",),
        groups=_LEAN_GROUPS + (
            "home/Area Filter",
            "data/From ArcGIS", "data/Project Data",
            "edit/Attributes", "edit/Data Charter", "edit/Protect",
            "analysis/Spatial Statistics", "analysis/Networks", "analysis/Raster", "analysis/Automation",
            "share/Map Design", "share/Review", "share/Reports",
        ),
        buttons=_LEAN + (
            "history_versions", "activity_report", "project_doctor", "who_locks", "saved_selections",
            "home/Learn/from_pro", "home/Inquiry/statistics",
            # data: the drawings, files and coordinates of the survey, not web services
            "add_postgis", "add_wms", "add_wfs", "add_xyz", "open_arcgis", "open_data", "osm_download",
            "overture", "dem_download", "quick_align", "indoor_map", "indoor_route", "las_profile",
            # edit
            "team_room",
            # analysis: the office's parcel and delivery tools only
            "qgis_toolbox", "spatial_join", "centroids", "multi_join", "eudr_check", "sales_map", "setbacks",
            "arabic_join", "quantities",
            "analysis/Engineering Office/whatsapp_points", "analysis/Engineering Office/parcel_atlas",
            "analysis/Engineering Office/compare_layers", "analysis/Engineering Office/cadastre_lookup",
            # quality
            "charter_health", "clean_table", "convert_dates", "hijri_dates", "spell_values", "remove_null",
            "validation_panel", "quality/Check/compare_layers", "quality/Check/delivery_spec",
            "quality/Topology/topological", "quality/Topology/snapping",
            # share: the sheet, the drawing and the files for the authority
            "map_studio", "map_book", "new_report", "four_colors", "label_cluster", "map_index",
            "tabular_labels", "movable_labels", "parcel_atlas",
            "web_map", "publish_site", "live_share", "portfolio", "earth_tour", "export_photos",
            "share/Field work/phone_gps",
        )),

    "office": Workbench(
        "Engineering office", "briefcase", "Projects, editing, maps and layouts, sharing, quantities and "
        "delivery checks",
        tabs=("thesis",),
        groups=_LEAN_GROUPS + (
            "home/Area Filter",
            "data/From ArcGIS", "data/Point Clouds",
            "edit/Data Charter", "edit/Protect",
            "analysis/Spatial Statistics", "analysis/Networks", "analysis/Raster", "analysis/Automation",
            "quality/Topology",
            "edit/Teamwork",       # the office works in one place: rooms, pigeons and shared computing are the GIS side
        ),
        buttons=_LEAN + (
            "saved_selections", "home/Learn/from_pro", "home/Inquiry/statistics", "home/Inquiry/coords",
            # data
            "add_postgis", "add_wms", "add_wfs", "add_xyz", "open_arcgis", "open_data", "overture",
            "dem_download", "indoor_map", "indoor_route", "import_track", "gnss_quality", "store_temporary",
            # edit: the office draws and divides parcels; the field side stays with the surveyor
            "tracing", "add_ring", "add_part",
            "attribute_rules", "field_order", "highlight_table", "natural_sort",
            "traverse_table", "field_to_finish", "ground_grid", "localization", "deed_survey",
            "edit/Survey/parcel_bundle",
            # analysis
            "qgis_toolbox", "centroids", "multi_join", "eudr_check",
            "analysis/Engineering Office/whatsapp_points", "analysis/Engineering Office/cadastre_lookup",
            "analysis/Engineering Office/parcel_description",
            # quality
            "charter_health", "convert_dates", "hijri_dates", "spell_values", "remove_duplicates",
            "remove_null",
            # share
            "four_colors", "label_cluster", "map_index", "tabular_labels", "contour_style", "movable_labels",
            "arabic_labels_fix", "share/Print Layout/parcel_atlas",
            "color_preview", "terrain_style", "map_caption", "place_icons", "symbols_by_scale",
            "install_symbols", "bilingual_labels", "label_priorities", "hidden_labels", "live_qr",
            "export_photos", "landxml_out", "publish_site", "portfolio", "earth_tour",
            "copies_package", "copies_apply",      # syncing copies: the GIS side; the certificate and proofs stay
            "copies_totals_proof", "copies_totals_check",     # agency-scale proofs and exchanges
            "basemap_pack", "site_preview", "geoai_model", "dashboard_seal_check", "portal_viewer", "map_service_check", "scrub_file",
            "share/Between Agencies/conflate_match", "share/Between Agencies/copies_private_match",
            "share/Review/sales_map", "sales_back", "share/Field work/phone_gps",
        )),

    "student": Workbench(
        "Student / researcher", "school", "Analysis and statistics, the graduation project, open data and maps",
        tabs=(),
        groups=_LEAN_GROUPS + (
            "home/My Country", "home/Area Filter",
            "data/From ArcGIS", "data/Point Clouds",
            "edit/Precision", "edit/Data Charter", "edit/Protect", "edit/Survey",
            "analysis/Engineering Office",
            "quality/Topology", "quality/Peer Review",
            "share/Review", "share/Field work", "share/Reports", "edit/Teamwork", "edit/Enterprise",
        ),
        buttons=_LEAN + (
            "history_versions", "activity_report", "project_doctor", "who_locks", "saved_selections",
            # data: open data and tables, not CAD or company servers
            "add_postgis", "add_wfs", "open_arcgis", "cadastre_lookup",
            "cad_to_gis", "dwg_import", "cad_georef", "quick_align", "lines_to_plots", "scan_parcels",
            "indoor_map", "indoor_route", "open_national", "cad_more",
            "gnss_quality", "whatsapp_points", "store_temporary",
            # edit: simple drawing
            "team_room", "rotate", "cut", "copy", "paste", "offset", "trim_extend", "simplify", "add_ring",
            "add_part", "topological", "tracing", "snapping_options",
            "attribute_rules", "field_order", "highlight_table", "natural_sort",
            # analysis
            "multi_join", "session_script",
            # quality
            "charter_health", "delivery_check", "delivery_spec", "compare_layers", "convert_dates",
            "hijri_dates",
            # share
            "map_book", "new_report", "four_colors", "arabic_labels_fix", "label_cluster", "map_index",
            "tabular_labels", "contour_style", "movable_labels", "parcel_atlas",
            "place_icons", "symbols_by_scale", "install_symbols", "label_priorities", "hidden_labels", "live_qr",
            "export_dxf", "export_photos", "landxml_out", "export_national",
            "publish_site", "live_share",
        )),

    "analyst": Workbench(
        "GIS analyst", "chart-dots-3", "Data, analysis and processing, and quality",
        tabs=("thesis",),
        groups=_LEAN_GROUPS + (
            "home/My Country", "home/Area Filter",
            "data/Point Clouds", "data/GPS",
            "edit/Precision", "edit/Protect", "edit/Survey",
            "analysis/Engineering Office",
            "share/Review", "share/Field work", "share/Reports",
        ),
        buttons=_LEAN + (
            "history_versions", "activity_report", "project_doctor", "who_locks",
            # data: services and files, no survey or CAD drawing tools
            "cadastre_lookup", "cad_georef", "georef_pdf", "quick_align", "lines_to_plots", "scan_parcels",
            "dwg_open", "dwg_save", "geoai_model", "basemap_pack", "site_preview", "dashboard_seal_check", "scrub_file",
            "team_pigeon", "light_send", "light_receive",      # field and air-gapped exchange: the surveyor's side
            "indoor_map", "indoor_route", "open_national", "cad_more",
            "export_mapx", "export_lyrx", "import_pagx", "arcgis_sync", "arcgis_signin",
            # edit
            "team_room", "highlight_table", "natural_sort",
            "charter_learn", "charter_fields", "charter_lists", "charter_kinds", "charter_rules",
            "charter_links", "charter_dictionary",
            # quality
            "convert_dates", "hijri_dates", "delivery_check", "delivery_spec",
            # share
            "map_sheet", "four_colors", "label_cluster", "map_index", "tabular_labels", "contour_style",
            "movable_labels", "parcel_atlas", "arabic_labels_fix",
            "place_icons", "symbols_by_scale", "install_symbols", "label_priorities", "hidden_labels",
            "map_caption", "live_qr",
            "export_dxf", "export_photos", "landxml_out", "export_national",
            "live_share", "portfolio", "earth_tour",
        )),
}


class Hidden:
    """What one workspace hides, asked for each place of the ribbon while it is built."""

    def __init__(self, name):
        wb = WORKBENCHES.get(name) or WORKBENCHES[DEFAULT]
        self.tabs = set(wb.tabs)
        self.groups = set(wb.groups)
        self.buttons = set(wb.buttons)

    def place(self, tab, group):
        """True when the whole group (or its tab) is hidden."""
        return tab in self.tabs or f"{tab}/{group}" in self.groups

    def button(self, tab, group, cid):
        """True when the button ``cid`` of the group ``tab/group`` is hidden. CORE commands never are."""
        if cid in CORE:
            return False
        return self.place(tab, group) or cid in self.buttons or f"{tab}/{group}/{cid}" in self.buttons

    def nothing(self):
        return not (self.tabs or self.groups or self.buttons)


def current():
    """The workspace chosen by the user ("all" when none was chosen or it no longer exists)."""
    from qgis.core import QgsSettings

    name = QgsSettings().value(KEY, DEFAULT, type=str)
    return name if name in WORKBENCHES else DEFAULT


def set_current(name):
    from qgis.core import QgsSettings

    QgsSettings().setValue(KEY, name if name in WORKBENCHES else DEFAULT)


def command_ids(name):
    """Every command id a workspace names (in ``buttons``), for checking them against the catalog."""
    return {b.rsplit("/", 1)[-1] for b in WORKBENCHES[name].buttons}
