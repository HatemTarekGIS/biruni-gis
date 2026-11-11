"""Tests for ArcMap symbology (biruni.arcgis.mxd_symbols): renderers, symbols, colours and labels.

The fixtures are written here, byte for byte the way ArcMap 10.x persists them (the layouts were worked out
on real documents against ArcGIS's own reading of them): a document with a unique-values layer (default
symbol, a multi-layer outline, a CMYK colour, a hollow class), a class-breaks layer, a cartographic dashed
line, simple / character / picture markers, labels shown and hidden, and a layer file (.lyr). No arcpy and
no real client documents are needed.

Run:  engine\\bin\\python-qgis.bat tests\\test_mxd_symbols.py
"""
import glob
import os
import struct
import sys
import tempfile
import uuid
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qgis.core import QgsApplication, QgsProject  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.stdout.reconfigure(line_buffering=True)
from osgeo import ogr, osr  # noqa: E402

from biruni.arcgis import open_arcgis_file  # noqa: E402
from biruni.arcgis.mxd import read_mxd  # noqa: E402
from biruni.arcgis.mxd_symbols import _LAB_TO_RGB, lab_to_rgb  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni_mxdsym_")

# ---- colours: CIELAB as ArcObjects keeps it -> RGB as ArcGIS shows it ---------------------------------------------
# (Lab values of real ArcMap colours with the RGB that ArcGIS reports for them)
KNOWN = [((54.04, 0, 0), (110, 110, 110)), ((94.95, -9.34, -6.41), (207, 245, 252)),
         ((17.87, 45.92, -66.78), (0, 6, 128)), ((35.5, 56.66, -5.71), (135, 0, 77)),
         ((67.81, -7.94, 65.6), (168, 151, 0)), ((100, 0, 0), (255, 255, 255)), ((0, 0, 0), (0, 0, 0))]
check(all(tuple(lab_to_rgb(*lab)) == rgb for lab, rgb in KNOWN), "CIELAB -> RGB matches ArcGIS (gamma 1.8)")


