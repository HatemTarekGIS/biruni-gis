r"""ModelBuilder import, off screen: a realistic ArcGIS Pro 3 toolbox (.atbx: toolbox.content, one folder per tool
with tool.content / tool.content.rc / tool.model, a script tool and a password-protected model) is read; its model
(Buffer, Clip, Select with %variables%, Add Field, Calculate Field, Make Feature Layer, Select Layer By Location /
Attribute, Append, an iterator, Dissolve, Project, Intersect, Erase, Union, Spatial Join, Feature To Point, Copy
Features, Merge, Delete Field) is converted: each common tool becomes the right native step with its values,
parameters and source data become model inputs, the steps that cannot be converted stay as note boxes and their
results become inputs. The model is saved to the user's models folder, loads back, every step is valid and it runs
on real data with the right result. An ArcMap .tbx (OLE) lists its models and tools. The tool and the report.

Run:  engine\bin\python-qgis.bat tests\test_modelbuilder_import.py
"""
import glob
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp(prefix="biruni_mb_profile_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from osgeo import ogr, osr  # noqa: E402
from qgis.core import QgsProcessingModelAlgorithm, QgsVectorLayer  # noqa: E402

from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.migrate import modelbuilder as mb  # noqa: E402
from biruni.migrate.report import conversion_html, write_conversion_report  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni_mb_")

# ---- data the model reads ------------------------------------------------------------------------------------
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
gpkg = os.path.join(tmp, "data.gpkg")
ds = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
roads = ds.CreateLayer("roads", srs, ogr.wkbLineString)
f = ogr.Feature(roads.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("LINESTRING(500000 2400050,500400 2400050)"))
roads.CreateFeature(f)
parcels = ds.CreateLayer("parcels", srs, ogr.wkbPolygon)
parcels.CreateField(ogr.FieldDefn("name", ogr.OFTString))
parcels.CreateField(ogr.FieldDefn("area", ogr.OFTReal))
for i, (name, area) in enumerate((("A", 50.0), ("B", 150.0), ("A", 300.0), ("C", 400.0))):
    x = 500000 + i * 100
    f = ogr.Feature(parcels.GetLayerDefn())
    f.SetField("name", name)
    f.SetField("area", area)
    f.SetGeometry(ogr.CreateGeometryFromWkt(f"POLYGON(({x} 2400000,{x + 90} 2400000,{x + 90} 2400100,{x} 2400100,"
                                            f"{x} 2400000))"))
    parcels.CreateFeature(f)
f = None
ds = None

MERC = ('PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID['
        '"WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Mercator_Auxiliary_Sphere"],PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",0.0],PARAMETER["Standard_Parallel_1",0.0],PARAMETER["Auxiliary_Sphere_Type",0.0],'
        'UNIT["Meter",1.0]]')


def layer_var(vid, value, catalog, param=None):
    v = {"id": vid, "title": f"$rc:model.element{vid}", "datatype": {"type": "GPFeatureLayer"}, "value": value,
         "value_xml": "<GPFeatureLayer xsi:type='typens:GPFeatureLayer'><Namestring>" + value + "</Namestring>"
                      "<DataElement xsi:type='typens:DEFeatureClass'><CatalogPath>" + catalog +
                      "</CatalogPath></DataElement></GPFeatureLayer>"}
    if param:
        v.update(connection_type="Parameter", param_name=param)
    return v


def out_var(vid, value, param=None, intermediate=True):
    v = {"id": vid, "title": f"$rc:model.element{vid}", "derived": "true",
         "datatype": {"type": "DEFeatureClass"}, "value": value}
    if intermediate:
        v["intermediate"] = "true"
    if param:
        v.update(connection_type="Parameter", param_name=param)
    return v


def proc(pid, tool, **params):
    return {"id": pid, "title": f"$rc:model.element{pid}", "system_tool": tool, "params": params}


def el(eid, direction=None):
    d = {"altered": "true", "element_id": eid}
    if direction:
        d["direction"] = direction
    return d


def val(v):
    return {"altered": "true", "value": v}


variables = [
    layer_var("1", "roads", ".\\data.gpkg\\main.roads", param="Roads"),
    layer_var("2", "parcels", ".\\data.gpkg\\main.parcels"),
    {"id": "3", "title": "$rc:model.element3", "connection_type": "Parameter", "param_name": "Distance",
     "datatype": {"type": "GPLinearUnit"}, "value": "50 Meters"},
    {"id": "4", "title": "$rc:model.element4", "connection_type": "Parameter", "param_name": "MinArea",
     "datatype": {"type": "GPDouble"}, "value": "100"},
    out_var("5", ".\\out.gdb\\roads_Buffer"), out_var("6", ".\\out.gdb\\parcels_Clip"),
    out_var("7", ".\\out.gdb\\parcels_Select"), out_var("8", ".\\out.gdb\\parcels_Select"),
    out_var("9", ".\\out.gdb\\Result", param="Result", intermediate=False),
    out_var("10", "parcels_Layer"), out_var("11", "parcels_Layer"), out_var("12", "parcels"),
    out_var("13", ".\\out.gdb\\parcels_Dissolve", intermediate=False),
    out_var("14", ".\\out.gdb\\Result_Project", intermediate=False), out_var("15", ".\\out.gdb\\Inter"),
    out_var("16", ".\\out.gdb\\Erased"), out_var("17", ".\\out.gdb\\Union", intermediate=False),
    out_var("18", ".\\out.gdb\\Joined", intermediate=False), out_var("19", ".\\out.gdb\\Points", intermediate=False),
    out_var("20", ".\\out.gdb\\Copy", intermediate=False), out_var("21", ".\\out.gdb\\Merged", intermediate=False),
    out_var("22", "parcels_Layer (2)"), out_var("23", ".\\out.gdb\\NoName", intermediate=False),
    out_var("24", "Selected"), out_var("25", "Value"),
]
processes = [
    proc("30", "analysis.Buffer", in_features=el("1"), buffer_distance_or_field=el("3"),
         out_feature_class=el("5", "out"), line_side={"value": "FULL"}, line_end_type={"value": "ROUND"},
         dissolve_option=val("ALL"), method={"value": "PLANAR"}),
    proc("31", "analysis.Clip", in_features=el("2"), clip_features=el("5"), out_feature_class=el("6", "out")),
    proc("32", "analysis.Select", in_features=el("6"), where_clause=val("area > %MinArea%"),
         out_feature_class=el("7", "out")),
    proc("33", "management.AddField", in_table=el("7"), field_name=val("score"), field_type=val("DOUBLE"),
         field_is_nullable={"value": "true"}, out_table=el("8", "out")),
    proc("34", "management.CalculateField", in_table=el("8"), field=val("score"), expression=val("!area! * 2"),
         expression_type={"value": "PYTHON3"}, out_table=el("9", "out"), enforce_domains={"value": "false"}),
    proc("35", "management.MakeFeatureLayer", in_features=el("2"), out_layer=el("10", "out")),
    proc("36", "management.SelectLayerByLocation", in_layer=el(["10"]), overlap_type=val("INTERSECT"),
         select_features=el("1"), selection_type={"value": "NEW_SELECTION"}, out_layer_or_view=el("11", "out"),
         invert_spatial_relationship={"value": "false"}),
    proc("37", "management.Append", inputs=el(["11"]), target=el("2"), schema_type=val("NO_TEST"),
         output=el("12", "out")),
    proc("38", "management.Dissolve", in_features=el("12"), dissolve_field=val("name"),
         out_feature_class=el("13", "out"), multi_part={"value": "MULTI_PART"}),
    proc("39", "mb.IterateFeatureSelection", in_features=el("2"), selection=el("24", "out"), value=el("25", "out")),
    proc("40", "management.Project", in_dataset=el("9"), out_dataset=el("14", "out"), out_coor_system=val(MERC)),
    proc("41", "analysis.Intersect", in_features=el(["2", "5"]), out_feature_class=el("15", "out"),
         join_attributes={"value": "ALL"}, output_type={"value": "INPUT"}),
    proc("42", "analysis.Erase", in_features=el("2"), erase_features=el("5"), out_feature_class=el("16", "out")),
    proc("43", "analysis.Union", in_features=el(["2", "5"]), out_feature_class=el("17", "out")),
    proc("44", "analysis.SpatialJoin", target_features=el("2"), join_features=el(["1", "0"]),
         out_feature_class=el("18", "out"), join_operation={"value": "JOIN_ONE_TO_ONE"},
         join_type={"value": "KEEP_ALL"}, match_option=val("INTERSECT")),
    proc("45", "management.FeatureToPoint", in_features=el("2"), point_location=val("INSIDE"),
         out_feature_class=el("19", "out")),
    proc("46", "management.CopyFeatures", in_features=el("7"), out_feature_class=el("20", "out")),
    proc("47", "management.Merge", inputs=el(["6", "16"]), output=el("21", "out")),
    proc("48", "management.SelectLayerByAttribute", in_layer_or_view=el("10"),
         selection_type={"value": "NEW_SELECTION"}, where_clause=val("[name] = 'A'"),
         out_layer_or_view=el("22", "out")),
    proc("49", "management.DeleteField", in_table=el("22"), drop_field=val("name"), out_table=el("23", "out")),
]
titles = {"1": "Roads", "2": "Parcels", "3": "Distance", "4": "MinArea", "5": "Roads_Buffer", "6": "Parcels_Clip",
          "7": "Parcels_Select", "8": "Parcels_Field", "9": "Result", "10": "Parcels_Layer", "11": "Near roads",
          "12": "Appended parcels", "13": "Dissolved", "14": "Result_Project", "15": "Inter", "16": "Erased",
          "17": "Union", "18": "Joined", "19": "Points", "20": "Copy", "21": "Merged", "22": "Type A",
          "23": "NoName", "24": "Selected", "25": "Value",
          "30": "Buffer", "31": "Clip", "32": "Select", "33": "Add Field", "34": "Calculate Field",
          "35": "Make Feature Layer", "36": "Select Layer By Location", "37": "Append", "38": "Dissolve",
          "39": "Iterate Feature Selection", "40": "Project", "41": "Intersect", "42": "Erase", "43": "Union (2)",
          "44": "Spatial Join", "45": "Feature To Point", "46": "Copy Features", "47": "Merge",
          "48": "Select Layer By Attribute", "49": "Delete Field"}
model_graph = {"version": "1.0", "updated": "2026-10-01 10:00:00", "variables": variables, "processes": processes}
tool_content = {"type": "ModelTool", "displayname": "$rc:title", "app_ver": "13.6", "product": "100",
                "params": {"Roads": {"displayname": "$rc:roads.title", "datatype": {"type": "GPFeatureLayer"}},
                           "Distance": {"displayname": "$rc:distance.title", "datatype": {"type": "GPLinearUnit"},
                                        "value": "50 Meters"},
                           "MinArea": {"displayname": "$rc:minarea.title", "datatype": {"type": "GPDouble"},
                                       "value": "100"},
                           "Result": {"direction": "out", "displayname": "$rc:result.title",
                                      "datatype": {"type": "DEFeatureClass"}}}}
rc = {"map": dict({f"model.element{k}": v for k, v in titles.items()}, title="Parcels near roads",
                  **{"roads.title": "Roads", "distance.title": "Distance", "minarea.title": "MinArea",
                     "result.title": "Result"})}

atbx = os.path.join(tmp, "Planning tools.atbx")
with zipfile.ZipFile(atbx, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("toolbox.content", json.dumps({"version": "1.0", "displayname": "$rc:title", "toolsets": {
        "<root>": {"tools": ["ParcelsNearRoads", "Script", "Locked"]}}}, indent=4))
    z.writestr("toolbox.content.rc", json.dumps({"map": {"title": "Planning tools"}}))
    z.writestr("ParcelsNearRoads.tool/tool.content", json.dumps(tool_content, indent=4))
    z.writestr("ParcelsNearRoads.tool/tool.content.rc", json.dumps(rc, ensure_ascii=False, indent=4))
    z.writestr("ParcelsNearRoads.tool/tool.model", json.dumps(model_graph, indent=4))
    z.writestr("ParcelsNearRoads.tool/tool.model.diagram", json.dumps({"version": "1.0", "scale": "1"}))
    z.writestr("Script.tool/tool.content", json.dumps({"type": "ScriptTool", "displayname": "$rc:title"}))
    z.writestr("Script.tool/tool.content.rc", json.dumps({"map": {"title": "Export sheets"}}))
    z.writestr("Script.tool/tool.script.execute.py", "import arcpy\narcpy.management.CopyFeatures('a', 'b')\n")
    z.writestr("Locked.tool/tool.content", json.dumps({"type": "ModelTool", "displayname": "$rc:title",
                                                       "pw": "00022e68abcdef"}))
    z.writestr("Locked.tool/tool.content.rc", json.dumps({"map": {"title": "Secret model"}}))
    z.writestr("Locked.tool/tool.model", "00022e682f7051727756746d705375616c3547766c")
def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


digest = sha(atbx)

# ---- reading -------------------------------------------------------------------------------------------------
box = mb.read_toolbox(atbx)
check(box.kind == "atbx" and box.title == "Planning tools", f"toolbox title from toolbox.content.rc ({box.title})")
check([t.name for t in box.tools] == ["ParcelsNearRoads", "Script", "Locked"], "tools in toolset order")
check([t.kind for t in box.tools] == ["model", "script", "model"], "models and script tools recognised")
model = box.tools[0]
check(model.title == "Parcels near roads" and len(model.tools) == 20, f"model title and its 20 steps ({len(model.tools)})")
check("arcpy.management.CopyFeatures" in box.tools[1].script, "embedded script tool code read")
check(box.tools[2].protected and box.tools[2].graph is None, "password-protected model recognised")

# ---- helpers -----------------------------------------------------------------------------------------------------
check(mb.normalise_tool("Buffer_analysis") == "analysis.buffer" == mb.normalise_tool("arcpy.analysis.Buffer"),
      "tool names normalised (Buffer_analysis, arcpy.analysis.Buffer)")
check(mb.tool_status("analysis.PairwiseClip") == "mapped" and mb.tool_status("mb.IterateRows") == "model_only"
      and mb.tool_status("management.Append") == "unmapped", "tool status: mapped / model only / unmapped")
check(mb.parse_distance("400 Meters") == (400.0, "meters") and mb.parse_distance("2,5 Kilometers") == (2.5, "kilometers")
      and mb.parse_distance("Shape_Length") is None, "linear units parsed")
expr, exact = mb.python_to_expression('!name! + "_" + str(!id!)')
check(expr.startswith('"name" + \'_\'') and not exact, f"python calculation translated, calls flagged ({expr})")
expr, exact = mb.python_to_expression("!area! * 2")
check(expr == '"area" * 2' and exact, "simple python calculation is exact")
check(mb.sql_to_expression("[kind] = 'B'") == "\"kind\" = 'B'", "ArcMap [field] where clause translated")
check(mb.gis_path(".\\x.gdb\\fds\\roads", tmp) == os.path.join(tmp, "x.gdb") + "|layername=roads",
      "geodatabase feature class path -> data source")

# ---- conversion ----------------------------------------------------------------------------------------------
conv = mb.convert_model(model, atbx)
alg = conv.algorithm
check(conv.error == "" and alg is not None, "model converted")
by_title = {s.title: s for s in conv.steps}
expected = {"Buffer": "native:buffer", "Clip": "native:clip", "Select": "native:extractbyexpression",
            "Add Field": "native:addfieldtoattributestable", "Calculate Field": "native:fieldcalculator",
            "Make Feature Layer": "-", "Select Layer By Location": "native:extractbylocation",
            "Dissolve": "native:dissolve", "Project": "native:reprojectlayer", "Intersect": "native:intersection",
            "Erase": "native:difference", "Union (2)": "native:union", "Spatial Join": "native:joinattributesbylocation",
            "Feature To Point": "native:pointonsurface", "Copy Features": "native:savefeatures",
            "Merge": "native:mergevectorlayers", "Select Layer By Attribute": "native:extractbyexpression",
            "Delete Field": "native:deletecolumn", "Append": "", "Iterate Feature Selection": ""}
wrong = {t: by_title[t].algorithm for t, a in expected.items() if t not in by_title or by_title[t].algorithm != a}
check(not wrong, f"every ArcGIS tool mapped to the right step ({wrong})")
check(sorted(conv.unmapped) == ["management.Append", "mb.IterateFeatureSelection"], f"unmapped reported ({conv.unmapped})")
check(conv.converted == 18 and len(conv.steps) == 20, f"18 of 20 steps converted ({conv.converted})")
inputs = {n: d for n, d, _v in conv.inputs}
check({"roads", "parcels", "distance", "minarea"} <= set(inputs), f"parameters and source data are inputs ({inputs})")
check(any("Append" in d or "management.Append" in d for d in inputs.values()),
      "the result of the step that was not converted became an input")
defs = {p.name(): p for p in alg.parameterDefinitions()}
check(defs["distance"].defaultValue() == 50.0 and defs["minarea"].defaultValue() == 100.0,
      "parameter values kept as defaults (50 Meters -> 50, 100)")
check(defs["parcels"].defaultValue() == os.path.join(tmp, "data.gpkg") + "|layername=parcels",
      f"source data path resolved next to the toolbox ({defs['parcels'].defaultValue()})")
children = {c.description(): c for c in alg.childAlgorithms().values()}
sel = children["Select"].parameterSources()["EXPRESSION"][0].staticValue()
check(sel == "area > @minarea", f"%MinArea% became the model variable @minarea ({sel})")
calc = children["Calculate Field"].parameterSources()
check(calc["FORMULA"][0].staticValue() == '"area" * 2' and calc["FIELD_NAME"][0].staticValue() == "score",
      "Calculate Field: expression and field kept")
buf = children["Buffer"].parameterSources()
check(buf["DISTANCE"][0].parameterName() == "distance" and buf["DISSOLVE"][0].staticValue() is True,
      "Buffer: distance from the model input, dissolve ALL kept")
check(children["Project"].parameterSources()["TARGET_CRS"][0].staticValue() == "EPSG:3857",
      "Project: Esri WKT coordinate system -> EPSG:3857")
loc = children["Select Layer By Location"].parameterSources()
check(loc["INPUT"][0].parameterName() == "parcels" and loc["INTERSECT"][0].parameterName() == "roads",
      "Make Feature Layer without a query: the next step reads the data itself")
check(children["Select Layer By Attribute"].parameterSources()["EXPRESSION"][0].staticValue() == "\"name\" = 'A'",
      "Select Layer By Attribute: where clause translated")
check(children["Delete Field"].parameterSources()["INPUT"][0].outputChildId() ==
      children["Select Layer By Attribute"].childId(), "steps chained through their outputs")
check(len(children["Merge"].parameterSources()["LAYERS"]) == 2, "Merge: both inputs kept")
outs = [o for c in alg.childAlgorithms().values() for o in c.modelOutputs()]
check("Result" in outs and "Dissolved" in outs and "Parcels_Select" not in outs,
      f"model parameter and final results are outputs, intermediate data is not ({outs})")
check(len(alg.groupBoxes()) == 2 and all("Not converted" in b.description() for b in alg.groupBoxes()),
      "steps that are not converted stay as note boxes")
check(any("Append" in b.description() and "target" in b.description() for b in alg.groupBoxes()),
      "the note box keeps the tool's parameters")
check(any("selection becomes a new layer" in n for n in by_title["Select Layer By Attribute"].notes),
      "selection semantics change noted")

# ---- saving into the user's models folder, loading back, validity -------------------------------------------
folder = mb.models_folder()
check(folder.startswith(SETTINGS_DIR), f"the models folder is in the user's profile ({folder})")
convs = mb.convert_toolbox(atbx)
saved = [c.file for c in convs if c.file]
check(len(convs) == 2 and len(saved) == 1 and os.path.dirname(saved[0]) == folder,
      "convert_toolbox saves the readable model to the models folder")
check(convs[1].error and "password" in convs[1].error, "protected model reported, not saved")
again = mb.convert_toolbox(atbx)
check(glob.glob(os.path.join(folder, "*.model3")) == saved and again[0].file == saved[0],
      "importing again replaces the earlier import, no duplicate")
loaded = QgsProcessingModelAlgorithm()
check(loaded.fromFile(saved[0]), "saved .model3 loads back")
invalid = [(cid, loaded.validateChildAlgorithm(cid)[1]) for cid in loaded.childAlgorithms()
           if not loaded.validateChildAlgorithm(cid)[0]]
check(len(loaded.childAlgorithms()) == 17 and not invalid, f"17 steps in the saved model, all valid ({invalid})")
prov = QgsApplication.processingRegistry().providerById("model")
mb.refresh_models()
names = [a.displayName() for a in prov.algorithms()] if prov else []
check("Parcels near roads" in names, f"the model appears in the toolbox ({names})")

# ---- running it ------------------------------------------------------------------------------------------------
params = {"roads": gpkg + "|layername=roads", "parcels": gpkg + "|layername=parcels", "distance": 60,
          "minarea": 100}
placeholder = [n for n, d in inputs.items() if "Append" in d][0]
params[placeholder] = gpkg + "|layername=parcels"
for d in loaded.destinationParameterDefinitions():
    params[d.name()] = "TEMPORARY_OUTPUT"
try:
    res = processing.run(loaded, params)
except Exception as e:  # noqa: BLE001
    res = {}
    print("run error:", e)
key = next((k for k in res if k.lower().endswith("result")), None)
lyr = res.get(key) if key else None
if isinstance(lyr, str):
    lyr = QgsVectorLayer(lyr, "r", "ogr")
rows = sorted((f["area"], f["score"]) for f in lyr.getFeatures()) if lyr else []
check(rows == [(150.0, 300.0), (300.0, 600.0), (400.0, 800.0)],
      f"the converted model runs: clip near roads, area > 100, score = area * 2 ({rows})")
proj = next((res[k] for k in res if k.lower().endswith("result_project")), None)
if isinstance(proj, str):
    proj = QgsVectorLayer(proj, "p", "ogr")
check(proj is not None and proj.crs().authid() == "EPSG:3857", "projected output in EPSG:3857")

# ---- ArcMap .tbx (OLE): models and their tools are listed ---------------------------------------------------------
def compound_file(streams):
    """Minimal OLE compound file (version 3, 512-byte sectors) with top-level streams (as in test_mxd)."""
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
    entries = []

    def entry(name, typ, left, right, child, start, size):
        raw = name.encode("utf-16-le") + b"\0\0"
        return (raw.ljust(64, b"\0") + struct.pack("<HBB3I", len(raw), typ, 1, left, right, child) + b"\0" * 36 +
                struct.pack("<IQ", start, size))
    entries.append(entry("Root Entry", 5, free, free, 1, mini_start, len(mini)))
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


def bstr(s):
    raw = s.encode("utf-16-le") + b"\0\0"
    return struct.pack("<I", len(raw)) + raw + b"\x01\x02"


def process_block(name, display, desc, toolset, toolbox):
    return b"".join(bstr(s) for s in (name, display, desc, toolset, toolbox,
                                      r"c:\program files (x86)\arcgis\desktop10.8\ArcToolbox\Toolboxes"))


tool0 = (bstr("Model1") + bstr("تحديث المخطط") + bstr(r"..\MdDlgContent.xsl") +
         process_block("Buffer", "Buffer", "Buffer features", "Proximity", "Analysis Tools") +
         process_block("CalculateField", "Calculate Field", "Calculate a field", "Fields", "Data Management Tools") +
         process_block("Append", "Append", "Append rows", "General", "Data Management Tools"))
tool1 = bstr("Script1") + bstr("Run script") + bstr(r"D:\scripts\run.py") + b"\0" * 20
tbx = os.path.join(tmp, "Old tools.tbx")
with open(tbx, "wb") as fh:
    fh.write(compound_file({"Tool0": tool0, "Tool1": tool1, "Version": bstr("10.8"),
                            "Contents": bstr("Model1") + bstr("Script1")}))
old = mb.read_toolbox(tbx)
check(old.kind == "tbx" and [t.name for t in old.tools] == ["Model1", "Script1"], "tbx: tools listed")
check(old.tools[0].title == "تحديث المخطط" and old.tools[0].kind == "model", "tbx: Arabic model title read")
check(old.tools[0].tools == ["analysis.Buffer", "management.CalculateField", "management.Append"],
      f"tbx: the model's tools read ({old.tools[0].tools})")
check(old.tools[1].kind == "script" and old.tools[1].script_path.endswith("run.py"), "tbx: script tool recognised")
oc = mb.convert_model(old.tools[0], tbx)
check(oc.algorithm is None and ".atbx" in oc.error and oc.unmapped == ["management.Append"],
      "tbx: not converted, advice to save as .atbx, unmapped tools listed")

# ---- report and the Processing tool ------------------------------------------------------------------------------
page = conversion_html(convs + [oc])
check("Parcels near roads" in page and "management.Append" in page and "native:buffer" in page,
      "conversion report lists steps, tools and what they became")
check("QGIS" not in page, "no engine name in the report")
from biruni import i18n  # noqa: E402

i18n.init("ar")
page_ar = conversion_html(convs)
check('dir="rtl"' in page_ar, "Arabic report is right to left")
i18n.init("en_US")
rep_path = write_conversion_report(convs, os.path.join(tmp, "out", "report.html"))
check(os.path.exists(rep_path), "report written")

out_dir = os.path.join(tmp, "copies")
res = processing.run("biruni:importmodelbuildermodels", {"TOOLBOX": atbx, "TO_MODELS": False, "FOLDER": out_dir,
                                                          "REPORT": os.path.join(tmp, "alg_report.html")})
check(res["MODELS"] == 1 and res["STEPS"] == 18 and res["UNMAPPED"] == 2, f"Processing tool counts ({res})")
check(len(glob.glob(os.path.join(out_dir, "*.model3"))) == 1 and os.path.exists(res["REPORT"]),
      "Processing tool saved the model and the report")
check(sha(atbx) == digest and
      sorted(os.listdir(tmp)) == sorted(["data.gpkg", "Planning tools.atbx", "Old tools.tbx", "out", "copies",
                                          "alg_report.html"]),
      "the toolbox is never changed and nothing is written next to it but our outputs")

# ---- real toolboxes on this machine (read-only; models saved to a temporary folder) ------------------------------
real = []
for root in (os.path.expanduser(r"~\Documents\ArcGIS"), "D:\\"):
    real += glob.glob(os.path.join(root, "**", "*.atbx"), recursive=True)[:20] if os.path.isdir(root) else []
real = real[:25]
if real:
    bad, n_models = [], 0
    real_out = tempfile.mkdtemp(prefix="biruni_mb_real_")
    for p in real:
        try:
            before = os.path.getmtime(p)
            for c in mb.convert_toolbox(p, folder=real_out):
                if c.file:
                    n_models += 1
                    m = QgsProcessingModelAlgorithm()
                    if not m.fromFile(c.file):
                        bad.append(c.file)
            if os.path.getmtime(p) != before:
                bad.append("changed " + p)
        except mb.ToolboxError:
            pass
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p}: {e}")
    check(not bad, f"{len(real)} real toolboxes: {n_models} models converted and load back ({bad[:3]})")
    shutil.rmtree(real_out, ignore_errors=True)
else:
    print("SKIP no real .atbx toolboxes on this machine")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
QgsApplication.processingRegistry().removeProvider(provider)
sys.exit(1 if failures else 0)
