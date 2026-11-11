"""Tests for saving to ArcGIS Pro formats (biruni.arcgis.writer): .lyrx / .mapx / .aprx.

Round trip: a project is built in the engine, written, read back with our own
reader, and layers, renderers, class values/colours, labels and filters are
compared. The CIM keys are checked against real ArcGIS Pro files on this machine
and, when ArcGIS Pro is installed, arcpy opens the files (read-only, temp folder).

Run:  engine\\bin\\python-qgis.bat tests\\test_arcgis_write.py
"""
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

from qgis.core import (Qgis, QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,
                       QgsFeature, QgsFillSymbol,
                       QgsGeometry, QgsGraduatedSymbolRenderer, QgsLinePatternFillSymbolLayer,
                       QgsLineSymbol, QgsMarkerSymbol, QgsPalLayerSettings, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsRendererCategory, QgsRendererRange,
                       QgsRuleBasedLabeling, QgsSimpleLineSymbolLayer, QgsSimpleMarkerSymbolLayer,
                       QgsSingleSymbolRenderer, QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor, QFont

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
from biruni.arcgis import open_arcgis_file  # noqa: E402
from biruni.arcgis.cim_write import cim_color, to_arcade, to_pt  # noqa: E402
from biruni.arcgis.writer import needs_copy, write_aprx, write_lyrx, write_mapx  # noqa: E402

provider = BiruniProvider()  # keep a reference: the registry does not own the Python object
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni_arcgis_write_")
data = os.path.join(tmp, "data")
os.makedirs(data)

# ---- fixtures ----------------------------------------------------------------
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


def fill(lyr, fields, rows):
    for fname, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    for wkt, vals in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        for (fname, _), v in zip(fields, vals):
            f.SetField(fname, v)
        if wkt:
            f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        lyr.CreateFeature(f)


PFIELDS = [("name", ogr.OFTString), ("kind", ogr.OFTString), ("code", ogr.OFTInteger), ("area", ogr.OFTReal)]
PROWS = [(sq(500000 + 120 * i, 2400000), (f"P{i}", "A" if i % 2 else "B", i, 100.0 * i)) for i in range(6)]
gdb = os.path.join(data, "survey.gdb")
ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(gdb)
fill(ds.CreateLayer("parcels", srs, ogr.wkbPolygon), PFIELDS, PROWS)
fill(ds.CreateLayer("roads", srs, ogr.wkbLineString), [("name", ogr.OFTString)],
     [("LINESTRING(500000 2399900, 501000 2399900)", ("Main",))])
fill(ds.CreateLayer("wells", srs, ogr.wkbPoint), [("depth", ogr.OFTReal)], [("POINT(500050 2400050)", (12.5,))])
fill(ds.CreateLayer("owners", None, ogr.wkbNone), [("owner", ogr.OFTString)], [(None, ("Hatem",))])
ds = None
ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(os.path.join(data, "plots.shp"))
fill(ds.CreateLayer("plots", srs, ogr.wkbPolygon), PFIELDS, PROWS)
ds = None
ds = ogr.GetDriverByName("GPKG").CreateDataSource(os.path.join(data, "extra.gpkg"))
fill(ds.CreateLayer("blocks", srs, ogr.wkbPolygon), PFIELDS, PROWS[:3])
ds = None
with open(os.path.join(data, "trees.geojson"), "w", encoding="utf-8") as fh:
    json.dump({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:32637"}},
               "features": [{"type": "Feature", "properties": {"sp": "palm"},
                             "geometry": {"type": "Point", "coordinates": [500200, 2400050]}}]}, fh)
tif = os.path.join(data, "dem.tif")
r = gdal.GetDriverByName("GTiff").Create(tif, 10, 10, 1, gdal.GDT_Float32)
r.SetGeoTransform((500000, 10, 0, 2400100, 0, -10))
r.SetProjection(srs.ExportToWkt())
r.GetRasterBand(1).WriteRaster(0, 0, 10, 10, struct.pack("100f", *[float(v) for v in range(100)]))
r = None


def vl(uri, name, provider="ogr"):
    lyr = QgsVectorLayer(uri, name, provider)
    assert lyr.isValid(), uri
    return lyr


def poly_symbol(fill_color, stroke="#000000", width=0.5):
    return QgsFillSymbol.createSimple({"color": fill_color, "outline_color": stroke, "outline_width": str(width),
                                       "outline_width_unit": "Point"})


prj = QgsProject()
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:32637"))

# parcels: categories + labels + filter + transparency + scale range + alias + hidden field
parcels = vl(f"{gdb}|layername=parcels", "Parcels")
cats = [QgsRendererCategory("A", poly_symbol("#ff0000"), "Kind A"),
        QgsRendererCategory(["B", "C"], poly_symbol("#0000ff"), "Kind B or C"),
        QgsRendererCategory("", poly_symbol("#c8c8c8"), "Other")]
parcels.setRenderer(QgsCategorizedSymbolRenderer("kind", cats))
pal = QgsPalLayerSettings()
pal.fieldName = '"name" || \' (\' || "code" || \')\''
pal.isExpression = True
fmt = QgsTextFormat()
font = QFont("Tahoma")
font.setBold(True)
fmt.setFont(font)
fmt.setSize(12)
fmt.setSizeUnit(Qgis.RenderUnit.Points)
fmt.setColor(QColor(0, 128, 0))
buf = QgsTextBufferSettings()
buf.setEnabled(True)
buf.setSize(1.5)
buf.setSizeUnit(Qgis.RenderUnit.Points)
buf.setColor(QColor("white"))
fmt.setBuffer(buf)
pal.setFormat(fmt)
pal.scaleVisibility = True
pal.minimumScale, pal.maximumScale = 20000, 100
parcels.setLabeling(QgsVectorLayerSimpleLabeling(pal))
parcels.setLabelsEnabled(True)
parcels.setSubsetString('"code" > 1')
parcels.setOpacity(0.6)
parcels.setScaleBasedVisibility(True)
parcels.setMinimumScale(50000)
parcels.setMaximumScale(500)
parcels.setFieldAlias(parcels.fields().lookupField("name"), "Parcel name")
cfg = parcels.attributeTableConfig()
cols = cfg.columns()
for c in cols:
    if c.name == "area":
        c.hidden = True
cfg.setColumns(cols)
parcels.setAttributeTableConfig(cfg)
parcels.setDisplayExpression('"name"')

# plots (shapefile): graduated, hatch fill on a second copy
plots = vl(os.path.join(data, "plots.shp"), "Plots")
plots.setRenderer(QgsGraduatedSymbolRenderer("area", [
    QgsRendererRange(0, 200, poly_symbol("#ffff00"), "small"),
    QgsRendererRange(200, 500, poly_symbol("#ff8000"), "large")]))
hatch = vl(os.path.join(data, "plots.shp"), "Hatched")
hl = QgsLinePatternFillSymbolLayer()
hl.setLineAngle(45)
hl.setDistance(4)
hl.setDistanceUnit(Qgis.RenderUnit.Points)
hl.setSubSymbol(QgsLineSymbol.createSimple({"color": "#000000", "width": "0.5", "width_unit": "Point"}))
hatch.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol([hl])))

