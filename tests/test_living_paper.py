r"""Living paper (living_paper/): the map inside the QR code, the code on a print layout, the phone page.

Run:  engine\bin\python-qgis.bat tests\test_living_paper.py

The page's own JavaScript decoder is run in node on addresses made here, so the phone reads exactly what Biruni
wrote.
"""
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsLayoutExporter, QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize,
                       QgsPointXY, QgsPrintLayout, QgsProject, QgsRectangle, QgsVectorLayer)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.living_paper import codec, decode, encode, public_page  # noqa: E402
from biruni.living_paper import layout as lp  # noqa: E402
from biruni.living_paper import page as lpage  # noqa: E402

provider = BiruniProvider()  # keep a Python reference, or its virtual methods die with the wrapper
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


work = tempfile.mkdtemp()
random.seed(7)
QR_CHARS = set(codec.ALPHABET)

# ---- the encoding ------------------------------------------------------------------------------------------------
check(all(codec.b43decode(codec.b43encode(b)) == b for b in (b"", b"\x00", b"\xff\xff", bytes(range(256)),
                                                              os.urandom(999))),
      "43-letter text gives back the same bytes, any length")
corners = [(46.700, 24.700), (46.720, 24.700), (46.720, 24.715), (46.700, 24.715)]
title = "مخطط حي النرجس — الرياض"
pts = [(46.70 + random.random() * 0.02, 24.70 + random.random() * 0.015, f"مدرسة {i}") for i in range(12)]
ring = [(46.71 + 0.004 * math.cos(a / 60 * 2 * math.pi), 24.707 + 0.003 * math.sin(a / 60 * 2 * math.pi))
        for a in range(60)]
url, rep = encode(title, corners, "1:2,500", pts, [("polygon", ring), ("line", [(46.701, 24.701), (46.719, 24.714)])],
                  lang="ar")
d = decode(url)
check(url.startswith(codec.PAGE_URL + "#") and set(url.split("#", 1)[1]) <= QR_CHARS,
      "the map is after '#', in letters the QR code's compact mode can carry")
check(d["title"] == title and d["scale"] == "1:2,500" and d["lang"] == "ar", "Arabic title, scale and language survive")
tol = 0.6 / 10 ** d["decimals"]
check(all(abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol for a, b in zip(d["corners"], corners)),
      f"corners come back to {d['decimals']} decimals")
check(len(d["points"]) == 12 and sorted(p[2] for p in d["points"]) == sorted(p[2] for p in pts),
      "all points and their Arabic labels come back")
check(all(any(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol and p[2] == q[2] for q in pts) for p in d["points"]),
      "each point is where it was")
check([s["kind"] for s in d["shapes"]] == ["polygon", "line"] and len(d["shapes"][1]["coords"]) == 2
      and 8 <= len(d["shapes"][0]["coords"]) <= 60, "the outline and the line come back (outline lightly simplified)")
check(rep["points"] == 12 and rep["shapes"] == 2 and rep["chars"] == len(url) and not rep["labels_shortened"],
      "the report says everything was kept")
u2, _r = encode("Site", corners)
check(decode(u2)["points"] == [] and decode(u2)["shapes"] == [] and decode(u2.split("#")[1])["title"] == "Site",
      "a bare map (frame and title only) works, from the address or the part after '#'")
check(decode(u2.lower()) == decode(u2), "a scanner that lower-cases the code part does not matter")

# ---- long content degrades gracefully ---------------------------------------------------------------------------
many = [(46.70 + random.random() * 0.02, 24.70 + random.random() * 0.015, f"Point of interest number {i} " * 2)
        for i in range(300)]
big_ring = [(46.71 + 0.006 * math.cos(a / 3000 * 2 * math.pi) * (1 + 0.05 * math.sin(a / 7)),
             24.707 + 0.005 * math.sin(a / 3000 * 2 * math.pi)) for a in range(3000)]
