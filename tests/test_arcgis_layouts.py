"""Tests for ArcGIS Pro layouts (biruni.arcgis.layouts): CIMLayout in .pagx / .aprx <-> print layouts.

Run:  engine\\bin\\python-qgis.bat tests\\test_arcgis_layouts.py
"""
import base64
import glob
import json
import os
import subprocess
import sys
import tempfile
import zipfile

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsFillSymbol, QgsLayoutItemLabel,
                       QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemMapGrid, QgsLayoutItemPicture,
                       QgsLayoutItemPolyline, QgsLayoutItemScaleBar, QgsLayoutItemShape, QgsLayoutPoint,
                       QgsLayoutSize, QgsPrintLayout, QgsProject, QgsRectangle, QgsTextFormat, QgsVectorLayer)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from qgis.PyQt.QtGui import QColor, QFont, QImage, QPolygonF

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
from osgeo import ogr, osr  # noqa: E402

from biruni.arcgis import open_arcgis_file  # noqa: E402
from biruni.arcgis.layouts import LayoutReader  # noqa: E402
from biruni.arcgis.writer import layout_to_pagx, write_aprx  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=0.01):
    return abs(a - b) <= tol


def items_of(layout, cls):
    return [i for i in layout.items() if isinstance(i, cls)]


tmp = tempfile.mkdtemp(prefix="biruni_arcgis_layouts_")
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
gdb = os.path.join(tmp, "data", "plan.gdb")
os.makedirs(os.path.dirname(gdb))
ds = ogr.GetDriverByName("OpenFileGDB").CreateDataSource(gdb)
for name, gtype, wkt in (("parcels", ogr.wkbPolygon, "POLYGON((500000 2400000,500100 2400000,500100 2400100,"
                                                      "500000 2400100,500000 2400000))"),
                         ("wells", ogr.wkbPoint, "POINT(500050 2400050)")):
    lyr = ds.CreateLayer(name, srs, gtype)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
    lyr.CreateFeature(f)
ds = None

img = QImage(8, 8, QImage.Format.Format_ARGB32)
img.fill(QColor("red"))
ba = QByteArray()
buf = QBuffer(ba)
buf.open(QIODevice.OpenModeFlag.WriteOnly)
img.save(buf, "PNG")
PNG64 = base64.b64encode(bytes(ba)).decode()


def rgb(r, g, b, a=100):
    return {"type": "CIMRGBColor", "values": [r, g, b, a]}


def ring(x0, y0, x1, y1):
    return {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}


def text_sym(size=10, bold=False, h="Left", v="Bottom"):
    return {"type": "CIMSymbolReference", "symbol": {
        "type": "CIMTextSymbol", "fontFamilyName": "Tahoma", "fontStyleName": "Bold" if bold else "Regular",
        "height": size, "horizontalAlignment": h, "verticalAlignment": v,
        "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [
            {"type": "CIMSolidFill", "enable": True, "color": rgb(0, 0, 0)}]}}}


def line_frame(c=rgb(0, 0, 0)):
    return {"type": "CIMGraphicFrame", "borderSymbol": {"type": "CIMSymbolReference", "symbol": {
        "type": "CIMLineSymbol", "symbolLayers": [{"type": "CIMSolidStroke", "enable": True, "width": 1, "color": c}]}}}


def fc(name):
    return {"type": "CIMFeatureLayer", "name": name, "uRI": f"CIMPATH=map/{name}.json", "visibility": True,
            "featureTable": {"type": "CIMFeatureTable", "dataConnection": {
                "type": "CIMStandardDataConnection", "workspaceConnectionString": "DATABASE=.\\data\\plan.gdb",
                "workspaceFactory": "FileGDB", "dataset": name, "datasetType": "esriDTFeatureClass"}},
            "renderer": {"type": "CIMSimpleRenderer"}}


