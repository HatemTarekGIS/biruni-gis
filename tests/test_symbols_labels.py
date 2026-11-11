"""Map Studio symbols (libraries, icons by name, scale-dependent symbology, hatches) and smart labels
(bilingual, priorities, hidden-label report).

Run:  engine\\bin\\python-qgis.bat tests\\test_symbols_labels.py
"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

from qgis.core import (QgsApplication, QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeature,
                       QgsGeometry, QgsMapSettings, QgsPointXY, QgsProject, QgsRectangle, QgsRenderContext,
                       QgsStyle, QgsVectorLayer)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor, QImage, QPainter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

app = QgsApplication([], False)
app.initQgis()
from biruni.map_studio import smart_labels as sl  # noqa: E402
from biruni.map_studio import symbols as sy  # noqa: E402

OUT = os.environ.get("BIRUNI_TEST_OUT") or tempfile.mkdtemp()
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- the library -----------------------------------------------------------------------------------------
sy.generate(force=True)
ids = sy.all_ids()
missing = [i for i in ids if not os.path.exists(sy.path(i))]
check(not missing, f"{len(ids)} SVG symbols written {missing}")
bad = []
for i in ids:
    try:
        root = ET.parse(sy.path(i)).getroot()
        text = open(sy.path(i), encoding="utf-8").read()
        if "param(fill)" not in text and "param(outline)" not in text:
            bad.append(i)
    except ET.ParseError:
        bad.append(i)
check(not bad, f"every SVG is valid XML with colour parameters {bad}")
check({"survey", "utility", "road", "poi"} <= {sy.DRAWN[i][0] for i in sy.DRAWN} | {"poi"}, "four groups")

# render a sheet of all symbols and check pixels are drawn in the requested colour
img = QImage(12 * 40, (len(ids) // 12 + 1) * 40, QImage.Format.Format_ARGB32)
img.fill(QColor("#ffffff"))
p = QPainter(img)
ctx = QgsRenderContext.fromQPainter(p)
blank = []
for k, i in enumerate(ids):
    sym = sy.marker(i, size=8)
    sym.startRender(ctx)
    sym.renderPoint(QgsPointXY(20 + 40 * (k % 12), 20 + 40 * (k // 12)).toQPointF(), None, ctx)
    sym.stopRender(ctx)
p.end()
for k, i in enumerate(ids):
    cx, cy = 20 + 40 * (k % 12), 20 + 40 * (k // 12)
    if all(img.pixelColor(cx + dx, cy + dy).name() == "#ffffff" for dx in range(-10, 11, 2) for dy in range(-10, 11, 2)):
        blank.append(i)
img.save(os.path.join(OUT, "symbols_sheet.png"))
check(not blank, f"every symbol draws something {blank}")
red = sy.marker("mosque", 8, fill="#ff0000")
check(red.symbolLayer(0).fillColor().name() == "#ff0000", "colours are parameters (any fill colour)")

style = QgsStyle()
style.createMemoryDatabase()
n = sy.install_style(style)
check(n == len(ids) + len(sy.HATCHES), f"{n} symbols installed in a style library")
check(len(style.symbolsWithTag(QgsStyle.StyleEntity.SymbolEntity, style.tagId("Biruni"))) == n, "all tagged Biruni")

# ---- icons by name -----------------------------------------------------------------------------------------
cases = {"مسجد الراجحي": "mosque", "جامع الملك فهد": "mosque", "Central Bank": "bank", "صيدلية النهدي": "pharmacy",
         "مستشفى الملك خالد": "hospital", "مركز صحي الروضة": "clinic", "Starbucks Cafe": "cafe",
         "محطة وقود ساسكو": "fuel", "مدرسة ابتدائية 12": "school", "أمانة منطقة الرياض": "government",
         "حديقة الملز": "park", "مطار الملك خالد": "airport", "Holiday Inn Hotel": "hotel", "قسم شرطة": "police",
         "شارع 15": None, "": None, None: None}
wrong = {k: (sy.symbol_for_value(k), v) for k, v in cases.items() if sy.symbol_for_value(k) != v}
check(not wrong, f"symbol from the name in Arabic and English {wrong}")

pts = QgsVectorLayer("Point?crs=EPSG:32638&field=name:string&field=kind:string&field=name_en:string"
                     "&field=rank:string", "places", "memory")
feats = []
names = [("مسجد الراجحي", "mosque", "Al Rajhi Mosque", "town"), ("صيدلية النهدي", "pharmacy", "Nahdi", "village"),
         ("مدرسة 12", "school", "School 12", "village"), ("الرياض", "city", "Riyadh", "capital"),
         ("حي النسيم", "x", "Al Naseem", "suburb")]
for k, (a, kind, e, r) in enumerate(names):
    f = QgsFeature(pts.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(670000 + k * 300, 2730000)))
    f.setAttributes([a, kind, e, r])
    feats.append(f)
pts.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(pts)
chosen = sy.apply_icons(pts, "name")
check(chosen["مسجد الراجحي"] == "mosque" and chosen["صيدلية النهدي"] == "pharmacy"
      and chosen["حي النسيم"] == "pin", f"apply_icons categories {chosen}")

# ---- scale dependent ---------------------------------------------------------------------------------------
sy.scale_dependent(pts, threshold=50000)
rules = pts.renderer().rootRule().children()
far, near = rules[0], rules[1]
check(far.maximumScale() == 50000 and near.minimumScale() == 50000 and len(near.children()) == len(chosen) + 1,
      "dots when zoomed out, icons when zoomed in")


def render(layer, extent, size=(600, 300), name=None):
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setDestinationCrs(layer.crs())
    ms.setExtent(extent)
    ms.setOutputSize(QSize(*size))
    ms.setBackgroundColor(QColor("#ffffff"))
    from qgis.core import QgsMapRendererParallelJob

    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    im = job.renderedImage()
    if name:
        im.save(os.path.join(OUT, name))
    return ms, im


render(pts, QgsRectangle(669800, 2729900, 671500, 2730100), name="near.png")

# ---- hatches -----------------------------------------------------------------------------------------------
h = sy.hatch_fill(angle=45, cross=True)
check(h.symbolLayerCount() == 3, "cross hatch = outline + two line patterns")

# ---- bilingual ---------------------------------------------------------------------------------------------
sl.bilingual(pts, "name", "name_en")
lab0 = pts.labeling()
expr = QgsExpression(lab0.settings().fieldName)
ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(pts))
ctx.setFeature(next(pts.getFeatures()))
val = expr.evaluate(ctx)
check(val == "مسجد الراجحي\nAl Rajhi Mosque", f"two-line label {val!r}")
f = QgsFeature(pts.fields())
f.setAttributes(["فقط عربي", "x", None, "village"])
ctx.setFeature(f)
check(expr.evaluate(ctx) == "فقط عربي", "a missing English name leaves one line")

# ---- priorities ----------------------------------------------------------------------------------------------
ranks = sl.priorities(pts, "rank")
check(ranks["capital"] == 10 and ranks["town"] == 6 and ranks["village"] == 4 and ranks["suburb"] == 2,
      f"importance ranks {ranks}")
lab = pts.labeling()  # keep the labeling: settings() of a temporary crashes (engine trap)
st = lab.settings()
props = st.dataDefinedProperties()
from qgis.core import QgsPalLayerSettings  # noqa: E402

check(props.isActive(QgsPalLayerSettings.Property.Priority) and props.isActive(QgsPalLayerSettings.Property.Size),
      "priority and size follow importance")
num = QgsVectorLayer("Point?crs=EPSG:4326&field=pop:integer", "n", "memory")
check(sl.rank_of("طريق سريع") == 10 and sl.rank_of("شارع فرعي") == 2 and sl.rank_of("Primary") == 8,
      "road classes")

# ---- hidden labels -----------------------------------------------------------------------------------------
crowd = QgsVectorLayer("Point?crs=EPSG:32638&field=name:string", "crowd", "memory")
fs = []
for k in range(60):
    g = QgsFeature(crowd.fields())
    g.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(670000 + (k % 10) * 20, 2730000 + (k // 10) * 20)))
    g.setAttributes([f"اسم طويل جدا رقم {k}"])
    fs.append(g)
crowd.dataProvider().addFeatures(fs)
QgsProject.instance().addMapLayer(crowd)
from qgis.core import QgsVectorLayerSimpleLabeling  # noqa: E402

s = QgsPalLayerSettings()
s.fieldName = "name"
s.setFormat(sl._format(10))
crowd.setLabeling(QgsVectorLayerSimpleLabeling(s))
crowd.setLabelsEnabled(True)
ms = QgsMapSettings()
ms.setLayers([crowd])
ms.setDestinationCrs(crowd.crs())
ms.setExtent(QgsRectangle(669900, 2729900, 670300, 2730200))
ms.setOutputSize(QSize(500, 400))
rep = sl.hidden_labels_report(ms)
check(rep["placed"] > 0 and rep["hidden"] > 0 and rep["placed"] + rep["hidden"] == 60,
      f"hidden labels counted (placed {rep['placed']}, hidden {rep['hidden']})")
check(rep["tips"] and rep["layers"]["crowd"]["examples"], "tips and examples given")
print("     " + sl.report_text(rep).replace("\n", "\n     "))

# layout target
from qgis.core import QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout  # noqa: E402

lay = QgsPrintLayout(QgsProject.instance())
lay.initializeDefaults()
m = QgsLayoutItemMap(lay)
m.attemptMove(QgsLayoutPoint(10, 10))
m.attemptResize(QgsLayoutSize(80, 60))
m.setLayers([crowd])
m.setExtent(ms.extent())
lay.addLayoutItem(m)
rep2 = sl.hidden_labels_report(lay)
check(rep2["placed"] + rep2["hidden"] == 60, f"report from a layout ({rep2['placed']}+{rep2['hidden']})")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