# roads: dashed red line with an offset, rule-based labels
roads = vl(f"{gdb}|layername=roads", "Roads")
line = QgsSimpleLineSymbolLayer(QColor("red"), 1.5)
line.setWidthUnit(Qgis.RenderUnit.Points)
line.setUseCustomDashPattern(True)
line.setCustomDashVector([6, 3])
line.setCustomDashPatternUnit(Qgis.RenderUnit.Points)
line.setOffset(-2)
line.setOffsetUnit(Qgis.RenderUnit.Points)
roads.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol([line])))
rl_root = QgsRuleBasedLabeling.Rule(None)
rs = QgsPalLayerSettings()
rs.fieldName = "name"
rs.isExpression = False
rule = QgsRuleBasedLabeling.Rule(rs)
rule.setFilterExpression("\"name\" = 'Main'")
rule.setDescription("Main roads")
rl_root.appendChild(rule)
roads.setLabeling(QgsRuleBasedLabeling(rl_root))
roads.setLabelsEnabled(True)

# wells: blue squares 10 pt with a black outline
wells = vl(f"{gdb}|layername=wells", "Wells")
mk = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Square, 10)
mk.setSizeUnit(Qgis.RenderUnit.Points)
mk.setColor(QColor(0, 0, 255))
mk.setStrokeColor(QColor(0, 0, 0))
wells.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol([mk])))

owners = vl(f"{gdb}|layername=owners", "Owners")
blocks = vl(os.path.join(data, "extra.gpkg") + "|layername=blocks", "Blocks")
trees = vl(os.path.join(data, "trees.geojson"), "Trees")
memory = QgsVectorLayer("Point?crs=EPSG:32637&field=label:string", "Sketch", "memory")
feat = QgsFeature(memory.fields())
feat.setAttributes(["here"])
feat.setGeometry(QgsGeometry.fromWkt("POINT(500300 2400050)"))
memory.dataProvider().addFeatures([feat])
memory.setSubsetString("\"label\" = 'here'")
dem = QgsRasterLayer(tif, "DEM")
check(dem.isValid(), "fixture raster valid")
from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer  # noqa: E402

