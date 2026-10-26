"""Standalone tests for the survey & CAD tools: COGO engine, traverse, parcel
subdivision, Helmert transformation and CAD to GIS.

Run:  engine\\bin\\python-qgis.bat tests\\test_survey.py
"""
import math
import os
import sys
import tempfile

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # the COGO panel test needs widgets, not a screen
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.survey import cogo, dxf  # noqa: E402
from biruni.survey.cogo_geom import traverse_geometry  # noqa: E402
from biruni.survey.transform import fit_affine, fit_rigid, fit_similarity, parse_pairs  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_survey_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


# ---- bearings, distances, radii ------------------------------------------
check(near(cogo.parse_bearing("45°30'15\""), 45.504166667, 1e-8), "DMS with symbols")
check(near(cogo.parse_bearing("45 30 15"), 45.504166667, 1e-8), "DMS with spaces")
check(near(cogo.parse_bearing("45-30-15"), 45.504166667, 1e-8), "DMS with dashes")
check(near(cogo.parse_bearing("45d30m15s"), 45.504166667, 1e-8), "DMS with d/m/s")
check(near(cogo.parse_bearing("123.5"), 123.5), "decimal azimuth")
check(near(cogo.parse_bearing("N45°30'E"), 45.5), "quadrant NE")
check(near(cogo.parse_bearing("S12d5mW"), 192 + 5 / 60, 1e-9), "quadrant SW")
check(near(cogo.parse_bearing("S 30 E"), 150), "quadrant SE with spaces")
check(near(cogo.parse_bearing("N10W"), 350), "quadrant NW")
check(near(cogo.parse_bearing("٤٥ ٣٠ ١٥"), 45.504166667, 1e-8), "Arabic-Indic digits")
check(near(cogo.parse_bearing("ش٤٥°٣٠'ق"), 45.5), "Arabic quadrant letters")
for bad in ("N95E", "45 75 00", "hello", ""):
    try:
        cogo.parse_bearing(bad)
        check(False, f"rejects bearing {bad!r}")
    except ValueError:
        check(True, f"rejects bearing {bad!r}")
check(cogo.format_bearing(45.504166667) == "45°30'15\"", "format DMS")
check(cogo.format_bearing(192.0833333, quadrant=True) == "S12°05'00\"W", "format quadrant")
check(near(cogo.parse_distance("٢٥٫٣٠ م"), 25.3), "Arabic distance with unit")
check(near(cogo.parse_distance("25,30"), 25.3), "comma decimal distance")
check(near(cogo.parse_distance("100 ft"), 30.48), "feet")
check(cogo.parse_radius("25 L") == (25.0, "L") and cogo.parse_radius("-25") == (25.0, "L")
      and cogo.parse_radius("R=40") == (40.0, "R") and cogo.parse_radius("") == (None, None)
      and cogo.parse_radius("يسار 12") == (12.0, "L"), "radius notations")

# ---- traverse: known closed square with a 0.03 / 0.02 misclosure -----------
courses = [cogo.course_from_text(b, d) for b, d in
           (("0", "100.00"), ("90", "100.03"), ("180", "100.02"), ("270", "100.00"))]
trav = cogo.Traverse((1000.0, 2000.0), courses)
dx, dy, lin = trav.misclosure()
check(near(dx, 0.03, 1e-9) and near(dy, -0.02, 1e-9), f"misclosure dX/dY = {dx:.4f}/{dy:.4f}")
check(near(lin, math.hypot(0.03, 0.02), 1e-9), "linear misclosure")
check(near(trav.precision(), 400.05 / math.hypot(0.03, 0.02), 1e-6), f"precision 1:{trav.precision():.0f}")
adj = trav.adjust()
check(near(adj[-1][0], 1000.0, 1e-9) and near(adj[-1][1], 2000.0, 1e-9), "Bowditch closes on the start")
k1 = 100.0 / 400.05
check(near(adj[1][0], 1000.0 - 0.03 * k1, 1e-9) and near(adj[1][1], 2100.0 + 0.02 * k1, 1e-9),
      "Bowditch first station")
