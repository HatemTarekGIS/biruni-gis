r"""Map Book (mapbook/): pages from a grid, strips, features, an index layer and Map Stops; the book layout, PDF
bookmarks and page links, files per page, the Arabic book, the panel.

Run:  engine\bin\python-qgis.bat tests\test_mapbook.py [png output dir]

(Run it without QT_QPA_PLATFORM=offscreen: the pages are drawn with the real Windows fonts. No window is shown.)
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsFeature, QgsFillSymbol, QgsGeometry, QgsLineSymbol, QgsPointXY, QgsProject,
                       QgsRendererCategory, QgsVectorLayer)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
QgsApplication.bookmarkManager().initialize(os.path.join(SETTINGS_DIR, "bookmarks.xml"))

t0 = time.perf_counter()
import biruni.mapbook  # noqa: E402,F401
import biruni.algorithms.mapbook  # noqa: E402,F401
light_import = not any(m in sys.modules for m in ("biruni.mapbook.render", "biruni.mapbook.book",
                                                   "biruni.mapbook.panel", "biruni.mapbook.pages"))

from qgis.PyQt.QtCore import Qt  # noqa: E402

from biruni import i18n  # noqa: E402
from biruni.mapbook import book as B  # noqa: E402
from biruni.mapbook import pages as P  # noqa: E402
from biruni.mapbook import pdf as PDF  # noqa: E402
from biruni.mapbook import render as R  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


OUT = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="mapbook_")
os.makedirs(OUT, exist_ok=True)
print("output:", OUT)

# the translations made for this feature (not yet merged into the app's files): used for the Arabic book
_EXTRA = os.environ.get("BIRUNI_MAPBOOK_I18N", "")
if _EXTRA and os.path.exists(_EXTRA):
    with open(_EXTRA, encoding="utf-8") as f:
        _extra = json.load(f)
    _orig_load = i18n.load_strings
    _cols = {"ar": 0, "es": 1, "pt_BR": 2, "pt": 2, "fr": 3, "tr": 4, "fa": 5}

    def _load(code):
        merged = dict(_orig_load(code))
        col = _cols.get(code)
        if col is not None:
            merged.update({k: v[col] for k, v in _extra.items() if isinstance(v, list) and len(v) > col})
        return merged

    i18n.load_strings = _load

check(light_import, "importing the package and the tool loads nothing heavy (the engine loads when used)")

# ---- test data (UTM 38N, Riyadh) ------------------------------------------------------------------------------
CRS = "EPSG:32638"
prj = QgsProject.instance()
prj.setCrs(QgsCoordinateReferenceSystem(CRS))
X0, Y0 = 670000.0, 2725000.0


def layer(kind, name, fields, rows):
    lyr = QgsVectorLayer(f"{kind}?crs={CRS}" + "".join(f"&field={n}:{t}" for n, t in fields), name, "memory")
    feats = []
    for wkt, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def box(x, y, w, h):
    return f"POLYGON(({x} {y},{x + w} {y},{x + w} {y + h},{x} {y + h},{x} {y}))"


# blocks in two clusters (south-west and north-east) - a plain grid over both wastes the empty middle
blocks_rows = []
uses = ["Residential", "Commercial", "Park"]
for cx, cy, use_set in ((X0, Y0, (0, 1)), (X0 + 1500, Y0 + 900, (2,))):
    for i in range(4):
        for j in range(3):
            use = uses[use_set[(i + j) % len(use_set)]]
            blocks_rows.append((box(cx + i * 70, cy + j * 60, 55, 45), [f"B{len(blocks_rows) + 1}", use]))
blocks = layer("Polygon", "Blocks", [("code", "string"), ("use", "string")], blocks_rows)
cats = []
for use, colour in (("Residential", "#F2D49B"), ("Commercial", "#E07A5F"), ("Park", "#81B29A")):
    cats.append(QgsRendererCategory(use, QgsFillSymbol.createSimple({"color": colour, "outline_color": "#6B6B6B",
                                                                     "outline_width": "0.2"}), use))
blocks.setRenderer(QgsCategorizedSymbolRenderer("use", cats))
streets = layer("LineString", "Streets", [("name", "string")], [
    (f"LINESTRING({X0 - 20} {Y0 + 52},{X0 + 300} {Y0 + 52})", ["شارع الملك فهد"]),
    (f"LINESTRING({X0 + 62} {Y0 - 20},{X0 + 62} {Y0 + 200})", ["طريق العليا"]),
    (f"LINESTRING({X0 + 1480} {Y0 + 952},{X0 + 1800} {Y0 + 952})", ["شارع التخصصي"]),
    (f"LINESTRING({X0 + 1480} {Y0 + 1010},{X0 + 1800} {Y0 + 1010})", ["Olaya Street"]),
])
streets.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "#7A7A7A", "width": "0.8"}))
prj.addMapLayers([blocks, streets])

# ---- 1. automatic grid: only cells with data, fewest pages --------------------------------------------------
geoms = [f.geometry() for f in blocks.getFeatures()] + [f.geometry() for f in streets.getFeatures()]
cover = P.DataCover(geoms)
scale = 2500
fw, fh = P.frame_mm("A4", True)
W, H = fw * scale / 1000, fh * scale / 1000
cols, rows = P.plain_grid(cover.extent, W, H)
pages, method, plain = P.grid_pages(cover, "A4", True, scale, 0.0)
check(plain == cols * rows and len(pages) < plain, f"grid keeps only cells with data ({len(pages)} of {plain} cells)")
data_cells = P.aligned_cells(cover, cover.extent, W, H)
check(len(pages) <= len(data_cells), f"fewest pages ({method}): {len(pages)} ≤ {len(data_cells)} of the aligned grid")
union = QgsGeometry.unaryUnion([QgsGeometry.fromWkt(p.core) for p in pages])
check(all(union.contains(g) or union.buffer(0.01, 2).contains(g) for g in geoms), "every feature is on a page")
# a case where placing each row on its own saves pages
rows_cover = P.DataCover([QgsGeometry.fromWkt(box(X0 + x, Y0 + y, 10, 10))
                          for x, y in ((0, 0), (450, 0), (450, -150), (940, -150))])
plain2 = P.aligned_cells(rows_cover, rows_cover.extent, 500, 100)
how, best2, _n = P.grid_cells(rows_cover, 500, 100)
check(len(best2) < len(plain2), f"rows placed on their own save pages ({len(best2)} instead of {len(plain2)}, {how})")

# ---- 2. best fit: orientation and the largest standard scale ------------------------------------------------
land, s = P.best_fit(400, 150, "A4")
check(land and s in P.BOOK_SCALES, f"a wide area goes landscape (1:{s})")
port, s2 = P.best_fit(150, 400, "A4")
check(not port and s2 == s, "a tall area goes portrait at the same scale")
fw, fh = P.frame_mm("A4", True)
check(400 * 1.16 / fw * 1000 <= s and P.BOOK_SCALES[P.BOOK_SCALES.index(s) - 1] < 400 * 1.16 / fw * 1000,
      "the scale is the largest standard scale that still fits")
items = [(QgsGeometry.fromWkt(box(X0, Y0, 40, 30)), "1", "small", 0), (QgsGeometry.fromWkt(box(X0, Y0, 800, 300)),
                                                                        "2", "big", 0)]
best = P.fit_pages(items, "A4", scale_mode="best")
common = P.fit_pages(items, "A4", scale_mode="common")
check(best[0].scale < best[1].scale and common[0].scale == common[1].scale == best[1].scale,
      f"best per page (1:{int(best[0].scale)} / 1:{int(best[1].scale)}) or one scale for the book")

# ---- 3. strips along an L-shaped route ----------------------------------------------------------------------
route = QgsGeometry.fromWkt(f"LINESTRING({X0} {Y0},{X0 + 1200} {Y0},{X0 + 1200} {Y0 + 900})")
overlap = 40.0
strips = P.strip_pages(route, "A4", True, 2000, overlap)
P.number_pages(strips)
P.find_neighbours(strips, sequence=True)
cov = QgsGeometry.unaryUnion([p.polygon() for p in strips])
check(route.difference(cov).length() < 0.01, f"{len(strips)} strip pages cover the whole route")
shared = []
for a, b in zip(strips, strips[1:]):
    shared.append(route.intersection(a.polygon()).intersection(b.polygon()).length())
check(all(x >= overlap * 0.98 for x in shared), f"neighbouring strips repeat ≥ {overlap:.0f} m of route "
                                                 f"(min {min(shared):.1f} m)")
first_leg = [p for p in strips if p.chain[1] <= 1200]
last_leg = [p for p in strips if p.chain[0] >= 1200]
check(first_leg and all(abs(p.angle) < 1 for p in first_leg), "pages on the east leg run east (turn 0°)")
check(last_leg and all(abs(p.angle - 90) < 1 for p in last_leg), "pages on the north leg are turned 90°")
inside = True
for p in strips:
    piece = route.intersection(p.polygon())
    inside = inside and not piece.isEmpty()
check(inside, "every strip page shows the route")
check(strips[0].neighbours.get("right") == 1 and strips[1].neighbours.get("left") == 0
      and "left" not in strips[0].neighbours, "strip neighbours: before on the left, after on the right")

# ---- 4. neighbours of a 3 × 3 grid --------------------------------------------------------------------------
g9 = []
for r in range(3):
    for c in range(3):
        rect = box(X0 + c * 100, Y0 - r * 80, 100, 80)
        g = QgsGeometry.fromWkt(rect)
        ctr = g.centroid().asPoint()
        p = P.Page(ctr.x(), ctr.y(), 110, 88, row=r, col=c, core=g.asWkt())
        g9.append(p)
P.number_pages(g9, "grid")
P.find_neighbours(g9)
mid = g9[4]
nb = {d: g9[k].number for d, k in mid.neighbours.items()}
check(nb == {"up": 2, "down": 8, "left": 4, "right": 6}, f"3×3 grid: the middle page's neighbours {nb}")
corner = {d: g9[k].number for d, k in g9[0].neighbours.items()}
check(corner == {"right": 2, "down": 4}, f"3×3 grid: the corner page has two neighbours {corner}")
check([p.name for p in g9[:4]] == ["A1", "B1", "C1", "A2"], "grid names A1, B1, C1, A2…")
order = P.reading_order(g9, "snake")
check([g9[k].name for k in order][:6] == ["A1", "B1", "C1", "C2", "B2", "A2"], "snake reading order")
order_rtl = P.reading_order(g9, "rows", rtl=True)
check([g9[k].name for k in order_rtl][:3] == ["C1", "B1", "A1"], "right-to-left rows start on the right")

# ---- 5. a whole book: grid, index, contents, key map, legend and table per page -----------------------------
book = B.new_book(name="Test book", title="Al Olaya district", subtitle="Blocks and streets", author="Survey office",
                  source="grid", scale=1000, paper="A4", orientation="landscape", naming="grid",
                  index_layer=streets.id(), index_field="name", table_layer=blocks.id(),
                  table_fields=["code", "use"], back=True, back_text="Field survey 2026.", lang="en",
                  page_title="{title}")
pl = B.build(book, prj)
check(len(pl) >= 2 and book.get("method"), f"grid book: {len(pl)} pages ({book.get('method')})")
pdf_path = os.path.join(OUT, "book_en.pdf")
res = R.export_book(book, pl, pdf_path, prj, per_page="png", seal=False)
bl = res["builder"]
nav = PDF.read_navigation(pdf_path)
expected = 1 + 1 + len(pl) + len([k for k in bl.kinds if k == "index"]) + 1
check(nav["pages"] == len(bl.plan) == expected, f"PDF pages: {nav['pages']} (cover, contents, maps, index, back)")
titles = [t for _d, t, _p in nav["outline"]]
check("Cover and key map" in titles and "Contents" in titles and "Map pages" in titles and "Index of names" in titles,
      "bookmarks: cover, contents, map pages, index")
map_marks = [(t, pg) for d, t, pg in nav["outline"] if d == 1]
check(len(map_marks) == len(pl) and all(pg == bl.page_of[p.number] for (t, pg), p in zip(map_marks, pl)),
      "one bookmark per map page, each to its page")
internal = [(k, box_, t) for k, box_, t in nav["links"] if isinstance(t, int)]
check(len(internal) == res["links"] and len(internal) > len(pl), f"{len(internal)} internal links written")
# neighbour arrows point to the neighbour's page
ok = True
n_arrows = 0
for p in pl:
    k = bl.page_of[p.number]
    targets = {t for kk, _b, t in internal if kk == k}
    for d, j in p.neighbours.items():
        n_arrows += 1
        ok = ok and bl.page_of[pl[j].number] in targets
check(ok and n_arrows > 0, f"neighbour arrows link to their pages ({n_arrows} arrows)")
cover_links = {t for kk, _b, t in internal if kk == 0}
check(cover_links == {bl.page_of[p.number] for p in pl}, "key map cells on the cover open every page")
contents_links = {t for kk, _b, t in internal if bl.kinds[kk] == "contents"}
check({bl.page_of[p.number] for p in pl} <= contents_links, "contents rows open their pages")
# a link rectangle sits where the arrow is drawn (PDF points, from the bottom)
p0 = next(p for p in pl if p.neighbours)
d0 = next(iter(p0.neighbours))
item = bl.layout.itemById(f"mapbook-next-{p0.number}-{d0}")
pos = item.pagePositionWithUnits()
k0 = bl.page_of[p0.number]
ph_pt = nav["sizes"][k0][1]
rect_pt = [b for kk, b, t in internal if kk == k0 and t == bl.page_of[pl[p0.neighbours[d0]].number]][0]
check(abs(rect_pt[0] - pos.x() * 72 / 25.4) < 0.6 and abs(rect_pt[3] - (ph_pt - pos.y() * 72 / 25.4)) < 0.6,
      "the arrow's link covers the arrow")
# per page: legend of the visible classes only
leg_ok = True
for p in pl:
    leg = bl.legends.get(p.number)
    if leg is None:
        continue
    leg.updateFilterByMap(False)
    shown = set()
    model = leg.model()
    for nl in model.rootGroup().findLayers():
        if nl.layerId() == blocks.id():
            for node in model.layerLegendNodes(nl):
                shown.add(node.data(Qt.ItemDataRole.DisplayRole))
    poly = p.polygon()
    present = {f["use"] for f in blocks.getFeatures() if f.geometry().intersects(poly)}
    if shown != present:
        leg_ok = False
        print("   page", p.name, "legend", sorted(shown), "on the page", sorted(present))
check(leg_ok, "each page's legend lists only the classes on that page")
tab_ok = True
for p in pl:
    t = bl.tables.get(p.number)
    want = R.page_features(blocks, p, bl.crs, ["code"], 12, prj)
    if t is None:
        tab_ok = tab_ok and not want
        continue
    t.refreshAttributes()
    got = len(t.contents())
    tab_ok = tab_ok and got == len(want)
check(tab_ok, "each page's table lists the blocks on that page")
check(bl.index_entries and any(e["name"] == "طريق العليا" for e in bl.index_entries), "name index made by the "
                                                                                      "map book index tool")
olaya = next(e for e in bl.index_entries if e["name"] == "طريق العليا")
check(olaya["pages"] and olaya["squares"], f"index entry with pages and squares: {olaya['pages']} / "
                                           f"{olaya['squares']}")
idx_links = [t for kk, _b, t in internal if bl.kinds[kk] == "index"]
check(idx_links and set(idx_links) <= {bl.page_of[p.number] for p in pl}, "index entries open their first page")
check(len(bl.qr_urls) == len(pl) and all(u.startswith("https://") for u in bl.qr_urls.values()),
      "a living paper QR code on every page")
names = R.file_names(pl, "png")
check(sorted(os.path.basename(f) for f in res["files"]) == sorted(names.values())
      and all(os.path.getsize(f) > 30000 for f in res["files"]), f"one picture per page: {sorted(names.values())[:3]}…")
check(bl.layout in prj.layoutManager().layouts(), "the book's layout is kept with the project's layouts")
# a map page as a picture, to look at
R.render_page_png(bl, pl[0].number, os.path.join(OUT, "page_en.png"), 110)
R.render_page_png(bl, 0, os.path.join(OUT, "cover_en.png"), 80, layout_page=True)
R.render_page_png(bl, len(bl.plan) - 2, os.path.join(OUT, "index_en.png"), 80, layout_page=True)
R.render_page_png(bl, 1, os.path.join(OUT, "contents_en.png"), 80, layout_page=True)

# visible area of a turned page equals the planned frame
mp = None
# ---- 6. strips book + per page PDFs + seal ------------------------------------------------------------------
rl = layer("LineString", "Route", [("id", "integer")], [(route.asWkt(), [1])])
rl.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "#C62828", "width": "1.4"}))
prj.addMapLayer(rl)
sbook = B.new_book(name="Route", title="Route 1", source="strips", layer=rl.id(), scale=2500, overlap=0.06,
                   cover=False, contents=False, lang="en", qr=False)
spl = B.build(sbook, prj)
sres = R.export_book(sbook, spl, os.path.join(OUT, "route.pdf"), prj, per_page="pdf", seal=True)
sbl = sres["builder"]
mp = sbl.map_items[spl[-1].number]
vis = QgsGeometry.fromQPolygonF(mp.visibleExtentPolygon())
check(vis.symDifference(spl[-1].polygon()).area() < 1e-3 * spl[-1].polygon().area(),
      f"a turned page shows exactly its frame (turn {spl[-1].angle:.0f}°)")
snav = PDF.read_navigation(sres["pdf"])
ok = all(any(t == sbl.page_of[spl[p.neighbours['right']].number] for kk, _b, t in snav["links"]
             if kk == sbl.page_of[p.number]) for p in spl if "right" in p.neighbours)
check(snav["pages"] == len(spl) and ok, "strip book: the → arrow opens the next sheet")
check(len(sres["files"]) == len(spl) and all(PDF.read_navigation(f)["pages"] == 1 for f in sres["files"]),
      "one PDF per page")
check(sres["seal"] and os.path.exists(sres["pdf"] + ".seal.json"), "the book's PDF is sealed")
R.render_page_png(sbl, spl[len(spl) // 2].number, os.path.join(OUT, "strip_page.png"), 90)

# ---- 7. Map Stops book ------------------------------------------------------------------------------------------
from biruni.stops.model import PROJECT, Frame, store_for  # noqa: E402

store = store_for(project=prj)
store.add("North cluster", Frame(X0 + 1640, Y0 + 960, 400, 260, 0.0, prj.crs()), scope=PROJECT)
store.add("South cluster", Frame(X0 + 130, Y0 + 80, 300, 300, 15.0, prj.crs()), scope=PROJECT, scale=2000)
kbook = B.new_book(name="Stops", title="Places", source="stops", naming="given", cover=True, contents=True,
                   lang="en")
kpl = B.build(kbook, prj)
check([p.name for p in kpl] == ["North cluster", "South cluster"], "one page per Map Stop, named after it")
check(abs(kpl[1].angle - 15) < 1e-6 and kpl[1].scale == 2000, "a stop keeps its turn and its scale")
kres = R.export_book(kbook, kpl, os.path.join(OUT, "stops.pdf"), prj)
knav = PDF.read_navigation(kres["pdf"])
check(knav["pages"] == 4 and len([1 for d, _t, _p in knav["outline"] if d == 1]) == 2, "stops book: cover, "
                                                                                     "contents and two pages")

# ---- 8. one page per feature (parcels) ----------------------------------------------------------------------
fbook = B.new_book(name="Blocks", title="Blocks", source="features", layer=blocks.id(), name_field="code",
                   naming="given", scale_mode="best", cover=False, contents=False, lang="en", qr=False)
fpl = B.build(fbook, prj)
check(len(fpl) == blocks.featureCount() and fpl[0].name.startswith("B"), "one page per feature, named from a field")
check(all(p.polygon().contains(QgsGeometry.fromWkt(p.core)) for p in fpl), "every feature fits its page")

# ---- 9. index layer pages ----------------------------------------------------------------------------------------
idx = layer("Polygon", "Sheets", [("sheet", "string")], [(box(X0 - 50, Y0 - 50, 400, 300), ["S-1"]),
                                                          (box(X0 + 350, Y0 - 50, 400, 300), ["S-2"])])
prj.addMapLayer(idx)
ibook = B.new_book(name="Sheets", title="Sheets", source="index", layer=idx.id(), name_field="sheet",
                   naming="given", scale_mode="common", orientation="portrait", lang="en", table_layer=blocks.id(),
                   page_title="{title} [% \"sheet\" %]")
ipl = B.build(ibook, prj)
check([p.name for p in ipl] == ["S-1", "S-2"] and ipl[0].neighbours.get("right") == 1
      and ipl[1].neighbours.get("left") == 0, "index layer: a page per polygon, touching sheets are neighbours")
ires = R.export_book(ibook, ipl, os.path.join(OUT, "sheets.pdf"), prj)
check(ires["builder"].pages[0].title == "Sheets S-1", "page titles from an expression on the index feature")
R.render_page_png(ires["builder"], 1, os.path.join(OUT, "page_portrait.png"), 90)
R.render_page_png(kres["builder"], 2, os.path.join(OUT, "page_stop_turned.png"), 90)

# ---- 10. kept in the project ------------------------------------------------------------------------------------
B.save_book(book, prj, pl)
again = B.load_book("Test book", prj)
check(again and len(B.page_list(again)) == len(pl) and B.page_list(again)[0].name == pl[0].name,
      "the book is kept in the project")
n_layouts = len(prj.layoutManager().layouts())
n_internal = len([l for l in prj.mapLayers().values() if l.customProperty("biruni/mapbook/internal")])
R.export_book(again, B.page_list(again), os.path.join(OUT, "book_en_again.pdf"), prj)
check(len(prj.layoutManager().layouts()) == n_layouts and n_internal ==
      len([l for l in prj.mapLayers().values() if l.customProperty("biruni/mapbook/internal")]),
      "exported again: the book's layout and key maps are replaced, not piled up")
p = B.page_list(again)
p[1].excluded = True
B.relink(again, p)
check(p[1].number is None and p[2].number == 2, "a page left out: the others are numbered again")

# ---- 11. the Arabic book ----------------------------------------------------------------------------------------
abook = B.new_book(name="Arabic", title="حي العليا", subtitle="البلوكات والشوارع", author="مكتب المساحة",
                   source="grid", scale=1000, paper="A4", orientation="landscape", naming="grid",
                   index_layer=streets.id(), index_field="name", table_layer=blocks.id(), table_fields=["code", "use"],
                   lang="ar")
apl = B.build(abook, prj, rtl=True)
ares = R.export_book(abook, apl, os.path.join(OUT, "book_ar.pdf"), prj)
abl = ares["builder"]
amap = abl.map_items[apl[0].number]
leg = abl.legends.get(apl[0].number)
check(R._BOOK["rtl"] and leg is not None and amap.pagePositionWithUnits().x() > leg.pagePositionWithUnits().x(),
      "Arabic book: the panel is on the left, the map on the right")
badge = abl.layout.itemById(f"mapbook-badge-{apl[0].number}")
check(badge.pagePositionWithUnits().x() > amap.pagePositionWithUnits().x(), "the page badge sits on the right")
texts = [i.text() for i in abl.layout.items() if hasattr(i, "text") and callable(i.text) and
         i.__class__.__name__ == "QgsLayoutItemLabel"]
check(any("١" in t or "٢" in t for t in texts), "Arabic-Indic digits in the Arabic book")
check(any(t == "مفتاح الخريطة" for t in texts), "the book's texts in Arabic")
anav = PDF.read_navigation(ares["pdf"])
check(any(any("؀" <= ch <= "ۿ" for ch in t) for _d, t, _p in anav["outline"]), "Arabic bookmarks")
R.render_page_png(abl, apl[0].number, os.path.join(OUT, "page_ar.png"), 110)
R.render_page_png(abl, 0, os.path.join(OUT, "cover_ar.png"), 80, layout_page=True)

# ---- 12. the panel: make, leave out, move and turn by hand, export ------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402

from biruni.mapbook.panel import MapBookPanel, PageTool  # noqa: E402


class _Bar:
    def pushSuccess(self, *a):  # noqa: N802
        pass


class _Iface:
    def __init__(self):
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(prj.crs())

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def messageBar(self):  # noqa: N802
        return _Bar()

    def mainWindow(self):  # noqa: N802
        return None


class _Plugin:
    iface = _Iface()


panel = MapBookPanel(_Plugin(), prj)
panel.title.setText("Panel book")
panel._set_combo(panel.source, "grid")
panel.scale.setCurrentText("1:1,000")
panel._set_combo(panel.lang, "en")
panel._set_combo(panel.naming, "grid")
panel.opt["qr"].setChecked(False)
panel.index_layer.setLayer(None)
check(panel.make_pages() and panel.list.count() == len(panel.pages) > 2, f"panel: {len(panel.pages)} pages made "
                                                                        "and listed")
prev = prj.mapLayer(panel.preview)
check(prev is not None and prev.featureCount() == len(panel.pages) and prev.labelsEnabled(),
      "panel: the pages shown on the map as numbered rectangles")
panel.list.item(1).setCheckState(Qt.CheckState.Unchecked)
check(panel.pages[1].excluded and panel.pages[1].number is None and panel.pages[2].number == 2,
      "panel: a page left out, the next ones numbered again")
tool = PageTool(panel.iface.mapCanvas(), panel)
p = panel.pages[0]
cx, cy = p.cx, p.cy
tool.apply((0, "move", QgsPointXY(cx, cy), p.copy()), QgsPointXY(cx + 30, cy - 10))
tool.apply((0, "turn", QgsPointXY(p.cx + 10, p.cy), p.copy()), QgsPointXY(p.cx, p.cy + 10))
s0 = p.scale
tool.apply((0, "size", QgsPointXY(p.cx + 10, p.cy), p.copy()), QgsPointXY(p.cx + 20, p.cy))
panel.pages_changed()
kept = B.page_list(B.load_book(panel.book["name"], prj))[0]
check(abs(kept.cx - cx - 30) < 1e-6 and abs(kept.angle - 90) < 1e-6 and kept.scale == 2000 and kept.manual
      and s0 == 1000, "panel: a page moved, turned and resized by hand, kept in the project")
check(tool.page_at(QgsPointXY(kept.cx, kept.cy)) == 0, "panel: clicking a page picks it")
pres = panel.export(os.path.join(OUT, "panel.pdf"))
pnav = PDF.read_navigation(os.path.join(OUT, "panel.pdf")) if pres else {"pages": 0}
check(pres and pnav["pages"] == len(pres["builder"].plan) and pres["pages"] == len(panel.pages) - 1,
      "panel: the book exported without the page left out")

# ---- 13. the Processing tool ------------------------------------------------------------------------------------
from qgis.core import QgsProcessingContext, QgsProcessingFeedback  # noqa: E402

from biruni.algorithms.mapbook import MapBook  # noqa: E402

alg = MapBook()
alg.initAlgorithm({})
ctx = QgsProcessingContext()
ctx.setProject(prj)
apath = os.path.join(OUT, "tool.pdf")
res, ok = alg.run({"SOURCE": 3, "LAYER": blocks, "NAME_FIELD": "code", "SCALE_MODE": 1, "TITLE": "Blocks",
                   "LANGUAGE": 2, "COVER": False, "CONTENTS": True, "OUTPUT": apath, "PAGES": "memory:"}, ctx,
                  QgsProcessingFeedback())
check(ok and res["PAGE_COUNT"] == blocks.featureCount() and PDF.read_navigation(apath)["pages"] ==
      blocks.featureCount() + 1, "the Processing tool writes the book (one page per block, contents)")

print("\nALL PASS" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