elements = [  # bottom-most first, as ArcGIS Pro stores them
    {"type": "CIMGraphicElement", "name": "Border", "visible": True, "graphic": {
        "type": "CIMPolygonGraphic", "polygon": ring(0.5, 0.5, 29.2, 20.5),
        "symbol": {"type": "CIMSymbolReference", "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [
            {"type": "CIMSolidStroke", "enable": True, "width": 2, "color": rgb(0, 0, 255)}]}}}},
    {"type": "CIMMapFrame", "name": "Main Map", "visible": True, "frame": ring(1, 3, 20, 20),
     "graphicFrame": line_frame(), "uRI": "CIMPATH=map/map.json",
     "view": {"type": "CIMMapView", "viewableObjectPath": "CIMPATH=map/map.json",
              "camera": {"type": "CIMViewCamera", "x": 500350, "y": 2400050, "scale": 5000}},
     "grids": [{"type": "CIMMeasuredGrid", "name": "UTM grid", "isVisible": True, "gridLines": [
         {"type": "CIMGridLine", "gridLineOrientation": "NorthSouth", "pattern": {"interval": 200}},
         {"type": "CIMGridLine", "gridLineOrientation": "EastWest", "pattern": {"interval": 250}}]}]},
    {"type": "CIMLegend", "name": "Legend", "visible": True, "frame": ring(21, 14, 28, 19), "mapFrame": "Main Map",
     "showTitle": True, "title": "Key", "titleSymbol": text_sym(14), "columns": 1,
     "items": [{"type": "CIMHorizontalLegendItem", "name": "parcels", "layer": "CIMPATH=map/parcels.json",
                "isVisible": True}]},
    {"type": "CIMMarkerNorthArrow", "name": "North Arrow", "visible": True, "frame": ring(24.7, 9.3, 25.3, 10.7),
     "mapFrame": "Main Map", "northType": "TrueNorth"},
    {"type": "CIMScaleLine", "name": "Scale Bar", "visible": True, "frame": ring(21, 4, 28, 5.2),
     "mapFrame": "Main Map", "units": {"uwkid": 9001}, "divisions": 3, "subdivisions": 2,
     "labelSymbol": text_sym(8)},
    {"type": "CIMDoubleFillScaleBar", "name": "Double Bar", "visible": True, "frame": ring(21, 2, 28, 3),
     "mapFrame": "Main Map", "style": "DoubleAlternating", "units": {"uwkid": 9036}},
    {"type": "CIMGraphicElement", "name": "Title", "visible": True, "graphic": {
        "type": "CIMTextGraphic", "symbol": text_sym(18, True),
        "text": 'Plan <BOL>A</BOL> <dyn type="date" format=""/>', "shape": {"x": 5, "y": 20.5}}},
    {"type": "CIMGraphicElement", "name": "Logo", "visible": True, "graphic": {
        "type": "CIMPictureGraphic", "referenceURI": "CIMPATH=Pictures/logo.png",
        "box": {"xmin": 22, "ymin": 5.5, "xmax": 25, "ymax": 8.5}}},
    {"type": "CIMGraphicElement", "name": "Circle", "visible": True, "graphic": {
        "type": "CIMPolygonGraphic", "polygon": {"curveRings": [[[26, 7], {"a": [[26, 7], [26, 6], 0, 1]}]]},
        "symbol": {"type": "CIMSymbolReference", "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [
            {"type": "CIMSolidFill", "enable": True, "color": rgb(255, 255, 0)}]}}}},
    {"type": "CIMGraphicElement", "name": "Rule", "visible": False, "graphic": {
        "type": "CIMLineGraphic", "line": {"paths": [[[1, 2], [20, 2]]]},
        "symbol": {"type": "CIMSymbolReference", "symbol": {"type": "CIMLineSymbol", "symbolLayers": [
            {"type": "CIMSolidStroke", "enable": True, "width": 1.5, "color": rgb(255, 0, 0)}]}}}},
    {"type": "CIMGroupElement", "name": "Notes", "visible": True, "elements": [
        {"type": "CIMGraphicElement", "name": "Note 1", "graphic": {
            "type": "CIMTextGraphic", "symbol": text_sym(9), "text": "Drawn by Hatem", "shape": {"x": 21, "y": 1.5}}},
        {"type": "CIMGraphicElement", "name": "Note 2", "graphic": {
            "type": "CIMTextGraphic", "symbol": text_sym(9), "text": "Sheet 1", "shape": {"x": 21, "y": 1}}}]},
    {"type": "CIMTableFrame", "name": "Attribute Table", "visible": True, "frame": ring(1, 1, 5, 2)},
]
pagx_doc = {"type": "CIMLayoutDocument", "version": "3.0.0",
            "binaryReferences": [{"type": "CIMBinaryReference", "uRI": "CIMPATH=Pictures/logo.png", "data": PNG64}],
            "mapDefinitions": [{"type": "CIMMap", "name": "Plan map", "uRI": "CIMPATH=map/map.json",
                                "layers": ["CIMPATH=map/parcels.json", "CIMPATH=map/wells.json"],
                                "spatialReference": {"wkid": 32637}}],
            "layerDefinitions": [fc("parcels"), fc("wells")],
            "layoutDefinition": {"type": "CIMLayout", "name": "Plan A4", "uRI": "CIMPATH=layout/layout.json",
                                 "page": {"type": "CIMPage", "width": 29.7, "height": 21, "units": {"uwkid": 1033}},
                                 "elements": elements}}
