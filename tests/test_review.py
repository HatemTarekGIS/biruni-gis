"""Map review round trip: the review page, the comment files that come back, the review layer.

Run:  engine\\bin\\python-qgis.bat tests\\test_review.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
from biruni.share import review  # noqa: E402
from biruni.share.review import (ReviewFileError, ensure_review_layers, export_replies, export_review_page,  # noqa: E402
                                 import_reviews, parse_review_file)

failures = []
TMP = tempfile.mkdtemp(prefix="biruni_review_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def layer(uri, name, rows):
    lyr = QgsVectorLayer(uri, name, "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def write(name, data):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
    return p


# ---- the review page ---------------------------------------------------------------------------------------------
parcels = layer("Polygon?crs=EPSG:32637&field=no:string&field=owner:string", "قطع", [
    ("POLYGON((500000 2400000,500050 2400000,500050 2400050,500000 2400050,500000 2400000))", ["15", "أحمد"]),
    ("POLYGON((500050 2400000,500100 2400000,500100 2400050,500050 2400050,500050 2400000))",
     ["16", "<script>alert(1)</script>"]),
])
poles = layer("Point?crs=EPSG:4326&field=name:string", "Poles", [("POINT(39.1 21.6)", ["P1 </script><b>"])])
page = os.path.join(TMP, "review.html")
path, rid = export_review_page([poles, parcels], page, "مراجعة المخطط </title><script>x()</script>",
                               "راجع أرقام القطع\nوالأعمدة", None, "satellite")
src = open(path, encoding="utf-8").read()
check(os.path.exists(path) and re.fullmatch(r"[0-9a-f]{16}", rid) is not None, f"page written, review id {rid}")
m = re.search(r'<script type="application/json" id="biruni-data">(.*?)</script>', src, re.S)
check(m is not None, "data block present")
block = m.group(1)
check("<" not in block, "no raw '<' inside the data block (nothing can close the script)")
data = json.loads(block)
check(data["format"] == "biruni-review" and data["review_id"] == rid and len(data["layers"]) == 2,
      "data: format, id and both layers")
names = [ly["name"] for ly in data["layers"]]
check(names == ["Poles", "قطع"], f"layer order kept, Arabic names ({names})")
fids = [f["properties"]["k"] for f in data["layers"][1]["geojson"]["features"]]
check(len(fids) == 2 and all(isinstance(k, int) for k in fids), "every feature carries its id")
vals = [f["properties"]["v"] for f in data["layers"][1]["geojson"]["features"]]
check(["16", "<script>alert(1)</script>"] in vals, "attribute values kept as data (escaped only in the JSON)")
x, y = data["layers"][1]["geojson"]["features"][0]["geometry"]["coordinates"][0][0]
check(38.9 < x < 39.1 and 21.6 < y < 21.8, f"reprojected to WGS84 ({x:.4f}, {y:.4f})")
check("<title>مراجعة المخطط &lt;/title&gt;&lt;script&gt;x()&lt;/script&gt;</title>" in src, "title escaped in <title>")
check(data["basemap"] == "satellite" and "s2cloudless" in data["basemaps"]["satellite"]["url"], "satellite basemap")
check(not re.search(r"<script[^>]+src=", src) and not re.search(r"<link[^>]+href=\"http", src),
      "no external scripts or stylesheets: Leaflet is inside the page")
urls = set(re.findall(r"https?://[^\s\"'<>)]+", block))
check(all(any(h in u for h in ("openstreetmap", "opentopomap", "eox.at", "s2maps.eu", "leafletjs")) for u in urls),
      f"only basemap and credit links ({len(urls)})")
check(data["text"]["note"] and data["text"]["send"] and data["limits"]["text"] == review.MAX_TEXT, "page texts")

# the page's own script is valid JavaScript (node --check), when node is installed
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.exists(p)), None)
if node:
    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    js = os.path.join(TMP, "page.js")
    with open(js, "w", encoding="utf-8") as f:
        f.write(scripts[-1])
    r = subprocess.run([node, "--check", js], capture_output=True, text=True)
    check(r.returncode == 0, "review page script parses " + (r.stderr.strip()[:200] if r.returncode else ""))
    # escaping of what is shown back: run esc() from the page
    esc_src = re.search(r"function esc\(s\)\{.*?\}\); \}", scripts[-1], re.S).group(0)
    probe = os.path.join(TMP, "esc.js")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(esc_src + "\nprocess.stdout.write(esc('<img src=x onerror=\"a()\">&\\'x'));")
    out = subprocess.run([node, probe], capture_output=True, text=True).stdout
    check(out == "&lt;img src=x onerror=&quot;a()&quot;&gt;&amp;&#39;x", f"esc() escapes everything ({out})")
else:
    print("     node not installed: page script not syntax-checked")

try:
    export_review_page([], os.path.join(TMP, "none.html"))
    check(False, "no layers refused")
except ValueError:
    check(True, "no layers refused")

# ---- reading review files ------------------------------------------------------------------------------------------
long_text = "ن" * (review.MAX_TEXT + 500)
good = {
    "format": "biruni-review", "version": 1, "review_id": rid, "title": "مراجعة", "author": "سارة",
    "exported": "2026-10-28T10:00:00.000Z",
    "comments": [
        {"id": "a1b2c3", "author": "سارة", "created": "2026-10-28T09:00:00.000Z", "category": "error",
         "text": "رقم القطعة غلط، المفروض 17", "geometry": {"type": "Point", "coordinates": [39.0, 21.7]},
         "feature": {"layer": "قطع", "layer_id": parcels.id(), "fid": fids[0], "label": "15"}, "zoom": 18},
        {"id": "d4e5f6", "created": "2026-10-28T09:05:00.000Z", "category": "question",
         "text": "<script>alert('x')</script> فين العمود؟", "geometry": {"type": "LineString",
                                                                       "coordinates": [[39.0, 21.7], [39.001, 21.701]]}},
        {"id": "g7h8i9", "created": "2026-10-28T09:06:00.000Z", "category": "hack", "text": long_text,
         "geometry": {"type": "Polygon", "coordinates": [[[39.0, 21.7], [39.001, 21.7], [39.001, 21.701]]]}},
        {"id": "bad id!", "text": "x", "geometry": {"type": "Point", "coordinates": [39, 21]}},
        {"id": "outside", "text": "x", "geometry": {"type": "Point", "coordinates": [39, 200]}},
        {"id": "nogeom", "text": "x"},
        {"id": "empty", "text": "   ", "geometry": {"type": "Point", "coordinates": [39, 21]}},
        {"id": "a1b2c3", "text": "duplicate in the same file", "geometry": {"type": "Point", "coordinates": [1, 1]}},
        "not an object",
        {"id": "nan", "text": "x", "geometry": {"type": "Point", "coordinates": ["39", 21]}},
        {"id": "bidi", "text": "‮abc\u0000def", "geometry": {"type": "Point", "coordinates": [39.0, 21.7]},
         "created": "garbage"},
    ]}
f_good = write("سارة.biruni-review.json", good)
meta, comments, warnings = parse_review_file(f_good)
ids = [c["id"] for c in comments]
check(ids == ["a1b2c3", "d4e5f6", "g7h8i9", "bidi"], f"good comments read, bad ones skipped ({ids})")
check(len([w for w in warnings if "skipped" in w]) == 6, f"each bad comment reported ({len(warnings)} warnings)")
by = {c["id"]: c for c in comments}
check(by["d4e5f6"]["author"] == "سارة", "comment without author takes the file's author")
check(by["g7h8i9"]["category"] == "note" and len(by["g7h8i9"]["text"]) == review.MAX_TEXT and by["g7h8i9"]["cut"],
      "unknown type -> note; too long text shortened")
check(by["g7h8i9"]["geometry"].isGeosValid() and by["g7h8i9"]["geometry"].type() == 2, "open ring closed")
check(by["bidi"]["text"] == "abcdef" and by["bidi"]["created"] is None, "control/direction characters removed")
check(by["a1b2c3"]["fid"] == fids[0] and by["a1b2c3"]["label"] == "15", "feature reference kept")
check(meta["review_id"] == rid and meta["author"] == "سارة", "file meta")

for name, content, why in (
        ("other.json", {"format": "something-else", "version": 1, "comments": []}, "another format"),
        ("newer.json", {"format": "biruni-review", "version": 9, "comments": []}, "a newer version"),
        ("broken.json", "{not json", "broken JSON"),
        ("list.json", [1, 2, 3], "not an object"),
        ("nocomments.json", {"format": "biruni-review", "version": 1, "comments": "x"}, "comments not a list")):
    try:
        parse_review_file(write(name, content))
        check(False, f"refused: {why}")
    except ReviewFileError:
        check(True, f"refused: {why}")
big = os.path.join(TMP, "big.json")
with open(big, "wb") as f:
    f.write(b" " * (review.MAX_FILE_BYTES + 10))
try:
    parse_review_file(big)
    check(False, "refused: too large")
except ReviewFileError:
    check(True, "refused: too large")
check(parse_review_file(write("bom.txt", "﻿" + json.dumps(good, ensure_ascii=False)))[1] != [],
      "a .txt copy with a BOM is read too (shared from a phone)")

# ---- import into a memory layer (temporary map) --------------------------------------------------------------------
res = import_reviews([f_good, write("garbage.json", "nope")])
lyr = res.layer
check(lyr is not None and lyr.providerType() == "memory" and lyr.featureCount() == 4, "4 comments imported (memory)")
check(res.new == 4 and res.updated == 0 and res.by_author.get("سارة") == 4, f"summary: {res.summary()!r}")
check(len(res.errors) == 1, "the broken file is reported, the good one still imported")
feats = {f["comment_id"]: f for f in lyr.getFeatures()}
check(all(f["status"] == "new" for f in feats.values()), "status starts as new")
check(feats["d4e5f6"]["text"].startswith("<script>"), "text stored as written (escaped when shown, not altered)")
p = feats["a1b2c3"].geometry().asPoint()
check(abs(p.x() - 39.0) < 1e-9 and abs(p.y() - 21.7) < 1e-9, "pin at the comment's place")
check(feats["d4e5f6"].geometry().asPoint().x() == 39.0, "line comment pinned at its start")
_, sketch = ensure_review_layers()
check(sketch is not None and sketch.featureCount() == 2, "line and area sketches in their own layer")
check(lyr.renderer().type() == "RuleRenderer" and lyr.labelsEnabled(), "styled by type and labelled")
check(lyr.editorWidgetSetup(lyr.fields().indexOf("status")).type() == "ValueMap", "status shown as a choice list")

# a reviewer answers and the same file comes back, then a changed one
fid = feats["a1b2c3"].id()
lyr.dataProvider().changeAttributeValues({fid: {lyr.fields().indexOf("status"): "resolved",
                                                lyr.fields().indexOf("reply"): "اتصلحت، شكرًا"}})
res2 = import_reviews([f_good])
check(res2.new == 0 and res2.updated == 0 and res2.unchanged == 4 and lyr.featureCount() == 4,
      "same file again: nothing duplicated")
good2 = json.loads(json.dumps(good))
good2["comments"][0]["text"] = "رقم القطعة غلط، المفروض 18"
good2["comments"][0]["updated"] = "2026-10-28T11:00:00.000Z"
good2["comments"].append({"id": "new1", "created": "2026-10-28T11:01:00.000Z", "category": "ok", "text": "تمام",
                          "geometry": {"type": "Point", "coordinates": [39.0005, 21.7005]}})
res3 = import_reviews([write("سارة-2.biruni-review.json", good2)])
f1 = next(lyr.getFeatures(f"\"comment_id\" = 'a1b2c3'"))
check(res3.new == 1 and res3.updated == 1 and f1["text"].endswith("18"), "changed comment updated, new one added")
check(f1["status"] == "resolved" and f1["reply"] == "اتصلحت، شكرًا", "status and reply kept on update")
# the older file after the newer one: the newer text stays when both come together
res4 = import_reviews([write("سارة-2b.json", good2), f_good])
f1 = next(lyr.getFeatures(f"\"comment_id\" = 'a1b2c3'"))
check(f1["text"].endswith("18"), "two versions of one comment at once: the latest wins")

# replies back to the reviewer
rp = os.path.join(TMP, "replies.json")
n = export_replies(lyr, rp)
rep = json.load(open(rp, encoding="utf-8"))
r1 = next(r for r in rep["replies"] if r["id"] == "a1b2c3")
check(n == 5 and rep["format"] == "biruni-review-replies" and rep["review_id"] == rid, "replies file written")
check(r1["status"] == "resolved" and r1["reply"] == "اتصلحت، شكرًا", "reply and status in the replies file")

# ---- in the project database ---------------------------------------------------------------------------------------
from biruni.project import create_folder_structure  # noqa: E402

prj = QgsProject.instance()
for lid in [lid for lid, lay in prj.mapLayers().items() if lay.customProperty(review.ROLE_KEY)]:
    prj.removeMapLayer(lid)
root, qgz, gpkg = create_folder_structure(TMP, "مشروع")
prj.setFileName(qgz)
res5 = import_reviews([f_good])
glyr = res5.layer
check(glyr.providerType() == "ogr" and "review_comments" in glyr.source() and glyr.featureCount() == 4,
      f"comments stored in the project database ({glyr.source()})")
g = {f["comment_id"]: f for f in glyr.getFeatures()}
check(g["a1b2c3"]["author"] == "سارة" and g["a1b2c3"]["category"] == "error" and g["a1b2c3"]["status"] == "new",
      "fields written by name (GeoPackage fid first)")
_, gs = ensure_review_layers()
check(gs is not None and gs.providerType() == "ogr" and gs.featureCount() == 2 and
      {f["comment_id"] for f in gs.getFeatures()} == {"d4e5f6", "g7h8i9"}, "sketches in the project database too")
reopened = QgsVectorLayer(f"{gpkg}|layername=review_comments", "x", "ogr")
check(reopened.isValid() and reopened.featureCount() == 4, "the table is really in the GeoPackage")
res6 = import_reviews([f_good])
check(res6.new == 0 and glyr.featureCount() == 4, "GeoPackage: no duplicates either")

# ---- the dialogs build -----------------------------------------------------------------------------------------------
from biruni.share.review_dialog import BringBackReviewDialog, SendForReviewDialog  # noqa: E402


class _Iface:
    def mainWindow(self):
        return None


class _Plugin:
    iface = _Iface()
    activity = None


d1 = SendForReviewDialog(_Plugin())
check(d1.layers.count() >= 2 and d1.basemap.count() == 4 and d1.out.path().endswith(".html"), "send dialog builds")
d2 = BringBackReviewDialog(_Plugin())
check(d2.paths() == [], "bring-back dialog builds")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
