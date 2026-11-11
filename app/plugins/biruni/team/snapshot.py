"""The map a new member receives: the host's layer tree with its styles (a layer definition,
.qlr), the shared GeoPackages copied at one point of the stream, and the map's other small
local files. On the member's computer the layers are pointed at the copies in the room folder."""
import glob
import os
import re

from qgis.core import QgsLayerDefinition, QgsMapLayer, QgsProject, QgsReadWriteContext
from qgis.PyQt.QtXml import QDomDocument

BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
REMOTE_PROVIDERS = ("wms", "wfs", "arcgismapserver", "arcgisfeatureserver", "vectortile", "wcs")


def safe_relative(name):
    """``name`` when it is a plain relative path inside a folder ("L1.gpkg", "extra/x/roads.shp"), else None -
    the host decides these names: "../", "C:\\", "\\\\server\\share" or a drive letter would write or read outside
    the room folder (into the Startup folder, say)."""
    if not isinstance(name, str) or not name or BAD_CHARS.search(name) or name.startswith("/"):
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") or p != p.strip() for p in parts):
        return None
    return name


def inside(folder, name):
    """The full path of ``name`` under ``folder``, or None when it would leave the folder."""
    name = safe_relative(name)
    if name is None:
        return None
    root = os.path.realpath(folder)
    real = os.path.realpath(os.path.join(root, *name.split("/")))
    if os.path.commonpath([root, real]) != root:
        return None
    # checked on the real path (links resolved), returned as the folder was written: callers compare it with
    # paths built the same way (a short "HATEMT~1" folder and its long real name would not match)
    return os.path.join(folder, *name.split("/"))


def _remote_ok(provider, source):
    """A web layer (a basemap, a web service) on http(s) - kept. Anything that reads files or network shares
    (/vsicurl, \\\\server, file:, a virtual layer's query) is not: a host could point it at the member's own
    files or make Windows send the member's login to another machine."""
    low = source.lower()
    if provider not in REMOTE_PROVIDERS:
        return False
    if "\\\\" in source or "file:" in low or "/vsi" in low:
        return False
    urls = re.findall(r"(?:url=['\"]?|^)(\w+)://", source)   # an ArcGIS service writes url='https://…'
    return bool(urls) and all(u.lower() in ("http", "https") for u in urls)


def sanitize(layer):
    """Take away what could run on this computer when a layer from another one is opened: Python form code,
    layer actions (they can run commands) and HTML map tips."""
    from qgis.core import Qgis, QgsVectorLayer

    if not isinstance(layer, QgsVectorLayer):
        return
    cfg = layer.editFormConfig()
    cfg.setInitCodeSource(Qgis.AttributeFormPythonInitCodeSource.NoSource)
    cfg.setInitCode("")
    cfg.setInitFunction("")
    cfg.setInitFilePath("")
    layer.setEditFormConfig(cfg)
    layer.actions().clearActions()
    layer.setMapTipTemplate("")


SIDECARS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qmd", ".qml",
            ".tfw", ".tifw", ".jgw", ".pgw", ".aux.xml", ".ovr", ".prj")


def write_qlr(path):
    """The whole layer tree of the current map as a layer definition file."""
    ok, err = QgsLayerDefinition.exportLayerDefinition(path, QgsProject.instance().layerTreeRoot().children())
    if not ok:
        raise OSError(err or "cannot write the map's layers")


def _file_of(layer):
    src = layer.source().split("|")[0].split("?")[0]
    if layer.providerType() in ("ogr", "gdal") and os.path.isfile(src):
        return src
    return None


def local_files(skip):
    """{layer id: [files]} for the map's layers that are files on this computer (not the shared ones)."""
    out = {}
    for lid, lyr in QgsProject.instance().mapLayers().items():
        if lid in skip or lyr.type() not in (QgsMapLayer.VectorLayer, QgsMapLayer.RasterLayer):
            continue
        src = _file_of(lyr)
        if not src:
            continue
        stem = os.path.splitext(src)[0]
        files = [src] + [p for p in glob.glob(glob.escape(stem) + ".*")
                         if p != src and any(p.lower().endswith(s) for s in SIDECARS)]
        out[lid] = files
    return out


def load_room_map(folder, layer_files, layer_tables, extra):
    """Put the room's layers in the current (empty) map, pointed at the copies in folder.
    layer_files: {layer id: GeoPackage file in folder}; layer_tables: {layer id: table};
    extra: {layer id: path in folder}. Returns the names of layers whose files only the host has."""
    doc = QDomDocument()
    with open(os.path.join(folder, "map.qlr"), "rb") as f:
        ok = doc.setContent(f.read())
    if not (ok[0] if isinstance(ok, tuple) else ok):
        raise OSError("the room's map cannot be read")
    layers = doc.elementsByTagName("maplayer")
    gone, drop = [], []
    for i in range(layers.count()):
        el = layers.at(i).toElement()
        lid = el.firstChildElement("id").text()
        ds = el.firstChildElement("datasource")
        node = ds.firstChild()
        provider = el.firstChildElement("provider").text().strip().lower()
        if lid in layer_files and inside(folder, layer_files[lid]):
            new = inside(folder, layer_files[lid]).replace("\\", "/") + "|layername=" + layer_tables[lid]
        elif lid in extra and inside(folder, extra[lid]):
            old = node.nodeValue()
            rest = old.split("|", 1)[1] if "|" in old else ""
            new = inside(folder, extra[lid]).replace("\\", "/") + ("|" + rest if rest else "")
        elif provider == "memory" or _remote_ok(provider, node.nodeValue() if not node.isNull() else ""):
            continue                       # a temporary layer, or a web layer: kept as it is
        else:
            # anything else points at files or shares the host decided: it is not opened here
            gone.append(el.firstChildElement("layername").text())
            drop.append(el)
            continue
        node.setNodeValue(new)
    for el in drop:
        el.parentNode().removeChild(el)
    root = QgsProject.instance().layerTreeRoot()
    before = set(QgsProject.instance().mapLayers())
    ok, err = QgsLayerDefinition.loadLayerDefinition(doc, QgsProject.instance(), root, QgsReadWriteContext())
    if not ok:
        raise OSError(err or "the room's layers cannot be opened")
    for lid, lyr in QgsProject.instance().mapLayers().items():
        if lid not in before:
            sanitize(lyr)
    return gone
