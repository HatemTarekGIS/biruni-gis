r"""Print output for every market: coordinate grids, sheet standards, the route parcel atlas and the EUDR check.

Run:  engine\bin\python-qgis.bat tests\test_print_global.py [png output dir]
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,  # noqa: E402
                       QgsExpression, QgsExpressionContext, QgsExpressionContextScope, QgsFeature, QgsGeometry,
                       QgsLayoutExporter, QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsPointXY,
                       QgsPrintLayout, QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsRectangle,
                       QgsVectorLayer)

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import map_grid, mapsheet, sheet_presets  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.eudr_check import geojson_geometry, ring_coords  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
os.makedirs(OUT, exist_ok=True)
ctx = QgsProcessingContext()
prj = QgsProject.instance()
ctx.setProject(prj)
fb = QgsProcessingFeedback()


def ev(expr, number, axis):
    scope = QgsExpressionContextScope()
    scope.setVariable("grid_number", number)
    scope.setVariable("grid_axis", axis)
    c = QgsExpressionContext()
    c.appendScope(scope)
    e = QgsExpression(expr)
    v = e.evaluate(c)
    return v if not e.hasEvalError() else "ERROR " + e.evalErrorString()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def plain(text):
    return str(text).replace("‎", "")


# ---- 1. annotation expressions --------------------------------------------------------------
check(ev(map_grid.projected_expression("pt"), 7420000, "y") == "7.420.000 N", "pt metric: 7.420.000 N")
check(ev(map_grid.projected_expression("pt"), 330000, "x") == "330.000 L", "pt metric easting: L (leste)")
check(ev(map_grid.projected_expression("en"), 330000, "x") == "330,000 E", "en metric: 330,000 E")
check(ev(map_grid.projected_expression("fr"), 330000, "x") == "330 000 E", "fr metric: narrow no-break space")
ar = plain(ev(map_grid.projected_expression("ar"), 330000, "x"))
check(ar == "٣٣٠٬٠٠٠ ق", f"ar metric: Arabic-Indic digits and ق ({ar})")
check(ev(map_grid.geographic_expression("pt", 0.5), -46.5, "x") == "46°30' O", "pt graticule 30': 46°30' O (oeste)")
check(ev(map_grid.geographic_expression("es", 1 / 120), -15.25, "y") == "15°15'00\" S", "es graticule 30\": seconds")
check(ev(map_grid.geographic_expression("en", 2), 24, "y") == "24° N", "whole degrees: minutes left out")
fa = plain(ev(map_grid.geographic_expression("fa", 1 / 60), 51.4166667, "x"))
check(fa == "۵۱°۲۵' خ", f"fa graticule: Persian digits and خ for east ({fa})")
check(ev(map_grid.geographic_expression("en", 1 / 3600), 30.99999999, "y") == "31°00'00\" N",
      "seconds round up into the next minute and degree")
check(map_grid.geographic_interval(0.4) == 10 / 60 and map_grid.geographic_interval(7) == 2,
      "geographic intervals: 10' for 0.4°, 2° for 7°")
check(map_grid.projected_interval(4300) == 1000 and map_grid.projected_interval(260) == 100,
      "metric intervals: round values")
check(set(map_grid.STYLES) >= {"auto", "projected", "geographic", "both", "ticks", "none"}, "grid styles listed")

# ---- 2. grids on a layout map (Brazil, across the 22/23 UTM boundary at 48°W) --------------------
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:31983"))  # SIRGAS 2000 / UTM 23S
to_utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4674"), prj.crs(), prj)
ll = to_utm.transform(QgsPointXY(-48.30, -15.95))
ur = to_utm.transform(QgsPointXY(-47.75, -15.60))
lots = QgsVectorLayer("Polygon?crs=EPSG:31983&field=lote:string", "Lotes", "memory")
feats = []
for k in range(6):
    x0 = ll.x() + k * (ur.x() - ll.x()) / 6
    f = QgsFeature(lots.fields())
    f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x0, ll.y(), x0 + (ur.x() - ll.x()) / 6.5, ur.y())))
    f.setAttributes([f"L{k + 1}"])
    feats.append(f)
lots.dataProvider().addFeatures(feats)
prj.addMapLayer(lots)


def map_layout(rotation=0.0):
    lay = QgsPrintLayout(prj)
    lay.initializeDefaults()
    mp = QgsLayoutItemMap(lay)
    lay.addLayoutItem(mp)
    mp.attemptMove(QgsLayoutPoint(30, 25))
    mp.attemptResize(QgsLayoutSize(230, 160))
    mp.setCrs(prj.crs())
    mp.setLayers([lots])
    mp.zoomToExtent(QgsRectangle(ll.x(), ll.y(), ur.x(), ur.y()))
    mp.setFrameEnabled(True)
    if rotation:
        mp.setMapRotation(rotation)
    return lay, mp


def export_png(lay, name):
    s = QgsLayoutExporter.ImageExportSettings()
    s.dpi = 110
    path = os.path.join(OUT, name)
    ok = QgsLayoutExporter(lay).exportToImage(path, s) == QgsLayoutExporter.ExportResult.Success
    return ok and os.path.getsize(path) > 10000


lay, mp = map_layout()
check(map_grid.map_zones(mp) == [22, 23], f"map crosses UTM zones 22 and 23 ({map_grid.map_zones(mp)})")
grids = map_grid.add_grid(mp, "projected", "pt", "extended")
check(len(grids) == 1 and grids[0].crs().authid() == "EPSG:31983", "extended: one grid in the project zone")
check(export_png(lay, "grid_extended_pt.png"), "extended grid exported")

lay, mp = map_layout()
grids = map_grid.add_grid(mp, "projected", "pt", "per_zone")
names = sorted(g.name() for g in grids)
check(names == ["biruni-grid-zone-22", "biruni-grid-zone-23"], f"per zone: one grid per zone ({names})")
z22 = [g for g in grids if g.name().endswith("22")][0]
check("+zone=22" in z22.crs().toProj() and "+south" in z22.crs().toProj() and "GRS80" in z22.crs().toProj(),
      "zone 22 keeps the SIRGAS 2000 datum and the southern hemisphere")
check(export_png(lay, "grid_per_zone_pt.png"), "per-zone grid exported")

lay, mp = map_layout()
grids = map_grid.add_grid(mp, "projected", "pt", "geographic")
check(len(grids) == 1 and grids[0].crs().isGeographic(), "zone mode 'geographic': graticule instead")
lay, mp = map_layout()
grids = map_grid.add_grid(mp, "both", "pt")
check(len(grids) == 2 and grids[1].crs().isGeographic(), "both: metric grid plus geographic frame")
check(export_png(lay, "grid_both_pt.png"), "topographic-style sheet exported")
labels = map_grid.add_corner_labels(lay, mp, "pt")
check(len({plain(lb.text()) for lb in labels}) == 4 and not any(plain(lb.text()).startswith("0 ") for lb in labels),
      "corner coordinates: four different corners")
check(len(labels) == 4 and all(" S" in plain(lb.text()) and (" O" in plain(lb.text()) or " L" in plain(lb.text()))
                                   or re.search(r"\d N", plain(lb.text())) for lb in labels),
      "corner coordinates: four, with Portuguese letters")
check(export_png(lay, "grid_both_corners_pt.png"), "corners exported")

lay, mp = map_layout(rotation=20)
grids = map_grid.add_grid(mp, "projected", "en")
check(grids[0].rotatedTicksEnabled() and grids[0].rotatedAnnotationsEnabled(), "rotated map: rotated ticks/labels")
check(export_png(lay, "grid_rotated.png"), "rotated map exported")

lay, mp = map_layout()
map_grid.add_grid(mp, "ticks", "ar")
check(export_png(lay, "grid_ticks_ar.png"), "Arabic ticks-only grid exported")
map_grid.remove_grids(mp)
check(mp.grids().size() == 0, "grids removed")

# ---- 3. sheet standards -------------------------------------------------------------------------
check(sheet_presets.for_country("BR")[0] == "abnt_a3" and sheet_presets.for_country("sa")[0] == "sa_kroki_a4",
      "country defaults: ABNT for Brazil, kroki for Saudi Arabia")
check(sheet_presets.for_country("CI")[0] == "fr_bornage_a4", "francophone Africa gets the bornage plan")
check(sheet_presets.for_country("ZZ")[0].startswith("iso_"), "unknown country: ISO sheets")
check(sheet_presets.get("abnt_a4").margins[0] == 25 and sheet_presets.get("abnt_a1").margins[1] == 10,
      "ABNT NBR 10068 margins: 25 mm binding, 10 mm on A1")
rows = sheet_presets.title_rows(sheet_presets.get("eg_kroki_a4"), {"basin": "حوض الجزيرة ٣"})
check(rows[1] == ("الحوض", "حوض الجزيرة ٣"), "Egyptian kroki title block in Arabic")
check(sheet_presets.field_label("owner", "pt") == "Proprietário", "Portuguese field labels")

lot = QgsGeometry.fromRect(QgsRectangle(ll.x() + 500, ll.y() + 500, ll.x() + 530, ll.y() + 540))
for pid, fields in (("abnt_a3", {"owner": "João da Silva", "licence": "CREA 12345/D"}),
                    ("sa_kroki_a4", {"owner": "عبدالله", "deed_no": "٤١٠١٢٣٤٥", "district": "النرجس"}),
                    ("pe_perimetrico_a3", {"owner": "María Quispe"}), ("ph_lot_plan", {"lot_no": "1234-A"}),
                    ("ng_survey_plan_a3", {"plan_number": "OY/1234/2026"}), ("fr_bornage_a4", {"section": "AB"}),
                    ("ansi_a", {}), ("eg_kroki_a4", {"basin": "الجزيرة"})):
    p = sheet_presets.get(pid)
    opt = mapsheet.SheetOptions(title=f"Sheet {pid}", preset=pid, fields=fields, parcel=(lot, lots.crs()),
                                rtl=p.lang == "ar", arabic_digits=p.lang == "ar", extent=lots.extent())
    lay = mapsheet.build_layout(prj, opt)
    size = lay.pageCollection().page(0).pageSize()
    w, h = p.page_size()
    check(abs(size.width() - w) < 0.01 and abs(size.height() - h) < 0.01, f"{pid}: page {w:g} × {h:g} mm")
    check(any(i.id() == "biruni-title-block" for i in lay.items() if hasattr(i, "id")), f"{pid}: title block")
    check(export_png(lay, f"sheet_{pid}.png"), f"{pid}: exported")

# ---- 4. route parcel atlas ------------------------------------------------------------------------
parcels = QgsVectorLayer("Polygon?crs=EPSG:32637&field=no:string&field=owner:string", "Parcels", "memory")
pf = []
for i in range(8):
    for j in range(2):
        f = QgsFeature(parcels.fields())
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(500000 + i * 50, 2600000 + j * 40,
                                                        500050 + i * 50, 2600040 + j * 40)))
        f.setAttributes([f"{i + 1}-{j + 1}", f"Owner {i}{j}"])
        pf.append(f)
parcels.dataProvider().addFeatures(pf)
route = QgsVectorLayer("LineString?crs=EPSG:32637", "Road", "memory")
rf = QgsFeature(route.fields())
rf.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(499990, 2600040), QgsPointXY(500410, 2600040)]))
route.dataProvider().addFeatures([rf])
for lyr in (parcels, route):
    prj.addMapLayer(lyr)
xlsx = os.path.join(OUT, "register.xlsx")
pdf = os.path.join(OUT, "atlas.pdf")
res = processing.run("biruni:parcelatlas", {"ROUTE": route, "WIDTH": 20, "PARCELS": parcels, "ID_FIELD": "no",
                                            "OWNER_FIELD": "owner", "MODE": 0, "OUTPUT": "memory:",
                                            "CORRIDOR": "memory:", "XLSX": xlsx, "PDF": pdf},
                     context=ctx, feedback=fb)
check(res["PARCEL_COUNT"] == 16, f"all 16 parcels touched by a corridor on their shared edge ({res['PARCEL_COUNT']})")
check(abs(res["TAKEN_M2"] - 400 * 20) < 1, f"area taken = 400 m × 20 m inside the parcels ({res['TAKEN_M2']})")
out_lyr = out(res["OUTPUT"])
first = sorted(out_lyr.getFeatures(), key=lambda f: f["chainage_m"])[0]
check(abs(first["taken_m2"] - 500) < 0.5 and abs(first["taken_pct"] - 25) < 0.01,
      f"each parcel: 50 × 10 m taken = 25 % ({first['taken_m2']}, {first['taken_pct']})")
check(first["owner"].startswith("Owner 0") and first["parcel_id"].startswith("1-"), "owner and number copied")
check(os.path.getsize(xlsx) > 4000, "Excel register written")
from openpyxl import load_workbook  # noqa: E402

ws = load_workbook(xlsx).active
check(ws.max_row >= 17 and str(ws.cell(row=19, column=5).value).startswith("=SUM("), "register: rows and totals")
check(res.get("PAGES") == 16 and os.path.getsize(pdf) > 20000, f"atlas PDF: one page per parcel ({res.get('PAGES')})")
pdf2 = os.path.join(OUT, "atlas_strips.pdf")
res2 = processing.run("biruni:parcelatlas", {"ROUTE": route, "WIDTH": 20, "PARCELS": parcels, "ID_FIELD": "no",
                                             "MODE": 1, "SHEET_LENGTH": 200, "OUTPUT": "memory:", "PDF": pdf2},
                      context=ctx, feedback=fb)
check(res2.get("PAGES") == 3, f"strip sheets: 420 m in 200 m sheets = 3 pages ({res2.get('PAGES')})")
sheets = {f["sheet"] for f in out(res2["OUTPUT"]).getFeatures()}
check(sheets == {1, 2}, f"parcels tagged with their sheet ({sorted(sheets)})")

# ---- 5. EUDR ------------------------------------------------------------------------------------
check(ring_coords([QgsPointXY(0, 0), QgsPointXY(0, 1), QgsPointXY(1, 1), QgsPointXY(1, 0), QgsPointXY(0, 0)], True)
      == [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]], "exterior ring made counter-clockwise (RFC 7946)")
g6 = geojson_geometry(QgsGeometry.fromWkt("POINT(-5.123456789 7.987654321)"))
check(g6["coordinates"] == [-5.123457, 7.987654], "six decimals")
plots = QgsVectorLayer("Polygon?crs=EPSG:4326&field=id:string&field=producer:string&field=cc:string"
                       "&field=crop:string", "Plots", "memory")
wkts = [
    ("ok", "Coop A", "CI", "cocoa", "POLYGON((-5.50 7.50, -5.49 7.50, -5.49 7.51, -5.50 7.51, -5.50 7.50))"),
    ("bowtie", "Coop A", "CI", "cocoa", "POLYGON((-5.40 7.40, -5.39 7.41, -5.39 7.40, -5.40 7.41, -5.40 7.40))"),
    ("overlap", "Coop A", "CI", "cocoa", "POLYGON((-5.495 7.505, -5.48 7.505, -5.48 7.52, -5.495 7.52, "
                                         "-5.495 7.505))"),
    ("nocountry", "Coop B", "Ivory Coast", "cocoa", "POLYGON((-5.30 7.30, -5.29 7.30, -5.29 7.31, -5.30 7.31, "
                                                     "-5.30 7.30))"),
    ("swapped", "Coop B", "CI", "coffee", "POLYGON((7.20 -5.20, 7.20 -5.19, 7.21 -5.19, 7.21 -5.20, 7.20 -5.20))"),
    ("noproducer", "", "CI", "banana", "POLYGON((-5.10 7.10, -5.09 7.10, -5.09 7.11, -5.10 7.11, -5.10 7.10))"),
]
pfeats = []
for pid_, prod, cc, crop, w in wkts:
    f = QgsFeature(plots.fields())
    f.setGeometry(QgsGeometry.fromWkt(w))
    f.setAttributes([pid_, prod, cc, crop])
    pfeats.append(f)
plots.dataProvider().addFeatures(pfeats)
gj = os.path.join(OUT, "eudr.geojson")
res = processing.run("biruni:eudrcheck", {"INPUT": plots, "PLOT_ID": "id", "PRODUCER": "producer",
                                          "COUNTRY_FIELD": "cc", "COMMODITY_FIELD": "crop", "FIX": True,
                                          "GEOJSON": gj, "OUTPUT": "memory:", "PROBLEMS": "memory:"},
                     context=ctx, feedback=fb)
probs = [(f["plot"], f["severity"], f["detail"]) for f in out(res["PROBLEMS"]).getFeatures()]
rules = {(p[0], p[1]) for p in probs}
check(("bowtie", "fixed") in rules, "bow-tie polygon repaired")
check(any(p[0] == "ok" and "overlap" in p[2].lower() for p in probs), "overlap between plots of one producer")
check(("nocountry", "error") in rules, "country name instead of ISO code flagged")
check(("swapped", "fixed") in rules, "swapped latitude/longitude detected through the country and fixed")
check(sum(1 for p in probs if p[0] == "noproducer") == 2, "missing producer and unknown commodity")
check(not any(p[0] == "ok" and p[1] == "error" and "overlap" not in p[2].lower() for p in probs),
      "the good plot has no other error")
data = json.load(open(gj, encoding="utf-8"))
check(data["type"] == "FeatureCollection" and len(data["features"]) == 6, "GeoJSON: six plots")
coords_ok = all(len(str(c).split(".")[-1]) <= 6 for feat in data["features"]
                for ring in (feat["geometry"]["coordinates"] if feat["geometry"]["type"] == "Polygon" else
                             [r for poly in feat["geometry"]["coordinates"] for r in poly])
                for pt in ring for c in pt)
check(coords_ok, "all coordinates have at most six decimals")
sw = [f for f in data["features"] if f["properties"]["ProductionPlace"] == "swapped"][0]
check(sw["geometry"]["coordinates"][0][0][0] < 0 < sw["geometry"]["coordinates"][0][0][1],
      "swapped plot written as longitude, latitude")
okf = [f for f in data["features"] if f["properties"]["ProductionPlace"] == "ok"][0]
check(abs(okf["properties"]["Area"] - 122.6) < 2, f"area in hectares ({okf['properties']['Area']})")
ring = okf["geometry"]["coordinates"][0]
check(sum(ring[k][0] * ring[k + 1][1] - ring[k + 1][0] * ring[k][1] for k in range(len(ring) - 1)) > 0,
      "exterior ring counter-clockwise in the file")
pts = QgsVectorLayer("Point?crs=EPSG:4326&field=id:string&field=producer:string&field=ha:double", "P", "memory")
pp = QgsFeature(pts.fields())
pp.setGeometry(QgsGeometry.fromWkt("POINT(106.9 12.6)"))
pp.setAttributes(["big", "Farm", 6.5])
pts.dataProvider().addFeatures([pp])
res = processing.run("biruni:eudrcheck", {"INPUT": pts, "PLOT_ID": "id", "PRODUCER": "producer",
                                          "AREA_FIELD": "ha", "COUNTRY": "VN", "COMMODITY": 2,
                                          "PROBLEMS": "memory:"}, context=ctx, feedback=fb)
check(res["PLOTS_WITH_ERRORS"] == 1, "point plot declared 6.5 ha needs a polygon")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