shader = QgsColorRampShader(0, 99)
shader.setColorRampType(Qgis.ShaderInterpolationMethod.Discrete)
shader.setColorRampItemList([QgsColorRampShader.ColorRampItem(30, QColor(0, 30, 0), "low"),
                             QgsColorRampShader.ColorRampItem(99, QColor(0, 200, 0), "high")])
rsh = QgsRasterShader()
rsh.setRasterShaderFunction(shader)
dem.setRenderer(QgsSingleBandPseudoColorRenderer(dem.dataProvider(), 1, rsh))

for lyr in (parcels, plots, hatch, roads, wells, owners, blocks, trees, memory, dem):
    prj.addMapLayer(lyr, False)
root = prj.layerTreeRoot()
for lyr in (parcels, plots, hatch, roads, wells, owners, blocks, trees, memory):
    root.addLayer(lyr)
grp = root.addGroup("Base")
grp.addLayer(dem)
grp.setItemVisibilityChecked(False)
root.findLayer(hatch.id()).setItemVisibilityChecked(False)

# ---- unit helpers -------------------------------------------------------------
check(abs(to_pt(10, Qgis.RenderUnit.Millimeters) - 28.3465) < 1e-3, "10 mm -> 28.35 pt")
check(cim_color(QColor(10, 20, 30, 128))["values"] == [10, 20, 30, 50.2], "QColor -> CIM RGB with alpha 0-100")
check(to_arcade('"name" || \' (\' || "code" || \')\'') == ('$feature.name + " (" + $feature.code + ")"', True),
      "QGIS concatenation -> Arcade")
check(to_arcade("upper(\"name\")")[0] == "Upper($feature.name)", "upper() -> Upper()")
check(to_arcade('"Arabic name"', True)[0] == '$feature["Arabic name"]', "field with a space -> $feature[\"...\"]")
check(to_arcade("name", False) == ("$feature.name", True), "plain field -> $feature.name")
check(to_arcade("CASE WHEN \"code\" > 2 THEN 'big' ELSE 'small' END")[0]
      == 'When($feature.code > 2, "big", "small")', "CASE -> When()")
e, exact = to_arcade("regexp_replace(\"name\", 'P', 'Q')")
check(e == "$feature.name" and not exact, "untranslatable function -> first field, flagged inexact")
check(needs_copy([parcels, plots, blocks, trees, memory]) == ["Trees", "Sketch"],
      "GeoJSON and memory layers need copying; gdb/shp/gpkg do not")


# ---- lyrx: one layer ----------------------------------------------------------
def read_back(path, **kw):
    p = QgsProject()
    return p, open_arcgis_file(path, p, services=False, **kw)


lyrx = os.path.join(tmp, "out", "parcels.lyrx")
os.makedirs(os.path.dirname(lyrx))
rep = write_lyrx(lyrx, [parcels], prj)
doc = load(lyrx)
check(doc["type"] == "CIMLayerDocument" and len(doc["layerDefinitions"]) == 1 and not rep.skipped,
      "lyrx written with one layer definition")
dc = doc["layerDefinitions"][0]["featureTable"]["dataConnection"]
check(dc["workspaceFactory"] == "FileGDB" and dc["dataset"] == "parcels"
      and dc["workspaceConnectionString"] == "DATABASE=..\\data\\survey.gdb",
      f"file gdb connection relative to the layer file: {dc['workspaceConnectionString']}")
p2, back = read_back(lyrx)
check(len(back.added) == 1 and not back.skipped, "lyrx read back: one layer")
b = back.layers[0]
rend = b.renderer()
bcats = rend.categories() if rend.type() == "categorizedSymbol" else []
check(rend.type() == "categorizedSymbol" and rend.classAttribute() == "kind" and len(bcats) == 3,
      f"categories survive ({rend.type()}, {len(bcats)})")
check([c.value() for c in bcats] == ["A", ["B", "C"], ""], f"category values {[c.value() for c in bcats]}")
check([c.symbol().color().name() for c in bcats] == ["#ff0000", "#0000ff", "#c8c8c8"],
      f"category colours {[c.symbol().color().name() for c in bcats]}")