pagx = os.path.join(tmp, "plan.pagx")
with open(pagx, "w", encoding="utf-8") as fh:
    json.dump(pagx_doc, fh)

# ---- .pagx into an empty project: layers + layout ------------------------------
prj = QgsProject()
rep = open_arcgis_file(pagx, prj, services=False)
check(len(rep.added) == 2 and rep.layouts == ["Plan A4"], f"pagx: 2 layers and the layout added ({rep.layouts})")
lay = prj.layoutManager().layoutByName("Plan A4")
page = lay.pageCollection().page(0)
check(near(page.pageSize().width(), 297) and near(page.pageSize().height(), 210), "page size 29.7 x 21 cm -> A4 mm")
maps = items_of(lay, QgsLayoutItemMap)
mp = maps[0] if maps else None
check(len(maps) == 1 and mp.id() == "Main Map", "one map frame, named")
pos, size = mp.positionWithUnits(), mp.sizeWithUnits()
check(near(pos.x(), 10) and near(pos.y(), 10) and near(size.width(), 190) and near(size.height(), 170),
      f"map frame placed from page cm, y flipped ({pos.x()}, {pos.y()}, {size.width()}, {size.height()})")
c = mp.extent().center()
check(near(mp.scale(), 5000, 0.5) and near(c.x(), 500350, 0.1) and near(c.y(), 2400050, 0.1),
      f"map frame scale 1:5000 and centre from the camera ({mp.scale():.1f})")
check(mp.crs().authid() == "EPSG:32637", "map frame uses the map's coordinate system")
grids = mp.grids().asList()
check(len(grids) == 1 and grids[0].intervalX() == 200 and grids[0].intervalY() == 250
      and grids[0].crs().authid() == "EPSG:32637", "measured grid with its intervals")
check(mp.frameEnabled(), "map frame border kept")
legs = items_of(lay, QgsLayoutItemLegend)
leg = legs[0] if legs else None
names = [n.name() for n in leg.model().rootGroup().findLayers()] if leg else []
check(leg is not None and leg.linkedMap() is mp and leg.title() == "Key" and names == ["parcels"],
      f"legend linked to the map, titled, only its listed layers {names}")