prev_points = None
for limit in (1500, 900, 400, 160):
    u, r = encode("A long title " * 3, corners, "1:10,000", many, [("polygon", big_ring)], max_chars=limit)
    dd = decode(u)
    check(len(u) <= limit and r["chars"] == len(u), f"address within {limit} characters ({len(u)})")
    check(len(dd["points"]) == r["points"] <= codec.MAX_POINTS and len(dd["shapes"]) == r["shapes"],
          f"the report at {limit} matches what the code holds ({r['points']} points, {r['shapes']} shapes, "
          f"{r['vertices']} vertices)")
    if prev_points is not None:
        check(r["points"] <= prev_points, f"a shorter address keeps no more points ({limit})")
    prev_points = r["points"]
_u, r1500 = encode("t", corners, "", many, [("polygon", big_ring)])
check(r1500["points_given"] == 300 and 0 < r1500["points"] <= 40 and r1500["labels_shortened"]
      and r1500["vertices"] < 3000, "1500 characters: points thinned to the 40 most spread, labels shortened, "
                                    "the outline simplified")
dd = decode(_u)
xs = [p[0] for p in dd["points"]]
check(max(xs) - min(xs) > 0.012, "the kept points still cover the map")
_u, r_small = encode("Only the frame fits here " * 10, corners, "", many, [("polygon", big_ring)], max_chars=120)
check(len(_u) <= 120 and r_small["points"] == 0 and r_small["title_shortened"],
      "a tiny limit keeps the frame and a shortened title")
check(raises(lambda: encode("t", corners, max_chars=60)), "an impossible limit is an error, not a broken code")
check(raises(lambda: encode("t", corners[:3])) and raises(lambda: encode("t", [(200, 0)] * 4)),
      "a frame that is not 4 longitude/latitude corners is refused")

# ---- broken addresses ------------------------------------------------------------------------------------------
good = url.split("#", 1)[1]
for bad, why in ((codec.PAGE_URL, "no '#'"), (codec.PAGE_URL + "#", "empty"), ("#Z12", "unknown format"),
                 ("#" + good[:len(good) // 2], "cut in half"), ("#A" + "!" * 6, "bad letters"),
                 ("#" + good[:-1], "one letter short"),
                 ("#A" + codec.b43encode(bytes([2]) + b"\x06"), "a newer version")):
    check(raises(lambda b=bad: decode(b)), f"a broken address is refused ({why})")

# ---- a real print layout -----------------------------------------------------------------------------------------
project = QgsProject.instance()
utm = QgsCoordinateReferenceSystem("EPSG:32638")
x0, y0 = 670000.0, 2733000.0  # Riyadh
pts_layer = QgsVectorLayer("Point?crs=EPSG:32638&field=name:string", "places", "memory")
feats = []
for i in range(25):
    f = QgsFeature(pts_layer.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x0 + 50 + (i % 5) * 180, y0 + 40 + (i // 5) * 130)))
    f["name"] = f"مسجد {i}" if i % 2 else f"School {i}"
    feats.append(f)
far = QgsFeature(pts_layer.fields())
far.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x0 + 50000, y0)))
far["name"] = "far away"
feats.append(far)
pts_layer.dataProvider().addFeatures(feats)
poly_layer = QgsVectorLayer("Polygon?crs=EPSG:32638&field=zone:string", "zones", "memory")
pf = QgsFeature(poly_layer.fields())
pf.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x0 + 100 + 300 * math.cos(a / 40 * 2 * math.pi),
                                                      y0 + 300 + 250 * math.sin(a / 40 * 2 * math.pi))
                                           for a in range(40)]]))
pf["zone"] = "Zone A"
poly_layer.dataProvider().addFeatures([pf])
project.addMapLayers([pts_layer, poly_layer])
project.setCrs(utm)