check([c.label() for c in bcats] == ["Kind A", "Kind B or C", "Other"], "category labels")
stroke = bcats[0].symbol().symbolLayer(1)
check(stroke.layerType() == "SimpleLine" and abs(stroke.width() - 0.5) < 1e-6, "outline width 0.5 pt survives")
check(b.subsetString() == '"code" > 1' and b.featureCount() == 4, f"definition query survives ({b.subsetString()})")
check(abs(b.opacity() - 0.6) < 1e-6, "transparency survives")
check(b.hasScaleBasedVisibility() and b.minimumScale() == 50000 and b.maximumScale() == 500, "scale range survives")
check(b.attributeAlias(b.fields().lookupField("name")) == "Parcel name", "field alias survives")
check([c.name for c in b.attributeTableConfig().columns() if c.hidden and c.name] == ["area"], "hidden field survives")
check(b.displayExpression() == '"name"', "display field survives")
lab = b.labeling().settings() if b.labeling() else None
check(b.labelsEnabled() and lab is not None and lab.isExpression
      and lab.fieldName == '"name" || \' (\' || "code" || \')\'', f"label expression survives ({lab and lab.fieldName})")
lf = lab.format()
check(lf.font().family() == "Tahoma" and lf.font().bold() and lf.size() == 12
      and lf.color().getRgb()[:3] == (0, 128, 0), "label font / size / colour survive")
check(lf.buffer().enabled() and lf.buffer().size() == 1.5 and lf.buffer().color().name() == "#ffffff",
      "label halo survives")
check(lab.scaleVisibility and lab.minimumScale == 20000 and lab.maximumScale == 100, "label scale range survives")

# ---- lyrx: a group ------------------------------------------------------------
glyrx = os.path.join(tmp, "out", "base_group.lyrx")
rep = write_lyrx(glyrx, [grp], prj)
p2, back = read_back(glyrx)
g = p2.layerTreeRoot().findGroup("Base")
check(g is not None and [c.name() for c in g.children()] == ["DEM"] and not g.itemVisibilityChecked(),
      "group lyrx: group, child and visibility survive")
ren = back.layers[0].renderer() if back.layers else None
items = ren.shader().rasterShaderFunction().colorRampItemList() if ren and ren.type() == "singlebandpseudocolor" \
    else []
check([round(i.value) for i in items] == [30, 99] and items[1].color.name() == "#00c800"
      and ren.shader().rasterShaderFunction().colorRampType() == Qgis.ShaderInterpolationMethod.Discrete,
      "raster classes survive")

# ---- mapx without copying ------------------------------------------------------
mapx = os.path.join(tmp, "out", "survey.mapx")
rep = write_mapx(mapx, prj)
why = dict(rep.skipped)
check(set(why) == {"Trees", "Sketch"} and "file geodatabase" in why["Trees"],
      f"data ArcGIS cannot open is reported, not dropped silently ({sorted(why)})")
doc = load(mapx)
m = doc["mapDefinition"]
check(doc["type"] == "CIMMapDocument" and m["spatialReference"]["wkid"] == 32637, "mapx: map with CRS")
check(len(m["standaloneTables"]) == 1 and doc["tableDefinitions"][0]["type"] == "CIMStandaloneTable",
      "mapx: attribute-only layer written as a standalone table")
gp = [d for d in doc["layerDefinitions"] if d["type"] == "CIMSqlQueryDataConnection"
      or (d.get("featureTable") or {}).get("dataConnection", {}).get("workspaceFactory") == "Sql"]
check(len(gp) == 1 and gp[0]["featureTable"]["dataConnection"]["dataset"] == "main.blocks",
      "GeoPackage written as an SQLite workspace (main.blocks)")
p2, back = read_back(mapx)
names = [n.name() for n in p2.layerTreeRoot().children()]
check(names == ["Parcels", "Plots", "Hatched", "Roads", "Wells", "Blocks", "Base", "Owners"],
      f"mapx: layer order and group kept {names}")
by = {lyr.name(): lyr for lyr in back.layers}
check(set(by) == {"Parcels", "Plots", "Hatched", "Roads", "Wells", "Owners", "Blocks", "DEM"}
      and not back.skipped, f"mapx read back: every written layer opens {sorted(by)} {back.skipped}")
