r"""Migration readiness scan, off screen: a folder tree made here holds an ArcGIS Pro project (layers that open, data
not found, an annotation layer, an Esri basemap, a layout), a map file, layer files, an ArcMap .mxd, arcpy scripts
(and a plain Python file that must be ignored), a toolbox with a model, a file geodatabase, shapefiles (one without
.prj), a layout file and things in AppData / node_modules / .git that must be skipped. The scan finds each item with
the right kind and judges it (works 100% / with changes / needs work / cannot be read) with the real readers,
read-only: no file is added or changed in the tree. Scripts use the arcpy analyser, or a simple count when it is
missing. Summary sentence, most missing functions, time limit, Stop, HTML (English, Arabic right to left) and Excel
reports, the window (background scan, sortable table, double-click opens a map) and the Processing tool.

Run:  engine\bin\python-qgis.bat tests\test_migrate.py
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import types
import uuid
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp(prefix="biruni_migrate_profile_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from osgeo import ogr, osr  # noqa: E402
from qgis.core import QgsProject  # noqa: E402
from qgis.PyQt.QtCore import QEventLoop, Qt, QTimer  # noqa: E402

from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.migrate import report, scanner  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def pump(ms=50):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


root = tempfile.mkdtemp(prefix="biruni_migrate_")
work = os.path.join(root, "GIS work")
out = os.path.join(root, "reports")          # outputs live outside the scanned tree
os.makedirs(out)


def path(*parts):
    p = os.path.join(work, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# ---- data ------------------------------------------------------------------------------------------------------
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(path("project", "city.gdb"))
for name, gtype, wkt in (("parcels", ogr.wkbPolygon, "POLYGON((500000 2400000,500100 2400000,500100 2400100,"
                                                      "500000 2400100,500000 2400000))"),
                         ("roads", ogr.wkbLineString, "LINESTRING(500000 2399900,501000 2399900)")):
    lyr = ds.CreateLayer(name, srs, gtype)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
    lyr.CreateFeature(f)
ds = None
for stem, prj in (("wells", True), ("old_points", False)):
    shp = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(path("shapes", stem + ".shp"))
    lyr = shp.CreateLayer(stem, srs if prj else None, ogr.wkbPoint)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(500050 2400050)"))
    lyr.CreateFeature(f)
    shp = None


# ---- ArcGIS Pro documents --------------------------------------------------------------------------------------
def fc(dataset, ws="DATABASE=.\\city.gdb"):
    return {"type": "CIMStandardDataConnection", "workspaceConnectionString": ws, "workspaceFactory": "FileGDB",
            "dataset": dataset, "datasetType": "esriDTFeatureClass"}


def simple(sym_type, layer):
    return {"type": "CIMSimpleRenderer", "symbol": {"type": "CIMSymbolReference", "symbol": {
        "type": sym_type, "symbolLayers": [layer]}}}


FILL = simple("CIMPolygonSymbol", {"type": "CIMSolidFill", "enable": True,
                                   "color": {"type": "CIMRGBColor", "values": [200, 10, 10, 100]}})
GRADIENT = simple("CIMLineSymbol", {"type": "CIMGradientStroke", "enable": True, "width": 2})


def layer(name, dc, renderer, **extra):
    d = {"type": "CIMFeatureLayer", "name": name, "uRI": f"CIMPATH=map/{name}.json", "visibility": True,
         "featureTable": {"type": "CIMFeatureTable", "dataConnection": dc}, "renderer": renderer}
    d.update(extra)
    return d


defs = [layer("parcels", fc("parcels"), FILL),
        layer("roads", fc("roads"), GRADIENT),            # opens, its symbol changes
        layer("lost", fc("lost", "DATABASE=.\\gone.gdb"), FILL),
        {"type": "CIMAnnotationLayer", "name": "labels", "uRI": "CIMPATH=map/labels.json"},
        {"type": "CIMVectorTileLayer", "name": "World Topographic Map", "uRI": "CIMPATH=map/topo.json",
         "layerType": "BasemapBackground", "dataConnection": {
             "type": "CIMVectorTileDataConnection",
             "uRI": "https://basemaps.arcgis.com/arcgis/rest/services/World_Basemap_v2/VectorTileServer"}}]
map_def = {"type": "CIMMap", "name": "City", "uRI": "CIMPATH=map/map.json", "layers": [d["uRI"] for d in defs],
           "spatialReference": {"wkid": 32637}}
aprx = path("project", "City.aprx")
with zipfile.ZipFile(aprx, "w") as z:
    z.writestr("map/map.json", json.dumps(map_def))
    for d in defs:
        z.writestr(d["uRI"][8:], json.dumps(d))
    z.writestr("layout/layout.json", json.dumps({"type": "CIMLayout", "name": "A3", "uRI": "CIMPATH=layout/layout.json"}))
    z.writestr("GISProject.json", json.dumps({"type": "CIMGISProject", "views": [
        {"type": "CIMMapView", "viewableObjectPath": "CIMPATH=map/map.json"}]}))
mapx = path("project", "Parcels.mapx")
with open(mapx, "w", encoding="utf-8") as fh:
    json.dump({"type": "CIMMapDocument", "mapDefinition": dict(map_def, layers=[defs[0]["uRI"]]),
               "layerDefinitions": [defs[0]]}, fh)
lyrx_ok = path("project", "Parcels.lyrx")
with open(lyrx_ok, "w", encoding="utf-8") as fh:
    json.dump({"type": "CIMLayerDocument", "layers": [defs[0]["uRI"]], "layerDefinitions": [defs[0]]}, fh)
lyrx_lost = path("project", "Lost.lyrx")
with open(lyrx_lost, "w", encoding="utf-8") as fh:
    json.dump({"type": "CIMLayerDocument", "layers": [defs[2]["uRI"]], "layerDefinitions": [defs[2]]}, fh)
bad_aprx = path("old", "Broken.aprx")
with open(bad_aprx, "wb") as fh:
    fh.write(b"not a zip at all")
pagx = path("project", "A3 layout.pagx")
with open(pagx, "w", encoding="utf-8") as fh:
    json.dump({"type": "CIMLayoutDocument", "layoutDefinition": {"type": "CIMLayout", "elements": [
        {"type": "CIMMapFrame"}, {"type": "CIMLegend"}, {"type": "CIMGraphicElement"}]}}, fh)


# ---- an ArcMap .mxd (OLE compound file; the same writer as test_mxd) -------------------------------------------
def g(s):
    return uuid.UUID(s).bytes_le


MAP, FEATURE = g("E6BDAA76-4D35-11D0-98BE-00805F7CED21"), g("E663A651-8AAD-11D0-BEC7-00805F7C4268")
WS_NAME, PROPSET = g("5A350011-E371-11D1-AA82-00C04FA33A15"), g("588E5A11-D09B-11D1-AA7C-00C04FA33A15")
FGDB, FC_NAME = g("71FE75F0-EA0C-4406-873E-B7D53748AE7E"), g("75E10086-4226-42AC-AFEC-CBB9B748F847")
_ids = iter(range(30, 10000))


def bstr(s):
    raw = s.encode("utf-16-le") + b"\0\0"
    return struct.pack("<I", len(raw)) + raw


def obj(cls, gap=2):
    return cls + struct.pack("<I", next(_ids)) + b"\x22\x00" + b"\0" * (gap - 2)


def mxd_feature(name, dataset, ws):
    dtype = "File Geodatabase Feature Class"
    return (obj(FEATURE) + bstr(name) + bstr(dtype) + b"\xff\xff" + b"\0" * 4 +
            obj(FC_NAME) + b"\x01\x00\x00\x00\x02\x00" + bstr(dataset) + b"\x02\0\0\0\0\0" + bstr(dtype) +
            bstr("Shape") + b"\x03\0\0\0\x01\0\0\0\0\0\x11\0" +
            obj(WS_NAME) + bstr(ws) + b"\x02\0\0\0\0\0" + bstr("city") +
            obj(PROPSET) + b"\x01\0\x01\0\0\0" + bstr("DATABASE") + b"\x08\0" + bstr(ws) + b"\x01" + FGDB +
            b"\0" * 30 + bstr("[kind]") + bstr("Default") + b"\0" * 80)


def compound_file(streams):
    ss, end, free, fat_sect = 512, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD
    sectors, fat = [], []

    def put(data):
        start = len(sectors)
        chunks = [data[i:i + ss].ljust(ss, b"\0") for i in range(0, len(data), ss)] or [b"\0" * ss]
        for k, c in enumerate(chunks):
            sectors.append(c)
            fat.append(start + k + 1 if k < len(chunks) - 1 else end)
        return start

    big = {n: d for n, d in streams.items() if len(d) >= 4096}
    small = {n: d for n, d in streams.items() if len(d) < 4096}
    starts, mini, minifat = {}, b"", []
    for n, d in small.items():
        first = len(mini) // 64
        blocks = (len(d) + 63) // 64
        mini += d.ljust(blocks * 64, b"\0")
        minifat += [first + k + 1 if k < blocks - 1 else end for k in range(blocks)]
        starts[n] = first
    for n, d in big.items():
        starts[n] = put(d)
    mini_start = put(mini) if mini else end
    minifat_start = put(b"".join(struct.pack("<I", v) for v in minifat).ljust(ss, b"\xff")) if minifat else end
    names = list(streams)

    def entry(name, typ, left, right, child, start, size):
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB3I", len(raw), typ, 1, left, right, child) + b"\0" * 36 +
                struct.pack("<IQ", start, size))
    entries = [entry("Root Entry", 5, free, free, 1, mini_start, len(mini))]
    for k, n in enumerate(names):
        entries.append(entry(n, 2, free, k + 2 if k + 1 < len(names) else free, free, starts[n], len(streams[n])))
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
              b"\0" * 6 + struct.pack("<9I", 0, n_fat, dir_start, 0, 4096, minifat_start, 1 if minifat else 0,
                                      end, 0) + struct.pack("<109I", *difat))
    return header + b"".join(sectors)


frame = obj(MAP) + bstr("Layers") + bstr("") + b"\0" * 60
mxd = path("project", "Old map.mxd")
with open(mxd, "wb") as fh:
    fh.write(compound_file({"Maps": frame + mxd_feature("Parcels", "parcels", "city.gdb"),
                            "Version": bstr("10.8")}))

# ---- scripts, toolbox, things that must be skipped -------------------------------------------------------------
script = path("scripts", "export maps.py")
with open(script, "w", encoding="utf-8") as fh:
    fh.write("import arcpy\narcpy.env.workspace = r'C:\\data'\narcpy.analysis.Buffer('a', 'b', '10 Meters')\n"
             "mxd = arcpy.mapping.MapDocument('x.mxd')\narcpy.mapping.ExportToPDF(mxd, 'x.pdf')\n")
plain = path("scripts", "helper.py")
with open(plain, "w", encoding="utf-8") as fh:
    fh.write("print('no GIS here')\n")
pyt = path("scripts", "Tools.pyt")
with open(pyt, "w", encoding="utf-8") as fh:
    fh.write("import arcpy\nclass Toolbox(object):\n    pass\narcpy.management.CopyFeatures('a', 'b')\n")
atbx = path("project", "City tools.atbx")
graph = {"variables": [{"id": "1", "title": "$rc:model.element1", "datatype": {"type": "GPFeatureLayer"},
                        "value": "parcels"},
                       {"id": "3", "title": "$rc:model.element3", "derived": "true", "value": "out"},
                       {"id": "5", "title": "$rc:model.element5", "derived": "true", "value": "out2"}],
         "processes": [{"id": "2", "title": "$rc:model.element2", "system_tool": "analysis.Buffer",
                        "params": {"in_features": {"element_id": "1"}, "buffer_distance_or_field": {"value": "5 Meters"},
                                   "out_feature_class": {"direction": "out", "element_id": "3"}}},
                       {"id": "4", "title": "$rc:model.element4", "system_tool": "management.Append",
                        "params": {"inputs": {"element_id": ["3"]}, "output": {"direction": "out", "element_id": "5"}}}]}
with zipfile.ZipFile(atbx, "w") as z:
    z.writestr("toolbox.content", json.dumps({"displayname": "City tools", "toolsets": {"<root>": {"tools": ["M"]}}}))
    z.writestr("M.tool/tool.content", json.dumps({"type": "ModelTool", "displayname": "$rc:title"}))
    z.writestr("M.tool/tool.content.rc", json.dumps({"map": {"title": "Buffer and append", "model.element1": "parcels",
                                                             "model.element2": "Buffer", "model.element3": "out",
                                                             "model.element4": "Append", "model.element5": "out2"}}))
    z.writestr("M.tool/tool.model", json.dumps(graph))
for skipped in (("node_modules", "pkg", "Hidden.aprx"), (".git", "hooks", "arcpy_hook.py"),
                ("AppData", "Local", "Temp.mxd")):
    with open(path(*skipped), "w", encoding="utf-8") as fh:
        fh.write("import arcpy\n")


def snapshot():
    out_ = {}
    for dp, _dn, fns in os.walk(work):
        for fn in fns:
            p = os.path.join(dp, fn)
            st = os.stat(p)
            out_[p] = (st.st_size, st.st_mtime_ns)
    return out_


before = snapshot()

# ---- finding -------------------------------------------------------------------------------------------------------
found, walked = scanner.find_items([work], exclude=[])
kinds = sorted((k, os.path.relpath(p, work)) for k, p in found)
expected = sorted([("project", os.path.join("old", "Broken.aprx")), ("project", os.path.join("project", "City.aprx")),
                   ("map", os.path.join("project", "Parcels.mapx")), ("map", os.path.join("project", "Old map.mxd")),
                   ("layer", os.path.join("project", "Parcels.lyrx")), ("layer", os.path.join("project", "Lost.lyrx")),
                   ("layout", os.path.join("project", "A3 layout.pagx")),
                   ("toolbox", os.path.join("project", "City tools.atbx")),
                   ("geodatabase", os.path.join("project", "city.gdb")), ("shapefiles", "shapes"),
                   ("script", os.path.join("scripts", "export maps.py")), ("script", os.path.join("scripts", "Tools.pyt"))])
check(kinds == expected, f"every item found with its kind; AppData, node_modules, .git and plain Python skipped "
                         f"({set(kinds) ^ set(expected)})")
check(not any(p.lower().endswith(".gdbtable") for _k, p in found), "geodatabase folders are not entered")

# ---- the scan (fake arcpy analyser so scripts are judged the same everywhere) ----------------------------------------
fake = types.ModuleType("biruni.arcpy_compat.analyse")


def fake_analyse(text):
    calls = []
    for i, line in enumerate(text.splitlines(), 1):
        for name in ("arcpy.analysis.Buffer", "arcpy.mapping.MapDocument", "arcpy.mapping.ExportToPDF",
                     "arcpy.management.CopyFeatures"):
            if name + "(" in line:
                calls.append({"name": name, "line": i,
                              "status": "missing" if "mapping" in name else "supported"})
    missing = [c["name"] for c in calls if c["status"] == "missing"]
    sup = len(calls) - len(missing)
    return {"calls": calls, "supported": sup, "partial": 0, "missing": len(missing),
            "score": int(100 * sup / len(calls)) if calls else 100, "missing_names": missing}


fake.analyse_script = fake_analyse
saved_module = sys.modules.get("biruni.arcpy_compat.analyse")
sys.modules["biruni.arcpy_compat.analyse"] = fake

events = []
t0 = time.monotonic()
result = scanner.scan([work], progress=lambda f, t: events.append((f, t)), exclude=[])
elapsed = time.monotonic() - t0
items = {os.path.relpath(it.path, work): it for it in result.items}
check(len(result.items) == len(expected) and not result.cancelled, f"{len(result.items)} items checked in {elapsed:.1f}s")
check(events and events[-1][0] == 1.0 and any(0 <= f < 1 for f, _t in events), "progress reported up to the end")

city = items[os.path.join("project", "City.aprx")]
c = city.counts
check((c["layers"], c["opened"], c["missing"], c["unsupported"], c["basemaps"]) == (5, 2, 1, 1, 1),
      f"project: 2 layers open, 1 data not found, 1 annotation layer, 1 Esri basemap ({c})")
check(c["symbols_exact"] == 1 and c["symbols_changed"] == 1, "project: one symbol exact, one changed (gradient)")
check(c["layouts"] == 1 and city.status == "needs" and city.score == 50,
      f"project: layout counted, needs work, score 50: 2 of the 4 layers that count open ({city.status}, {city.score})")
check(("layer", "annotation layers are not supported") in city.missing and
      any(cat == "symbology" for cat, _k in city.missing), "project: unsupported layer and symbol change recorded")
mp = items[os.path.join("project", "Parcels.mapx")]
check(mp.status == "ok" and mp.score == 100 and mp.counts["opened"] == 1, "map file: works 100%")
check(items[os.path.join("project", "Parcels.lyrx")].status == "ok", "layer file: works 100%")
lost = items[os.path.join("project", "Lost.lyrx")]
check(lost.status == "needs" and lost.counts["missing"] == 1 and "gone.gdb" in " ".join(lost.details),
      "layer file with missing data: needs work, the missing path is shown")
old = items[os.path.join("project", "Old map.mxd")]
check(old.status == "changes" and old.counts["opened"] == 1 and old.counts["symbols_default"] == 1,
      f"ArcMap document: layer opens, default styles -> works with changes ({old.status}, {old.note})")
check(items[os.path.join("old", "Broken.aprx")].status == "fails", "damaged project: cannot be read")
sc = items[os.path.join("scripts", "export maps.py")]
check(sc.status == "needs" and sc.counts == {"calls": 3, "supported": 1, "partial": 0, "missing": 2}
      and sc.score == 33, f"script judged by the arcpy analyser ({sc.counts}, {sc.score})")
check(items[os.path.join("scripts", "Tools.pyt")].status == "ok", "python toolbox with supported calls runs")
tb = items[os.path.join("project", "City tools.atbx")]
check(tb.status == "needs" and tb.counts["steps"] == 2 and tb.counts["converted_steps"] == 1 and
      ("tool", "management.Append") in tb.missing, f"toolbox: 1 of 2 steps convert, Append missing ({tb.note})")
gdb = items[os.path.join("project", "city.gdb")]
check(gdb.status == "ok" and gdb.counts["tables"] == 2, "file geodatabase opens with its 2 feature classes")
shp = items["shapes"]
check(shp.counts == {"shapefiles": 2, "incomplete": 0, "no_prj": 1} and shp.status == "changes",
      "shapefiles grouped per folder; one without .prj noted")
check(items[os.path.join("project", "A3 layout.pagx")].status == "needs", "layout file: to rebuild")

text = scanner.summary_text(result.items)
check("6 projects, maps and layer files: 2 open 100%" in text and "1 open with changes" in text
      and "2 need work" in text and "1 cannot be read" in text, f"summary sentence for maps ({text})")
check("2 arcpy scripts: 1 run" in text and "1 use missing functions" in text, "summary sentence for scripts")
check("1 toolboxes with 1 models: 1 of 2 steps convert" in text and "1 layout files" in text,
      "summary sentence for toolboxes and layouts")
top = scanner.top_missing(result.items)
keys = [k for _c, k, _f, _t in top]
check("arcpy.mapping.ExportToPDF" in keys and "management.Append" in keys and
      "annotation layers are not supported" in keys, "most missing functions and features ranked")
lay = [t for t in top if t[1] == scanner.tr("page layouts (rebuild them in the print layout)")]
check(lay and lay[0][2] == 2, "the same missing feature in two files counts 2")
check(snapshot() == before, "nothing was added or changed in the scanned folders")

# fallback when the analyser is not available
sys.modules["biruni.arcpy_compat.analyse"] = None   # import fails
res2 = scanner.analyse_script(script)
check(res2.status == "unknown" and res2.counts["calls"] == 3 and "not checked" in res2.note,
      f"without the analyser the arcpy calls are counted and marked unknown ({res2.note})")
if saved_module is not None:
    sys.modules["biruni.arcpy_compat.analyse"] = saved_module
else:
    del sys.modules["biruni.arcpy_compat.analyse"]
real = scanner.analyse_script_text("import arcpy\narcpy.analysis.Buffer('a', 'b', '1 Meters')\n")
check(isinstance(real.get("calls"), list) and len(real["calls"]) >= 1,
      "the real analyser (or the fallback when it is not ready) answers")

# time limit and Stop
slow = scanner.analyse_item("project", aprx, time_limit=0)
check(slow.status == "unknown" and slow.counts["timeout"] >= 3, f"time limit: layers left unchecked ({slow.note})")
calls = []
stopped = scanner.scan([work], cancel=lambda: calls.append(1) or len(calls) > 3, exclude=[])
check(stopped.cancelled and len(stopped.items) < len(expected), "Stop ends the scan early")

# ---- reports -------------------------------------------------------------------------------------------------------
html_path = report.write_html(result, os.path.join(out, "readiness.html"))
with open(html_path, encoding="utf-8") as fh:
    page = fh.read()
check('dir="ltr"' in page and "City.aprx" in page and "arcpy.mapping.ExportToPDF" in page and text in page,
      "HTML report: summary, items and most missing")
check("QGIS" not in page, "no engine name in the report")
xlsx = report.write_excel(result, os.path.join(out, "readiness.xlsx"))
from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(xlsx)
check(len(wb.sheetnames) == 3 and wb.worksheets[1].max_row == len(result.items) + 1,
      f"Excel report: 3 sheets, one row per item ({wb.sheetnames})")
from biruni import i18n  # noqa: E402

i18n.init("ar")
page_ar = report.html_text(result)
check('dir="rtl"' in page_ar and 'lang="ar"' in page_ar, "Arabic HTML report is right to left")
xlsx_ar = report.write_excel(result, os.path.join(out, "readiness_ar.xlsx"))
check(load_workbook(xlsx_ar).worksheets[1].sheet_view.rightToLeft, "Arabic Excel sheets are right to left")
i18n.init("en_US")

# ---- the window ------------------------------------------------------------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.migrate import dialog as mdialog  # noqa: E402


class Bar:
    def __init__(self):
        self.messages = []

    def pushSuccess(self, *a):  # noqa: N802
        self.messages.append(("ok",) + a)

    def pushWarning(self, *a):  # noqa: N802
        self.messages.append(("warn",) + a)


class Iface:
    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.bar = Bar()

    def mainWindow(self):  # noqa: N802
        return self.win

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return self.bar


class Plugin:
    def __init__(self):
        self.iface = Iface()


plugin = Plugin()
dlg = mdialog.open_dialog(plugin)
dlg.folders.list.clear()
dlg.folders.list.addItem(work)
dlg.start()
end = time.monotonic() + 60
while dlg.busy() and time.monotonic() < end:
    pump(30)
pump(50)
check(not dlg.busy() and dlg.table.rowCount() == len(expected), f"window: scan in the background, {dlg.table.rowCount()} rows")
check(dlg.summary.text() == text and dlg.html_button.isEnabled(), "window: summary shown, reports can be exported")
dlg.table.sortByColumn(3, Qt.SortOrder.DescendingOrder)
scores = [int(dlg.table.item(r, 3).text()) for r in range(dlg.table.rowCount())]
check(scores == sorted(scores, reverse=True), "window: the table sorts by score as numbers")
row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 1).text() == "Parcels.mapx")
dlg.table.selectRow(row)
check("Parcels.mapx" in dlg.details.toPlainText(), "window: the selected item's details are shown")
check(dlg.missing.rowCount() == len(scanner.top_missing(result.items, 200)), "window: most missing tab filled")
QgsProject.instance().clear()
dlg.open_row(row)
pump(50)
names = [lyr.name() for lyr in QgsProject.instance().mapLayers().values()]
check(names == ["parcels"], f"window: double-click opens the map in the project ({names})")
tb_row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 1).text() == "City tools.atbx")
dlg.table.selectRow(tb_row)
check(dlg.models_button.isEnabled(), "window: a toolbox can be imported from the list")
convs = mdialog.import_models(plugin, atbx, dlg, show=False)
check(len(convs) == 1 and convs[0].file and convs[0].file.startswith(SETTINGS_DIR),
      "importing the toolbox's models saves them to the user's models folder")
dlg.start()
pump(5)
dlg.stop()
end = time.monotonic() + 30
while dlg.busy() and time.monotonic() < end:
    pump(30)
check(not dlg.busy() and dlg.scan_button.isEnabled(), "window: Stop ends a running scan")
i18n.init("ar")
dlg_ar = mdialog.MigrateDialog(plugin)
check(dlg_ar.layoutDirection() == Qt.LayoutDirection.RightToLeft, "window: Arabic is right to left")
dlg_ar.close()
i18n.init("en_US")
dlg.close()
QgsProject.instance().clear()

# ---- the Processing tool ------------------------------------------------------------------------------------------
res = processing.run("biruni:migrationscan", {"FOLDER": work, "MORE_FOLDERS": "", "TIME_LIMIT": 20,
                                              "HTML": os.path.join(out, "alg.html"),
                                              "EXCEL": os.path.join(out, "alg.xlsx")})
check(res["ITEMS"] == len(expected) and os.path.exists(res["HTML"]) and os.path.exists(res["EXCEL"]),
      f"Processing tool writes both reports ({res['ITEMS']} items)")
check(res["SUMMARY"] == text, "Processing tool summary")
check(snapshot() == before, "after everything, the scanned folders are untouched")

shutil.rmtree(root, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
QgsApplication.processingRegistry().removeProvider(provider)
sys.exit(1 if failures else 0)