def _inv3(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


_RGB_TO_XYZ = _inv3(_LAB_TO_RGB)


def rgb_to_lab(rgb):
    lin = [(v / 255) ** 1.8 for v in rgb]
    xyz = [sum(m * v for m, v in zip(row, lin)) for row in _RGB_TO_XYZ]

    def f(t):
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29
    fx, fy, fz = (f(t) for t in xyz)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


# ---- writing ArcObjects -------------------------------------------------------------------------------------------
def g(s):
    return uuid.UUID(s).bytes_le


C = {k: g(v) for k, v in {
    "Map": "E6BDAA76-4D35-11D0-98BE-00805F7CED21", "FeatureLayer": "E663A651-8AAD-11D0-BEC7-00805F7C4268",
    "WorkspaceName": "5A350011-E371-11D1-AA82-00C04FA33A15", "FcName": "75E10086-4226-42AC-AFEC-CBB9B748F847",
    "PropertySet": "588E5A11-D09B-11D1-AA7C-00C04FA33A15", "FGDB": "71FE75F0-EA0C-4406-873E-B7D53748AE7E",
    "Shapefile": "A06ADB96-D95C-11D1-AA81-00C04FA33A15",
    "RgbColor": "7EE9C496-D123-11D0-8383-080009B996CC", "CmykColor": "7EE9C497-D123-11D0-8383-080009B996CC",
    "SimpleLine": "7914E5F9-C892-11D0-8BB6-080009EE4E41", "CartoLine": "7914E5FB-C892-11D0-8BB6-080009EE4E41",
    "MultiLine": "7914E5FA-C892-11D0-8BB6-080009EE4E41", "SimpleFill": "7914E603-C892-11D0-8BB6-080009EE4E41",
    "SimpleMarker": "7914E5FE-C892-11D0-8BB6-080009EE4E41", "CharMarker": "7914E600-C892-11D0-8BB6-080009EE4E41",
    "PicMarker": "7914E602-C892-11D0-8BB6-080009EE4E41", "Template": "41093A71-CCE1-11D0-BFAA-0080C7E24280",
    "SimpleRenderer": "F3435801-5779-11D0-98BF-00805F7CED21", "UVRenderer": "C3346D29-B2BC-11D1-8817-080009EC732A",
    "CBRenderer": "AE5F7EA2-8B48-11D0-8356-080009B996CC", "LegendGroup": "167C5EA2-AF20-11D1-8817-080009EC732A",
    "LegendClass": "167C5EA3-AF20-11D1-8817-080009EC732A", "StdFont": "0BE35203-8F91-11CE-9DE3-00AA004BB851",
    "AnnoColl": "1D5849F3-0D33-11D2-A26F-080009B6F22B", "LabelProps": "01004145-0D1C-11D2-A26F-080009B6F22B",
    "TextSymbol": "B65A3E74-2993-11D1-9A43-0080C7EC5C96", "VBScript": "AA157207-E079-11D2-9F48-00C04F6BC6A5",
    "FieldInfo": "A2BAAE2D-969B-11D2-AE77-080009EC732A"}.items()}
_ids = iter(range(100, 100000))


def bstr(s):
    raw = s.encode("utf-16-le") + b"\0\0"
    return struct.pack("<I", len(raw)) + raw


def d(*v):
    return struct.pack(f"<{len(v)}d", *v)


def u32(*v):
    return struct.pack(f"<{len(v)}I", *v)


def hdr(cls, ver=1, oid=None):
    oid = next(_ids) if oid is None else oid
    return C[cls] + u32(oid) + struct.pack("<H", ver), oid


def obj(cls, ver=1):
    return hdr(cls, ver)[0]


def ref(cls, oid):
    return C[cls] + u32(oid)


def color(rgb, alpha=100, null=False):
    t = 255 if null else 255 - round(alpha * 2.55)
    return obj("RgbColor") + b"\x01\x00\x00" + d(*rgb_to_lab(rgb)) + b"\x01" + bytes([t])


def cmyk(c, m, y, k):
    return hdr("CmykColor", 4)[0] + bytes([k, y, c, m]) + b"\x00\x00\x01\x00"


def simple_line(col, width, style=0):
    return obj("SimpleLine") + col + d(width) + u32(style, 13, 0)


def simple_fill(col, outline, style=0):
    return obj("SimpleFill") + outline + col + u32(13, style, 0)


def simple_marker(col, size, style, outline_col, outline_width):
    return (obj("SimpleMarker", 2) + col + d(size) + u32(style, 13, 0) + d(0, 0, 0) + b"\x01" + d(outline_width) +
            outline_col + b"\xff\xff")


def carto_line(col, width, pattern, interval=1.0, cap=0, join=1):
    tpl = obj("Template") + d(interval) + u32(len(pattern) // 2) + d(*pattern)
    return (obj("CartoLine") + u32(cap, join) + d(width) + b"\0" * 9 + col + tpl + b"\0" * 16 + u32(13) +
            b"\0" * 13 + d(10))


def multi_line(*layers):
    n = len(layers)
    return obj("MultiLine", 2) + u32(13, 0, n) + b"".join(layers) + u32(*([1] * n)) + u32(*([0] * n)) + bstr("") * n


def std_font(name, size_pt, bold=False):
    raw = name.encode("latin-1")
    return (obj("StdFont")[:-2] + b"\x01" + struct.pack("<HBH", 0, 0, 700 if bold else 400) +
            u32(int(size_pt * 10000)) + bytes([len(raw)]) + raw)


def char_marker(col, index, size, font):
    return (obj("CharMarker", 4) + col + u32(index) + d(0, size, 0, 0, 1, 1) + u32(13, 0) + b"\xff\xff" +
            bstr(font) + b"\0" * 31 + std_font(font, size))


def png_bytes():
    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)
    raw = b"".join(b"\0" + b"\xff\x00\x00\xff" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def picture_marker(size):
    png = png_bytes()
    return (obj("PicMarker", 9) + u32(2, 2, len(png)) + png + b"\0" * 16 + color((255, 255, 255), null=True) +
            b"\0" * 16 + d(size, 0, 0, 0, 1, 1) + u32(13) + b"\0" * 13)


def legend_group(heading, n):
    return obj("LegendGroup", 2) + b"\xff\xff\xff\xff" + bstr(heading) + u32(n)


def legend_class(symbol, label):
    return obj("LegendClass", 2) + symbol + bstr(label) + bstr("") + b"\0" * 24


def simple_renderer(sym_cls, sym):
    head, oid = hdr("SimpleRenderer", 3)
    sym_oid = struct.unpack_from("<I", sym, 16)[0]
    return head + sym + legend_group("", 1) + legend_class(ref(sym_cls, sym_oid), "")


def uv_renderer(field, default, classes, values):
    """classes: [(symbol bytes, label)]; values: [(value, class index)]."""
    dflt_oid = struct.unpack_from("<I", default, 16)[0]
    dflt_cls = next(k for k, v in C.items() if v == default[:16])
    out = obj("UVRenderer", 3) + u32(1) + bstr(field) + b"\x01\x00\x00" + bstr(", ") + default + u32(2)
    out += legend_group("", 1) + legend_class(ref(dflt_cls, dflt_oid), "<all other values>")
    out += legend_group(field, len(classes)) + b"".join(legend_class(s, lab) for s, lab in classes)
    out += u32(len(values)) + b"".join(bstr(v) + u32(1, c, 0) for v, c in values)
    return out + b"\0" * 40


def cb_renderer(field, breaks, minimum, symbols):
    n = len(breaks)
    lower = [minimum] + breaks[:-1]
    out = obj("CBRenderer", 10) + bstr(field) + bstr("Yellow to Red") + u32(n) + d(*breaks) + d(*lower)
    return out + legend_group(field, n) + b"".join(legend_class(s, f"{lo:g} - {hi:g}")
                                                   for s, lo, hi in zip(symbols, lower, breaks))


def labels(expr, font, size, rgb, shown):
    text = (obj("TextSymbol", 4) + color(rgb) + b"\xff\xff\xff\xff" + u32(3, 1) + b"\0" * 40 +
            std_font(font, size, bold=True) + b"\xff\xff")
    props = (obj("AnnoColl", 2) + u32(1) + obj("LabelProps", 7) + bstr(expr) + b"\xff\xff" + b"\0" * 30 +
             bstr("Default") + bstr("") + text + b"\0" * 20 + C["VBScript"] + u32(next(_ids)) + b"\0" * 30)
    flag = (bstr("kind") + b"\0" * 16 + (b"\xff\xff" if shown else b"\0\0") + b"\0" * 20 + bstr("") + bstr("") +
            b"\0" * 4 + u32(3) + bstr("OBJECTID") + C["FieldInfo"] + u32(next(_ids)) + b"\x04\x00" + b"\0" * 30)
    return props + flag


def feature(name, dataset, ws, factory, dtype, renderer, label=b""):
    return (obj("FeatureLayer", 2) + bstr(name) + bstr(dtype) + b"\xff\xff" + b"\0" * 4 +
            obj("FcName") + b"\x01\x00\x00\x00\x02\x00" + bstr(dataset) + b"\x02\0\0\0\0\0" + bstr(dtype) +
            bstr("Shape") + b"\x03\0\0\0\x01\0\0\0\0\0\x11\0" +
            obj("WorkspaceName") + bstr(ws) + b"\x02\0\0\0\0\0" + bstr(os.path.splitext(os.path.basename(ws))[0]) +
            obj("PropertySet") + b"\x01\0\x01\0\0\0" + bstr("DATABASE") + b"\x08\0" + bstr(ws) + b"\x01" + C[factory] +
            b"\0" * 30 + renderer + label + b"\0" * 60)


def compound_file(streams):
    """Minimal OLE compound file (version 3, 512-byte sectors) with top-level streams of 4096+ bytes."""
    ss, end, free, fat_sect = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    sectors, fat = [], []

    def put(data):
        start = len(sectors)
        chunks = [data[i:i + ss].ljust(ss, b"\0") for i in range(0, len(data), ss)] or [b"\0" * ss]
        for k, c in enumerate(chunks):
            sectors.append(c)
            fat.append(start + k + 1 if k < len(chunks) - 1 else end)
        return start
    starts = {n: put(dt) for n, dt in streams.items()}
    entries = []

    def entry(name, typ, right, child, start, size):
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB3I", len(raw), typ, 1, free, right, child) + b"\0" * 36 +
                struct.pack("<IQ", start, size))
    names = list(streams)
    entries.append(entry("Root Entry", 5, free, 1, end, 0))
    for k, n in enumerate(names):
        entries.append(entry(n, 2, k + 2 if k + 1 < len(names) else free, free, starts[n], len(streams[n])))
    dir_start = put(b"".join(entries))
    n_fat = 1
    while (len(sectors) + n_fat) > n_fat * (ss // 4):
        n_fat += 1
    fat_start = len(sectors)
    fat += [fat_sect] * n_fat
    fat += [free] * (n_fat * (ss // 4) - len(fat))
    for k in range(n_fat):
        sectors.append(b"".join(struct.pack("<I", v) for v in fat[k * 128:(k + 1) * 128]))
    difat = [fat_start + k for k in range(n_fat)] + [free] * (109 - n_fat)
    header = (bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) +
              b"\0" * 6 + struct.pack("<9I", 0, n_fat, dir_start, 0, 4096, end, 0, end, 0) +
              struct.pack("<109I", *difat))
    return header + b"".join(sectors)


# ---- data the document points to ----------------------------------------------------------------------------------
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
os.makedirs(os.path.join(tmp, "data"))
gdb = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(os.path.join(tmp, "data", "city.gdb"))
for name, gtype, wkt in (("parcels", ogr.wkbPolygon, "POLYGON((500000 2400000,500100 2400000,500100 2400100,"
                                                      "500000 2400100,500000 2400000))"),
                         ("roads", ogr.wkbLineString, "LINESTRING(500000 2399900,501000 2399900)"),
                         ("wells", ogr.wkbPoint, "POINT(500010 2400010)")):
    lyr = gdb.CreateLayer(name, srs, gtype)
    lyr.CreateField(ogr.FieldDefn("kind", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("pop", ogr.OFTReal))
    for k, v in (("A", 5.0), ("B", 15.0), ("C", 25.0)):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("kind", k)
        f.SetField("pop", v)
        f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(f)
gdb = None

FC = "File Geodatabase Feature Class"
GREY, BLUE, ORANGE, GREEN, RED = (110, 110, 110), (0, 92, 230), (255, 170, 0), (56, 168, 0), (230, 0, 0)

parcels = feature("Parcels", "parcels", r"data\city.gdb", "FGDB", FC, uv_renderer(
    "kind", simple_fill(color((200, 200, 200)), simple_line(color(GREY), 0.4)),
    [(simple_fill(color(ORANGE), multi_line(simple_line(color(GREY), 1.5), simple_line(color(RED), 0.5))), "Kind A"),
     (simple_fill(cmyk(0, 100, 100, 0), simple_line(color(GREY), 0)), "Kind B"),
     (simple_fill(color(GREEN, null=True), simple_line(color(BLUE), 2)), "Kind C (hollow)")],
    [("A", 0), ("B", 1), ("C", 2)]), labels("[kind]", "Tahoma", 9, (0, 0, 128), shown=True))
density = feature("Density", "parcels", r"data\city.gdb", "FGDB", FC, cb_renderer(
    "pop", [10.0, 20.0, 30.0], 1.0,
    [simple_fill(color(c), simple_line(color(GREY), 0.4)) for c in ((255, 255, 190), ORANGE, RED)]),
    labels("[pop]", "Arial", 8, (0, 0, 0), shown=False))
roads = feature("Roads", "roads", r"data\city.gdb", "FGDB", FC, simple_renderer(
    "CartoLine", carto_line(color(RED), 2.0, [6, 2, 1, 2], interval=1.0)))
wells = feature("Wells", "wells", r"data\city.gdb", "FGDB", FC, simple_renderer(
    "SimpleMarker", simple_marker(color(BLUE, alpha=60), 8, 1, color((0, 0, 0)), 1.0)))
wells2 = feature("Wells (font)", "wells", r"data\city.gdb", "FGDB", FC, simple_renderer(
    "CharMarker", char_marker(color(GREEN), 72, 14, "ESRI Default Marker")))
wells3 = feature("Wells (picture)", "wells", r"data\city.gdb", "FGDB", FC, simple_renderer(
    "PicMarker", picture_marker(12)))
plain = feature("Unstyled", "wells", r"data\city.gdb", "FGDB", FC, b"")

stream = (obj("Map", 2) + bstr("Layers") + bstr("") + b"\0" * 40 +
          parcels + density + roads + wells + wells2 + wells3 + plain)
stream += b"\0" * max(0, 4096 - len(stream))
mxd_path = os.path.join(tmp, "styled.mxd")
with open(mxd_path, "wb") as fh:
    fh.write(compound_file({"Maps": stream}))

# ---- reading the symbology ----------------------------------------------------------------------------------------
frames = read_mxd(mxd_path)
by = {lyr.name: lyr for lyr in frames[0].layers}
check(list(by) == ["Parcels", "Density", "Roads", "Wells", "Wells (font)", "Wells (picture)", "Unstyled"],
      f"layers read: {list(by)}")


def rgb_of(cim_color):
    return tuple(cim_color["values"][:3])


p = by["Parcels"].renderer or {}
check(p.get("type") == "CIMUniqueValueRenderer" and p.get("fields") == ["kind"], "unique values renderer on 'kind'")
classes = (p.get("groups") or [{}])[0].get("classes") or []
check([c["values"][0]["fieldValues"] for c in classes] == [["A"], ["B"], ["C"]], "class values")
check([c["label"] for c in classes] == ["Kind A", "Kind B", "Kind C (hollow)"], "class labels")
a_layers = classes[0]["symbol"]["symbol"]["symbolLayers"] if classes else []
check([sl["type"] for sl in a_layers] == ["CIMSolidStroke", "CIMSolidStroke", "CIMSolidFill"] and
      rgb_of(a_layers[2]["color"]) == ORANGE and rgb_of(a_layers[1]["color"]) == RED and
      a_layers[0]["width"] == 1.5, "multi-layer outline + fill colour (class A)")
b_layers = classes[1]["symbol"]["symbol"]["symbolLayers"] if len(classes) > 1 else []
check([sl["type"] for sl in b_layers] == ["CIMSolidFill"] and rgb_of(b_layers[0]["color"]) == (255, 0, 0),
      "CMYK colour read; a 0-width outline is not drawn (class B)")
c_layers = classes[2]["symbol"]["symbol"]["symbolLayers"] if len(classes) > 2 else []
check([sl["type"] for sl in c_layers] == ["CIMSolidStroke"] and rgb_of(c_layers[0]["color"]) == BLUE,
      "'no colour' fill is hollow (class C)")
check(p.get("useDefaultSymbol") and p.get("defaultLabel") == "<all other values>" and
      rgb_of(p["defaultSymbol"]["symbol"]["symbolLayers"][-1]["color"]) == (200, 200, 200), "default symbol")
lab = by["Parcels"].label_classes
check(by["Parcels"].labels_visible and lab and lab[0]["expression"] == "[kind]" and
      lab[0]["expressionEngine"] == "VBScript", "labels: expression, engine and 'Label features' on")
ts = lab[0]["textSymbol"]["symbol"] if lab else {}
check(ts.get("fontFamilyName") == "Tahoma" and ts.get("height") == 9 and ts.get("fontStyleName") == "Bold" and
      rgb_of(ts["symbol"]["symbolLayers"][0]["color"]) == (0, 0, 128), "labels: font, size, bold, colour")

cb = by["Density"].renderer or {}
check(cb.get("type") == "CIMClassBreaksRenderer" and cb.get("field") == "pop" and cb.get("minimumBreak") == 1.0 and
      [b["upperBound"] for b in cb.get("breaks", [])] == [10.0, 20.0, 30.0], "class breaks: field, minimum, breaks")
check([rgb_of(b["symbol"]["symbol"]["symbolLayers"][-1]["color"]) for b in cb.get("breaks", [])] ==
      [(255, 255, 190), ORANGE, RED] and cb["breaks"][0]["label"] == "1 - 10", "class breaks: colours and labels")
check(by["Density"].label_classes and not by["Density"].labels_visible, "labels kept but switched off")

r = (by["Roads"].renderer or {}).get("symbol", {}).get("symbol", {})
stroke = (r.get("symbolLayers") or [{}])[0]
check(r.get("type") == "CIMLineSymbol" and stroke.get("width") == 2.0 and rgb_of(stroke["color"]) == RED and
      stroke.get("capStyle") == "Butt" and stroke["effects"][0]["dashTemplate"] == [6, 2, 1, 2],
      "cartographic line: width, colour, cap and dash template")
w = (by["Wells"].renderer or {}).get("symbol", {}).get("symbol", {})
vm = (w.get("symbolLayers") or [{}])[0]
graphic = (vm.get("markerGraphics") or [{}])[0]
gl = graphic.get("symbol", {}).get("symbolLayers", [])
check(w.get("type") == "CIMPointSymbol" and vm.get("size") == 8 and "rings" in graphic.get("geometry", {}) and
      rgb_of(gl[1]["color"]) == BLUE and gl[1]["color"]["values"][3] == 60 and rgb_of(gl[0]["color"]) == (0, 0, 0),
      "simple marker: square, size, colour with transparency, outline")
cm = ((by["Wells (font)"].renderer or {}).get("symbol", {}).get("symbol", {}).get("symbolLayers") or [{}])[0]
check(cm.get("type") == "CIMCharacterMarker" and cm.get("fontFamilyName") == "ESRI Default Marker" and
      cm.get("characterIndex") == 72 and cm.get("size") == 14, "character marker: font, character, size")
pm = ((by["Wells (picture)"].renderer or {}).get("symbol", {}).get("symbol", {}).get("symbolLayers") or [{}])[0]
check(pm.get("type") == "CIMPictureMarker" and pm.get("url", "").startswith("data:image/png;base64,") and
      pm.get("size") == 12, "picture marker: embedded PNG and size")
check(by["Unstyled"].renderer is None, "a layer without a renderer stays without one")

# ---- opening into a project: the engine's styles --------------------------------------------------------------------
project = QgsProject()
rep = open_arcgis_file(mxd_path, project, "Layers")
lay = {x.name(): x for x in rep.layers}
check(len(lay) == 7, f"all layers added ({sorted(lay)})")
uv = lay["Parcels"].renderer()
check(uv.type() == "categorizedSymbol" and uv.classAttribute() == "kind" and
      [c.value() for c in uv.categories()][:3] == ["A", "B", "C"], "categorized renderer in the map")
cats = uv.categories()
sym_a = cats[0].symbol()
check(sym_a.symbolLayer(0).color().name() == "#ffaa00" and sym_a.symbolLayerCount() == 3,
      "category colour in the map (fill under the two outlines)")
check(lay["Parcels"].labelsEnabled() and lay["Parcels"].labeling() is not None, "labels on in the map")
check(not lay["Density"].labelsEnabled(), "hidden labels stay off in the map")
gr = lay["Density"].renderer()
ranges = gr.ranges()
check(gr.type() == "graduatedSymbol" and gr.classAttribute() == "pop" and len(ranges) == 3 and
      ranges[0].lowerValue() == 1.0 and ranges[2].upperValue() == 30.0, "graduated renderer in the map")
line = lay["Roads"].renderer().symbol()
check(line.color().name() == "#e60000" and abs(line.width() - 2.0) < 1e-6, "line colour and width in the map")
pt = lay["Wells"].renderer().symbol()
check(pt.symbolLayer(0).color().name() == "#005ce6" and abs(pt.size() - 8) < 1e-6, "marker colour and size")
check(lay["Wells (picture)"].renderer().symbol().symbolLayer(0).layerType() == "RasterMarker",
      "picture marker drawn from the embedded image")
notes = " ".join(rep.warnings)
check("1 ArcMap layers" in notes and "default styles" in notes, "only the unstyled layer is reported")

# ---- layer files (.lyr) ----------------------------------------------------------------------------------------------
lyr_stream = parcels + b"\0" * max(0, 4096 - len(parcels))
lyr_path = os.path.join(tmp, "parcels.lyr")
with open(lyr_path, "wb") as fh:
    fh.write(compound_file({"Layer": lyr_stream}))
lf = read_mxd(lyr_path)
check(len(lf) == 1 and lf[0].name == "parcels" and [x.name for x in lf[0].layers] == ["Parcels"] and
      (lf[0].layers[0].renderer or {}).get("type") == "CIMUniqueValueRenderer", "layer file read with its symbology")
p2 = QgsProject()
rep2 = open_arcgis_file(lyr_path, p2)
check([n for n, _k in rep2.added] == ["Parcels"] and rep2.layers[0].renderer().type() == "categorizedSymbol",
      "layer file opens with its symbology")

# ---- damaged symbology never loses the layer -------------------------------------------------------------------------
broken = bytearray(stream)
i = broken.find(C["UVRenderer"])
broken[i + 22:i + 26] = u32(77)        # an impossible field count
bad_path = os.path.join(tmp, "broken.mxd")
with open(bad_path, "wb") as fh:
    fh.write(compound_file({"Maps": bytes(broken)}))
fb = {x.name: x for x in read_mxd(bad_path)[0].layers}
check(len(fb) == 7 and fb["Roads"].renderer is not None, "a damaged renderer does not stop the document")
check((fb["Parcels"].renderer or {}).get("type") == "CIMSimpleRenderer", "a damaged renderer falls back to a symbol")

# ---- real documents on this machine (optional, read-only) ------------------------------------------------------------
real = sorted(glob.glob(os.path.expanduser(r"~\Documents\**\*.mxd"), recursive=True))[:30]
if real:
    n_layers = n_styled = errors = 0
    for path in real:
        try:
            for fr in read_mxd(path):
                stack = list(fr.layers)
                while stack:
                    x = stack.pop()
                    stack += x.children
                    if x.kind == "feature":
                        n_layers += 1
                        n_styled += x.renderer is not None
        except Exception as e:  # noqa: BLE001
            errors += 1
            print("   ERROR", path, e)
    print(f"   real files: {len(real)}, {n_styled}/{n_layers} feature layers with symbology")
    check(errors == 0, "real ArcMap documents read without errors")
else:
    print("SKIP no ArcMap documents in Documents")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
