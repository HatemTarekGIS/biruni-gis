"""Tests for opening ArcGIS Pro files (biruni.arcgis) and the File Geodatabase export.

Run:  engine\\bin\\python-qgis.bat tests\\test_arcgis.py
"""
import base64
import glob
import json
import os
import struct
import sys
import tempfile
import urllib.parse
import zipfile

from qgis.core import (Qgis, QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QBuffer, QByteArray
from qgis.PyQt.QtGui import QColor, QImage

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from osgeo import gdal, ogr, osr  # noqa: E402

from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.arcgis import list_maps, open_arcgis_file  # noqa: E402
from biruni.arcgis.cim import Converter, color, translate_expression  # noqa: E402
from biruni.arcgis.reader import ArcGISError  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni_arcgis_")

# ---- fixtures: a file geodatabase, a shapefile and a raster ------------------
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
gdb_path = os.path.join(tmp, "data", "test.gdb")
os.makedirs(os.path.dirname(gdb_path))
ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(gdb_path)


def make(name, gtype, fields, rows):
    lyr = ds.CreateLayer(name, srs if gtype != ogr.wkbNone else None, gtype)
    for fname, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    for wkt, vals in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        for (fname, _), v in zip(fields, vals):
            if v is not None:
                f.SetField(fname, v)
        if wkt:
            f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(f)


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


make("parcels", ogr.wkbPolygon, [("name", ogr.OFTString), ("kind", ogr.OFTString), ("code", ogr.OFTInteger),
                                  ("area", ogr.OFTReal)],
     [(sq(500000 + 120 * i, 2400000), (f"P{i}", "A" if i % 2 else "B", i, 100.0 * i)) for i in range(6)])
make("roads", ogr.wkbLineString, [("name", ogr.OFTString)],
     [("LINESTRING(500000 2399900, 501000 2399900)", ("Main",))])
make("wells", ogr.wkbPoint, [("depth", ogr.OFTReal)], [("POINT(500050 2400050)", (12.5,))])
make("owners", ogr.wkbNone, [("owner", ogr.OFTString)], [(None, ("Hatem",))])
ds = None

shp = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(os.path.join(tmp, "data", "pts.shp"))
lyr = shp.CreateLayer("pts", srs, ogr.wkbPoint)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(500010 2400010)"))
lyr.CreateFeature(f)
shp = None

tif = os.path.join(tmp, "data", "dem.tif")
r = gdal.GetDriverByName("GTiff").Create(tif, 10, 10, 1, gdal.GDT_Float32)
r.SetGeoTransform((500000, 10, 0, 2400100, 0, -10))
r.SetProjection(srs.ExportToWkt())
r.GetRasterBand(1).WriteRaster(0, 0, 10, 10, struct.pack("100f", *[float(v) for v in range(100)]))
r = None

DB = "DATABASE=.\\data\\test.gdb"


def fc(dataset, dtype="esriDTFeatureClass", factory="FileGDB", ws=DB):
    return {"type": "CIMStandardDataConnection", "workspaceConnectionString": ws, "workspaceFactory": factory,
            "dataset": dataset, "datasetType": dtype}


def rgb(r, g, b, a=100):
    return {"type": "CIMRGBColor", "values": [r, g, b, a]}


def solid_fill(c):
    return {"type": "CIMSolidFill", "enable": True, "color": c}


def stroke(c, w=1, effects=None):
    d = {"type": "CIMSolidStroke", "enable": True, "width": w, "color": c}
    if effects:
        d["effects"] = effects
    return d


def poly_sym(*layers):
    return {"type": "CIMSymbolReference", "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": list(layers)}}


def feature_layer(name, dc, renderer, uri=None, **extra):
    d = {"type": "CIMFeatureLayer", "name": name, "uRI": uri or f"CIMPATH=map/{name}.json", "visibility": True,
         "featureTable": {"type": "CIMFeatureTable", "dataConnection": dc}, "renderer": renderer}
    d.update(extra)
    return d


