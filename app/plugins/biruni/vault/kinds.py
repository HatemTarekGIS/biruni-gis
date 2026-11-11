"""What a file is, and the colour and icon of every kind of thing in the Vault (one colour per kind, the same in
every part of the pane; a lighter tone in the dark theme)."""
import os

from ..i18n import tr

# extension -> kind of the files of a folder
EXT_KIND = {
    # vectors
    ".shp": "vector", ".geojson": "vector", ".kml": "vector", ".kmz": "vector", ".gml": "vector",
    ".tab": "vector", ".mif": "vector", ".fgb": "vector", ".gpx": "vector", ".geojsonl": "vector",
    ".topojson": "vector", ".gdb": "vector", ".osm": "vector", ".pbf": "vector",
    # rasters
    ".tif": "raster", ".tiff": "raster", ".jp2": "raster", ".img": "raster", ".vrt": "raster", ".asc": "raster",
    ".dem": "raster", ".ecw": "raster", ".sid": "raster", ".nc": "raster", ".hgt": "raster", ".bil": "raster",
    # CAD
    ".dxf": "cad", ".dwg": "cad", ".dgn": "cad",
    # tables
    ".csv": "table", ".xlsx": "table", ".xls": "table", ".ods": "table", ".tsv": "table",
    # databases
    ".gpkg": "database", ".sqlite": "database", ".db": "database", ".mbtiles": "database",
    # documents
    ".pdf": "document", ".docx": "document", ".doc": "document", ".pptx": "document", ".txt": "document",
    ".html": "document", ".htm": "document", ".md": "document", ".rtf": "document", ".odt": "document",
    ".jpg": "document", ".jpeg": "document", ".png": "document", ".webp": "document", ".zip": "document",
    # the project's own things
    ".model3": "model", ".py": "script", ".qml": "style", ".sld": "style", ".stylx": "style",
    ".qgz": "project", ".qgs": "project",
}
# files that belong to another file (shapefile parts, world files, seals …) - never listed alone
SIDECAR_EXT = {".shx", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qmd", ".ovr", ".tfw", ".tifw", ".wld", ".pgw",
               ".jgw", ".aux", ".xml", ".fix", ".atx", ".ain", ".aih", ".lock", ".qgd", ".gpkg-wal", ".gpkg-shm",
               ".sqlite-journal", ".bak", ".tmp", ".pyc", ".log"}
SEAL_SUFFIXES = (".seal.json", ".seal.png", ".seal.html")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
WORLD_FILES = {".jpg": (".jgw", ".jpgw", ".wld"), ".jpeg": (".jgw", ".wld"), ".png": (".pgw", ".pngw", ".wld")}
DATA_KINDS = ("vector", "raster", "cad", "table")

# kind -> (light, dark) colour
TINTS = {
    "map": ("#0E7C74", "#2CC5B4"), "theme": ("#0F766E", "#5EEAD4"), "layer": ("#0E7C74", "#2CC5B4"),
    "layout": ("#4F46E5", "#A5B4FC"), "mapbook": ("#7C3AED", "#C4B5FD"), "stop": ("#E5484D", "#FF8A8E"),
    "charter": ("#15803D", "#4ADE80"), "photos": ("#D97706", "#FBBF24"), "indoor": ("#0891B2", "#67E8F9"),
    "figure": ("#2563EB", "#93C5FD"), "model": ("#9333EA", "#D8B4FE"), "script": ("#9333EA", "#D8B4FE"),
    "style": ("#DB2777", "#F9A8D4"), "sealed": ("#047857", "#6EE7B7"), "vector": ("#0D9488", "#2DD4BF"),
    "raster": ("#EA580C", "#FDBA74"), "cad": ("#475569", "#CBD5E1"), "table": ("#0284C7", "#7DD3FC"),
    "document": ("#64748B", "#CBD5E1"), "database": ("#B45309", "#FCD34D"), "connection": ("#B45309", "#FCD34D"),
    "folder": ("#CA8A04", "#FDE047"), "recent": ("#0369A1", "#7DD3FC"), "project": ("#0E7C74", "#2CC5B4"),
    "today": ("#0E7C74", "#2CC5B4"), "section_project": ("#0E7C74", "#2CC5B4"),
    "section_folder": ("#CA8A04", "#FDE047"), "section_db": ("#B45309", "#FCD34D"),
    "section_folders": ("#0369A1", "#7DD3FC"),
}
# kind -> Tabler icon name
KIND_ICON = {
    "map": "map", "theme": "stack-2", "layer": "stack", "layout": "layout-board", "mapbook": "book",
    "stop": "stops/map-route", "charter": "license", "photos": "camera", "indoor": "building", "figure": "school",
    "model": "category", "script": "code", "style": "palette", "sealed": "shield-check", "vector": "vector",
    "raster": "photo", "cad": "ruler-2", "table": "table", "document": "file-text", "database": "database",
    "connection": "database", "folder": "folder", "recent": "clock-hour-4", "project": "folder-open",
    "section_project": "map-2", "section_folder": "folder-open", "section_db": "database",
    "section_folders": "folders",
}