g = traverse_geometry(trav, True)
check(near(g.area(), trav.area(), 1e-6), f"traverse polygon area {g.area():.4f} = engine {trav.area():.4f}")

# Curve: half-disc of radius 10 closing on its diameter (arc to the left of travel going east->west? check both)
semi = cogo.Traverse((0.0, 0.0), [cogo.Course(90, 20.0), cogo.Course(270, 20.0, radius=10.0, side="R")])
check(near(semi.area(), math.pi * 50, 1e-6), f"half-disc area {semi.area():.6f} = {math.pi * 50:.6f}")
gs = traverse_geometry(semi, True)
check(near(gs.area(), math.pi * 50, 1e-4), f"half-disc geometry area {gs.area():.6f} (true arc)")
check(gs.constGet().hasCurvedSegments(), "traverse keeps the arc as a curve")
arc_len = cogo.Course(0, math.pi * 5, radius=10, side="R", arc_length=True)
check(near(arc_len.chord(), 2 * 10 * math.sin(math.pi / 4), 1e-9), "chord from arc length")

# Traverse algorithm on a table
tbl = QgsVectorLayer("None?field=no:integer&field=brg:string&field=dist:string&field=rad:string", "t", "memory")
rows = [(2, "N90E", "50", ""), (1, "0 00 00", "50", ""), (3, "S0E", "50", ""), (4, "270", "50", "")]
fs = []
for r in rows:
    f = QgsFeature(tbl.fields())
    f.setAttributes(list(r))
    fs.append(f)
tbl.dataProvider().addFeatures(fs)
res = processing.run("biruni:traverse", {"INPUT": tbl, "BEARING_FIELD": "brg", "DISTANCE_FIELD": "dist",
                                         "RADIUS_FIELD": "rad", "ORDER_FIELD": "no", "START": "500000,3000000 [EPSG:32636]",
                                         "CRS": "EPSG:32636", "CLOSED": True, "ADJUST": True, "KIND": 0,
                                         "OUTPUT": "memory:", "POINTS": "memory:"}, context=ctx, feedback=fb)
poly = next(out(res["OUTPUT"]).getFeatures())
check(near(res["AREA"], 2500.0, 1e-6) and near(poly.geometry().area(), 2500.0, 1e-6),
      f"traverse algorithm area {res['AREA']}")
check(out(res["POINTS"]).featureCount() == 5, "traverse stations")

