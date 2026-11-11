"""The friendly name, icon and colour of every kind of data the Add Data window recognises."""
from ..i18n import tr

# kind -> (light, dark) colour
TINTS = {
    "points": ("#E5484D", "#FF8A8E"), "lines": ("#2563EB", "#93C5FD"), "areas": ("#0D9488", "#2DD4BF"),
    "shapes": ("#0D9488", "#2DD4BF"), "table": ("#0284C7", "#7DD3FC"), "spreadsheet": ("#15803D", "#4ADE80"),
    "image": ("#EA580C", "#FDBA74"), "elevation": ("#92400E", "#FCD34D"), "pointcloud": ("#7C3AED", "#C4B5FD"),
    "mesh": ("#0891B2", "#67E8F9"), "vectortile": ("#4F46E5", "#A5B4FC"), "scene": ("#4F46E5", "#A5B4FC"),
    "cad": ("#475569", "#CBD5E1"), "gps": ("#D97706", "#FBBF24"), "web": ("#0369A1", "#7DD3FC"),
    "service": ("#0369A1", "#7DD3FC"), "project": ("#0E7C74", "#2CC5B4"), "archive": ("#CA8A04", "#FDE047"),
    "unknown": ("#94A3B8", "#64748B"),
}
ICONS = {
    "points": "map-pins", "lines": "route-2", "areas": "polygon", "shapes": "vector-triangle", "table": "table",
    "spreadsheet": "file-spreadsheet", "image": "photo", "elevation": "mountain", "pointcloud": "chart-dots-3",
    "mesh": "grid-pattern", "vectortile": "stack-2", "scene": "building", "cad": "ruler-2", "gps": "route",
    "web": "world", "service": "world-download", "project": "map", "archive": "file-zip", "unknown": "file",
}


def tint(kind, dark=False):
    pair = TINTS.get(kind) or TINTS["unknown"]
    return pair[1] if dark else pair[0]


def icon_name(kind):
    return ICONS.get(kind, "file")


def kind_name(kind):
    """The friendly name of a kind (shown on the cards)."""
    if kind == "points":
        return tr("Points")
    if kind == "lines":
        return tr("Lines")
    if kind == "areas":
        return tr("Areas")
    if kind == "shapes":
        return tr("Shapes")
    if kind == "table":
        return tr("Table")
    if kind == "spreadsheet":
        return tr("Spreadsheet")
    if kind == "image":
        return tr("Image (raster)")
    if kind == "elevation":
        return tr("Elevation")
    if kind == "pointcloud":
        return tr("Point cloud")
    if kind == "mesh":
        return tr("Mesh")
    if kind == "vectortile":
        return tr("Vector tiles")
    if kind == "scene":
        return tr("3D scene")
    if kind == "cad":
        return tr("CAD drawing")
    if kind == "gps":
        return tr("GPS track")
    if kind == "web":
        return tr("Web map")
    if kind == "service":
        return tr("Web map service")
    if kind == "project":
        return tr("Project file")
    if kind == "archive":
        return tr("Archive")
    return tr("Cannot be read")