pics = items_of(lay, QgsLayoutItemPicture)
north = [p for p in pics if p.linkedMap() is mp]
check(len(north) == 1 and north[0].picturePath().endswith(".svg")
      and north[0].northMode() == QgsLayoutItemPicture.NorthMode.TrueNorth, "north arrow linked to the map")
logo = [p for p in pics if p.picturePath().startswith("base64:")]
check(len(logo) == 1 and near(logo[0].positionWithUnits().x(), 220) and near(logo[0].sizeWithUnits().width(), 30),
      "picture from the file's embedded image, placed by its box")
bars = {b.id(): b for b in items_of(lay, QgsLayoutItemScaleBar)}
sb = bars.get("Scale Bar")
check(sb is not None and sb.linkedMap() is mp and sb.style() == "Line Ticks Up"
      and sb.units() == Qgis.DistanceUnit.Meters and sb.numberOfSegments() == 3, "scale line: map, units, divisions")
check(bars.get("Double Bar") is not None and bars["Double Bar"].style() == "Double Box"
      and bars["Double Bar"].units() == Qgis.DistanceUnit.Kilometers, "double alternating bar -> Double Box, km")
labels = {lb.id(): lb for lb in items_of(lay, QgsLayoutItemLabel)}
title = labels.get("Title")
check(title is not None and title.text().startswith("Plan A [% format_date(now()"),
      f"text: formatting tags removed, date tag -> expression ({title and title.text()})")
check(title is not None and title.textFormat().size() == 18 and title.textFormat().font().bold(), "text font 18 bold")
ll = title.pagePositionWithUnits() if title else None
check(title is not None and near(title.positionWithUnits().x(), 50) and near(title.positionWithUnits().y(), 5),
      "text anchored at its point (left / bottom)")
shapes = {s.id(): s for s in items_of(lay, QgsLayoutItemShape)}
check(shapes.get("Border") is not None and shapes["Border"].shapeType() == QgsLayoutItemShape.Shape.Rectangle
      and shapes["Border"].symbol().symbolLayer(0).color().name() == "#0000ff", "rectangle with its outline colour")
check(shapes.get("Circle") is not None and shapes["Circle"].shapeType() == QgsLayoutItemShape.Shape.Ellipse,
      "curved polygon -> ellipse")
lines = items_of(lay, QgsLayoutItemPolyline)
check(len(lines) == 1 and len(lines[0].nodes()) == 2 and not lines[0].isVisible(), "line graphic, hidden")
check(shapes["Border"].zValue() < mp.zValue() < title.zValue(), "drawing order kept (border < map < title)")
from qgis.core import QgsLayoutItemGroup  # noqa: E402

groups = items_of(lay, QgsLayoutItemGroup)
check(len(groups) == 1 and groups[0].id() == "Notes" and len(groups[0].items()) == 2, "group element -> item group")
check(any("CIMTableFrame" in w for w in rep.warnings), "table frame reported as not converted")

# ---- .pagx into a project that already has layers --------------------------------
prj2 = QgsProject()
prj2.addMapLayer(QgsVectorLayer(f"{gdb}|layername=wells", "mine", "ogr"))
rep2 = open_arcgis_file(pagx, prj2, services=False)
check(not rep2.added and rep2.layouts == ["Plan A4"] and len(prj2.mapLayers()) == 1,
      "pagx into a working project: only the layout is added")
rep3 = open_arcgis_file(pagx, prj2, services=False)
check(rep3.layouts == ["Plan A4 (2)"], "a second import gets a new layout name")

# ---- unknown page units, dynamic text ------------------------------------------------
reader = LayoutReader(QgsProject(), {}, {}, lambda u: None, lambda w: None)
lay_in = reader.build({"type": "CIMLayout", "name": "Letter", "page": {"width": 11, "height": 8.5,
                                                                        "units": {"uwkid": 109008}}})
