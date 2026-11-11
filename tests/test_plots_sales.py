"""Plots from loose CAD lines, and the plot sales map.

Run:  engine\\bin\\python-qgis.bat tests\\test_plots_sales.py
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.lines_to_plots import is_length_like, number_candidates, side_lengths  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_plots_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def line_layer(lines, name="lines", crs="EPSG:32637"):
    lyr = QgsVectorLayer(f"LineString?crs={crs}&field=layer:string", name, "memory")
    feats = []
    for pl in lines:
        f = QgsFeature(lyr.fields())
        if isinstance(pl, QgsGeometry):
            f.setGeometry(pl)
        else:
            f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pl]))
        f.setAttributes(["0"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def text_layer(items, crs="EPSG:32637"):
    lyr = QgsVectorLayer(f"Point?crs={crs}&field=text:string&field=layer:string", "texts", "memory")
    feats = []
    for (x, y), t in items:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([t, "numbers"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


AR = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# ---- helpers -----------------------------------------------------------------------------------------
check(is_length_like("20.00") and is_length_like("٢٠٫٥٠") and is_length_like("600 م2") and is_length_like("15م")
      and is_length_like("12.5 m"), "lengths and areas are recognised")
check(not is_length_like("15") and not is_length_like("١٢") and not is_length_like("قطعة 7")
      and not is_length_like("12/أ"), "plot numbers are not lengths")
check(number_candidates(["20.00", "١٢", "شارع", "600 م2"]) == ["١٢"], "number candidates")
sq = [QgsPointXY(0, 0), QgsPointXY(0, 30), QgsPointXY(20, 30), QgsPointXY(20, 0), QgsPointXY(0, 0)]
mid = [QgsPointXY(0, 0), QgsPointXY(0, 15), QgsPointXY(0, 30), QgsPointXY(20, 30), QgsPointXY(20, 0),
       QgsPointXY(0, 0)]
check([round(s, 2) for s in side_lengths(sq, lambda a, b: a.distance(b))] == [20.0, 30.0, 20.0, 30.0],
      "side lengths clockwise from the north-west corner")
check([round(s, 2) for s in side_lengths(mid, lambda a, b: a.distance(b))] == [20.0, 30.0, 20.0, 30.0],
      "a vertex in the middle of a side does not split it")

# ---- a CAD-like plan ----------------------------------------------------------------------------------
# 2 x 3 blocks; a block is 2 rows x 5 plots of 20 x 30 m; streets 15 m; an outer boundary around all.
W, H, COLS, ROWS, STREET = 20.0, 30.0, 5, 2, 15.0
X0, Y0 = 500000.0, 2400000.0
random.seed(7)
lines, texts, expected = [], [], {}
plot_no = 0
blocks = [(bx, by) for by in range(2) for bx in range(3)]
undershoots = overshoots = 0
for b, (bx, by) in enumerate(blocks):
    ox = X0 + bx * (COLS * W + STREET)
    oy = Y0 + by * (ROWS * H + STREET)
    # horizontal lines: one piece per plot side (bottom, middle, top)
    for r in range(ROWS + 1):
        for c in range(COLS):
            lines.append([(ox + c * W, oy + r * H), (ox + (c + 1) * W, oy + r * H)])
    # vertical lines, per row, with defects
    for r in range(ROWS):
        for c in range(COLS + 1):
            x = ox + c * W
            y1, y2 = oy + r * H, oy + (r + 1) * H
            if b == 0 and r == 0 and c == 2:
                y2 -= 3.0      # a gap no tolerance closes: plots 3 and 4 of block 0 merge, reported
            elif b == 1 and r == 1 and c == 3:
                y1 += 1.5      # a gap closed by extending the line along its direction
            elif 0 < c < COLS and random.random() < 0.35:
                y2 -= 0.3      # an undershoot inside the tolerance
                undershoots += 1
            elif 0 < c < COLS and random.random() < 0.3:
                y2 += 0.4      # an overshoot past the line
                overshoots += 1
            lines.append([(x, y1), (x, y2)])
            if random.random() < 0.2:
                lines.append([(x, y1), (x, y2)])           # an exact duplicate
    for r in range(ROWS):
        for c in range(COLS):
            plot_no += 1
            cx, cy = ox + c * W + W / 2, oy + r * H + H / 2
            label = str(plot_no).translate(AR) if plot_no % 2 else str(plot_no)
            texts.append(((cx, cy), label))
            texts.append(((cx, oy + r * H + 1.0), "20.00"))       # a side length written inside the plot
            texts.append(((cx + 4, cy - 6), "600 م2"))            # its area
            expected[str(plot_no)] = (cx, cy)
# an outer boundary around the whole plan (makes a street area with holes, which must be dropped)
minx, miny = X0 - STREET / 2, Y0 - STREET / 2
maxx = X0 + 3 * COLS * W + 2 * STREET + STREET / 2
maxy = Y0 + 2 * ROWS * H + STREET + STREET / 2
lines.append([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny + 0.3)])  # nearly closed

lyr = line_layer(lines)
tl = text_layer(texts)
t0 = time.time()
res = processing.run("biruni:linestoplots", {"INPUT": [lyr], "TEXTS": tl, "TEXT_FIELD": "text", "TOLERANCE": 0.5,
                                             "EXTEND": 2.0, "MIN_AREA": 1.0, "ONLY_NUMBERED": False,
                                             "OUTPUT": "memory:", "PROBLEMS": "memory:"}, context=ctx, feedback=fb)
plots, probs = out(res["OUTPUT"]), out(res["PROBLEMS"])
print(f"     {res['PLOT_COUNT']} plots, {res['OPEN_ENDS']} open ends, {res['GAPS_CLOSED']} gaps closed "
      f"in {time.time() - t0:.2f}s ({undershoots} undershoots, {overshoots} overshoots drawn)")
by_no = {f["plot_no"]: f for f in plots.getFeatures() if f["plot_no"]}
check(res["PLOT_COUNT"] == 59, f"59 plots (60 minus the pair that cannot close): {res['PLOT_COUNT']}")
check(res["NUMBERED_COUNT"] == 59, f"every plot has a number: {res['NUMBERED_COUNT']}")
exact = [f for f in plots.getFeatures() if abs(f["area_m2"] - 600) < 0.01]
check(len(exact) >= 50, f"most plots are exactly 600 m²: {len(exact)}")
near = [f for f in plots.getFeatures() if abs(f["area_m2"] - 600) < 6 or abs(f["area_m2"] - 1200) < 6]
check(len(near) == 59, f"all areas right within snapping shifts: {len(near)}")
merged = [f for f in plots.getFeatures() if abs(f["area_m2"] - 1200) < 6]
check(len(merged) == 1 and "several numbers" in (merged[0]["note"] or ""),
      f"the unclosed pair is one plot with both numbers noted: {[m['note'] for m in merged]}")
check(all(f["plot_no"] and f["plot_no"].isdigit() for f in plots.getFeatures()), "numbers in Latin digits")
check(by_no.get("1") is not None and by_no["1"]["label"] == "١", "the label keeps the Arabic digits")
check("20.00" not in by_no and "600" not in by_no, "lengths and areas are never taken as plot numbers")
p7 = by_no.get("7")
check(p7 is not None and p7["n_sides"] == 4 and p7["sides"] == "20.00 - 30.00 - 20.00 - 30.00",
      f"plot 7 sides: {p7['sides'] if p7 else None}")
check(p7 is not None and abs(p7["perimeter_m"] - 100) < 0.01 and abs(p7["x"] - expected["7"][0]) < 0.01
      and abs(p7["y"] - expected["7"][1]) < 0.01, "perimeter and centroid")
ext = by_no.get("19")
check(ext is not None and abs(ext["area_m2"] - 600) < 0.01, "the 1.5 m gap was closed by extending the line")
kinds = [f["kind"] for f in probs.getFeatures()]
open_ends = [f for f in probs.getFeatures() if f["kind"] == "open_end"]
check(len(open_ends) == 1 and open_ends[0]["gap"] is not None and abs(open_ends[0]["gap"] - 3.0) < 0.01,
      f"one open end, gap 3 m: {[(f['kind'], f['gap']) for f in probs.getFeatures()]}")
oe = open_ends[0].geometry().asPoint() if open_ends else None
check(oe is not None and abs(oe.x() - (X0 + 2 * W)) < 0.01 and abs(oe.y() - (Y0 + H - 3)) < 0.01,
      "the open end is reported where it is")
check(not any(abs(f["area_m2"] - 600) > 6 and abs(f["area_m2"] - 1200) > 6 for f in plots.getFeatures()),
      "the street area around the blocks was dropped")

# only numbered: same result here (streets have no number), and without texts no notes
res2 = processing.run("biruni:linestoplots", {"INPUT": [lyr], "TOLERANCE": 0.5, "EXTEND": 2.0,
                                              "OUTPUT": "memory:", "PROBLEMS": "memory:"}, context=ctx, feedback=fb)
check(res2["PLOT_COUNT"] == 59 and res2["NUMBERED_COUNT"] == 0, "without texts: plots, no numbers")

# arcs, polygons as input and a layer in another CRS
arc = QgsGeometry.fromWkt(f"CompoundCurve(CircularString({X0} {Y0 - 200}, {X0 + 10} {Y0 - 206}, {X0 + 20} {Y0 - 200}),"
                          f"({X0 + 20} {Y0 - 200}, {X0 + 20} {Y0 - 170}, {X0} {Y0 - 170}, {X0} {Y0 - 200}))")
arc_lyr = line_layer([arc], "arc")
poly = QgsVectorLayer("Polygon?crs=EPSG:32637", "poly", "memory")
pf = QgsFeature()
pf.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({X0 + 40} {Y0 - 200},{X0 + 60} {Y0 - 200},{X0 + 60} {Y0 - 170},"
                                   f"{X0 + 40} {Y0 - 170},{X0 + 40} {Y0 - 200}))"))
poly.dataProvider().addFeatures([pf])
QgsProject.instance().addMapLayer(poly)
res3 = processing.run("biruni:linestoplots", {"INPUT": [arc_lyr, poly], "OUTPUT": "memory:", "PROBLEMS": "memory:"},
                      context=ctx, feedback=fb)
arcs = sorted(out(res3["OUTPUT"]).getFeatures(), key=lambda f: f["x"])
check(res3["PLOT_COUNT"] == 2 and arcs[0]["n_sides"] == 4 and 600 < arcs[0]["area_m2"] < 700
      and abs(arcs[1]["area_m2"] - 600) < 0.01,
      f"an arc side stays one side; polygons are read too: {[(f['n_sides'], f['sides']) for f in arcs]}")

# every plot drawn on its own as a closed polyline, shared sides almost (not exactly) on top of each other
jit = []
random.seed(3)
for r in range(3):
    for c in range(4):
        pts = [(c * W, r * H), (c * W, (r + 1) * H), ((c + 1) * W, (r + 1) * H), ((c + 1) * W, r * H)]
        pts = [(x + random.uniform(-0.03, 0.03), y + random.uniform(-0.03, 0.03)) for x, y in pts]
        jit.append(pts + [pts[0]])
jl = line_layer(jit, "jitter")
res5 = processing.run("biruni:linestoplots", {"INPUT": [jl], "OUTPUT": "memory:", "PROBLEMS": "memory:"},
                      context=ctx, feedback=fb)
jareas = [f["area_m2"] for f in out(res5["OUTPUT"]).getFeatures()]
check(res5["PLOT_COUNT"] == 12 and all(abs(a - 600) < 3 for a in jareas),
      f"plots drawn one by one with slightly different shared sides: 12 plots, no slivers ({len(jareas)})")

# ---- speed: 600 plots drawn as loose pieces with duplicates --------------------------------------------
big = []
for r in range(31):
    for c in range(30):
        big.append([(c * W, r * H), ((c + 1) * W, r * H)])
for r in range(30):
    for c in range(31):
        big.append([(c * W, r * H), (c * W, (r + 1) * H - (0.2 if (r + c) % 3 == 0 else 0))])
        if (r * c) % 5 == 0:
            big.append([(c * W, r * H), (c * W, (r + 1) * H)])
blyr = line_layer(big, "big")
t0 = time.time()
res4 = processing.run("biruni:linestoplots", {"INPUT": [blyr], "OUTPUT": "memory:", "PROBLEMS": "memory:"},
                      context=ctx, feedback=fb)
dt = time.time() - t0
check(res4["PLOT_COUNT"] == 900 and res4["OPEN_ENDS"] == 0, f"900 plots from {len(big)} pieces: {res4['PLOT_COUNT']}")
check(dt < 20, f"fast enough: {dt:.1f}s")

# ======================================================================================================
# the plot sales map
# ======================================================================================================
from biruni.share import sales_map as sm  # noqa: E402

check(sm.plot_key("١٥") == "15" and sm.plot_key("015") == "15" and sm.plot_key("قطعة 15") == "15"
      and sm.plot_key("Plot 15") == "15" and sm.plot_key(15.0) == "15", "plot keys: digits, zeros, prefixes")
check(sm.plot_key("north 1") == "north1" and sm.plot_key("12/أ") == "12ا" and sm.plot_key("A-12") == "a12",
      "plot keys keep real letters")
st = {v: sm.status_key(v)[0] for v in ("متاحة", "مباع", "محجوزة", "Sold", "under offer", "غير متاح", "موقوف", "",
                                       None, "تم البيع", "Available")}
check(st["متاحة"] == "available" and st["مباع"] == "sold" and st["محجوزة"] == "reserved" and st["Sold"] == "sold"
      and st["under offer"] == "reserved" and st["تم البيع"] == "sold" and st["Available"] == "available",
      f"statuses read in Arabic and English: {st}")
check(st["غير متاح"].startswith("x:") and st["موقوف"] == "x:موقوف" and sm.status_key("موقوف")[1] == "موقوف"
      and st[""] == "unknown" and st[None] == "unknown", "negated and custom statuses stay custom; empty is unknown")
check(sm.parse_price("1,250,000") == 1250000 and sm.parse_price("١٬٢٥٠٬٠٠٠ ريال") == 1250000
      and sm.parse_price("-5") is None and sm.parse_price("abc") is None and sm.parse_price(980000) == 980000,
      "prices from numbers and text")
check(sm.sales_phone("+966 50 123 4567") == "966501234567" and sm.sales_phone("00966501234567") == "966501234567"
      and sm.sales_phone("٠٥٠١٢٣٤٥٦٧") == "0501234567" and sm.sales_phone("123") == "", "sales phone numbers")

EVIL = '<script>alert(1)</script><img src=x onerror=alert(2)>'
plots_lyr = QgsVectorLayer("Polygon?crs=EPSG:32637&field=plot_no:string&field=status:string&field=price:double"
                           "&field=notes:string", "plots", "memory")
pfeats = []
plot_rows = [("١", "متاحة", 500000, "زاوية"), ("2", "مباع", 650000, None), ("3", "محجوزة", None, EVIL),
             ("4", "موقوف", 700000, ""), ("05", "", None, None), ("6", "available", 720000, "على شارعين")]
for i, (no, status, price, notes) in enumerate(plot_rows):
    f = QgsFeature(plots_lyr.fields())
    x = X0 + i * 20
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {Y0},{x + 20} {Y0},{x + 20} {Y0 + 30},{x} {Y0 + 30},{x} {Y0}))"))
    f.setAttributes([no, status, price, notes])
    pfeats.append(f)
plots_lyr.dataProvider().addFeatures(pfeats)
QgsProject.instance().addMapLayer(plots_lyr)


def page_data(path):
    text = open(path, encoding="utf-8").read()
    m = re.search(r'<script type="application/json" id="biruni-data">(.*?)</script>', text, re.S)
    return text, json.loads(m.group(1)), m.group(1)


html_path = os.path.join(TMP, "sales.html")
res = sm.export_sales_map(plots_lyr, html_path, "plot_no", status_field="status", price_field="price",
                          notes_field="notes", title="مخطط النخيل", phone="+966 50 123 4567", currency="ريال",
                          editor=True)
text, data, blob = page_data(html_path)
check(len(data["plots"]) == 6 and data["phone"] == "966501234567" and data["editor"] is False,
      "the page holds the 6 plots and the phone")
counts = {}
for p in data["plots"]:
    counts[p["s"]] = counts.get(p["s"], 0) + 1
check(counts == {"available": 2, "sold": 1, "reserved": 1, "x:موقوف": 1, "unknown": 1}, f"status counts: {counts}")
check(res.data.without_status == ["05"] and "05" in res.summary(), "plots without a status are listed")
check([s["key"] for s in data["statuses"]][:3] == ["available", "reserved", "sold"]
      and any(s["key"] == "x:موقوف" and s["label"] == "موقوف" for s in data["statuses"]),
      "standard statuses first, the custom one keeps its words")
p1 = next(p for p in data["plots"] if p["k"] == "1")
check(p1["n"] == "١" and p1["p"] == 500000 and abs(p1["a"] - 600) < 1 and p1["t"] == "زاوية",
      "plot card values (Arabic number kept, area measured)")
check("<" not in blob and "<script>alert" not in text and "<img" not in text,
      "nothing from the data can break out of the data block")
check(EVIL in json.dumps(data, ensure_ascii=False), "the notes are kept as text (escaped when shown)")
check(res.editor_path and os.path.exists(res.editor_path) and page_data(res.editor_path)[1]["editor"] is True,
      "the editor copy is written next to it")

# prices hidden from buyers, kept for the editor
hp = os.path.join(TMP, "noprice.html")
res_np = sm.export_sales_map(plots_lyr, hp, "plot_no", status_field="status", price_field="price", show_prices=False,
                             editor=True, map_id=res.map_id)
_, dnp, _ = page_data(hp)
_, dne, _ = page_data(res_np.editor_path)
check(all(p["p"] is None for p in dnp["plots"]) and dnp["prices"] is False
      and any(p["p"] for p in dne["plots"]) and dnp["map_id"] == res.map_id,
      "prices hidden from buyers but in the editor copy; the map keeps its id")

# statuses from an Excel sheet with Arabic headers under a title row
import openpyxl  # noqa: E402

xlsx = os.path.join(TMP, "statuses.xlsx")
wb = openpyxl.Workbook()
ws = wb.active
ws.append(["حالة قطع مخطط النخيل"])
ws.append([])
ws.append(["رقم القطعة", "الحالة", "السعر", "ملاحظات"])
ws.append(["١", "مباعة", "٥٥٠٬٠٠٠", "بيعت أمس"])
ws.append([2, "متاح", 640000, None])
ws.append(["قطعة 3", "محجوز", None, None])
ws.append(["99", "متاح", 1, None])
ws.append(["05", "Reserved", "900,000", None])
wb.save(xlsx)
rows, headers, problems = sm.read_status_table(xlsx)
check(set(headers) == {"no", "status", "price", "notes"} and len(rows) == 5, f"sheet columns found: {headers}")
res2 = sm.export_sales_map(plots_lyr, os.path.join(TMP, "sheet.html"), "plot_no", table=xlsx, price_field="price")
_, d2, _ = page_data(res2.path)
s2 = {p["k"]: (p["s"], p["p"]) for p in d2["plots"]}
check(s2["1"] == ("sold", 550000) and s2["2"] == ("available", 640000) and s2["3"][0] == "reserved"
      and s2["5"] == ("reserved", 900000) and s2["4"][0] == "unknown",
      f"statuses and prices come from the sheet: {s2}")
check(res2.data.unmatched == ["99"], f"the sheet row with no plot is reported: {res2.data.unmatched}")

# the page script is valid JavaScript
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",
                                                  r"C:\Program Files (x86)\nodejs\node.exe") if os.path.exists(p)), None)
if node:
    script = text.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    js = os.path.join(TMP, "page.js")
    open(js, "w", encoding="utf-8").write(script)
    r = subprocess.run([node, "--check", js], capture_output=True, text=True)
    check(r.returncode == 0, "page script passes node --check " + r.stderr[:300])
    # the search key in the page is the same as Biruni's
    probe = script.split("var msgTimer", 1)[0].split("function key(s)", 1)
    fn = "function digits" + script.split("function digits", 1)[1].split("var msgTimer", 1)[0]
    js2 = os.path.join(TMP, "keys.js")
    samples = ["١٥", "015", "قطعة 15", "Plot 15", "12/أ", "north 1", "A-12", "القطعة ٧"]
    open(js2, "w", encoding="utf-8").write(fn + "\nconsole.log(JSON.stringify(" + json.dumps(samples, ensure_ascii=False)
                                           + ".map(key)));\n")
    r = subprocess.run([node, js2], capture_output=True, text=True, encoding="utf-8")
    page_keys = json.loads(r.stdout or "[]")
    check(page_keys == [sm.plot_key(s) for s in samples], f"search keys match Biruni: {page_keys}")
else:
    print("     (node not found: page script not checked)")

# ---- statuses coming back from the editor copy -------------------------------------------------------
target = QgsVectorLayer("Polygon?crs=EPSG:32637&field=plot_no:string&field=notes:string", "target", "memory")
tf = []
for i, (no, *_rest) in enumerate(plot_rows):
    f = QgsFeature(target.fields())
    f.setGeometry(pfeats[i].geometry())
    f.setAttributes([no, "قديمة"])
    tf.append(f)
target.dataProvider().addFeatures(tf)
items = [{"n": "1", "s": "sold", "p": 555000, "t": "تم"}, {"n": "٢", "s": "x:موقوف مؤقتا", "l": "موقوف مؤقتا"},
         {"n": "3", "s": "reserved", "t": EVIL}, {"n": "77", "s": "sold"}, {"n": True, "s": "sold"},
         {"n": "4", "s": "hacked"}, {"n": "6", "s": "available", "p": "lots"}, {"n": "05", "s": "sold", "t": "x" * 5000}]
sfile = os.path.join(TMP, "back.biruni-sales.json")
json.dump(sm.status_file_payload(items, res.map_id), open(sfile, "w", encoding="utf-8"), ensure_ascii=False)
r = sm.import_sales_status(target, sfile, "plot_no", "status", "price", "notes", expected_map_id=res.map_id)
LBL = sm.status_labels()  # the words of the interface language
vals = {f["plot_no"]: (f["status"], f["price"], f["notes"]) for f in target.getFeatures()}
check(vals["١"] == (LBL["sold"], 555000, "تم") and vals["2"][0] == "موقوف مؤقتا" and vals["3"][2] == EVIL,
      f"statuses, prices and notes written (missing fields added): {vals['١']}, {vals['2']}")
check(r.not_found == ["77"] and r.updated == 5, f"updated {r.updated}, not found {r.not_found}")
check(any("plot 5" in w or "plot 4" in w or "(not readable)" in w for w in r.warnings)
      and any("price" in w for w in r.warnings) and any("shortened" in w for w in r.warnings)
      and vals["05"][2] and set(vals["05"][2]) == {"x"} and len(vals["05"][2]) <= sm.MAX_NOTES
      and vals["4"][0] is None,
      f"bad entries skipped, bad prices ignored, long notes cut: {r.warnings}")
check(not target.isEditable(), "the changes are saved")
r_other = sm.import_sales_status(target, sfile, "plot_no", "status", expected_map_id="another")
check(any("another sales map" in w for w in r_other.warnings) and r_other.updated == 0,
      "a file of another map is flagged; reading it twice changes nothing")

# not saved when asked, undone by rolling back
items2 = [{"n": "1", "s": "available"}]
s2file = os.path.join(TMP, "back2.json")
json.dump(sm.status_file_payload(items2), open(s2file, "w", encoding="utf-8"))
sm.import_sales_status(target, s2file, "plot_no", "status", commit=False)
check(target.isEditable() and next(f for f in target.getFeatures() if f["plot_no"] == "١")["status"]
      == LBL["available"], "with commit off the change waits in the edit session")
target.rollBack()
check(next(f for f in target.getFeatures() if f["plot_no"] == "١")["status"] == LBL["sold"], "and Undo takes it back")

# a sheet back: no notes column -> notes untouched
csvp = os.path.join(TMP, "back.csv")
open(csvp, "w", encoding="utf-8-sig").write("رقم القطعة,الحالة,السعر\n٣,مباعة,800000\n")
r3 = sm.import_sales_status(target, csvp, "plot_no", "status", "price", "notes")
v3 = next(f for f in target.getFeatures() if f["plot_no"] == "3")
check(r3.updated == 1 and v3["status"] == LBL["sold"] and v3["price"] == 800000 and v3["notes"] == EVIL,
      "a CSV sheet updates status and price and leaves the notes alone")

# files that are not statuses files
bad = os.path.join(TMP, "bad.json")
for content, what in (('{"format":"other","version":1,"plots":[]}', "another format"),
                      ('{"format":"biruni-sales-status","version":99,"plots":[]}', "a newer version"),
                      ("not json at all", "not JSON"), ('{"format":"biruni-sales-status","version":1,"plots":{}}',
                                                        "plots not a list")):
    open(bad, "w", encoding="utf-8").write(content)
    try:
        sm.parse_status_file(bad)
        check(False, f"refused: {what}")
    except sm.SalesFileError:
        check(True, f"refused: {what}")
big_file = os.path.join(TMP, "big.json")
open(big_file, "w", encoding="utf-8").write('{"format":"biruni-sales-status","version":1,"plots":[' +
                                            ",".join(['{"n":"1","s":"sold","t":"' + "y" * 900 + '"}'] * 2500) + "]}")
try:
    sm.parse_status_file(big_file)
    check(False, "a file over 2 MB is refused")
except sm.SalesFileError:
    check(True, "a file over 2 MB is refused")

# ---- the dialogs open --------------------------------------------------------------------------------
from biruni.share.sales_map_dialog import BringBackSalesDialog, SalesMapDialog  # noqa: E402


class _Iface:
    def mainWindow(self):
        return None

    def activeLayer(self):
        return plots_lyr


class _Plugin:
    iface = _Iface()
    activity = None


dlg = SalesMapDialog(_Plugin())
check(dlg.layer.currentLayer() is plots_lyr and dlg.number.currentField() == "plot_no"
      and dlg.status.currentField() == "status" and dlg.price.currentField() == "price",
      "the sales map dialog picks the plots layer and its fields")
dlg.out.edit.setText(os.path.join(TMP, "dialog.html"))
back = BringBackSalesDialog(_Plugin(), plots_lyr, "plot_no", "status")
check(back.number.currentField() == "plot_no" and back.status.currentField() == "status",
      "the bring-back dialog opens with the fields")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