def make_layout(name, rotation=0.0):
    lay = QgsPrintLayout(project)
    lay.initializeDefaults()
    lay.setName(name)
    mp = QgsLayoutItemMap(lay)
    mp.setId("map")
    lay.addLayoutItem(mp)
    mp.attemptMove(QgsLayoutPoint(10, 20))
    mp.attemptResize(QgsLayoutSize(277, 170))
    mp.setCrs(utm)
    mp.setLayers([pts_layer, poly_layer])
    mp.setExtent(QgsRectangle(x0, y0, x0 + 1000, y0 + 640))
    mp.setMapRotation(rotation)
    title_item = QgsLayoutItemLabel(lay)
    title_item.setText("Al Narjis district")
    fmt = title_item.textFormat()
    fmt.setSize(20)
    title_item.setTextFormat(fmt)
    lay.addLayoutItem(title_item)
    title_item.attemptMove(QgsLayoutPoint(10, 5))
    title_item.attemptResize(QgsLayoutSize(150, 12))
    project.layoutManager().addLayout(lay)
    return lay, mp


lay, mp = make_layout("Narjis A4")
check(lp.layout_title(lay) == "Al Narjis district", "the title is read from the layout's largest text")
check(lp.scale_text(mp).startswith("1:"), "the scale is written 1:n")
# something already sits in the reading side's bottom corner: the code must go elsewhere
legend_like = QgsLayoutItemLabel(lay)
legend_like.setText("Legend")
lay.addLayoutItem(legend_like)
legend_like.attemptMove(QgsLayoutPoint(10 + 277 - 2.5 - 60, 20 + 170 - 2.5 - 50))
legend_like.attemptResize(QgsLayoutSize(60, 50))
url_l, rep_l, pic = lp.add_live_qr(lay, pts_layer, "name", lang="en")
dl = decode(url_l)
cap = lay.itemById(lp.CAPTION_ID)
check(lay.itemById(lp.QR_ID) is pic and cap is not None and not pic.isMissingImage(),
      "the layout has the QR picture and its caption")
check(cap.text() == "Scan: this map on your phone", "the caption says what the code does")
ppos, psize = pic.positionWithUnits(), pic.sizeWithUnits()
check(10 <= ppos.x() and ppos.x() + psize.width() <= 287 and 20 <= ppos.y()
      and cap.positionWithUnits().y() + cap.sizeWithUnits().height() <= 190, "the code sits inside the map frame")
check(not (ppos.x() > 200 and ppos.y() > 120), "the code avoids the corner already taken")
check(psize.width() >= lp.MIN_SIZE_MM and abs(psize.width() - psize.height()) < 0.01
      and psize.width() / rep_l["qr_version"] < 4, f"square, large enough to scan ({psize.width():.1f} mm, "
                                                   f"QR version {rep_l['qr_version']})")
check(dl["title"] == "Al Narjis district" and len(dl["points"]) == 25 and "far away" not in [p[2] for p in dl["points"]]
      and any(p[2] == "مسجد 1" for p in dl["points"]), "the points inside the frame travel with their labels")
lon_min = min(c[0] for c in dl["corners"])
lat_min = min(c[1] for c in dl["corners"])
check(46.6 < lon_min < 46.8 and 24.6 < lat_min < 24.8, "the frame is in longitude/latitude (Riyadh)")
pic.attemptMove(QgsLayoutPoint(30, 40))
lp.add_live_qr(lay, None, lang="en")
qrs = [i for i in lay.items() if getattr(i, "id", lambda: "")() == lp.QR_ID]
check(len(qrs) == 1 and abs(qrs[0].positionWithUnits().x() - 30) < 0.01,
      "running again replaces the code and keeps the place it was moved to")
check(decode(lay.customProperty(lp.PROP + "url"))["points"] == [], "the layout remembers the current address")