check(not p2.layerTreeRoot().findLayer(by["Hatched"].id()).itemVisibilityChecked(), "hidden layer stays hidden")
rngs = by["Plots"].renderer().ranges() if by["Plots"].renderer().type() == "graduatedSymbol" else []
check(len(rngs) == 2 and [(r.lowerValue(), r.upperValue()) for r in rngs] == [(0, 200), (200, 500)]
      and [r.symbol().color().name() for r in rngs] == ["#ffff00", "#ff8000"]
      and [r.label() for r in rngs] == ["small", "large"], "graduated classes: bounds, colours, labels")
hs = by["Hatched"].renderer().symbol().symbolLayer(0)
check(hs.layerType() == "LinePatternFill" and hs.lineAngle() == 45 and hs.distance() == 4, "hatch fill survives")
rs_ = by["Roads"].renderer().symbol().symbolLayer(0)
check(rs_.layerType() == "SimpleLine" and list(rs_.customDashVector()) == [6, 3] and rs_.offset() == -2
      and rs_.color().name() == "#ff0000" and rs_.width() == 1.5, "dashed line, offset, colour and width survive")
rlab = by["Roads"].labeling()
check(isinstance(rlab, QgsRuleBasedLabeling) and rlab.rootRule().children()[0].filterExpression()
      == "\"name\" = 'Main'" and rlab.rootRule().children()[0].settings().fieldName == "name",
      "rule-based label: one class with its where clause")
ws = by["Wells"].renderer().symbol().symbolLayer(0)
check(ws.layerType() == "SimpleMarker" and ws.shape() == Qgis.MarkerShape.Square and ws.size() == 10
      and ws.color().name() == "#0000ff", "square marker 10 pt survives")
check(by["Blocks"].isValid() and by["Blocks"].featureCount() == 3, "GeoPackage layer reopens")
check(by["Owners"].featureCount() == 1 and not by["Owners"].isSpatial(), "table reopens")

# ---- mapx with copying into a file geodatabase -----------------------------------
mapx2 = os.path.join(tmp, "out2", "copied.mapx")
os.makedirs(os.path.dirname(mapx2))
rep = write_mapx(mapx2, prj, copy_data=True)
check(not rep.skipped and dict(rep.copied).keys() == {"Trees", "Sketch"}
      and os.path.isdir(os.path.join(tmp, "out2", "copied_data.gdb")), f"copy into copied_data.gdb ({rep.copied})")
p2, back = read_back(mapx2)
by = {lyr.name(): lyr for lyr in back.layers}
check(by.get("Sketch") is not None and by["Sketch"].featureCount() == 1
      and by["Sketch"].subsetString() == "\"label\" = 'here'", "copied memory layer reopens with its filter")
check(by.get("Trees") is not None and by["Trees"].featureCount() == 1, "copied GeoJSON layer reopens")

# ---- aprx -------------------------------------------------------------------
from qgis.core import (QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemPicture,  # noqa: E402
                       QgsLayoutItemScaleBar, QgsLayoutItemShape, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout,
                       QgsRectangle)
from qgis.PyQt.QtGui import QImage  # noqa: E402

layout = QgsPrintLayout(prj)
layout.initializeDefaults()
layout.setName("Plan")
mp = QgsLayoutItemMap(layout)
layout.addLayoutItem(mp)
mp.attemptMove(QgsLayoutPoint(10, 10))
mp.attemptResize(QgsLayoutSize(180, 150))
mp.zoomToExtent(QgsRectangle(500000, 2399800, 501000, 2400300))
mp.setId("Main map")
lg = QgsLayoutItemLegend(layout)
layout.addLayoutItem(lg)
lg.setLinkedMap(mp)
lg.attemptMove(QgsLayoutPoint(200, 20))
sb = QgsLayoutItemScaleBar(layout)
layout.addLayoutItem(sb)
sb.setLinkedMap(mp)
sb.applyDefaultSize()
sb.attemptMove(QgsLayoutPoint(200, 170))
lbl = QgsLayoutItemLabel(layout)
layout.addLayoutItem(lbl)
lbl.setText("Survey plan")
lbl.adjustSizeToText()
lbl.attemptMove(QgsLayoutPoint(200, 10))
north = QgsLayoutItemPicture(layout)
layout.addLayoutItem(north)
north.setPicturePath(":/images/north_arrows/layout_default_north_arrow.svg")
north.setLinkedMap(mp)
north.attemptMove(QgsLayoutPoint(260, 150))
north.attemptResize(QgsLayoutSize(15, 20))
box = QgsLayoutItemShape(layout)
layout.addLayoutItem(box)
box.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
box.attemptMove(QgsLayoutPoint(195, 5))
box.attemptResize(QgsLayoutSize(95, 200))
box.setSymbol(QgsFillSymbol.createSimple({"color": "255,255,255,0", "outline_color": "#333333"}))
png = os.path.join(data, "logo.png")
img = QImage(16, 16, QImage.Format.Format_ARGB32)
img.fill(QColor("orange"))
img.save(png)
logo = QgsLayoutItemPicture(layout)
layout.addLayoutItem(logo)
logo.setPicturePath(png)
logo.attemptMove(QgsLayoutPoint(200, 120))
logo.attemptResize(QgsLayoutSize(20, 20))
prj.layoutManager().addLayout(layout)