def write_lyrx(name, layers, top=None):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "CIMLayerDocument", "version": "3.2.0",
                   "layers": top or [lyr["uRI"] for lyr in layers], "layerDefinitions": layers}, fh)
    return path


def open_one(path, **kw):
    prj = QgsProject()
    rep = open_arcgis_file(path, prj, services=False, **kw)
    return prj, rep


# ---- colours ---------------------------------------------------------------
check(color(rgb(10, 20, 30, 50)).getRgb() == (10, 20, 30, 128), "RGB colour with 50% alpha")
check(color({"type": "CIMHSVColor", "values": [0, 100, 100, 100]}).getRgb()[:3] == (255, 0, 0), "HSV colour -> red")
check(color({"type": "CIMCMYKColor", "values": [0, 100, 100, 0, 100]}).getRgb()[:3] == (255, 0, 0), "CMYK colour -> red")
lab = color({"type": "CIMLABColor", "values": [50, 0, 0, 100]}).getRgb()
check(lab[0] == lab[1] == lab[2] and 115 <= lab[0] <= 122, f"LAB L50 -> mid grey {lab[:3]}")
check(color({"type": "CIMHSVColor", "values": [-1, 0, 100, 100]}).getRgb()[:3] == (255, 255, 255),
      "HSV with hue -1 (ArcGIS grey) -> white")
check(color(None, "#123456").name() == "#123456", "missing colour -> default")

# ---- expressions -------------------------------------------------------------
check(translate_expression("$feature.NAME", "Arcade")[:2] == ("NAME", False), "Arcade lone field -> field name")
e, is_expr, exact = translate_expression('$feature.name + " (" + $feature["code"] + ")"', "Arcade")
check(is_expr and exact and e == '"name" || \' (\' || "code" || \')\'', f"Arcade concatenation -> {e}")
e, is_expr, _ = translate_expression("[name] & vbNewLine & [code]", "VBScript")
check(is_expr and e == '"name" || \'\\n\' || "code"', f"VBScript & vbNewLine -> {e}")
e, is_expr, _ = translate_expression("Upper($feature.name)", "Arcade")
check(e == 'upper("name")', f"Arcade Upper -> {e}")
e, is_expr, exact = translate_expression("def FindLabel([name]):\n  return [name]", "Python")
check(e == "name" and not exact, "Python code block -> first field, flagged inexact")
e, is_expr, exact = translate_expression("var x = $feature.code; return x * 2", "Arcade")
check(e == "code" and not exact, "Arcade script -> first field, flagged inexact")
check(translate_expression("[code] * 2", "Python")[0] == '"code" * 2', "numeric + kept as arithmetic")

