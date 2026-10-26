"""Read ArcMap documents (.mxd / .mxt) - best effort.

An .mxd is an OLE compound file whose "Maps" stream (ArcGIS 10.x) or "Mx
Document" stream (packaged/older documents) holds the ArcObjects of each data
frame, persisted one after another: a class id (GUID), an object id, a version
and the object's fields. The format is not published, so only what can be
recognised reliably is read:

    data frames  - name and coordinate system
    layers       - name, visibility, group nesting, data source (workspace,
                   dataset), definition query
    kinds        - feature layers (geodatabase, shapefile, CAD, services),
                   raster layers and group layers

Symbology, labels and the page layout are not read. Everything here is pure
Python (no QGIS) so it can be tested on its own; reader.py turns the result
into CIM-like dictionaries and builds the layers with the shared code."""
import os
import re
import struct
import uuid
from dataclasses import dataclass, field

MXD_EXTENSIONS = (".mxd", ".mxt")


class MxdError(Exception):
    """The file is not an ArcMap document that can be read."""


# ---- OLE compound file ------------------------------------------------------
_CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_END = 0xFFFFFFFA  # every sector number at or above this ends a chain


def _streams(data):
    """{stream name: bytes} of an OLE compound file (top-level streams)."""
    if data[:8] != _CFB_MAGIC or len(data) < 512:
        raise MxdError("not an OLE compound file")
    ss = 1 << struct.unpack_from("<H", data, 30)[0]
    mss = 1 << struct.unpack_from("<H", data, 32)[0]
    n_fat, dir_start, _tx, cutoff, minifat_start, n_minifat, difat_start, n_difat = \
        struct.unpack_from("<8I", data, 44)
    if ss not in (512, 4096):
        raise MxdError("unsupported sector size")
    n_sectors = len(data) // ss - 1

    def sector(i):
        if i >= n_sectors:
            raise MxdError("sector out of range")
        return data[(i + 1) * ss:(i + 2) * ss]

    difat = [s for s in struct.unpack_from("<109I", data, 76)]
    s, guard = difat_start, 0
    while s < _END and guard < n_difat:
        ents = struct.unpack(f"<{ss // 4}I", sector(s))
        difat.extend(ents[:-1])
        s, guard = ents[-1], guard + 1
    fat = []
    for f in difat[:n_fat]:
        if f < _END:
            fat.extend(struct.unpack(f"<{ss // 4}I", sector(f)))

    def chain(start, size=None):
        out, i, seen = [], start, set()
        while i < _END and i not in seen and i < len(fat):
            seen.add(i)
            out.append(sector(i))
            i = fat[i]
            if size is not None and len(out) * ss >= size:
                break
        return b"".join(out)

    directory = chain(dir_start)
    entries = []
    for k in range(len(directory) // 128):
        e = directory[k * 128:(k + 1) * 128]
        nl = struct.unpack_from("<H", e, 64)[0]
        name = e[:max(min(nl, 64) - 2, 0)].decode("utf-16-le", "replace")
        left, right, child = struct.unpack_from("<3I", e, 68)
        start, size = struct.unpack_from("<IQ", e, 116)
        entries.append((name, e[66], left, right, child, start, size))
    if not entries:
        raise MxdError("empty compound file")
    root = entries[0]
    mini = chain(root[5]) if root[6] else b""
    minifat = list(struct.unpack(f"<{n_minifat * ss // 4}I", chain(minifat_start)[:n_minifat * ss])) \
        if n_minifat else []

    def read(e):
        size = e[6]
        if size < cutoff:
            out, i, seen = [], e[5], set()
            while i < _END and i < len(minifat) and i not in seen and len(out) * mss < size:
                seen.add(i)
                out.append(mini[i * mss:(i + 1) * mss])
                i = minifat[i]
            return b"".join(out)[:size]
        return chain(e[5], size)[:size]

    found = {}

    def walk(i, depth=0):
        if i >= _END or i >= len(entries) or depth > 64:
            return
        name, typ, left, right, _child, _s, _z = entries[i]
        walk(left, depth + 1)
        if typ == 2:
            found[name] = read(entries[i])
        walk(right, depth + 1)

    walk(root[4])
    return found


# ---- ArcObjects -------------------------------------------------------------
def _g(s):
    return uuid.UUID(s).bytes_le


_MAP = _g("E6BDAA76-4D35-11D0-98BE-00805F7CED21")
_FEATURE_LAYER = _g("E663A651-8AAD-11D0-BEC7-00805F7C4268")
_GROUP_LAYER = _g("EDAD6647-1810-11D1-86AE-0000F8751720")
_RASTER_LAYER = _g("D02371C9-35F7-11D2-B1F2-00C04F8EDEFF")
_CAD_ANNOTATION_LAYER = _g("0C22A4C9-DAFD-11D2-9F46-00C04F6BC78E")
_ANNOTATION_LAYER = _g("CE8F3972-E9BE-11D1-A232-080009B6F22B")
_BASEMAP_LAYER = _g("DA4122BF-7B07-4158-88B0-19D342BED8BA")
_SERVICE_LAYER = _g("1DED52F5-8837-40DA-ADC3-596C1C4A29CE")
_WORKSPACE_NAME = _g("5A350011-E371-11D1-AA82-00C04FA33A15")
_FEATURE_DATASET_NAME = _g("198846CF-CA42-11D1-AA7C-00C04FA33A15")
_XY_EVENT_SOURCE_NAME = _g("309AA920-EAEC-11D3-9F8A-00C04F6BDF06")
_TABLE_NAME = _g("06783DB1-E5EE-11D1-B0A2-0000F8780820")

# class id -> (kind, bytes between the class id and the name)
_LAYERS = {
    _MAP: ("map", 6),
    _FEATURE_LAYER: ("feature", 6),
    _GROUP_LAYER: ("group", 6),
    _RASTER_LAYER: ("raster", 8),
    _CAD_ANNOTATION_LAYER: ("cad_annotation", 8),
    # recognised so that group nesting stays right; not opened
    _ANNOTATION_LAYER: ("annotation", 12),
    _BASEMAP_LAYER: ("basemap", 6),
    _SERVICE_LAYER: ("service", 6),
}
_OPAQUE = ("annotation", "basemap", "service")

# workspace factory class id -> CIM workspaceFactory
_FACTORIES = {
    _g("71FE75F0-EA0C-4406-873E-B7D53748AE7E"): "FileGDB",
    _g("A06ADB96-D95C-11D1-AA81-00C04FA33A15"): "Shapefile",
    _g("DD48C96A-D92A-11D1-AA81-00C04FA33A15"): "Access",
    _g("D9B4FA40-D6D9-11D1-AA81-00C04FA33A15"): "SDE",
    _g("4C91D963-3390-11D2-8D25-0000F8780535"): "Raster",
    _g("30F6F271-852B-4EE8-BD2D-099F51D6B238"): "Excel",
}

# dataset type description (the ArcMap "Data Type") -> workspace factory, when no class id is found
_TYPE_HINTS = (
    ("File Geodatabase", "FileGDB"), ("Shapefile", "Shapefile"), ("Personal Geodatabase", "Access"),
    ("SDE", "SDE"), ("CAD", "Cad"), ("Raster", "Raster"), ("Text File", "TextFile"),
)

_RASTER_DESC = re.compile(r"Workspace\s*=\s*(?P<ws>[^;]*);\s*RasterDataset\s*=\s*(?P<ds>[^;]*);?", re.IGNORECASE)
# a definition query starts with a field compared to something - not a label expression ([Field]),
# a class label ("> 5 - 8") or a description
_QUERY = re.compile(r'^\s*(NOT\s+)?\(*\s*("[^"]{1,64}"|\[[^\]]{1,64}\]|[^\W\d][\w.]{0,63})\s*'
                    r"(<>|<=|>=|!=|=|<|>|\bLIKE\b|\bIN\s*\(|\bIS\s+(NOT\s+)?NULL\b|\bBETWEEN\b|\bNOT\s+(LIKE|IN)\b)",
                    re.IGNORECASE)
_CONNECTION = re.compile(r"^\s*[\w ]+=[^;]*;\s*[\w ]+=")   # Provider=...;Data Source=...


def _bstr(b, i):
    """Length-prefixed UTF-16 string at i -> (text, end) or (None, i)."""
    if i < 0 or i + 4 > len(b):
        return None, i
    n = struct.unpack_from("<I", b, i)[0]
    if n < 2 or n > 8192 or n % 2 or i + 4 + n > len(b) or b[i + 2 + n:i + 4 + n] != b"\0\0":
        return None, i
    try:
        s = b[i + 4:i + 2 + n].decode("utf-16-le")
    except UnicodeDecodeError:
        return None, i
    if "\0" in s or not all(c.isprintable() or c in "\r\n\t" for c in s):
        return None, i
    return s, i + 4 + n


def _strings(b, start, end):
    """Every length-prefixed string in b[start:end], in order: [(offset, text)]."""
    out, i = [], start
    while i < end - 5:
        s, j = _bstr(b, i)
        if s is not None and j <= end:
            out.append((i, s))
            i = j
        else:
            i += 1
    return out


def _find_all(b, needle, start=0, end=None):
    end = len(b) if end is None else end
    out, i = [], b.find(needle, start, end)
    while i >= 0:
        out.append(i)
        i = b.find(needle, i + 1, end)
    return out


@dataclass
class MxdLayer:
    name: str
    kind: str                      # feature | group | raster | cad_annotation
    visible: bool = True
    factory: str = ""              # FileGDB, Shapefile, Access, SDE, Raster, Cad, FeatureService...
    workspace: str = ""            # path (as stored: absolute or relative to the document) or URL
    dataset: str = ""
    feature_dataset: str = ""      # CAD drawing file name for CAD layers
    dataset_type: str = ""         # ArcMap's description, e.g. "File Geodatabase Feature Class"
    definition_query: str = ""
    children: list = field(default_factory=list)


@dataclass
class MxdMap:
    name: str
    wkt: str = ""
    layers: list = field(default_factory=list)


def _visible(b, i):
    return b[i:i + 2] != b"\0\0"


def _source(layer, b, start, end):
    """Fill the data source of a feature/raster layer from its bytes."""
    strs = [(o, s) for o, s in _strings(b, start, end) if s.strip()]
    texts = [s for _o, s in strs]
    if layer.kind == "raster":
        for s in texts:
            m = _RASTER_DESC.search(s)
            if m:
                layer.factory = "Raster"
                layer.workspace, layer.dataset = m.group("ws").strip(), m.group("ds").strip()
                break
    else:
        # name block: [type], dataset, [type] ... - the dataset is the string before the repeated type
        if texts:
            layer.dataset_type = texts[0]
            for k in range(1, len(texts) - 1):
                if texts[k + 1] == texts[0]:
                    layer.dataset = texts[k]
                    break
    ws = [p for p in _find_all(b, _WORKSPACE_NAME, start, end)]
    for p in ws:
        path, _j = _bstr(b, p + 22)
        if path:
            if not layer.workspace:
                layer.workspace = path
            # the workspace factory follows the workspace name
            nxt = min([q for q in ws if q > p] + [end])
            for cls, fac in _FACTORIES.items():
                if b.find(cls, p, nxt) >= 0:
                    layer.factory = layer.factory or fac
                    break
            break
    for p in _find_all(b, _FEATURE_DATASET_NAME, start, end):
        name, _j = _bstr(b, p + 22)
        if name:
            layer.feature_dataset = name
            break
    if b.find(_XY_EVENT_SOURCE_NAME, start, end) >= 0:   # points made from a table's X/Y columns
        layer.factory = "XYEvent"
        p = b.find(_TABLE_NAME, start, end)
        layer.dataset = (_bstr(b, p + 22)[0] or "") if p >= 0 else ""
    if not layer.factory:
        if layer.workspace.lower().startswith(("http://", "https://")):
            layer.factory = "FeatureService"
        else:
            for hint, fac in _TYPE_HINTS:
                if hint.lower() in layer.dataset_type.lower():
                    layer.factory = fac
                    break
    if layer.factory == "FeatureService" and not layer.dataset:
        m = re.search(r"/(\d+)/?$", layer.workspace)
        if m:
            layer.workspace, layer.dataset = layer.workspace[:m.start()], m.group(1)
    if layer.factory == "Cad" and not layer.feature_dataset:
        for s in texts:
            if s.lower().endswith((".dwg", ".dxf", ".dgn")):
                layer.feature_dataset = s.replace("\\", "/").split("/")[-1]
                break
    for _o, s in strs if layer.kind != "raster" else ():
        if (_QUERY.match(s) and "<dyn " not in s and not _CONNECTION.match(s) and len(s) < 4000
                and s != layer.name):
            layer.definition_query = s
            break


def package_candidates(workspace, factory, doc_dir):
    """Where an unpacked map package (.mpk) keeps a workspace the document still
    names by its original path: geodatabases next to the document (personal ones
    converted to file geodatabases, names in lower case with underscores, maybe
    numbered) and folders under ../commondata. -> [(path, factory)] that exist."""
    name = re.split(r"[\\/]", workspace.rstrip("\\/"))[-1] if workspace else ""
    if not name or workspace.lower().startswith(("http://", "https://")):
        return []
    stem, ext = os.path.splitext(name)
    norm = re.sub(r"\s+", "_", stem).lower()
    parent = os.path.dirname(doc_dir)
    roots = (doc_dir, os.path.join(parent, "commondata"), os.path.join(doc_dir, "commondata"))
    if ext.lower() in (".gdb", ".mdb"):
        names, factory = (stem + ".gdb", norm + ".gdb"), "FileGDB"
        pattern = re.compile(re.escape(norm) + r"\d*\.gdb$", re.IGNORECASE)
    else:
        names = (name, norm)
        # raster folders are packaged as commondata/raster_data, raster_data1...
        pattern = re.compile(r"(" + re.escape(norm) + (r"|raster_data" if factory == "Raster" else "") + r")\d*$",
                             re.IGNORECASE)
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        found = [os.path.join(root, n) for n in names]
        found += [os.path.join(root, n) for n in sorted(os.listdir(root)) if pattern.match(n)]
        for p in found:
            if os.path.exists(p) and (p, factory) not in out:
                out.append((p, factory))
    return out


def read_mxd(path):
    """ArcMap document -> [MxdMap] (data frames in document order). Raises MxdError."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise MxdError(str(e)) from e
    streams = _streams(data)
    b = streams.get("Maps") or streams.get("Mx Document")
    if not b:
        raise MxdError("no map stream")

    # every recognised object, in stream order: (offset, kind, object id, name, end of name)
    marks = []
    for cls, (kind, gap) in _LAYERS.items():
        for p in _find_all(b, cls):
            oid = struct.unpack_from("<I", b, p + 16)[0] if p + 20 <= len(b) else -1
            name, end = _bstr(b, p + 16 + gap)
            marks.append((p, kind, oid, name, end))
    marks.sort()

    maps, seen = [], {}
    current, stack = None, []   # stack: [[group layer, children still to come]]
    basemap = False             # the last layer was an Esri basemap (its services follow it)

    def attach(layer):
        if stack:
            stack[-1][0].children.append(layer)
            stack[-1][1] -= 1
        else:
            current.layers.append(layer)
        while stack and stack[-1][1] <= 0:
            stack.pop()

    for k, (p, kind, oid, name, end) in enumerate(marks):
        if (kind, oid) in seen:
            # an object written before is only referred to: the same layer listed again in a group
            if kind == "map":           # a data frame referred to by the layout: its layers are done
                current, stack = None, []
            elif stack and current is not None and seen[(kind, oid)] is not None:
                attach(seen[(kind, oid)])
            continue
        if name is None:
            continue
        seen[(kind, oid)] = None
        if kind == "map":
            current, stack = MxdMap(name=name), []
            maps.append(current)
            wkt = re.search(rb"(PROJCS|GEOGCS)\[[\x20-\x7e]{10,8000}?\]\](?=\x00)", b[end:end + 12000])
            if wkt:
                current.wkt = wkt.group().decode("ascii")
            continue
        if current is None:
            continue                    # a layer used elsewhere (overview, legend...)
        layer = MxdLayer(name=name, kind=kind)
        seen[(kind, oid)] = layer
        # the object's data runs to the next object written in full (references have no name)
        nxt = next((q[0] for q in marks[k + 1:] if q[3] is not None), len(b))
        count = 0
        if kind == "group":
            layer.visible = _visible(b, end)
            # visible flag, 4 bytes, the two scale limits (doubles), then the number of children
            count = struct.unpack_from("<I", b, end + 22)[0] if end + 26 <= len(b) else 0
        elif kind in _OPAQUE:
            layer.visible = _visible(b, end)
            if kind == "service" and basemap:
                continue                # a service inside a basemap layer
        else:
            # feature/raster layers carry one more string right after the name; the flag follows it
            t, after = _bstr(b, end)
            layer.visible = _visible(b, after if t is not None else end)
            _source(layer, b, end, nxt)
        basemap = kind == "basemap"
        attach(layer)
        if kind == "group" and 0 < count < 10000:
            stack.append([layer, count])
    if not maps:
        raise MxdError("no data frames found")
    return maps