aprx = os.path.join(tmp, "out3", "survey.aprx")
os.makedirs(os.path.dirname(aprx))
rep = write_aprx(aprx, prj, copy_data=True)
check(rep.layouts == ["Plan"] and not rep.skipped, f"aprx written with its layout ({rep.layouts}, {rep.skipped})")
with zipfile.ZipFile(aprx) as z:
    names = z.namelist()
    index = json.loads(z.read("Index.json"))
    gis = json.loads(z.read("GISProject.json"))
    info = z.read("DocumentInfo.xml").decode()
check({"GISProject.json", "Index.json", "DocumentInfo.xml", "map/map.json"} <= set(names)
      and any(n.startswith("Layout/") for n in names), "aprx parts: GISProject, Index, DocumentInfo, map, layout")
check(all(n["FileName"] in names for n in index["Nodes"]) and index["NumberOfNodes"] == len(index["Nodes"])
      and len(index["Nodes"]) == len(names) - 3, "Index.json lists every part and only existing parts")
check([i["itemType"] for i in gis["projectItems"]] == ["FolderConnection", "GDB", "Map", "Layout"]
      and gis["views"][0]["viewableObjectPath"] == "CIMPATH=map/map.json", "GISProject items and map view")
check("<UseRelativePath>true</UseRelativePath>" in info, "relative paths declared")
p2, back = read_back(aprx)
check(len(back.added) == 10 and not back.skipped, f"aprx read back: all 10 layers ({len(back.added)}, {back.skipped})")
check(back.layouts == ["Plan"], f"aprx read back: layout recreated ({back.layouts})")
lay2 = p2.layoutManager().layoutByName("Plan")
kinds = sorted(type(i).__name__ for i in lay2.items() if type(i).__name__.startswith("QgsLayoutItem")
               and type(i).__name__ != "QgsLayoutItemPage") if lay2 else []
check(kinds == ["QgsLayoutItemLabel", "QgsLayoutItemLegend", "QgsLayoutItemMap", "QgsLayoutItemPicture",
                 "QgsLayoutItemPicture", "QgsLayoutItemScaleBar", "QgsLayoutItemShape"], f"layout items survive {kinds}")
check(any(n.startswith("Pictures/") for n in names), "the logo picture is stored inside the project")

# ---- processing algorithm --------------------------------------------------------
ctx = QgsProcessingContext()
ctx.setProject(prj)
out = os.path.join(tmp, "out4", "alg.mapx")
res = processing.run("biruni:exporttoarcgispro", {"FORMAT": 1, "LAYERS": [parcels, wells], "COPY_DATA": False,
                                                  "LAYOUTS": False, "OUTPUT": out},
                     context=ctx, feedback=QgsProcessingFeedback())
p2, back = read_back(res["OUTPUT"])
check(res["WRITTEN"] == 2 and sorted(lyr.name() for lyr in back.layers) == ["Parcels", "Wells"],
      "processing biruni:exporttoarcgispro writes a map file of the chosen layers")
res = processing.run("biruni:exporttoarcgispro", {"FORMAT": 2, "COPY_DATA": True, "OUTPUT":
                                                  os.path.join(tmp, "out4", "all.lyrx")},
                     context=ctx, feedback=QgsProcessingFeedback())
check(res["WRITTEN"] == 10 and res["SKIPPED"] == 0, f"processing: whole-map layer file with copying ({res})")


# ---- schema check against real ArcGIS Pro files -------------------------------
def collect(obj, table):
    if isinstance(obj, dict):
        t = obj.get("type")
        if isinstance(t, str) and t.startswith("CIM"):
            table.setdefault(t, set()).update(obj.keys())
        for v in obj.values():
            collect(v, table)
    elif isinstance(obj, list):
        for v in obj:
            collect(v, table)