# ---- simple renderer, transparency, scales, filter, aliases, labels -----------
p = write_lyrx("simple.lyrx", [feature_layer(
    "Parcels", fc("parcels"),
    {"type": "CIMSimpleRenderer", "symbol": poly_sym(stroke(rgb(0, 0, 255), 2), solid_fill(rgb(255, 0, 0)))},
    transparency=40, minScale=50000, maxScale=500, labelVisibility=True,
    featureTable={"type": "CIMFeatureTable", "dataConnection": fc("parcels"), "definitionExpression": "code > 1",
                  "displayField": "name",
                  "fieldDescriptions": [{"type": "CIMFieldDescription", "fieldName": "name", "alias": "Parcel name",
                                         "visible": True},
                                        {"type": "CIMFieldDescription", "fieldName": "area", "alias": "area",
                                         "visible": False}]},
    labelClasses=[{"type": "CIMLabelClass", "expression": '$feature.name + " (" + $feature.code + ")"',
                   "expressionEngine": "Arcade", "visibility": True,
                   "textSymbol": {"type": "CIMSymbolReference", "symbol": {
                       "type": "CIMTextSymbol", "fontFamilyName": "Tahoma", "fontStyleName": "Bold", "height": 12,
                       "haloSize": 1.5, "haloSymbol": {"type": "CIMPolygonSymbol",
                                                       "symbolLayers": [solid_fill(rgb(255, 255, 255))]},
                       "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [solid_fill(rgb(0, 128, 0))]}}}}])])
prj, rep = open_one(p)
check(len(rep.added) == 1 and not rep.skipped, "lyrx: one layer added")
lyr = rep.layers[0]
sym = lyr.renderer().symbol()
check(lyr.renderer().type() == "singleSymbol" and sym.symbolLayerCount() == 2, "simple renderer: fill + outline layers")
check(sym.symbolLayer(0).color().getRgb()[:3] == (255, 0, 0), "fill colour red (drawn first)")
check(sym.symbolLayer(1).color().getRgb()[:3] == (0, 0, 255) and sym.symbolLayer(1).width() == 2,
      "outline blue 2 pt on top")
check(abs(lyr.opacity() - 0.6) < 1e-6, "transparency 40% -> opacity 0.6")
check(lyr.hasScaleBasedVisibility() and lyr.minimumScale() == 50000 and lyr.maximumScale() == 500, "scale range")
check(lyr.subsetString() == "code > 1" and lyr.featureCount() == 4, "definition query -> 4 of 6 parcels")
check(lyr.attributeAlias(lyr.fields().lookupField("name")) == "Parcel name", "field alias")
hidden = [c.name for c in lyr.attributeTableConfig().columns() if c.hidden and c.name]
check(hidden == ["area"], f"hidden field {hidden}")
lab = lyr.labeling().settings()
check(lyr.labelsEnabled() and lab.isExpression and '"name"' in lab.fieldName, f"label expression {lab.fieldName}")
fmt = lab.format()
check(fmt.size() == 12 and fmt.font().bold() and fmt.color().getRgb()[:3] == (0, 128, 0) and fmt.buffer().enabled(),
      "label font 12 bold green with halo")
check(lyr.displayExpression() == '"name"', "display field")
check(lyr.customProperty("biruni/arcgis_source") == p, "source file remembered on the layer")

# ---- unique values: multi-field, list values, default, HSV --------------------
uv = {"type": "CIMUniqueValueRenderer", "fields": ["kind", "code"], "useDefaultSymbol": True,
      "defaultSymbol": poly_sym(solid_fill(rgb(200, 200, 200))), "defaultLabel": "Other",
      "groups": [{"type": "CIMUniqueValueGroup", "classes": [
          {"type": "CIMUniqueValueClass", "label": "B0", "symbol": poly_sym(solid_fill(
              {"type": "CIMHSVColor", "values": [120, 100, 100, 100]})),
           "values": [{"type": "CIMUniqueValue", "fieldValues": ["B", "0"]}]},
          {"type": "CIMUniqueValueClass", "label": "A1 or A3", "symbol": poly_sym(solid_fill(rgb(0, 0, 255))),
           "values": [{"type": "CIMUniqueValue", "fieldValues": ["A", "1"]},
                      {"type": "CIMUniqueValue", "fieldValues": ["A", "3"]}]}]}]}
prj, rep = open_one(write_lyrx("unique.lyrx", [feature_layer("U", fc("parcels"), uv)]))
rend = rep.layers[0].renderer()
cats = rend.categories()
check(rend.type() == "categorizedSymbol" and len(cats) == 3, "unique values -> 3 categories (incl. default)")
check(cats[0].value() == "B,0" and cats[0].symbol().color().getRgb()[:3] == (0, 255, 0), "multi-field value + HSV")
check(cats[1].value() == ["A,1", "A,3"], f"class with two values -> list {cats[1].value()}")
check(cats[2].value() == "" and cats[2].label() == "Other", "default symbol -> all other values")
ctx_feat = next(rep.layers[0].getFeatures("code = 0"))
from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils  # noqa: E402
ec = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(rep.layers[0]))
ec.setFeature(ctx_feat)
check(QgsExpression(rend.classAttribute()).evaluate(ec) == "B,0", "concat expression matches ArcGIS value strings")

# ---- class breaks -----------------------------------------------------------
cb = {"type": "CIMClassBreaksRenderer", "field": "area", "minimumBreak": 0,
      "breaks": [{"type": "CIMClassBreak", "upperBound": 200, "label": "small",
                  "symbol": poly_sym(solid_fill(rgb(255, 255, 0)))},
                 {"type": "CIMClassBreak", "upperBound": 500, "symbol": poly_sym(solid_fill(rgb(255, 0, 0)))}]}
prj, rep = open_one(write_lyrx("breaks.lyrx", [feature_layer("C", fc("parcels"), cb)]))
rend = rep.layers[0].renderer()
rngs = rend.ranges()
check(rend.type() == "graduatedSymbol" and len(rngs) == 2, "class breaks -> 2 ranges")
check(rngs[0].lowerValue() == 0 and rngs[0].upperValue() == 200 and rngs[1].lowerValue() == 200
      and rngs[0].label() == "small" and rngs[1].label() == "200 - 500", "range bounds and labels")

# ---- lines: dashes, offset, markers at ends ---------------------------------
arrow = {"type": "CIMVectorMarker", "enable": True, "size": 8,
         "markerPlacement": {"type": "CIMMarkerPlacementAtExtremities", "extremityPlacement": "JustEnd"},
         "markerGraphics": [{"type": "CIMMarkerGraphic", "geometry": {"rings": [[[-1, -1], [1, 0], [-1, 1], [-1, -1]]]},
                             "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [solid_fill(rgb(0, 0, 0))]}}]}
line = {"type": "CIMSimpleRenderer", "symbol": {"type": "CIMSymbolReference", "symbol": {
    "type": "CIMLineSymbol", "symbolLayers": [arrow, stroke(rgb(255, 0, 0), 1.5, [
        {"type": "CIMGeometricEffectDashes", "dashTemplate": [6, 3]},
        {"type": "CIMGeometricEffectOffset", "offset": 2}])]}}}
prj, rep = open_one(write_lyrx("line.lyrx", [feature_layer("L", fc("roads"), line)]))
sym = rep.layers[0].renderer().symbol()
st, ml = sym.symbolLayer(0), sym.symbolLayer(1)
check(st.layerType() == "SimpleLine" and st.useCustomDashPattern() and list(st.customDashVector()) == [6, 3],
      "dash template")
check(st.offset() == -2, "line offset")
check(ml.layerType() == "MarkerLine" and ml.placements() == Qgis.MarkerLinePlacement.LastVertex,
      "arrow marker at the end of the line")
check(ml.subSymbol().symbolLayer(0).shape() == Qgis.MarkerShape.Triangle, "3-point marker ring -> triangle")

# ---- point markers -----------------------------------------------------------
png = QImage(4, 4, QImage.Format.Format_ARGB32)
png.fill(QColor("red"))
buf = QBuffer()
buf.open(QBuffer.OpenModeFlag.WriteOnly)
png.save(buf, "PNG")
b64 = base64.b64encode(bytes(buf.data())).decode()


def vm(geom, fill=rgb(0, 128, 255), size=10):
    return {"type": "CIMVectorMarker", "enable": True, "size": size,
            "markerGraphics": [{"type": "CIMMarkerGraphic", "geometry": geom, "symbol": {
                "type": "CIMPolygonSymbol", "symbolLayers": [stroke(rgb(0, 0, 0), 0.5), solid_fill(fill)]}}]}


markers = [
    ("circle", vm({"curveRings": [[[0, 2], {"a": [[0, 2], [0, 0], 0, 1]}]]}), Qgis.MarkerShape.Circle),
    ("square", vm({"rings": [[[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]]]}), Qgis.MarkerShape.Square),
    ("diamond", vm({"rings": [[[0, -1], [-1, 0], [0, 1], [1, 0], [0, -1]]]}), Qgis.MarkerShape.Diamond),
    ("cross", vm({"paths": [[[-1, 0], [1, 0]], [[0, -1], [0, 1]]]}), Qgis.MarkerShape.Cross),
]
for label, marker, shape in markers:
    ren = {"type": "CIMSimpleRenderer", "symbol": {"type": "CIMSymbolReference", "symbol": {
        "type": "CIMPointSymbol", "symbolLayers": [marker]}}}
    prj, rep = open_one(write_lyrx(f"m_{label}.lyrx", [feature_layer("W", fc("wells"), ren)]))
    sl = rep.layers[0].renderer().symbol().symbolLayer(0)
    check(sl.shape() == shape and sl.size() == 10 and sl.sizeUnit() == Qgis.RenderUnit.Points, f"{label} marker")
conv = Converter()
pic = conv.marker_symbol({"type": "CIMPointSymbol", "symbolLayers": [
    {"type": "CIMPictureMarker", "enable": True, "size": 12, "url": "data:image/png;base64," + b64}]})
check(pic.symbolLayer(0).layerType() == "RasterMarker" and pic.symbolLayer(0).path().startswith("base64:"),
      "picture marker with embedded PNG")
chm = conv.marker_symbol({"type": "CIMPointSymbol", "symbolLayers": [
    {"type": "CIMCharacterMarker", "enable": True, "size": 9, "fontFamilyName": "No Such Esri Font", "characterIndex": 33,
     "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [solid_fill(rgb(255, 0, 0))]}}]})
check(chm.symbolLayer(0).layerType() == "SimpleMarker" and any("not installed" in w for w in conv.warnings),
      "missing marker font -> circle + warning")
chm = Converter().marker_symbol({"type": "CIMPointSymbol", "symbolLayers": [
    {"type": "CIMCharacterMarker", "enable": True, "size": 9, "fontFamilyName": "Arial", "characterIndex": 65,
     "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [solid_fill(rgb(255, 0, 0))]}}]})
check(chm.symbolLayer(0).layerType() == "FontMarker", "installed font -> font marker")

# ---- hatch fill and marker fill --------------------------------------------
hatch = Converter().fill_symbol({"type": "CIMPolygonSymbol", "symbolLayers": [
    {"type": "CIMHatchFill", "enable": True, "rotation": 45, "separation": 4,
     "lineSymbol": {"type": "CIMLineSymbol", "symbolLayers": [stroke(rgb(0, 0, 0), 0.5)]}}]})
check(hatch.symbolLayer(0).layerType() == "LinePatternFill" and hatch.symbolLayer(0).lineAngle() == 45
      and hatch.symbolLayer(0).distance() == 4, "hatch fill -> line pattern 45 deg / 4 pt")
dots = Converter().fill_symbol({"type": "CIMPolygonSymbol", "symbolLayers": [dict(
    vm({"curveRings": [[[0, 1], {"a": [[0, 1], [0, 0], 0, 1]}]]}),
    markerPlacement={"type": "CIMMarkerPlacementInsidePolygon", "stepX": 8, "stepY": 8})]})
check(dots.symbolLayer(0).layerType() == "PointPatternFill", "marker fill -> point pattern")

# ---- group layers, order, visibility, missing data, basemap, web tiles ---------
g_layers = [
    {"type": "CIMGroupLayer", "name": "Group", "uRI": "CIMPATH=map/group.json", "visibility": False,
     "layers": ["CIMPATH=map/a.json", "CIMPATH=map/b.json"]},
    feature_layer("A", fc("wells"), {"type": "CIMSimpleRenderer"}, uri="CIMPATH=map/a.json"),
    feature_layer("B", fc("pts", factory="Shapefile", ws="DATABASE=.\\data"), {"type": "CIMSimpleRenderer"},
                  uri="CIMPATH=map/b.json", visibility=False),
    feature_layer("Gone", fc("nothing", ws="DATABASE=.\\data\\missing.gdb"), {}, uri="CIMPATH=map/gone.json"),
    {"type": "CIMTiledServiceLayer", "name": "World Imagery", "uRI": "CIMPATH=map/img.json",
     "layerType": "BasemapBackground",
     "serviceConnection": {"type": "CIMAGSServiceConnection",
                           "url": "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer"}},
    {"type": "CIMTiledServiceLayer", "name": "OSM tiles", "uRI": "CIMPATH=map/osm.json",
     "serviceConnection": {"type": "CIMStandardServiceConnection",
                           "url": "https://tile.openstreetmap.org/{level}/{col}/{row}.png"}},
    {"type": "CIMAnnotationLayer", "name": "Anno", "uRI": "CIMPATH=map/anno.json"},
]
p = write_lyrx("group.lyrx", g_layers, top=["CIMPATH=map/group.json", "CIMPATH=map/gone.json",
                                            "CIMPATH=map/img.json", "CIMPATH=map/osm.json", "CIMPATH=map/anno.json"])
prj = QgsProject()
existing = QgsVectorLayer("Point?crs=EPSG:4326", "existing", "memory")
prj.addMapLayer(existing)
rep = open_arcgis_file(p, prj, services=False)
root = prj.layerTreeRoot()
names = [n.name() for n in root.children()]
check(names == ["Group", "OSM tiles", "existing"], f"layer file inserted at the top, in order {names}")
grp = root.findGroup("Group")
check([c.name() for c in grp.children()] == ["A", "B"], "group keeps child order")
check(not grp.itemVisibilityChecked() and not grp.children()[1].itemVisibilityChecked()
      and grp.children()[0].itemVisibilityChecked(), "visibility of group and layers")
why = dict(rep.skipped)
check("data not found" in why.get("Gone", ""), "missing data reported")
check("Esri basemap" in why.get("World Imagery", ""), "Esri basemap skipped with reason")
check("annotation" in why.get("Anno", ""), "annotation layer reported as unsupported")
osm = prj.mapLayersByName("OSM tiles")[0]
check("{z}/{x}/{y}" in urllib.parse.unquote(osm.source()), "web tile template {level}/{col}/{row} -> {z}/{x}/{y}")
check(QgsProject().mapLayers() == {}, "sanity")

# ---- rasters ---------------------------------------------------------------
RASTER = {"type": "CIMStandardDataConnection", "workspaceConnectionString": "DATABASE=.\\data",
          "workspaceFactory": "Raster", "dataset": "dem.tif", "datasetType": "esriDTAny"}


def raster_lyrx(name, colorizer):
    return write_lyrx(name + ".lyrx", [{"type": "CIMRasterLayer", "name": name, "uRI": f"CIMPATH=map/{name}.json",
                              "visibility": True, "dataConnection": RASTER, "colorizer": colorizer}])


prj, rep = open_one(raster_lyrx("stretch", {
    "type": "CIMRasterStretchColorizer", "stretchType": "MinimumMaximum",
    "colorRamp": {"type": "CIMLinearContinuousColorRamp", "fromColor": rgb(255, 0, 0), "toColor": rgb(0, 255, 0)}}))
ren = rep.layers[0].renderer()
check(ren.type() == "singlebandpseudocolor" and ren.classificationMin() == 0 and ren.classificationMax() == 99,
      "stretch with colour ramp -> pseudocolour 0..99")
prj, rep = open_one(raster_lyrx("gray", {
    "type": "CIMRasterStretchColorizer", "stretchType": "MinimumMaximum",
    "stretchStats": {"type": "StatsHistogram", "min": 10, "max": 50},
    "colorRamp": {"type": "CIMPolarContinuousColorRamp", "fromColor": {"type": "CIMHSVColor", "values": [-1, 0, 0, 100]},
                  "toColor": {"type": "CIMHSVColor", "values": [-1, 0, 100, 100]}}}))
ren = rep.layers[0].renderer()
ce = ren.contrastEnhancement()
check(ren.type() == "singlebandgray" and ce.minimumValue() == 10 and ce.maximumValue() == 50,
      "black-to-white stretch -> grey 10..50 from saved statistics")
prj, rep = open_one(raster_lyrx("classify", {
    "type": "CIMRasterClassifyColorizer",
    "classBreaks": [{"type": "CIMRasterClassBreak", "upperBound": b, "label": f"<={b}", "color": rgb(0, b, 0)}
                    for b in (10, 50, 99)]}))
ren = rep.layers[0].renderer()
items = ren.shader().rasterShaderFunction().colorRampItemList()
check(ren.type() == "singlebandpseudocolor" and [i.value for i in items] == [10, 50, 99]
      and ren.shader().rasterShaderFunction().colorRampType() == Qgis.ShaderInterpolationMethod.Discrete,
      "classify colorizer -> discrete classes")
prj, rep = open_one(raster_lyrx("unique", {
    "type": "CIMRasterUniqueValueColorizer",
    "groups": [{"type": "CIMRasterUniqueValueGroup", "classes": [
        {"type": "CIMRasterUniqueValueClass", "values": ["1"], "label": "one", "color": rgb(255, 0, 0)},
        {"type": "CIMRasterUniqueValueClass", "values": ["2"], "label": "two", "color": rgb(0, 0, 255)}]}]}))
ren = rep.layers[0].renderer()
check(ren.type() == "paletted" and [c.value for c in ren.classes()] == [1, 2], "unique-value raster -> paletted")

# ---- .mapx and .aprx -------------------------------------------------------
map_def = {"type": "CIMMap", "name": "Survey", "uRI": "CIMPATH=map/map.json",
           "layers": ["CIMPATH=map/parcels.json", "CIMPATH=map/wells.json"],
           "standaloneTables": ["CIMPATH=map/owners.json"],
           "spatialReference": {"wkid": 32637, "latestWkid": 32637},
           "defaultExtent": {"xmin": 500000, "ymin": 2400000, "xmax": 501000, "ymax": 2400500,
                             "spatialReference": {"wkid": 32637}}}
defs = [feature_layer("parcels", fc("parcels"), {"type": "CIMSimpleRenderer", "symbol": poly_sym(
    solid_fill(rgb(1, 2, 3)))}, uri="CIMPATH=map/parcels.json"),
        feature_layer("wells", fc("wells"), {"type": "CIMSimpleRenderer"}, uri="CIMPATH=map/wells.json"),
        {"type": "CIMStandaloneTable", "name": "owners", "uRI": "CIMPATH=map/owners.json",
         "dataConnection": fc("owners", "esriDTTable")}]
mapx = os.path.join(tmp, "survey.mapx")
with open(mapx, "w", encoding="utf-8") as fh:
    json.dump({"type": "CIMMapDocument", "mapDefinition": map_def, "layerDefinitions": defs[:2],
               "tableDefinitions": defs[2:]}, fh)
check(list_maps(mapx) == ["Survey"], "mapx: list_maps")
prj, rep = open_one(mapx)
check(rep.maps == ["Survey"] and len(rep.added) == 3, f"mapx: 2 layers + 1 table added ({rep.added})")
check(prj.crs().authid() == "EPSG:32637" and rep.extent is not None and rep.extent.width() == 1000,
      "mapx: map CRS and extent applied to an empty project")
check([n.name() for n in prj.layerTreeRoot().children()] == ["parcels", "wells", "owners"],
      "mapx into empty project: layers at the root, ArcGIS order")

aprx = os.path.join(tmp, "proj.aprx")
map2 = dict(map_def, name="Second", uRI="CIMPATH=map2/map.json", layers=["CIMPATH=map/wells.json"],
            standaloneTables=[])
with zipfile.ZipFile(aprx, "w") as z:
    z.writestr("map/map.json", json.dumps(map_def))
    z.writestr("map2/map.json", json.dumps(map2))
    for d in defs:
        z.writestr(d["uRI"][8:], json.dumps(d))
    z.writestr("GISProject.json", json.dumps({"type": "CIMGISProject", "views": [
        {"type": "CIMMapView", "viewableObjectPath": "CIMPATH=map2/map.json"}]}))
check(list_maps(aprx) == ["Second", "Survey"], f"aprx: active map listed first {list_maps(aprx)}")
prj, rep = open_one(aprx)
check(rep.maps == ["Second"] and len(rep.added) == 1, "aprx: default opens the active map")
prj, rep = open_one(aprx, map_name="*")
groups = [n.name() for n in prj.layerTreeRoot().children()]
check(sorted(groups) == ["Second", "Survey"] and len(rep.added) == 4, f"aprx: all maps, one group each {groups}")

old = os.path.join(tmp, "old.aprx")
with zipfile.ZipFile(old, "w") as z:
    z.writestr("Map/Map.xml", "<CIMMap/>")
try:
    open_one(old)
    check(False, "old XML aprx raises ArcGISError")
except ArcGISError as e:
    check("1.x" in str(e), "old XML aprx raises a clear ArcGISError")
try:
    open_one(os.path.join(tmp, "data", "pts.shp"))
    check(False, "non-ArcGIS file rejected")
except ArcGISError:
    check(True, "non-ArcGIS file rejected")

# ---- packages (ZIP) ---------------------------------------------------------
pkg = os.path.join(tmp, "pkg", "survey.mpkx")
os.makedirs(os.path.dirname(pkg))
with zipfile.ZipFile(pkg, "w") as z:
    z.write(mapx, "p30/survey.mapx")
    for root_dir, _dirs, files in os.walk(gdb_path):
        for fn in files:
            z.write(os.path.join(root_dir, fn), os.path.join("p30", "data", "test.gdb", fn))
prj, rep = open_one(pkg)
check(len(rep.added) == 3 and os.path.isdir(os.path.join(tmp, "pkg", "survey_unpacked")),
      "zip map package unpacked next to it and opened")

# ---- File Geodatabase export -------------------------------------------------
ctx = QgsProcessingContext()
proj = QgsProject.instance()
ctx.setProject(proj)
mem = QgsVectorLayer("Polygon?crs=EPSG:32637&field=OBJECTID:integer&field=name:string", "1 قطع أراضي", "memory")
feat = QgsFeature(mem.fields())
feat.setAttributes([7, "x"])
feat.setGeometry(QgsGeometry.fromWkt(sq(0, 0)))
mem.dataProvider().addFeatures([feat])
pts = QgsVectorLayer("Point?crs=EPSG:32637&field=name:string", "wells", "memory")
f2 = QgsFeature(pts.fields())
f2.setAttributes(["w"])
f2.setGeometry(QgsGeometry.fromWkt("POINT(1 1)"))
pts.dataProvider().addFeatures([f2])
proj.addMapLayers([mem, pts])
outdir = os.path.join(tmp, "export")
for attempt in (1, 2):  # second run overwrites the existing geodatabase
    res = processing.run("biruni:exportlayers", {"LAYERS": [mem, pts], "FORMAT": 7, "SELECTED": False,
                                                 "FOLDER": outdir}, context=ctx, feedback=QgsProcessingFeedback())
    gdb = ogr.Open(os.path.join(outdir, "export.gdb"))
    names = sorted(gdb.GetLayer(i).GetName() for i in range(gdb.GetLayerCount())) if gdb else []
    counts = [gdb.GetLayerByName(n).GetFeatureCount() for n in names] if gdb else []
    check(res["COUNT"] == 2 and names == ["L_1_قطع_أراضي", "wells"] and counts == [1, 1],
          f"File Geodatabase export run {attempt}: {names} {counts}")
    gdb = None

# ---- the real ArcGIS Pro files on this machine (read-only) ------------------------
real = sorted(set(glob.glob(os.path.expanduser(r"~\Documents\ArcGIS\Projects\*\*.aprx"))
                  + glob.glob(os.path.expanduser(r"~\Documents\ArcGIS\Packages\*\p30\*.aprx"))
                  + glob.glob(os.path.expanduser(r"~\Documents\ArcGIS\Packages\*\p30\*.lyrx"))))
if real:
    total_added = total_skipped = errors = 0
    no_reason = []
    for path in real:
        try:
            for name in list_maps(path) or [None]:
                prj, rep = open_one(path, map_name=name) if name else open_one(path)
                total_added += len(rep.added)
                total_skipped += len(rep.skipped)
                no_reason += [n for n, why in rep.skipped if not why]
        except Exception as e:  # noqa: BLE001
            errors += 1
            print("   ERROR", path, e)
    print(f"   real files: {len(real)} files, {total_added} layers added, {total_skipped} skipped")
    check(errors == 0, f"all {len(real)} real ArcGIS files open without errors")
    check(total_added >= 50 and not no_reason, "real files: layers resolved and every skip has a reason")
else:
    print("SKIP no ArcGIS Pro files on this machine")

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
