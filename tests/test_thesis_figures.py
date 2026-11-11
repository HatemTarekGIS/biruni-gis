r"""Thesis map figures (thesis/figures.py): figure records and numbering, the university styles, the template
items, exports at 300 dpi, the list of figures, style files, the Processing tools and the dialog.

Run:  engine\bin\python-qgis.bat tests\test_thesis_figures.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsFeature, QgsGeometry, QgsLayoutItemMap, QgsPointXY, QgsProject, QgsRendererCategory,
                       QgsSymbol, QgsVectorLayer)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
from qgis.PyQt.QtCore import QRectF  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage  # noqa: E402

from biruni.thesis import figures as tf  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- a small study area: districts, roads and schools around Alexandria (UTM 35N) ---------------------------------
project = QgsProject.instance()
project.setCrs(QgsCoordinateReferenceSystem("EPSG:32635"))
X0, Y0 = 772000, 3446000
districts = QgsVectorLayer("Polygon?crs=EPSG:32635&field=name:string&field=type:string", "lyr_districts", "memory")
feats = []
kinds = ["Residential", "Industrial", "Agricultural", "Residential", "Agricultural", "Residential"]
for k in range(6):
    col, row = k % 3, k // 3
    x, y = X0 + col * 4200, Y0 + row * 3600
    ring = [QgsPointXY(x, y), QgsPointXY(x + 4000 + (k % 2) * 300, y + 200), QgsPointXY(x + 4100, y + 3400),
            QgsPointXY(x - 150, y + 3500)]
    f = QgsFeature(districts.fields())
    f.setGeometry(QgsGeometry.fromPolygonXY([ring]))
    f.setAttributes([f"District {k + 1}", kinds[k]])
    feats.append(f)
districts.dataProvider().addFeatures(feats)
cats = []
for kind, color in (("Residential", "#F2C57C"), ("Industrial", "#B08EA2"), ("Agricultural", "#A7C796")):
    sym = QgsSymbol.defaultSymbol(districts.geometryType())
    sym.setColor(QColor(color))
    cats.append(QgsRendererCategory(kind, sym, kind))
districts.setRenderer(QgsCategorizedSymbolRenderer("type", cats))
roads = QgsVectorLayer("LineString?crs=EPSG:32635", "main_roads", "memory")
r = QgsFeature()
r.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(X0 - 300, Y0 + 3500), QgsPointXY(X0 + 6000, Y0 + 3700),
                                          QgsPointXY(X0 + 12700, Y0 + 3300)]))
roads.dataProvider().addFeatures([r])
roads.renderer().symbol().setColor(QColor("#C0392B"))
roads.renderer().symbol().setWidth(0.6)
schools = QgsVectorLayer("Point?crs=EPSG:32635", "schools", "memory")
pts = []
for k in range(9):
    p = QgsFeature()
    p.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + 800 + (k * 1370) % 11500, Y0 + 600 + (k * 2310) % 6400)))
    pts.append(p)
schools.dataProvider().addFeatures(pts)
schools.metadata()
md = schools.metadata()
md.setRights(["Field survey 2026"])
schools.setMetadata(md)
project.addMapLayers([districts, roads, schools])

work = tempfile.mkdtemp()

# ---- styles -----------------------------------------------------------------------------------------------------
names = [s["id"] for s in tf.all_styles()]
check(names[:3] == ["ar_university", "en_apa", "plain"], f"three built-in styles first ({names})")
check(tf.get_style("nope")["id"] == "ar_university", "an unknown style falls back to the Arabic university style")

# ---- three figures in two chapters --------------------------------------------------------------------------------
f1 = tf.add_figure(project, 3, "توزيع الأحياء حسب استخدام الأرض", "Districts by land use", "ar_university",
                   source="Field survey 2026")
f2 = tf.add_figure(project, 3, "شبكة الطرق الرئيسية", "Main road network", "ar_university")
f3 = tf.add_figure(project, 4, "توزيع المدارس", "Distribution of schools", "ar_university")
figs = tf.figures(project)
check([f["number_ar"] for f in figs] == ["3-1", "3-2", "4-1"], f"Arabic numbers {[f['number_ar'] for f in figs]}")
check([f["number_en"] for f in figs] == ["3.1", "3.2", "4.1"], f"English numbers {[f['number_en'] for f in figs]}")
check(figs[1]["text_ar"] == "شكل (3-2): شبكة الطرق الرئيسية", f"Arabic caption text: {figs[1]['text_ar']}")
check(figs[1]["text_en"] == "Figure 3.2: Main road network", f"English caption text: {figs[1]['text_en']}")
check(figs[0]["layout"] == f1.name() and figs[0]["caption"] == "توزيع الأحياء حسب استخدام الأرض",
      "figures() gives the layout name and the caption")
rec = json.loads(f2.customProperty("biruni/thesis-figure"))
check(rec == {"chapter": 3, "order": 2, "caption": "شبكة الطرق الرئيسية", "caption_en": "Main road network"},
      f"the record is stored under biruni/thesis-figure: {rec}")
check(f2.itemById("thesis-caption-ar").text() == "شكل (3-2): شبكة الطرق الرئيسية", "the page shows the number")


# ---- the template ---------------------------------------------------------------------------------------------------
def page_rect(layout):
    s = layout.pageCollection().page(0).pageSize()
    return QRectF(0, 0, s.width(), s.height())


def item_rect(item):
    return item.sceneBoundingRect()


for lay in (f1, f3):
    page = page_rect(lay)
    for item_id in ("map", "legend", "north", "scalebar", "thesis-caption-ar", "thesis-source-ar"):
        it = lay.itemById(item_id)
        check(it is not None, f"{lay.name()}: {item_id} exists")
        if it is not None:
            check(page.contains(item_rect(it)), f"{lay.name()}: {item_id} inside the page {item_rect(it)}")
    mp = lay.itemById("map")
    check(mp.grids().size() >= 1, f"{lay.name()}: coordinate grid on the map")
    frame = QRectF(mp.pos().x(), mp.pos().y(), mp.rect().width(), mp.rect().height())
    for item_id in ("legend", "north", "scalebar"):
        check(frame.contains(item_rect(lay.itemById(item_id))), f"{lay.name()}: {item_id} inside the map frame")
    inside = [item_rect(lay.itemById(i)) for i in ("legend", "north", "scalebar") if lay.itemById(i)]
    if lay.itemById("inset") is not None:
        inside.append(item_rect(lay.itemById("inset")))
    clash = [(a, b) for k, a in enumerate(inside) for b in inside[k + 1:] if a.intersects(b)]
    check(not clash, f"{lay.name()}: legend, north arrow, scale bar and inset do not overlap")
    cap = item_rect(lay.itemById("thesis-caption-ar"))
    src = item_rect(lay.itemById("thesis-source-ar"))
    room = tf.grid_room(tf.layout_style(lay))
    check(room >= 7 * tf.PT_MM + 1.5, "room for one line of grid labels")
    full = frame.adjusted(-room, -room, room, room)      # the frame with its grid labels
    check(page.contains(full), f"{lay.name()}: grid labels inside the page {full}")
    check(cap.top() >= full.bottom() - 0.5, f"{lay.name()}: caption below the map and its grid labels")
    check(src.top() >= cap.bottom() - 0.1, f"{lay.name()}: source line under the caption")
    m = tf.get_style("ar_university")["margins"]
    check(cap.left() >= m["left"] - 0.01 and cap.right() <= page.width() - m["right"] + 0.01, "caption in margins")
    # drawn: nothing (no grid label) in the gap between the labelled frame and the caption
    from qgis.core import QgsLayoutExporter
    from qgis.PyQt.QtCore import QSize

    img = QgsLayoutExporter(lay).renderPageToImage(0, QSize(), 150)
    px = 150 / 25.4
    y_a, y_b = int((full.bottom() + 0.4) * px), int((cap.top() - 0.4) * px)
    ink = sum(1 for y in range(y_a, y_b) for x in range(int(full.left() * px), int(full.right() * px), 2)
              if QColor(img.pixel(x, y)).lightness() < 200)
    check(y_b > y_a and ink == 0, f"{lay.name()}: grid labels stay in their room ({ink} dark pixels in the gap)")
    left_gap = [x for x in range(int((full.left() - 3) * px), int((full.left() + 0.2) * px))]
    ink = sum(1 for x in left_gap for y in range(int(frame.top() * px), int(frame.bottom() * px), 2)
              if QColor(img.pixel(x, y)).lightness() < 200)
    check(ink == 0, f"{lay.name()}: left grid labels stay in their room ({ink})")
src_text = f1.itemById("thesis-source-ar").text()
check(src_text.startswith("المصدر:") and "Field survey 2026" in src_text, f"source line: {src_text}")
check("Field survey 2026" in f3.itemById("thesis-source-ar").text(), "layer rights metadata reach the source line")
sb = f1.itemById("scalebar")
units = sb.unitsPerSegment()
check(any(abs(units / 10 ** e - m) < 1e-6 for e in range(-3, 7) for m in (1, 2, 2.5, 5)),
      f"scale bar segment is a round length ({units} {sb.unitLabel()})")
sc = f1.itemById("map").scale()
check(any(abs(sc - s) < 1e-3 for s in tf.round_scales()), f"map at a round scale 1:{sc:.0f}")
check(sc < 150001, f"the round scale does not shrink the map to half (1:{sc:.0f})")

# ---- renumbering ------------------------------------------------------------------------------------------------
tf.delete_figure(f1, project=project)
figs = tf.figures(project)
check([f["number_ar"] for f in figs] == ["3-1", "4-1"], f"after deleting 3-1: {[f['number_ar'] for f in figs]}")
check(json.loads(f2.customProperty(tf.KEY))["order"] == 1, "the stored order closes the gap")
check(f2.itemById("thesis-caption-ar").text() == "شكل (3-1): شبكة الطرق الرئيسية", "the page number follows")
f4 = tf.add_figure(project, 3, "استخدامات الأرض", "Land use", "ar_university")
tf.move_figure(f3, 3, 1, project)
figs = tf.figures(project)
order = [(f["layout"], f["number_ar"]) for f in figs]
check(order == [(f3.name(), "3-1"), (f2.name(), "3-2"), (f4.name(), "3-3")], f"after moving 4-1 to 3-1: {order}")
check(f3.itemById("thesis-caption-ar").text().startswith("شكل (3-1)"), "moved figure shows its new number")
tf.move_figure(f4, 3, 2, project)
check([f["layout"] for f in tf.figures(project)] == [f3.name(), f4.name(), f2.name()], "move inside a chapter")

# ---- English APA-like style: continuous numbers, caption above ---------------------------------------------------
tf.apply_style_all("en_apa", project)
figs = tf.figures(project)
check([f["number"] for f in figs] == ["1", "2", "3"], f"continuous numbering {[f['number'] for f in figs]}")
check(figs[1]["text_en"] == "Figure 2. Land use", f"APA text {figs[1]['text_en']}")
num = f4.itemById("thesis-number-en")
cap = f4.itemById("thesis-caption-en")
check(num is not None and num.text() == "Figure 2" and cap.text() == "Land use", "APA number line and title line")
m4 = f4.itemById("map")
check(cap is not None and item_rect(cap).bottom() <= m4.pos().y() - tf.grid_room(tf.get_style("en_apa")) + 0.01,
      "APA caption above the map and its grid labels")
check(f4.itemById("thesis-source-en").text().startswith("Note."), "APA note line")
check(f4.itemById("legend").title() == "Legend", "English legend title")

# ---- both languages ---------------------------------------------------------------------------------------------
both = dict(tf.get_style("ar_university"), id="both_test", language="both", grid="metres", orientation="landscape",
            numbering="continuous")
tf.apply_style(f2, both, project)
check(f2.itemById("thesis-caption-ar") is not None and f2.itemById("thesis-caption-en") is not None,
      "both languages: Arabic and English captions")
check(item_rect(f2.itemById("thesis-caption-en")).top() >= item_rect(f2.itemById("thesis-caption-ar")).bottom() - 0.1,
      "English caption under the Arabic one")
pw = f2.pageCollection().page(0).pageSize()
check(pw.width() > pw.height(), "landscape page")
m2 = f2.itemById("map")
r2 = tf.grid_room(both)
check(page_rect(f2).contains(QRectF(m2.pos().x() - r2, m2.pos().y() - r2, m2.rect().width() + 2 * r2,
                                    m2.rect().height() + 2 * r2)), "metric grid labels inside the page")

# ---- export at 300 dpi --------------------------------------------------------------------------------------------
out = os.path.join(work, "figures")
res = tf.export_all(out, dpi=300, formats=("png", "jpg", "pdf"), project=project)
check(not res["failed"], f"no failed export {res['failed']}")
expect = {f"Fig_{n}.{e}" for n in ("1", "2", "3") for e in ("png", "jpg", "pdf")}
check(expect <= set(os.listdir(out)), f"files named Fig_n: {sorted(os.listdir(out))}")
img = QImage(os.path.join(out, "Fig_1.png"))
check(abs(img.width() - 2480) <= 1 and abs(img.height() - 3508) <= 1, f"A4 portrait at 300 dpi: {img.width()}x{img.height()}")
img = QImage(os.path.join(out, "Fig_3.png"))      # f2: landscape
check(abs(img.width() - 3508) <= 1 and abs(img.height() - 2480) <= 1, f"A4 landscape at 300 dpi: {img.width()}x{img.height()}")
check(abs(img.dotsPerMeterX() / 39.3701 - 300) < 2, "300 dpi written in the PNG")
check(os.path.getsize(os.path.join(out, "Fig_1.pdf")) > 1000, "PDF written")

# list of figures (no python-docx here: html + txt)
lists = res["list"]
check(lists, f"list of figures written {lists}")
blob = "".join(open(p, encoding="utf-8-sig").read() for p in lists if not p.endswith(".docx"))
for c in ("استخدامات الأرض", "شبكة الطرق الرئيسية", "توزيع المدارس", "Land use", "Main road network",
          "Distribution of schools", "قائمة الأشكال", "List of Figures"):
    check(c in blob, f"list of figures has «{c}»")
if any(p.endswith(".docx") for p in lists):
    import zipfile

    xml = zipfile.ZipFile([p for p in lists if p.endswith(".docx")][0]).read("word/document.xml").decode("utf-8")
    check("Main road network" in xml and "توزيع المدارس" in xml, "docx list has the captions")

# back to Chapter numbering for a 3-2 file name
tf.apply_style_all("ar_university", project)
res = tf.export_all(os.path.join(work, "ch"), dpi=150, formats=("png",), project=project, with_list=False)
check(sorted(os.path.basename(p) for p in res["files"]) == ["Fig_3-1.png", "Fig_3-2.png", "Fig_3-3.png"],
      f"chapter file names {[os.path.basename(p) for p in res['files']]}")

# ---- style files ------------------------------------------------------------------------------------------------
mine = dict(tf.get_style("plain"), id="cairo_geo", name="Cairo University - Geography", caption_pt=13.0,
            margins={"top": 30, "bottom": 25, "left": 25, "right": 40}, caption_ar="خريطة ({num}) {caption}",
            numbering="continuous", language="both", grid="degrees", inset=True)
path = os.path.join(work, "cairo.json")
tf.export_style(mine, path)
back = tf.import_style(path)
check(back == tf.normalise_style(mine), "style JSON round trip")
check("cairo_geo" in [s["id"] for s in tf.all_styles()], "imported style is kept among the user's styles")
bad = tf.normalise_style({"grid": "zigzag", "caption_pt": "huge", "margins": {"top": -5}, "page": [100, 150],
                          "language": "ar"})
check(bad["grid"] == "degrees" and bad["caption_pt"] == 12.0 and bad["margins"]["top"] == 0
      and bad["page"] == [100.0, 150.0], "a broken style file is made safe")
check(tf.caption_text(back, "ar", "5", "x") == "خريطة (5) x", "custom caption pattern")

# ---- record written by hand / read by other tools ------------------------------------------------------------
f2.setCustomProperty(tf.KEY, json.dumps({"chapter": "2", "order": 7, "caption": "يدوي", "caption_en": ""}))
figs = tf.figures(project)
check(figs[0]["layout"] == f2.name() and figs[0]["number_ar"] == "2-1", "a hand-edited record is read and numbered")
check(figs[0]["text_en"] == "Figure 2.1: يدوي", "English falls back to the Arabic caption")

# ---- Processing tools -------------------------------------------------------------------------------------------
from qgis.core import QgsProcessingContext, QgsProcessingFeedback  # noqa: E402

from biruni.algorithms import thesis_figures as alg_mod  # noqa: E402

ctx = QgsProcessingContext()
ctx.setProject(project)
fb = QgsProcessingFeedback()
alg = alg_mod.ApplyThesisStyle()
alg.initAlgorithm()
res, ok = alg.run({"STYLE": 2, "STYLE_FILE": ""}, ctx, fb)
check(ok and res.get("FIGURES") == 3, f"apply style tool {res}")
check(tf.layout_style(f3)["id"] == "plain", "apply style tool used the plain style")
alg = alg_mod.ExportThesisFigures()
alg.initAlgorithm()
res, ok = alg.run({"FOLDER": os.path.join(work, "alg"), "DPI": 96, "FORMATS": [0], "LIST": True}, ctx, fb)
check(ok and len(res.get("FILES", [])) == 3 and res.get("LIST"), f"export tool {res}")

# ---- dialog (built, never shown) ------------------------------------------------------------------------------
from biruni.thesis import figures_dialog as fdlg  # noqa: E402


class Plugin:
    iface = None
    mw = None


dlg = fdlg.FiguresDialog(Plugin())
check(dlg.table.rowCount() == 3, f"dialog lists the figures ({dlg.table.rowCount()})")
check(dlg.table.item(0, 2).text() == "2-1" or dlg.table.item(0, 2).text() == "2.1", "dialog shows the numbers")
dlg.deleteLater()

# ---- one figure to look at -----------------------------------------------------------------------------------
tf.apply_style(f4, "ar_university", project)
look = os.path.join(os.environ.get("BIRUNI_LOOK", work), "look_ar.png")
tf.export_figure(f4, look, dpi=110)
tf.apply_style(f2, "en_apa", project)
tf.export_figure(f2, os.path.join(os.environ.get("BIRUNI_LOOK", work), "look_en.png"), dpi=110)
print("look at:", look)

project.clear()
shutil.rmtree(work, ignore_errors=True) if not os.environ.get("BIRUNI_LOOK") else None
print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