real = {}
corpus = (glob.glob(os.path.expanduser(r"~\Documents\ArcGIS\Projects\*\*.aprx"))
          + glob.glob(r"C:\Program Files\ArcGIS\Pro\Resources\LayoutTemplates\en-US\*.pagx")
          + glob.glob(r"C:\Program Files\ArcGIS\Pro\Resources\ArcToolBox\Templates\ExportWebMapTemplates\*.pagx"))
for path in corpus:
    try:
        if path.lower().endswith(".aprx"):
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.endswith(".json"):
                        collect(json.loads(z.read(n).decode("utf-8-sig")), real)
        else:
            collect(load(path), real)
    except (ValueError, OSError, zipfile.BadZipFile):
        continue
if len(real) > 30:
    ours = {}
    with zipfile.ZipFile(aprx) as z:
        for n in z.namelist():
            if n.endswith(".json") and n != "Index.json":
                collect(json.loads(z.read(n)), ours)
    collect(load(mapx), ours)
    collect(load(lyrx), ours)
    # standard CIM keys that the sample files above happen not to use (all written by ArcGIS Pro 3.7
    # itself when the matching option is set, and read by reader.py)
    known = {"CIMFeatureLayer": {"minScale", "maxScale", "transparency"},
             "CIMLabelClass": {"minimumScale", "maximumScale", "whereClause"},
             "CIMTextSymbol": {"haloSymbol"}, "CIMUniqueValueClass": {"editable"},
             "CIMRasterClassifyColorizer": {"bandIndex"}, "CIMLegend": {"items", "makeColumnsSameWidth"},
             "CIMHorizontalLegendItem": {"layer"}}
    for t, keys in known.items():
        real.setdefault(t, set()).update(keys)
    unknown_keys = {t: sorted(keys - real[t] - {"type"}) for t, keys in ours.items() if t in real}
    unknown_keys = {t: k for t, k in unknown_keys.items() if k}
    unseen = sorted(t for t in ours if t not in real)
    print(f"   schema: {len(corpus)} real files, {len(real)} CIM types; ours {len(ours)} types, "
          f"not in the real files: {unseen}")
    check(not unknown_keys, f"every key we write exists in real ArcGIS Pro files {unknown_keys}")
else:
    print("SKIP schema check: no real ArcGIS Pro files on this machine")