check(near(lay_in.pageCollection().page(0).pageSize().width(), 279.4), "inch pages converted to millimetres")
txt = reader.text_content('<dyn type="mapFrame" name="Map Frame" property="scale" preStr="1:"/>')
check(txt == "1:[% format_number(map_get(item_variables('Map Frame'), 'map_scale'), 0) %]",
      f"dynamic scale text -> expression ({txt})")

# ---- the layout templates installed with ArcGIS Pro (read-only) -------------------------
templates = sorted(glob.glob(r"C:\Program Files\ArcGIS\Pro\Resources\LayoutTemplates\en-US\*.pagx")
                   + glob.glob(r"C:\Program Files\ArcGIS\Pro\Resources\ArcToolBox\Templates\ExportWebMapTemplates"
                               r"\*.pagx"))
if templates:
    ok = with_map = 0
    for path in templates:
        p = QgsProject()
        try:
            r = open_arcgis_file(path, p, services=False)
            lt = p.layoutManager().layoutByName(r.layouts[0]) if r.layouts else None
            if lt is not None:
                ok += 1
                with_map += bool(items_of(lt, QgsLayoutItemMap))
        except Exception as e:  # noqa: BLE001
            print("   ERROR", path, e)
    print(f"   Pro templates: {len(templates)} files, {ok} layouts built, {with_map} with a map frame")
    check(ok == len(templates) and with_map == len(templates), "every ArcGIS Pro layout template converts")
else:
    print("SKIP ArcGIS Pro layout templates: ArcGIS Pro is not installed")

# ---- print layout -> aprx / pagx -> print layout ------------------------------------------
src = QgsProject()
src.setCrs(QgsCoordinateReferenceSystem("EPSG:32637"))
parcels = QgsVectorLayer(f"{gdb}|layername=parcels", "Parcels", "ogr")
src.addMapLayer(parcels)
out_lay = QgsPrintLayout(src)
out_lay.initializeDefaults()
out_lay.setName("Export test")
m = QgsLayoutItemMap(out_lay)
out_lay.addLayoutItem(m)
m.attemptMove(QgsLayoutPoint(12, 15))
m.attemptResize(QgsLayoutSize(160, 120))
m.zoomToExtent(QgsRectangle(499900, 2399950, 500200, 2400150))
m.setScale(2500)
m.setId("Map 1")
m.setFrameEnabled(True)
g = QgsLayoutItemMapGrid("Grid", m)
g.setIntervalX(100)
g.setIntervalY(100)
m.grids().addGrid(g)
lg = QgsLayoutItemLegend(out_lay)
out_lay.addLayoutItem(lg)
lg.setLinkedMap(m)
lg.setTitle("Legend title")
lg.attemptMove(QgsLayoutPoint(180, 20))
for style, y in (("Single Box", 150), ("Double Box", 165), ("Line Ticks Middle", 180)):
    s = QgsLayoutItemScaleBar(out_lay)
    out_lay.addLayoutItem(s)
    s.setLinkedMap(m)
    s.applyDefaultSize()
    s.setStyle(style)
    s.setId(style)
    s.attemptMove(QgsLayoutPoint(180, y))
t = QgsLayoutItemLabel(out_lay)
out_lay.addLayoutItem(t)
t.setText("مخطط الموقع")
tf = QgsTextFormat()
fnt = QFont("Tahoma")
fnt.setBold(True)
tf.setFont(fnt)
tf.setSize(20)
tf.setColor(QColor(200, 0, 0))
t.setTextFormat(tf)
t.adjustSizeToText()
t.attemptMove(QgsLayoutPoint(20, 5))
t.setId("Heading")
rect = QgsLayoutItemShape(out_lay)
out_lay.addLayoutItem(rect)
rect.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
rect.attemptMove(QgsLayoutPoint(5, 140))
rect.attemptResize(QgsLayoutSize(50, 20))
rect.setSymbol(QgsFillSymbol.createSimple({"color": "#00ff00", "outline_color": "#000000"}))
rect.setId("Box")
pl = QgsLayoutItemPolyline(QPolygonF([QPointF(10, 200), QPointF(100, 200), QPointF(100, 190)]), out_lay)
out_lay.addLayoutItem(pl)
pl.setId("Leader")
png_path = os.path.join(tmp, "data", "logo.png")
img.save(png_path)
pc = QgsLayoutItemPicture(out_lay)
out_lay.addLayoutItem(pc)
pc.setPicturePath(png_path)
pc.attemptMove(QgsLayoutPoint(250, 180))
pc.attemptResize(QgsLayoutSize(20, 20))
pc.setId("Logo")
src.layoutManager().addLayout(out_lay)