def tint(kind, dark=False):
    pair = TINTS.get(kind) or TINTS["document"]
    return pair[1] if dark else pair[0]


def icon_name(kind):
    return KIND_ICON.get(kind, "file")


def kind_title(kind):
    """The plural title of a group of things of one kind."""
    if kind == "vector":
        return tr("Vectors")
    if kind == "raster":
        return tr("Rasters")
    if kind == "cad":
        return tr("CAD drawings")
    if kind == "table":
        return tr("Tables")
    if kind == "document":
        return tr("Documents")
    if kind == "database":
        return tr("Databases")
    if kind == "map":
        return tr("Maps")
    if kind == "layout":
        return tr("Layouts")
    if kind == "mapbook":
        return tr("Map Books")
    if kind == "stop":
        return tr("Map Stops")
    if kind == "charter":
        return tr("Data Charters")
    if kind == "photos":
        return tr("Field photos")
    if kind == "indoor":
        return tr("Indoor maps")
    if kind == "figure":
        return tr("Thesis figures")
    if kind in ("model", "script"):
        return tr("Models & scripts")
    if kind == "style":
        return tr("Styles")
    if kind == "sealed":
        return tr("Sealed files")
    if kind == "folder":
        return tr("Folders")
    if kind == "recent":
        return tr("Recent data")
    if kind == "project":
        return tr("Projects")
    return tr("Other")


def kind_name(kind):
    """The singular name of a kind (shown on cards and searched)."""
    if kind == "vector":
        return tr("Vector")
    if kind == "raster":
        return tr("Raster")
    if kind == "cad":
        return tr("CAD drawing")
    if kind == "table":
        return tr("Table")
    if kind == "document":
        return tr("Document")
    if kind == "database":
        return tr("Database")
    if kind == "layout":
        return tr("Layout")
    if kind == "stop":
        return tr("Map Stop")
    if kind == "style":
        return tr("Style")
    if kind == "model":
        return tr("Model")
    if kind == "script":
        return tr("Script")
    return ""


def is_seal_file(name):
    low = name.lower()
    return low.endswith(SEAL_SUFFIXES)


def file_kind(path, names_here=None):
    """The kind of a file, or None for files that are part of another one (or not interesting).
    names_here: the lower-case names of the files of its folder (to tell a shapefile's .dbf from a lone table and a
    georeferenced picture from a photo)."""
    name = os.path.basename(path)
    low = name.lower()
    if low.startswith(("~$", ".")) or is_seal_file(low):
        return None
    if low.endswith(".aux.xml") or low.endswith(".shp.xml"):
        return None
    stem, ext = os.path.splitext(low)
    if ext == ".dbf":
        if names_here is not None and stem + ".shp" in names_here:
            return None
        return "table"
    if ext in SIDECAR_EXT:
        return None
    if ext in WORLD_FILES and names_here is not None:
        if any(stem + w in names_here for w in WORLD_FILES[ext]) or low + ".aux.xml" in names_here:
            return "raster"
    if ext == ".json" and names_here is not None:
        return None
    return EXT_KIND.get(ext)