# ---- arcpy validation (read-only, optional) ---------------------------------------
PRO_PY = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
BLANK = r"C:\Program Files\ArcGIS\Pro\Resources\ArcToolBox\Services\routingservices\data\Blank.aprx"
ARCPY_SCRIPT = r'''
import json, os, shutil, sys
res = {}
try:
    import arcpy
    print(json.dumps({"product": arcpy.ProductInfo()}), flush=True)
except Exception as e:
    print(json.dumps({"import_error": str(e)}), flush=True); sys.exit(0)
work = sys.argv[1]
def layers(ls):
    out = []
    for l in ls:
        d = {"name": l.name, "group": l.isGroupLayer}
        if l.supports("DATASOURCE"):
            d["broken"] = l.isBroken
        if l.isFeatureLayer:
            d["renderer"] = getattr(getattr(l.symbology, "renderer", None), "type", None)
            d["dq"] = l.definitionQuery
            d["labels"] = l.showLabels
            try:
                d["count"] = int(arcpy.management.GetCount(l)[0])
            except Exception as e:
                d["count_error"] = str(e)[:150]
        out.append(d)
    return out
for p in sys.argv[3:]:
    r = {}
    try:
        ext = os.path.splitext(p)[1].lower()
        if ext == ".aprx":
            a = arcpy.mp.ArcGISProject(p)
            r["maps"] = [{"name": m.name, "layers": layers(m.listLayers()),
                          "tables": [t.name for t in m.listTables()]} for m in a.listMaps()]
            r["layouts"] = []
            for lay in a.listLayouts():
                pdf = os.path.join(work, lay.name + ".pdf")
                lay.exportToPDF(pdf)
                r["layouts"].append({"name": lay.name, "elements": sorted(e.type for e in lay.listElements()),
                                     "pdf_bytes": os.path.getsize(pdf),
                                     "legend_items": [len(e.items) for e in lay.listElements("LEGEND_ELEMENT")],
                                     "legend_height": [e.elementHeight for e in lay.listElements("LEGEND_ELEMENT")]})
            a.saveACopy(os.path.join(work, "resaved.aprx"))
            r["resaved"] = True
        elif ext == ".lyrx":
            r["layers"] = layers(arcpy.mp.LayerFile(p).listLayers())
        elif ext == ".mapx":
            blank = os.path.join(work, "blank_%d.aprx" % len(res))
            shutil.copyfile(sys.argv[2], blank)
            a = arcpy.mp.ArcGISProject(blank)
            m = a.importDocument(p)
            r["maps"] = [{"name": m.name, "layers": layers(m.listLayers()), "tables": [t.name for t in m.listTables()]}]
    except Exception as e:
        r["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    res[p] = r
    print(json.dumps({p: r}), flush=True)
'''
if os.path.isfile(PRO_PY) and os.path.isfile(BLANK) and not os.environ.get("BIRUNI_SKIP_ARCPY"):
    work = tempfile.mkdtemp(prefix="biruni_arcpy_")
    script = os.path.join(work, "validate.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(ARCPY_SCRIPT)
    targets = [aprx, mapx, mapx2, lyrx, glyrx]
    engine = os.path.normcase(os.path.abspath(QgsApplication.prefixPath() + "/../.."))
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PYTHON", "QGIS", "GDAL", "PROJ", "QT_", "O4W", "OSGEO"))}
    env["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                  if p and not os.path.normcase(os.path.abspath(p)).startswith(engine))
    try:
        proc = subprocess.run([PRO_PY, script, work, BLANK] + targets, capture_output=True, text=True,
                              timeout=420, cwd=work, encoding="utf-8", errors="replace", env=env)
        result = {}
        for ln in proc.stdout.strip().splitlines():
            try:
                result.update(json.loads(ln))
            except ValueError:
                continue
        if not result:
            result = {"import_error": "arcpy printed nothing: " + proc.stderr.strip()[-300:]}
    except (subprocess.TimeoutExpired, OSError) as e:
        result = {"import_error": f"arcpy check did not finish: {e}"}
    if "import_error" in result or result.get("product") in (None, "NotInitialized"):
        print(f"SKIP arcpy validation: {result.get('import_error') or 'no ArcGIS Pro licence'}")
    else:
        print("   arcpy:", json.dumps({os.path.basename(k): v for k, v in result.items()}, ensure_ascii=False)[:3000])
        a = result.get(aprx, {})
        check("error" not in a and a.get("resaved"), f"arcpy opens and re-saves the .aprx {a.get('error', '')}")
        lyrs = [x for m in a.get("maps", []) for x in m["layers"]]
        check(len(lyrs) == 10 and not any(x.get("broken") for x in lyrs),
              f"arcpy: 10 map entries, no broken data source ({[x['name'] for x in lyrs if x.get('broken')]})")
        by = {x["name"]: x for x in lyrs}
        check(by.get("Parcels", {}).get("renderer") == "UniqueValueRenderer"
              and by.get("Plots", {}).get("renderer") == "GraduatedColorsRenderer"
              and by.get("Roads", {}).get("renderer") == "SimpleRenderer",
              "arcpy: unique values / graduated colours / simple renderers")
        check(by.get("Parcels", {}).get("dq") == '"code" > 1' and by.get("Parcels", {}).get("count") == 4
              and by.get("Parcels", {}).get("labels"), "arcpy: definition query applied (4 features), labels on")
        check(by.get("Blocks", {}).get("count") == 3 and by.get("Sketch", {}).get("count") == 1,
              "arcpy: GeoPackage and copied layers read their features")
        check(a.get("maps", [{}])[0].get("tables") == ["Owners"], "arcpy: standalone table")
        lays = a.get("layouts", [])
        check(len(lays) == 1 and lays[0]["pdf_bytes"] > 1000 and lays[0]["elements"] == sorted(
            ["MAPFRAME_ELEMENT", "LEGEND_ELEMENT", "MAPSURROUND_ELEMENT", "MAPSURROUND_ELEMENT", "TEXT_ELEMENT",
             "GRAPHIC_ELEMENT", "PICTURE_ELEMENT"]), f"arcpy: every layout element loads and the PDF export works {lays}")
        check(lays and lays[0]["legend_items"] == [9] and lays[0]["legend_height"][0] > 10,
              f"arcpy: legend lists the 9 map layers and has a size {lays and lays[0].get('legend_height')}")
        for p in (mapx, mapx2, lyrx, glyrx):
            r_ = result.get(p, {})
            ls = [x for m in r_.get("maps", []) for x in m["layers"]] + r_.get("layers", [])
            check("error" not in r_ and ls and not any(x.get("broken") for x in ls),
                  f"arcpy opens {os.path.basename(p)} without broken layers {r_.get('error', '')}")
    shutil.rmtree(work, ignore_errors=True)
else:
    print("SKIP arcpy validation: ArcGIS Pro is not installed")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