aprx = os.path.join(tmp, "out", "layouts.aprx")
os.makedirs(os.path.dirname(aprx))
wrep = write_aprx(aprx, src)
check(wrep.layouts == ["Export test"], f"layout written into the aprx ({wrep.warnings})")
with zipfile.ZipFile(aprx) as z:
    cim = json.loads(z.read([n for n in z.namelist() if n.startswith("Layout/")][0]))
types = [e["type"] for e in cim["elements"]]
check(types[0] == "CIMMapFrame" and "CIMDoubleFillScaleBar" in types and "CIMScaleLine" in types
      and types.count("CIMGraphicElement") == 4, f"CIM element types, bottom-most first {types}")
check(cim["page"]["units"] == {"uwkid": 1025} and near(cim["page"]["width"], 297), "page written in millimetres")

back = QgsProject()
brep = open_arcgis_file(aprx, back, services=False)
bl = back.layoutManager().layoutByName("Export test")
check(bl is not None, "layout read back from the aprx")
bm = items_of(bl, QgsLayoutItemMap)[0]
check(near(bm.positionWithUnits().x(), 12) and near(bm.positionWithUnits().y(), 15)
      and near(bm.sizeWithUnits().width(), 160) and near(bm.sizeWithUnits().height(), 120),
      "map frame position and size survive")
check(near(bm.scale(), 2500, 0.01) and near(bm.extent().center().x(), m.extent().center().x(), 0.01),
      f"map scale and centre survive ({bm.scale():.3f})")
check(bm.frameEnabled() and [gg.intervalX() for gg in bm.grids().asList()] == [100], "map border and grid survive")
bleg = items_of(bl, QgsLayoutItemLegend)
check(len(bleg) == 1 and bleg[0].title() == "Legend title" and bleg[0].linkedMap() is bm, "legend survives")
bbars = {s.id(): s.style() for s in items_of(bl, QgsLayoutItemScaleBar)}
check(bbars == {"Single Box": "Single Box", "Double Box": "Double Box", "Line Ticks Middle": "Line Ticks Up"},
      f"scale bar styles survive {bbars}")
blab = {x.id(): x for x in items_of(bl, QgsLayoutItemLabel)}.get("Heading")
check(blab is not None and blab.text() == "مخطط الموقع" and blab.textFormat().size() == 20
      and blab.textFormat().font().bold() and blab.textFormat().color().name() == "#c80000",
      "Arabic text, size, bold and colour survive")
check(blab is not None and near(blab.positionWithUnits().x(), t.positionWithUnits().x(), 0.5)
      and near(blab.positionWithUnits().y(), t.positionWithUnits().y(), 0.5), "text position survives")
bbox = {x.id(): x for x in items_of(bl, QgsLayoutItemShape)}.get("Box")
check(bbox is not None and bbox.shapeType() == QgsLayoutItemShape.Shape.Rectangle
      and near(bbox.positionWithUnits().x(), 5) and near(bbox.sizeWithUnits().width(), 50)
      and bbox.symbol().color().name() == "#00ff00", "rectangle, size and fill survive")