# the code prints: dark modules where the picture is (and, when OpenCV is around, it reads back as the address)
png = os.path.join(work, "layout.png")
img = QgsLayoutExporter(lay).renderPageToImage(0, dpi=300)
img.save(png)
q = qrs[0]
px = lambda mm: int(mm / 25.4 * 300)  # noqa: E731
qx, qy, qw = px(q.positionWithUnits().x()), px(q.positionWithUnits().y()), px(q.sizeWithUnits().width())
dark = sum(1 for xx in range(qx, qx + qw, 3) for yy in range(qy, qy + qw, 3) if img.pixelColor(xx, yy).lightness() < 90)
total = len(range(qx, qx + qw, 3)) ** 2
check(0.25 < dark / total < 0.65, f"the printed code has its modules ({dark / total:.0%} dark)")
crop = img.copy(qx - px(3), qy - px(3), qw + px(6), qw + px(6))
crop_png = os.path.join(work, "qr.png")
crop.save(crop_png)
read = None
for sys_py in (shutil.which("py"), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python311",
                                                "python.exe"), os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                                                            "Programs", "Python", "Python312",
                                                                            "python.exe")):
    if not sys_py or not os.path.isfile(sys_py):
        continue
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "QT_", "QGIS", "GDAL", "PROJ"))}
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        out = subprocess.run([sys_py, "-c", "import cv2,sys;print(cv2.QRCodeDetector().detectAndDecode("
                                            "cv2.imread(sys.argv[1]))[0])", crop_png],
                             capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        continue
    if out.returncode == 0:
        read = out.stdout.strip()
        break
if read is not None:
    check(read == lay.customProperty(lp.PROP + "url"), "a QR reader gets the exact address back from the print")
else:
    print("SKIP OpenCV not found: the printed code is not read back")

# a rotated map: the 4 corners keep its true shape
lay_r, mp_r = make_layout("Rotated", rotation=30.0)
cr = lp.frame_corners(mp_r, project)
c0 = lp.frame_corners(make_layout("Straight")[1], project)
k = math.cos(math.radians(24.7))


def edge_angle(cs):
    return math.degrees(math.atan2(cs[1][1] - cs[0][1], (cs[1][0] - cs[0][0]) * k))


ang = abs(edge_angle(cr) - edge_angle(c0)) % 180
check(len(cr) == 4 and 25 < min(ang, 180 - ang) < 35, f"a map rotated 30° gives a frame turned by {ang:.1f}°")
side = lambda cs, i: math.hypot((cs[(i + 1) % 4][0] - cs[i][0]) * k, cs[(i + 1) % 4][1] - cs[i][1])  # noqa: E731
check(abs(side(cr, 0) - side(cr, 2)) < 1e-6 * 50 and abs(side(cr, 1) - side(cr, 3)) < 1e-6 * 50,
      "the rotated frame is still a rectangle")
u_r, _rr = lp.add_live_qr(lay_r, poly_layer, "zone", lang="en")[:2]
d_r = decode(u_r)
check(all(abs(a[0] - b[0]) < 2e-6 and abs(a[1] - b[1]) < 2e-6 for a, b in zip(d_r["corners"], cr)),
      "the rotated corners travel as they are")
check(len(d_r["shapes"]) == 1 and d_r["shapes"][0]["kind"] == "polygon" and d_r["points"][0][2] == "Zone A",
      "a polygon layer gives its outline and a labelled point")

# ---- the Processing tool --------------------------------------------------------------------------------------
lay_p, _mp = make_layout("For processing")
res = processing.run("biruni:liveqr", {"LAYOUT": "For processing", "LAYER": pts_layer, "LABEL_FIELD": "name",
                                      "TITLE": "عنوان على الهاتف"})
dp = decode(res["URL"])
check(dp["title"] == "عنوان على الهاتف" and res["POINTS"] == 25 and lay_p.itemById(lp.QR_ID) is not None,
      "the Processing tool runs without a window and puts the code on the named layout")
check("Points: 25 of 25." in res["CARRIED"], "it says what the code carries: " + res["CARRIED"])
res2 = processing.run("biruni:liveqr", {"LAYOUT": "For processing", "MAX_CHARS": 200})
check(len(res2["URL"]) <= 200 and res2["POINTS"] == 0, "the longest address can be set")

# ---- the phone page -------------------------------------------------------------------------------------------
html = public_page()
check(html.startswith("<!doctype html>") and "</html>" in html and "__" not in re.sub(r"__[a-z]", "", html)
      .replace("__proto__", ""), "the page is complete (every placeholder filled)")
texts = json.loads(re.search(r"const ALL=(\{.*?\}),\$=", html, re.S).group(1))
check(sorted(texts) == sorted(["en", "ar", "es", "pt_BR", "fr", "tr", "fa"]), "the page has all 7 languages")
keys = set(texts["en"])
check(all(set(texts[k]) == keys for k in texts), "every language has every text")
check(all(len(texts[k]["dirs"]) == 8 and "{d}" in texts[k]["outside"] and "{dir}" in texts[k]["outside"]
          and "{n}" in texts[k]["m"] and "{n}" in texts[k]["km"] and "{s}" in texts[k]["scale"] for k in texts),
      "every language has the 8 directions and keeps the placeholders")
check("Biruni GIS" in texts["fa"]["made"] and "Biruni GIS" in texts["ar"]["made"], "the name stays in Latin letters")
check("tile.openstreetmap.org" in html and "openstreetmap.org/copyright" in html
      and not re.search(r"arcgisonline|google|bing|mapbox|esri", html, re.I),
      "OpenStreetMap only, with its attribution")
check(f"leaflet/{lpage.LEAFLET_VERSION}/leaflet.min.js" in html and 'integrity="sha512-' in html,
      "Leaflet pinned to one version, checked by its hash")
check("biruni-lang" in html and "localStorage" in html, "the language choice is remembered like the field page's")
check("DeviceOrientationEvent.requestPermission" in html and "watchPosition" in html,
      "position, compass and the iPhone's permission on a tap")
check("fetch(" not in html and "XMLHttpRequest" not in html and "sendBeacon" not in html,
      "the page sends nothing anywhere")

node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)
if node:
    scripts = re.findall(r"<script id=\"(\w+)\">(.*?)</script>", html, re.S)
    check([n for n, _ in scripts] == ["decoder", "ui"], "the page has its decoder and its interface scripts")
    for name, js in scripts:
        path = os.path.join(work, f"{name}.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        out = subprocess.run([node, "--check", path], capture_output=True, text=True)
        check(out.returncode == 0, f"the page's {name} script has no syntax error " + out.stderr[:200])
    decoder = dict(scripts)["decoder"]
    raw_url, raw_rep = encode("x", corners)
    samples = [url, url_l, u_r, res["URL"], raw_url, u2.lower(), _u]
    check(raw_url.split("#")[1][0] == "A" and url.split("#")[1][0] == "B", "both forms (plain and compressed) tested")
    bad_samples = [codec.PAGE_URL, "#" + good[:len(good) // 2], "#A" + codec.b43encode(bytes([2]) + b"\x06"), "#Z1"]
    runner = os.path.join(work, "run.js")
    with open(os.path.join(work, "in.json"), "w", encoding="utf-8") as fh:
        json.dump({"ok": samples, "bad": bad_samples}, fh, ensure_ascii=False)
    with open(runner, "w", encoding="utf-8") as fh:
        fh.write(decoder + "\nconst IN=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8'));"
                 "(async()=>{const out={ok:[],bad:[]};for(const u of IN.ok)out.ok.push(await decodeHash(u));"
                 "for(const u of IN.bad){try{await decodeHash(u);out.bad.push('decoded')}catch(e){out.bad.push(e.message)}}"
                 "process.stdout.write(JSON.stringify(out))})();")
    out = subprocess.run([node, runner, os.path.join(work, "in.json")], capture_output=True, text=True,
                         encoding="utf-8")
    try:
        js_out = json.loads(out.stdout)
    except ValueError:
        js_out = {"ok": [], "bad": []}
        print(out.stderr[:500])
    same = [js == decode(u) for js, u in zip(js_out["ok"], samples)]
    check(len(same) == len(samples) and all(same), f"the page's decoder reads exactly what Python wrote ({same})")
    check(js_out["bad"] == ["bad", "bad", "newer", "bad"], f"the page refuses broken addresses kindly "
                                                           f"({js_out['bad']})")
else:
    print("SKIP node not found: page scripts not checked")

project.clear()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