# ---- subdivision ---------------------------------------------------------
def poly_layer(wkts, crs="EPSG:32636"):
    lyr = QgsVectorLayer(f"Polygon?crs={crs}&field=name:string", "p", "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes([f"p{i}"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


rect = "POLYGON((0 0, 100 0, 100 40, 0 40, 0 0))"
lshape = "POLYGON((0 0, 60 0, 60 20, 20 20, 20 60, 0 60, 0 0))"  # concave, area 2000
res = processing.run("biruni:subdividepolygon", {"INPUT": poly_layer([rect, lshape]), "MODE": 0, "PARTS": 3,
                                                 "DIRECTION": 1, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
parts = list(out(res["OUTPUT"]).getFeatures())
by_parent = {}
for f in parts:
    by_parent.setdefault(f["name"], []).append(f)
for name, total in (("p0", 4000.0), ("p1", 2000.0)):
    ps = by_parent.get(name, [])
    areas = [f.geometry().area() for f in ps]
    check(len(ps) == 3, f"{name}: 3 parts")
    check(all(abs(a - total / 3) <= 1e-4 for a in areas), f"{name}: each part = {total / 3:.4f} ± 0.0001 "
                                                          f"({', '.join(f'{a:.5f}' for a in areas)})")
    check(near(sum(areas), total, 1e-6), f"{name}: parts sum to the parcel")
    check(all(abs(f["part_area"] - f.geometry().area()) < 1e-6 for f in ps), f"{name}: part_area field")
res = processing.run("biruni:subdividepolygon", {"INPUT": poly_layer([rect]), "MODE": 1, "AREA": 1500,
                                                 "DIRECTION": 1, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
areas = sorted(f.geometry().area() for f in out(res["OUTPUT"]).getFeatures())
check(len(areas) == 3 and near(areas[0], 1000, 1e-4) and near(areas[1], 1500, 1e-4) and near(areas[2], 1500, 1e-4),
      f"parts of 1500 + remainder 1000: {[round(a, 5) for a in areas]}")
res = processing.run("biruni:subdividepolygon", {"INPUT": poly_layer([rect]), "MODE": 2, "AREA": 600,
                                                 "DIRECTION": 2, "BEARING": 30, "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
areas = sorted(f.geometry().area() for f in out(res["OUTPUT"]).getFeatures())
check(len(areas) == 2 and near(areas[0], 600, 1e-4) and near(areas[1], 3400, 1e-4),
      f"cut off 600 at bearing 30: {[round(a, 5) for a in areas]}")
# Geographic parcel: measured in UTM
geo = "POLYGON((31.2 30.0, 31.201 30.0, 31.201 30.001, 31.2 30.001, 31.2 30.0))"
res = processing.run("biruni:subdividepolygon", {"INPUT": poly_layer([geo], "EPSG:4326"), "MODE": 0, "PARTS": 4,
                                                 "DIRECTION": 0, "OUTPUT": "memory:"}, context=ctx, feedback=fb)
pa = [f["part_area"] for f in out(res["OUTPUT"]).getFeatures()]
check(len(pa) == 4 and max(pa) - min(pa) < 2e-4, f"geographic parcel in 4 equal UTM parts ({pa[0]:.4f} m²)")

# ---- Helmert / similarity ---------------------------------------------------
theta, s, tx, ty = math.radians(30), 1.5, 500000.0, 3000000.0


def true_t(x, y):
    return (s * (x * math.cos(theta) - y * math.sin(theta)) + tx, s * (x * math.sin(theta) + y * math.cos(theta)) + ty)


src = [(0, 0), (100, 0), (100, 50), (0, 80)]
pairs = [(p, true_t(*p)) for p in src]
t2 = fit_similarity(pairs[:2])
check(near(t2.scale, 1.5, 1e-9) and near(t2.rotation, 30, 1e-9), "2-point similarity exact")
t4 = fit_similarity(pairs)
check(t4.rmse < 1e-6 and near(t4.apply(50, 50)[0], true_t(50, 50)[0], 1e-6), "4-point similarity RMSE 0")
noisy = [(p, (q[0] + e, q[1] - e)) for (p, q), e in zip(pairs, (0.01, -0.02, 0.015, 0.0))]
tn = fit_similarity(noisy)
check(0.005 < tn.rmse < 0.03 and len(tn.residuals) == 4, f"noisy fit RMSE {tn.rmse:.4f}")
rig = fit_rigid([(p, (q[0], q[1])) for p, q in [((0, 0), (10, 10)), ((10, 0), (10, 20))]])
check(near(rig.scale, 1.0) and near(rig.rotation, 90, 1e-9), "rigid: rotation only")
aff = fit_affine([((0, 0), (0, 0)), ((1, 0), (2, 0)), ((0, 1), (0, 3)), ((1, 1), (2, 3))])
check(near(aff.apply(2, 2)[0], 4) and near(aff.apply(2, 2)[1], 6), "affine with different X/Y scales")
check(parse_pairs("0,0>10,10; 1 0 -> 11 10\n٢,٠=>12,10") == [((0, 0), (10, 10)), ((1, 0), (11, 10)),
                                                              ((2, 0), (12, 10))], "control point text")
lyr = poly_layer(["POLYGON((0 0, 100 0, 100 50, 0 50, 0 0))"], "EPSG:32636")
text = "; ".join(f"{p[0]},{p[1]} > {q[0]},{q[1]}" for p, q in pairs)
res = processing.run("biruni:similaritytransform", {"INPUT": lyr, "CONTROL_TEXT": text, "METHOD": 0,
                                                    "OUTPUT": "memory:", "RESIDUALS": "memory:"},
                     context=ctx, feedback=fb)
moved = next(out(res["OUTPUT"]).getFeatures()).geometry()
check(near(moved.area(), 5000 * 1.5 ** 2, 1e-4), f"moved area scaled by s² ({moved.area():.4f})")
v0 = moved.vertexAt(0)
check(near(v0.x(), tx, 1e-6) and near(v0.y(), ty, 1e-6), "origin lands on its real position")
check(out(res["RESIDUALS"]).featureCount() == 4 and res["RMSE"] < 1e-6, "residual table")
ctl = QgsVectorLayer("LineString?crs=EPSG:32636", "c", "memory")
cf = []
for p, q in pairs[:2]:
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt(f"LINESTRING({p[0]} {p[1]}, {q[0]} {q[1]})"))
    cf.append(f)
ctl.dataProvider().addFeatures(cf)
res = processing.run("biruni:similaritytransform", {"INPUT": lyr, "CONTROL": ctl, "METHOD": 0, "OUTPUT": "memory:"},
                     context=ctx, feedback=fb)
check(near(res["SCALE"], 1.5, 1e-9), "control lines layer")

# ---- MTEXT / xarb cleaning ------------------------------------------------
check(dxf.clean_mtext(r"{\fArial|b1|i0|c178|p2;\C1;Plot}\P12") == "Plot 12", "MTEXT codes stripped, braces kept")
check(dxf.clean_mtext(r"A\~B \S1^2; \\path \{x\}") == r"A B 1/2 \path {x}", "MTEXT escapes")
check(dxf.clean_mtext(r"\U+0642\U+0637\U+0639\U+0629") == "قطعة", "\\U+ escapes")
check(dxf.clean_text("%%c10 45%%d") == "Ø10 45°", "TEXT %% codes")
check(dxf.decode_xarb("pl] uvH[ l{vfD") == "حمد عراج مجربي", "xarb decode")
check(dxf.decode_xarb("ahvUuvQ 8.00 ljv") == "شارععرض 8.00 متر", "xarb keeps 8.00")


# ---- CAD to GIS on a synthetic drawing --------------------------------------
def pairs_text(*pairs_):
    return "".join(f"{c}\n{v}\n" for c, v in pairs_)


def lw(handle, layer, verts, flag=1, extra=()):
    s = pairs_text((0, "LWPOLYLINE"), (5, handle), (100, "AcDbEntity"), *extra, (8, layer),
                   (100, "AcDbPolyline"), (90, len(verts)), (70, flag))
    for v in verts:
        s += pairs_text((10, v[0]), (20, v[1]))
        if len(v) > 2:
            s += pairs_text((42, v[2]))
    return s


def text(handle, layer, x, y, value, style="STANDARD"):
    return pairs_text((0, "TEXT"), (5, handle), (8, layer), (10, x), (20, y), (40, 1.0), (1, value), (7, style))


entities = "".join([
    lw("A1", "Parcels", [(0, 0), (10, 0), (10, 10), (0, 10)]),
    lw("A2", "Parcels", [(20, 20), (30, 20), (30, 30), (20, 30), (20, 20)], flag=128),
    lw("A3", "Parcels", [(40, 0, 1.0), (50, 0), (50, 10), (40, 10)]),
    pairs_text((0, "POLYLINE"), (5, "A4"), (8, "Old"), (66, 1), (70, 1)),
    *[pairs_text((0, "VERTEX"), (8, "Old"), (10, x), (20, y)) for x, y in ((60, 0), (70, 0), (70, 10), (60, 10))],
    pairs_text((0, "SEQEND"), (8, "Old")),
    lw("A5", "Parcels", [(80, 0), (80, 10), (80, 0), (90, 0), (90, 10), (80, 10)]),
    lw("A6", "Paper", [(0, 0), (500, 0), (500, 500)], extra=((67, 1),)),
    lw("A7", "Open", [(95, 95), (105, 95), (105, 105), (95, 105)], flag=0),
    text("B1", "Labels", 5, 5, "P1"),
    "TEXT_ARABIC",
    text("B3", "Labels", 45, 5, "pl] uvH[", style="ARAB"),
    pairs_text((0, "MTEXT"), (5, "B4"), (8, "Labels"), (10, 65), (20, 5), (40, 1.0),
               (1, r"{\fArial|b1;\C1;Plot}\P12")),
    text("B5", "Labels", 100, 100, "Free"),
    pairs_text((0, "INSERT"), (5, "C1"), (8, "Trees"), (66, 1), (2, "TREE"), (10, 5), (20, 50), (50, 15)),
    pairs_text((0, "ATTRIB"), (5, "C2"), (8, "Trees"), (10, 5), (20, 50), (1, "Palm"), (2, "TYPE")),
    pairs_text((0, "ATTRIB"), (5, "C3"), (8, "Trees"), (10, 5), (20, 51), (1, "5"), (2, "HEIGHT")),
    pairs_text((0, "SEQEND"), (5, "C4"), (8, "Trees")),
    pairs_text((0, "LINE"), (5, "D1"), (8, "Lines"), (10, 0), (20, 100), (11, 10), (21, 100)),
    pairs_text((0, "LINE"), (5, "D2"), (8, "Lines"), (10, 10), (20, 100), (11, 5), (21, 110)),
    pairs_text((0, "LINE"), (5, "D3"), (8, "Lines"), (10, 5), (20, 110), (11, 0), (21, 100)),
    pairs_text((0, "CIRCLE"), (5, "E1"), (8, "Wells"), (10, 200), (20, 0), (40, 5)),
    pairs_text((0, "ARC"), (5, "E2"), (8, "Arcs"), (10, 300), (20, 0), (40, 10), (50, 0), (51, 90)),
])
header = pairs_text((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1015"), (9, "$DWGCODEPAGE"),
                    (3, "ANSI_1256"), (0, "ENDSEC"),
                    (0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "STYLE"),
                    (0, "STYLE"), (2, "STANDARD"), (3, "txt"), (0, "STYLE"), (2, "ARAB"), (3, "xarb.shx"),
                    (0, "ENDTAB"), (0, "ENDSEC"),
                    (0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (8, "0"), (2, "TREE"), (10, 0), (20, 0))
blocks = lw("F1", "0", [(-1, -1), (1, -1), (1, 1), (-1, 1)]) + pairs_text((0, "ENDBLK"), (0, "ENDSEC"))
body = header + blocks + pairs_text((0, "SECTION"), (2, "ENTITIES")) + entities + pairs_text((0, "ENDSEC"),
                                                                                             (0, "EOF"))
arabic = text("B2", "Labels", 25, 25, "قطعة 7")
raw = body.replace("TEXT_ARABIC", arabic).encode("cp1256")
path = os.path.join(TMP, "synthetic.dxf")
with open(path, "wb") as fh:
    fh.write(raw)

d = dxf.read(path)
check(d.encoding == "cp1256", f"encoding from $DWGCODEPAGE ({d.encoding})")
check(d.skipped_paper == 1 and d.blocks_ignored == 1, "paper space skipped, block definitions not drawn")

gpkg = os.path.join(TMP, "cad.gpkg")
res = processing.run("biruni:cadtogis", {"INPUT": path, "CRS": "EPSG:32636", "SPLIT": 0, "LABELS": True,
                                         "BUILD_POLYGONS": True, "KEEP_CURVES": True, "OUTPUT": gpkg},
                     context=ctx, feedback=fb)
check(sorted(res["LAYERS"]) == ["blocks", "lines", "polygons", "texts"], f"layers {res['LAYERS']}")
polys = {f["handle"] or f["source"]: f for f in QgsVectorLayer(f"{gpkg}|layername=polygons", "p", "ogr").getFeatures()}
check(set(polys) == {"A1", "A2", "A3", "A4", "A5", "E1", "built from lines"}, f"polygons {sorted(polys)}")
check(near(polys["A2"]["area"], 100.0), "closed without the flag (70=128) -> polygon")
check(near(polys["A3"]["area"], 100 + math.pi * 12.5, 1e-6), f"bulge arc exact area {polys['A3']['area']:.6f}")
check(polys["A3"].geometry().constGet().hasCurvedSegments(), "bulge kept as a true arc")
check(near(polys["A4"]["area"], 100.0), "old R12 POLYLINE read")
check(near(polys["A5"]["area"], 100.0) and polys["A5"].geometry().constGet().nCoordinates() == 5,
      "spike removed (rectangle with 4 corners)")
check(near(polys["E1"]["area"], math.pi * 25, 1e-6), f"circle exact area {polys['E1']['area']:.6f}")
check(near(polys["built from lines"]["area"], 50.0), "polygon built from 3 lines")
check(polys["A1"]["label"] == "P1", "text inside polygon -> label")
check(polys["A2"]["label"] == "قطعة 7", f"Arabic (cp1256) label: {polys['A2']['label']}")
check(polys["A3"]["label"] == "حمد عراج", f"xarb label: {polys['A3']['label']}")
check(polys["A4"]["label"] == "Plot 12", f"MTEXT label: {polys['A4']['label']}")
texts = {f["handle"]: f for f in QgsVectorLayer(f"{gpkg}|layername=texts", "t", "ogr").getFeatures()}
check(texts["B5"]["in_polygon"] in (False, 0), "open polyline does not swallow a text")
blocks = list(QgsVectorLayer(f"{gpkg}|layername=blocks", "b", "ogr").getFeatures())
check(len(blocks) == 1 and blocks[0]["block"] == "TREE" and blocks[0]["TYPE"] == "Palm"
      and blocks[0]["HEIGHT"] == "5" and near(blocks[0]["rotation"], 15), "block with attributes")
lines = {f["handle"]: f for f in QgsVectorLayer(f"{gpkg}|layername=lines", "l", "ogr").getFeatures()}
check("A7" in lines and near(lines["E2"]["length"], math.pi * 5, 1e-6), f"ARC exact length {lines['E2']['length']:.6f}")
check("A6" not in lines and "A6" not in polys, "paper-space entity not imported")

res = processing.run("biruni:cadtogis", {"INPUT": path, "CRS": "EPSG:32636", "SPLIT": 1, "LABELS": True,
                                         "KEEP_CURVES": False, "OUTPUT": os.path.join(TMP, "cad2.gpkg")},
                     context=ctx, feedback=fb)
check("Parcels_polygons" in res["LAYERS"] and "Trees_blocks" in res["LAYERS"] and "Old_polygons" in res["LAYERS"],
      f"one layer per CAD layer: {res['LAYERS']}")
lin = QgsVectorLayer(f"{os.path.join(TMP, 'cad2.gpkg')}|layername=Parcels_polygons", "x", "ogr")
a3 = next(f for f in lin.getFeatures() if f["handle"] == "A3")
check(not a3.geometry().constGet().hasCurvedSegments() and abs(a3.geometry().area() - polys["A3"]["area"]) < 0.002,
      f"curves densified on request (area {a3.geometry().area():.4f})")

# UTF-8 drawing (AutoCAD 2007+) with \U+ escapes
utf = (pairs_text((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1027"), (0, "ENDSEC"),
                  (0, "SECTION"), (2, "ENTITIES")) + lw("U1", "طبقة", [(0, 0), (10, 0), (10, 10), (0, 10)])
       + text("U2", "طبقة", 5, 5, "مخطط \\U+0661") + pairs_text((0, "ENDSEC"), (0, "EOF")))
upath = os.path.join(TMP, "utf8.dxf")
with open(upath, "w", encoding="utf-8") as fh:
    fh.write(utf)
res = processing.run("biruni:cadtogis", {"INPUT": upath, "CRS": "EPSG:32636", "SPLIT": 1,
                                         "OUTPUT": os.path.join(TMP, "utf.gpkg")}, context=ctx, feedback=fb)
ul = QgsVectorLayer(f"{os.path.join(TMP, 'utf.gpkg')}|layername={res['LAYERS'][0]}", "u", "ogr")
uf = next(ul.getFeatures())
check(uf["cad_layer"] == "طبقة" and uf["label"] == "مخطط ١", f"UTF-8 drawing: {uf['cad_layer']} / {uf['label']}")

# A real survey file from the field (optional: set BIRUNI_REAL_DXF to its path)
REAL = os.environ.get("BIRUNI_REAL_DXF", "")
if REAL and os.path.exists(REAL):
    res = processing.run("biruni:cadtogis", {"INPUT": REAL, "CRS": "EPSG:32637", "SPLIT": 0,
                                             "OUTPUT": os.path.join(TMP, "real.gpkg")}, context=ctx, feedback=fb)
    rl = QgsVectorLayer(f"{os.path.join(TMP, 'real.gpkg')}|layername=polygons", "r", "ogr")
    bounds = [f for f in rl.getFeatures() if f["cad_layer"] == "حدود المخطط"]
    check(bounds and any(abs(f["area"] - 3444) < 1 for f in bounds),
          f"real file: unflagged plan boundary found ({[round(f['area'], 2) for f in bounds]})")
    labelled = sum(1 for f in rl.getFeatures() if f["label"])
    print(f"     real file: {rl.featureCount()} polygons, {labelled} with labels")

# ---- COGO panel (offscreen, with a minimal stand-in for the QGIS interface) --------
from qgis.core import QgsCoordinateReferenceSystem  # noqa: E402
from qgis.gui import QgsMapCanvas, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.survey.cogo_dock import CogoDock  # noqa: E402
from biruni.theme import TOKENS  # noqa: E402


class _Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32636"))
        self.bar = QgsMessageBar(self.mw)

    def mainWindow(self):  # noqa: N802
        return self.mw

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def activeLayer(self):  # noqa: N802
        return None

    def messageBar(self):  # noqa: N802
        return self.bar


class _Theme:
    def tokens(self):
        return TOKENS["light"]


class _Plugin:
    def __init__(self):
        self.iface = _Iface()
        self.theme = _Theme()


plugin = _Plugin()
dock = CogoDock(plugin)
dock.refresh_theme()
plugin.iface.mw.show()
dock.show()
dock.x.setText("500000")
dock.y.setText("3000000")
dock.table.setCurrentCell(0, 0)
dock.table.paste("0\t40\n90\t40\n180\t40.02\n270\t40")
trav, bad = dock.compute()
check(not bad and trav is not None and len(trav.courses) == 4, "panel: 4 courses pasted from Excel")
check("1:" in dock.result.text() and "0.0200" in dock.result.text(), f"panel readout: {dock.result.text()!r}")
check(dock.band is not None, "panel: live preview drawn")
f = dock.create("polygon")
check(f is not None and near(f.geometry().area(), trav.area(), 1e-4), f"panel: polygon created ({trav.area():.4f} m²)")
cogo_layers = [lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.customProperty("biruni/cogo")]
check(len(cogo_layers) == 1 and cogo_layers[0].featureCount() == 1, "panel: COGO layer holds the parcel")
dock.table.paste("45\tabc")
check(dock.table.courses()[1] == [5], "panel: unreadable row reported")
dock.shutdown()

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