bpl = items_of(bl, QgsLayoutItemPolyline)
check(len(bpl) == 1 and len(bpl[0].nodes()) == 3, "polyline survives")
bpics = [p for p in items_of(bl, QgsLayoutItemPicture) if p.linkedMap() is None]
check(len(bpics) == 1 and near(bpics[0].sizeWithUnits().width(), 20), "picture survives")

pagx_out = os.path.join(tmp, "out", "export.pagx")
prep = layout_to_pagx(pagx_out, out_lay, src)
p3 = QgsProject()
r3 = open_arcgis_file(pagx_out, p3, services=False)
check(r3.layouts == ["Export test"] and len(r3.added) == 1, "pagx written by us reopens with its map layer")

# ---- arcpy: ArcGIS Pro imports the .pagx and the layouts of the .aprx (optional) -------------------
PRO_PY = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
BLANK = r"C:\Program Files\ArcGIS\Pro\Resources\ArcToolBox\Services\routingservices\data\Blank.aprx"
SCRIPT = r'''
import json, os, shutil, sys
try:
    import arcpy
except Exception as e:
    print(json.dumps({"import_error": str(e)})); sys.exit(0)
work, blank, aprx, pagx = sys.argv[1:5]
out = {"product": arcpy.ProductInfo()}
try:
    a = arcpy.mp.ArcGISProject(aprx)
    lay = a.listLayouts()[0]
    out["aprx"] = sorted(e.type for e in lay.listElements())
    lay.exportToPNG(os.path.join(work, "aprx_layout.png"), resolution=60)
    out["aprx_png"] = os.path.getsize(os.path.join(work, "aprx_layout.png"))
except Exception as e:
    out["aprx_error"] = str(e)[:300]
try:
    b = os.path.join(work, "blank.aprx")
    shutil.copyfile(blank, b)
    p = arcpy.mp.ArcGISProject(b)
    lay = p.importDocument(pagx)
    out["pagx"] = sorted(e.type for e in lay.listElements())
except Exception as e:
    out["pagx_error"] = str(e)[:300]
print(json.dumps(out), flush=True)
'''
if os.path.isfile(PRO_PY) and os.path.isfile(BLANK) and not os.environ.get("BIRUNI_SKIP_ARCPY"):
    work = tempfile.mkdtemp(prefix="biruni_arcpy_layout_")
    script = os.path.join(work, "check.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(SCRIPT)
    engine = os.path.normcase(os.path.abspath(QgsApplication.prefixPath() + "/../.."))
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PYTHON", "QGIS", "GDAL", "PROJ", "QT_", "O4W", "OSGEO"))}
    env["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                  if p and not os.path.normcase(os.path.abspath(p)).startswith(engine))
    try:
        proc = subprocess.run([PRO_PY, script, work, BLANK, aprx, pagx_out], capture_output=True, text=True,
                              timeout=300, cwd=work, env=env, encoding="utf-8", errors="replace")
        res = json.loads((proc.stdout.strip().splitlines() or ["{}"])[-1])
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        res = {"import_error": str(e)}
    if "import_error" in res or not res.get("product") or res.get("product") == "NotInitialized":
        print(f"SKIP arcpy layout check: {res.get('import_error') or 'no ArcGIS Pro licence'}")
    else:
        print("   arcpy:", res)
        expected = sorted(["MAPFRAME_ELEMENT", "LEGEND_ELEMENT", "MAPSURROUND_ELEMENT", "MAPSURROUND_ELEMENT",
                           "MAPSURROUND_ELEMENT", "TEXT_ELEMENT", "GRAPHIC_ELEMENT", "GRAPHIC_ELEMENT",
                           "PICTURE_ELEMENT"])
        check(res.get("aprx") == expected and res.get("aprx_png", 0) > 1000,
              f"arcpy: every written layout element loads and the layout renders {res.get('aprx_error', '')}")
        check(res.get("pagx") == expected, f"arcpy imports our .pagx {res.get('pagx_error', '')}")
else:
    print("SKIP arcpy layout check: ArcGIS Pro is not installed")

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
