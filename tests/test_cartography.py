r"""Cartography tools: tabular labels, contour masking and uphill numbers, movable labels, legend that fits,
lean PDF with clickable links, label clustering with leaders, and the map-book index.

Run:  engine\bin\python-qgis.bat tests\test_cartography.py [png output dir]
"""
import os
import re
import sys
import tempfile

import numpy as np
from osgeo import gdal, osr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsLayoutItemLabel, QgsLayoutItemLegend,  # noqa: E402
                       QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsMapRendererSequentialJob, QgsMapSettings,
                       QgsPalLayerSettings, QgsPointXY, QgsPrintLayout, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

gdal.UseExceptions()
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import cartography as carto, mapsheet  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.label_cluster import (group_features, label_box, style_label_layer,  # noqa: E402
                                             style_leader_layer)
from biruni.algorithms.map_index import column_letters, grid_cells, ref_text, sort_key  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
TMP = tempfile.mkdtemp()
prj = QgsProject.instance()
ctx = QgsProcessingContext()
ctx.setProject(prj)
fb = QgsProcessingFeedback()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def memory(geom, fields, rows, name="t"):
    lyr = QgsVectorLayer(f"{geom}?crs=EPSG:32637&{fields}", name, "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    prj.addMapLayer(lyr)
    return lyr


def render(layers, ext, name, size=(900, 600)):
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(layers[0].crs())
    ms.setExtent(ext)
    ms.setOutputSize(QSize(*size))
    ms.setBackgroundColor(QColor("white"))
    job = QgsMapRendererSequentialJob(ms)
    job.start()
    job.waitForFinished()
    path = os.path.join(OUT, name)
    job.renderedImage().save(path)
    return path


# ---- tabular labels -----------------------------------------------------------------------------------------------
par = memory("Polygon", "field=no:integer&field=owner:string&field=area:double",
             [(f"POLYGON(({500000 + k * 60} 2700000,{500055 + k * 60} 2700000,{500055 + k * 60} 2700050,"
               f"{500000 + k * 60} 2700050,{500000 + k * 60} 2700000))",
               [k + 1, ["أحمد علي", "سارة محمد", "شركة النور", "خالد"][k], 2750.5 + k]) for k in range(4)], "parcels")
expr, aliases, rtl = carto.tabular_label_expression(par, ["owner", "area", "missing"], header="no")
check(aliases == ["owner", "area"] and not rtl, "tabular label: unknown fields skipped, names without aliases")
check(expr.startswith('coalesce(to_string("no")') and "'owner' || '\t'" in expr, "tabular label: header then name<TAB>value")
par.setFieldAlias(1, "المالك")
par.setFieldAlias(2, "المساحة")
pal = carto.tabular_label(par, ["owner", "area"], header="no")
check(par.labelsEnabled() and pal.isExpression and "'\t' || 'المالك'" in pal.fieldName,
      "tabular label with Arabic aliases: value<TAB>name (right to left)")
check(pal.format().tabStopDistance() > 5 and pal.format().background().enabled(), "tab stop and white frame set")
try:
    carto.tabular_label(par, ["nothing"])
    check(False, "no valid field raises")
except ValueError:
    check(True, "no valid field raises")
render([par], QgsRectangle(499990, 2699990, 500250, 2700060), "tabular_ar.png", (900, 300))

# ---- contours -------------------------------------------------------------------------------------------------------
check(carto.contour_interval([100, 105, 110, 115, 125, None]) == 5.0, "contour interval = most common gap")
check(carto.contour_interval([10.5, 11.0, 11.5]) == 0.5, "decimal interval")
n = 200
yy, xx = np.mgrid[0:n, 0:n]
z = (300 + 80 * np.exp(-((xx - 70) ** 2 + (yy - 90) ** 2) / 1500)
     + 50 * np.exp(-((xx - 150) ** 2 + (yy - 60) ** 2) / 900) - yy * 0.3)
dem_path = os.path.join(TMP, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(dem_path, n, n, 1, gdal.GDT_Float32)
ds.SetGeoTransform([500000, 5, 0, 2701000, 0, -5])
srs = osr.SpatialReference()
srs.ImportFromEPSG(32637)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(z)
ds = None
dem = QgsRasterLayer(dem_path, "dem")
res = processing.run("gdal:contour", {"INPUT": dem_path, "BAND": 1, "INTERVAL": 5, "FIELD_NAME": "ELEV",
                                     "OUTPUT": os.path.join(TMP, "contours.gpkg")})
con = QgsVectorLayer(res["OUTPUT"], "contours", "ogr")
prj.addMapLayer(con)
info = carto.style_contours(con, dem=dem)
check(info["interval"] == 5.0 and info["index_step"] == 25.0 and info["field"] == "ELEV",
      f"contours: interval and index step found ({info})")
rules = con.renderer().rootRule().children()
check(len(rules) == 2 and rules[1].isElse(), "index and intermediate contour rules")
con_labeling = con.labeling()  # keep a reference: settings() points into it
lab = con_labeling.settings()
mask = lab.format().mask()
check(mask.enabled() and len(mask.maskedSymbolLayers()) == 2, "labels mask both contour symbols")
check(lab.dataDefinedProperties().isActive(QgsPalLayerSettings.Property.Show), "only index contours labelled")
from qgis.core import QgsExpression, QgsExpressionContext  # noqa: E402

ex = QgsExpression(carto.index_expression("ELEV", 5, 5))
ctxe = QgsExpressionContext()
vals = {}
for v in (250.0, 275.0, 280.0, 300.0):
    f = QgsFeature(con.fields())
    f.setAttribute("ELEV", v)
    ctxe.setFeature(f)
    vals[v] = bool(ex.evaluate(ctxe))
check(vals == {250.0: True, 275.0: True, 280.0: False, 300.0: True}, f"index expression ({vals})")
# every line now has higher ground on its left (numbers read uphill)
prov = dem.dataProvider()
good = bad = 0
for f in con.getFeatures():
    g = f.geometry()
    line = g.asMultiPolyline()[0] if g.isMultipart() else g.asPolyline()
    if len(line) < 3:
        continue
    a, b = line[len(line) // 2 - 1], line[len(line) // 2]
    dx, dy = b.x() - a.x(), b.y() - a.y()
    ln = (dx * dx + dy * dy) ** 0.5 or 1
    mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
    zl, ok1 = prov.sample(QgsPointXY(mx - dy / ln * 8, my + dx / ln * 8), 1)
    zr, ok2 = prov.sample(QgsPointXY(mx + dy / ln * 8, my - dx / ln * 8), 1)
    if ok1 and ok2 and abs(zl - zr) > 1e-6:
        good += zl > zr
        bad += zl < zr
check(info["turned"] > 0 and good > 0 and bad <= max(1, good // 20),
      f"contours turned so higher ground is on the left ({info['turned']} turned; {good} uphill, {bad} not)")
render([con], con.extent(), "contours.png", (800, 800))
con.rollBack()

# ---- labels movable by hand -----------------------------------------------------------------------------------------
pts = memory("Point", "field=code:string&field=kind:string",
             [(f"POINT({500000 + k * 30} 2700000)", [f"P{k}", "x"]) for k in range(5)], "manual")
ok, msg = carto.enable_manual_labels(pts)
check(not ok and "labels" in msg.lower(), "manual labels: asks to turn labels on first")
pl = QgsPalLayerSettings()
pl.fieldName = "code"
pts.setLabeling(QgsVectorLayerSimpleLabeling(pl))
pts.setLabelsEnabled(True)
ok, msg = carto.enable_manual_labels(pts)
check(ok and pts.auxiliaryLayer() is not None and carto.manual_labels_ready(pts),
      "manual labels: auxiliary storage on the unique field, position bound")
same = memory("Point", "field=kind:string", [(f"POINT({500000 + k} 2700000)", ["x"]) for k in range(3)], "nokey")
same.setLabeling(QgsVectorLayerSimpleLabeling(pl))
same.setLabelsEnabled(True)
ok2, msg2 = carto.enable_manual_labels(same)
check(not ok2 and "ID" in msg2, "manual labels: a layer with no unique field is explained")

# ---- legend that fits ------------------------------------------------------------------------------------------------
many = []
for k in range(14):
    lyr = memory("Point", "field=a:integer", [(f"POINT({500000 + k * 10} 2700000)", [k])],
                 f"Layer {k + 1} with a rather long descriptive name that needs wrapping")
    many.append(lyr)
layout = QgsPrintLayout(prj)
layout.initializeDefaults()
mp = QgsLayoutItemMap(layout)
layout.addLayoutItem(mp)
mp.attemptMove(QgsLayoutPoint(10, 10))
mp.attemptResize(QgsLayoutSize(150, 150))
mp.zoomToExtent(QgsRectangle(499990, 2699990, 500150, 2700010))
leg = QgsLayoutItemLegend(layout)
leg.setLinkedMap(mp)
layout.addLayoutItem(leg)
leg.attemptMove(QgsLayoutPoint(170, 10))
natural = carto.legend_size(leg)
cols, scale, fits = carto.fit_legend(leg, 110, 75)
need = carto.legend_size(leg)
check(natural.height() > 75 and fits and (cols > 1 or scale < 1) and need.height() <= 75 and need.width() <= 110
      and abs(leg.rect().height() - 75) < 0.5,
      f"legend fits 110×75 mm ({natural.width():.0f}×{natural.height():.0f} mm → {cols} columns, text ×{scale}, "
      f"{need.width():.0f}×{need.height():.0f} mm)")
check(leg.autoWrapLinesAfter() > 0, "long names wrap inside the legend")
mapsheet.export(layout, os.path.join(OUT, "legend_fit.png"))
for lyr in many:
    prj.removeMapLayer(lyr.id())

# ---- PDF: lean export and clickable links ----------------------------------------------------------------------------
link = QgsLayoutItemLabel(layout)
link.setText("Web map: https://example.com/map")
layout.addLayoutItem(link)
link.attemptMove(QgsLayoutPoint(20, 180))
link.attemptResize(QgsLayoutSize(90, 8))
box = QgsLayoutItemLabel(layout)
box.setText("Office")
box.setCustomProperty("biruni_link", "mailto:office@example.com")
layout.addLayoutItem(box)
box.attemptMove(QgsLayoutPoint(150, 180))
box.attemptResize(QgsLayoutSize(30, 8))
found = carto.layout_links(layout)
check(sorted(u for _p, _r, u in found) == ["https://example.com/map", "mailto:office@example.com"],
      "links found in labels and in the link property")
pdf = os.path.join(OUT, "links.pdf")
ok, nlinks = carto.export_pdf(layout, pdf)
data = open(pdf, "rb").read()
check(ok and nlinks == 2 and data.count(b"/Subtype /Link") == 2 and b"/URI (https://example.com/map)" in data,
      "PDF has two clickable links")
# every xref entry of the last update points at its object
xref = data.rindex(b"\nxref\n") + 1
entries = re.findall(rb"(\d+) 1\n(\d{10}) 00000 n", data[xref:])
check(entries and all(data[int(off):int(off) + 20].startswith(num + b" 0 obj") for num, off in entries),
      f"incremental update: {len(entries)} cross-reference offsets are exact")
link_objs = re.findall(rb"(\d+) 0 obj\s*<< /Type /Annot /Subtype /Link", data)
latest_arrays = re.findall(rb"\[([^\]]*)\]\s*endobj", data[data.rindex(b"%%EOF", 0, xref):])
check(link_objs and any(all(n + b" 0 R" in arr for n in link_objs) for arr in latest_arrays),
      "the page's annotation list holds both links")
start = data.rindex(b"startxref")
check(int(data[start + 10:].split()[0]) == xref, "startxref points at the new table")
plain = os.path.join(TMP, "plain.pdf")
ok_plain, n0 = carto.export_pdf(layout, plain, lean=False, links=False)
check(ok_plain and n0 == 0 and b"/Subtype /Link" not in open(plain, "rb").read(), "links can be left out")
s = carto.pdf_settings(lean=True)
from qgis.core import Qgis  # noqa: E402

check(s.simplifyGeometries and not s.rasterizeWholeImage and s.textRenderFormat == Qgis.TextRenderFormat.AlwaysText,
      "lean settings: simplified lines, text as text, no rasterising")

# map sheet: link printed and clickable, legend fitted
prj.addMapLayer(par)
opt = mapsheet.SheetOptions(title="Links", paper="A4", landscape=True, link="www.example.org/plan", rtl=False)
sheet = mapsheet.build_layout(prj, opt)
item = sheet.itemById("biruni-link")
check(item is not None and item.customProperty("biruni_link") == "https://www.example.org/plan",
      "map sheet prints the web link")
check(sheet.itemById("biruni-legend") is not None, "map sheet legend placed")
sheet_pdf = os.path.join(OUT, "sheet_links.pdf")
check(mapsheet.export(sheet, sheet_pdf) and b"/URI (https://www.example.org/plan)" in open(sheet_pdf, "rb").read(),
      "map sheet PDF link is clickable")
mapsheet.export(sheet, os.path.join(OUT, "sheet_links.png"))

# ---- label clustering -------------------------------------------------------------------------------------------------
wells = [(f"POINT({500100 + dx} {2700100 + dy})", ["بئر"]) for dx, dy in ((0, 0), (20, 10), (35, -15))]
poles = [(f"POINT({500300 + dx} {2700100 + dy})", ["Pole"]) for dx, dy in ((0, 0), (25, 5))]
lone = [("POINT(500200 2700300)", ["Tank"])]
far_well = [("POINT(500500 2700500)", ["بئر"])]
feat = memory("Point", "field=name:string", wells + poles + lone + far_well, "features")
items = {f.id(): (f.geometry(), f["name"]) for f in feat.getFeatures()}
groups = group_features(items, 50, True)
check(sorted(len(g) for g in groups) == [1, 1, 2, 3], f"grouping by name within 50 m ({[len(g) for g in groups]})")
check(sorted(len(g) for g in group_features(items, 50, False)) == [1, 1, 2, 3], "grouping without names")
check(sorted(len(g) for g in group_features(items, 500, False)) == [7], "a long distance makes one group")
res = processing.run("biruni:labelcluster", {"INPUT": feat, "FIELD": "name", "DISTANCE": 50, "HEIGHT": 8,
                                            "OFFSET": 0, "SINGLES": True, "COUNT": True,
                                            "LABELS": "memory:", "LEADERS": "memory:"}, context=ctx, feedback=fb)
labels, leaders = out(res["LABELS"]), out(res["LEADERS"])
texts = sorted(f["label"] for f in labels.getFeatures())
check(texts == sorted(["بئر (3)", "Pole (2)", "Tank", "بئر"]), f"one label per group with its count ({texts})")
check(res["LEADER_COUNT"] == 5 and leaders.featureCount() == 5, "a leader to each grouped feature (3 + 2)")
boxes = {f["label"]: label_box(f.geometry().asPoint(), f["label"], 8) for f in labels.getFeatures()}
clear = all(not QgsGeometry.fromRect(b).intersects(g.buffer(0.5, 4)) for b in boxes.values()
            for g, _v in items.values())
check(clear, "no label box covers a feature")
bl = list(boxes.values())
check(all(not bl[i].intersects(bl[j]) for i in range(len(bl)) for j in range(i + 1, len(bl))), "labels do not overlap")
ends = [f.geometry().asPolyline()[-1] for f in leaders.getFeatures()]
member_pts = [g.asPoint() for g, _v in items.values()]
check(all(min(e.distance(p) for p in member_pts) < 1e-6 for e in ends), "every leader ends on its feature")
res2 = processing.run("biruni:labelcluster", {"INPUT": feat, "FIELD": None, "DISTANCE": 50, "HEIGHT": 8,
                                             "SINGLES": False, "LABELS": "memory:", "LEADERS": "memory:"},
                      context=ctx, feedback=fb)
counts = sorted(f["label"] for f in out(res2["LABELS"]).getFeatures())
check(counts == ["2", "3"], f"without a field: one count label per group, singles left out ({counts})")
style_label_layer(labels, 8)
style_leader_layer(leaders)
render([labels, leaders, feat], QgsRectangle(500040, 2700020, 500420, 2700340), "cluster.png", (900, 760))

# ---- map-book index ---------------------------------------------------------------------------------------------------
check([column_letters(k) for k in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"], "column letters")
cells = grid_cells(QgsRectangle(0, 0, 400, 200), 4, 2)
check(cells[0][0] == "A1" and cells[0][1].yMaximum() == 200 and cells[-1][0] == "D2", "grid: A1 top-left, D2 bottom-right")
check(ref_text({"12": {"C3", "B3", "B10"}, "2": set()}) == "2 · 12: B3, B10, C3", "references in natural order")
keys = sorted(["شارع الملك فهد", "حي الروضة", "الأمير سلطان", "Zeta", "alpha", "2nd street", "10th street"],
              key=lambda s: sort_key(s, True, False))
check(keys[:4] == ["2nd street", "10th street", "alpha", "Zeta"] and keys[4:] == ["الأمير سلطان", "حي الروضة",
                                                                                  "شارع الملك فهد"],
      f"sorting: Latin natural, Arabic without ال / شارع / حي ({keys})")
streets = memory("LineString", "field=name:string",
                 [("LINESTRING(0 50, 390 50)", ["شارع الملك فهد"]),       # crosses both pages
                  ("LINESTRING(10 150, 90 150)", ["Olaya St"]),
                  ("LINESTRING(250 150, 290 160)", ["Olaya St"]),           # same name, another page
                  ("LINESTRING(210 20, 230 30)", ["الأمير سلطان"]),
                  ("LINESTRING(1000 1000, 1010 1010)", ["Outside"])], "streets")
pages = memory("Polygon", "field=page_no:integer",
               [("POLYGON((0 0, 200 0, 200 200, 0 200, 0 0))", [7]),
                ("POLYGON((200 0, 400 0, 400 200, 200 200, 200 0))", [8])], "pages")
xlsx = os.path.join(OUT, "index.xlsx")
html_path = os.path.join(OUT, "index.html")
res = processing.run("biruni:mapindex", {"INPUT": streets, "FIELD": "name", "PAGES": pages, "PAGE_FIELD": "page_no",
                                        "USE_GRID": True, "COLUMNS": 2, "ROWS": 2, "STRIP": True,
                                        "OUTPUT": "memory:", "GRID": "memory:", "XLSX": xlsx, "HTML": html_path},
                     context=ctx, feedback=fb)
idx = {f["name"]: f for f in out(res["OUTPUT"]).getFeatures()}
check(set(idx) == {"شارع الملك فهد", "Olaya St", "الأمير سلطان"}, f"index names, outside left out ({sorted(idx)})")
check(idx["شارع الملك فهد"]["pages"] == "7, 8" and idx["شارع الملك فهد"]["squares"] == "7: A2, B2 · 8: A2, B2",
      f"a street crossing two pages ({idx['شارع الملك فهد']['squares']})")
check(idx["Olaya St"]["pages"] == "7, 8" and idx["Olaya St"]["parts"] == 2, "a name in two pieces is listed once")
check(idx["الأمير سلطان"]["squares"] == "8: A2", "grid square inside the page")
check(out(res["GRID"]).featureCount() == 8, "grid squares layer: 2 × 2 per page")
from openpyxl import load_workbook  # noqa: E402

ws = load_workbook(xlsx).active
values = [c.value for c in ws["A"] if c.value]
check("Olaya St" in values and "شارع الملك فهد" in values and "O" in values, "Excel index with letter headings")
page_html = open(html_path, encoding="utf-8").read()
check("الأمير سلطان" in page_html and "7: A2, B2 · 8: A2, B2" in page_html, "printable HTML index")
res3 = processing.run("biruni:mapindex", {"INPUT": streets, "FIELD": "name", "USE_GRID": True, "COLUMNS": 4,
                                         "ROWS": 0, "OUTPUT": "memory:", "GRID": "memory:"}, context=ctx, feedback=fb)
idx3 = {f["name"]: f for f in out(res3["OUTPUT"]).getFeatures()}
check(len(idx3) == 4 and idx3["Outside"]["squares"] and idx3["Outside"]["pages"] == "",
      f"without pages: one grid over everything ({idx3['Outside']['squares']})")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
print("images in", OUT)
sys.exit(1 if failures else 0)
