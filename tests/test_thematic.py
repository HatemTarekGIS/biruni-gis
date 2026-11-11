"""Map Studio: the thematic map wizard (analyse / recommend / apply / undo).

Run:  engine\\bin\\python-qgis.bat tests\\test_thematic.py
"""
import os
import random
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from qgis.core import (QgsApplication, QgsCategorizedSymbolRenderer, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsGraduatedSymbolRenderer, QgsHeatmapRenderer, QgsLayoutExporter, QgsMapLayerStyle,
                       QgsMapRendererParallelJob, QgsMapSettings, QgsPointXY, QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QSize  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.map_studio import thematic as T  # noqa: E402

OUT = os.environ.get("BIRUNI_TEST_PNG", tempfile.mkdtemp())
P = QgsProject.instance()
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def mem(kind, name, rows, fields):
    lyr = QgsVectorLayer(f"{kind}?crs=EPSG:32638&{fields}", name, "memory")
    feats = []
    for geom, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(geom)
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    P.addMapLayer(lyr)
    return lyr


def sq(x, y, s=1000):
    return QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))")


def style_xml(lyr):
    st = QgsMapLayerStyle()
    st.readFromLayer(lyr)
    return st.xmlData()


def render(layers, name):
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(layers[0].crs())
    ext = layers[0].extent()
    for l in layers[1:]:
        ext.combineExtentWith(l.extent())
    ms.setExtent(ext.buffered(ext.width() * 0.03))
    ms.setOutputSize(QSize(500, 500))
    ms.setBackgroundColor(QColor("white"))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    path = os.path.join(OUT, name + ".png")
    job.renderedImage().save(path)
    return path


rnd = random.Random(7)
X0, Y0 = 500000, 2700000
# 10×10 districts: skewed population, a land-use type, a +/- change, a rate, a year
rows = []
for i in range(100):
    r, c = divmod(i, 10)
    pop = int(rnd.lognormvariate(8, 1.1))
    rows.append((sq(X0 + c * 1000, Y0 + r * 1000),
                 [pop, ["سكني", "تجاري", "صناعي", "زراعي"][i % 4], round(rnd.uniform(-35, 50), 1),
                  round(rnd.uniform(0, 1), 3), 2020 + i % 4, rnd.randint(1, 900)]))
districts = mem("Polygon", "districts", rows, "field=population:integer&field=landuse:string&field=change:double"
                                              "&field=literacy_rate:double&field=year:integer&field=houses:integer")

# ---- analyse --------------------------------------------------------------------------------------------------
a = T.analyse(districts, "population")
check(a["kind"] == "count" and a["n"] == 100 and a["skew"] > 1, f"population = skewed count ({a['kind']}, "
                                                                  f"skew {a.get('skew', 0):.2f})")
check(T.analyse(districts, "landuse")["kind"] == "qualitative", "land use = qualitative")
check(T.analyse(districts, "change")["kind"] == "diverging", "change = diverging")
check(T.analyse(districts, "literacy_rate")["kind"] == "rate", "literacy_rate = rate")

# ---- recommend ------------------------------------------------------------------------------------------------
rec = T.recommend(districts, "population")
check(rec[0]["type"] == "proportional", f"counts → proportional first ({[r['type'] for r in rec]})")
cho = [r for r in rec if r["type"] == "choropleth"]
check(cho and cho[0]["warnings"], "choropleth of raw counts carries the normalisation warning")
check(T.recommend(districts, "change")[0]["type"] == "diverging", "diverging recommended for +/-")
check(T.recommend(districts, "literacy_rate")[0]["type"] == "choropleth", "rate → choropleth")
check(T.recommend(districts, "landuse")[0]["type"] == "categorized", "names → categorized")
check(any(r["type"] == "bivariate" for r in T.recommend(districts, "literacy_rate", "population")),
      "bivariate offered with a second field")
check(T.method_for(0.1)[0] == "equal" and T.method_for(1.0)[0] == "jenks" and T.method_for(3)[0] == "quantile",
      "method by skewness")

# ---- nice breaks ----------------------------------------------------------------------------------------------
b, unit = T.nice_breaks([3.7, 1234.56, 4987.1, 10342.9, 27891.2], 3.7, 27891.2)
check(all(abs(x / unit - round(x / unit)) < 1e-9 for x in b), f"breaks are round {b}")
check(all(x < y for x, y in zip(b, b[1:])) and b[0] <= 3.7 and b[-1] >= 27891.2, "breaks increase and cover data")
b2, _ = T.nice_breaks([0.012, 0.0134, 0.0151, 0.0198], 0.012, 0.0198)
check(all(x < y for x, y in zip(b2, b2[1:])), f"close small breaks stay increasing {b2}")

# ---- apply each type, undo exactly ----------------------------------------------------------------------------
original = style_xml(districts)
res = T.apply(districts, "population", "choropleth", {"classes": 5})
r = districts.renderer()
check(isinstance(r, QgsGraduatedSymbolRenderer) and len(r.ranges()) >= 3, "choropleth applied")
check(sum(int(rg.label().rsplit("(", 1)[1].rstrip(")").replace(",", "").replace("٬", "")) for rg in r.ranges())
      == 100, f"class counts in labels add up to 100 ({[rg.label() for rg in r.ranges()]})")
check(T.has_backup(districts), "backup stored")
render([districts], "choropleth")
check(T.undo(districts) and style_xml(districts) == original, "undo restores the exact style")
check(not T.has_backup(districts), "backup cleared after undo")

T.apply(districts, "change", "diverging", {"classes": 6})
r = districts.renderer()
lows = [rg.lowerValue() for rg in r.ranges()] + [r.ranges()[-1].upperValue()]
check(0.0 in lows and len(r.ranges()) == 6, f"diverging centred on 0 ({lows})")
render([districts], "diverging")
T.undo(districts)

T.apply(districts, "landuse", "categorized")
check(isinstance(districts.renderer(), QgsCategorizedSymbolRenderer)
      and len(districts.renderer().categories()) == 4, "categorized: 4 classes")
T.undo(districts)
many = mem("Point", "shops", [(QgsGeometry.fromPointXY(QgsPointXY(X0 + i, Y0)), [f"t{i % 20}"]) for i in range(200)],
           "field=kind:string")
T.apply(many, "kind", "categorized")
cats = many.renderer().categories()
check(len(cats) == T.MAX_CATEGORIES and "(" in cats[-1].label(), "20 kinds → 11 + «Other»")
T.undo(many)

T.apply(districts, "literacy_rate", "unclassed")
check(T.has_backup(districts), "unclassed applied")
render([districts], "unclassed")
T.undo(districts)

T.apply(districts, "population", "proportional")
check(districts.renderer().orderByEnabled(), "proportional: big under small")
render([districts], "proportional")
T.undo(districts)
check(style_xml(districts) == original, "undo after proportional")

T.apply(districts, "literacy_rate", "bivariate", {"second_field": "population"})
check(len(districts.renderer().categories()) == 9, "bivariate 3×3")
render([districts], "bivariate")
T.undo(districts)

T.apply(districts, "literacy_rate", "value_alpha", {"second_field": "population"})
check(isinstance(districts.renderer(), QgsGraduatedSymbolRenderer), "value by alpha applied")
T.undo(districts)

n_layers = len(P.mapLayers())
out = T.apply(districts, "population", "dot_density")
dots = out["layer"]
check(dots.featureCount() > 1000 and "1" in dots.name(), f"dot density: {dots.featureCount()} dots ({out['message']})")
render([districts, dots], "dot_density")
T.undo(districts)
check(len(P.mapLayers()) == n_layers and style_xml(districts) == original, "undo removes the dots layer")

name = T.normalise(districts, "population", "area")
vals = [f[name] for f in districts.getFeatures()]
pop0 = next(districts.getFeatures())["population"]
check(abs(vals[0] - pop0) < pop0 * 0.02 + 1e-6, f"per km²: 1 km² cells ≈ same value ({vals[0]:.1f} vs {pop0})")
name2 = T.normalise(districts, "population", "houses")
check(districts.fields().indexOf(name2) >= 0, "per-field ratio added")
T.undo(districts)
check(districts.fields().indexOf(name) < 0 and districts.fields().indexOf(name2) < 0, "undo removes virtual fields")

pts = mem("Point", "incidents", [(QgsGeometry.fromPointXY(QgsPointXY(X0 + rnd.gauss(5000, 1500),
                                                                     Y0 + rnd.gauss(5000, 1500))),
                                  [rnd.randint(1, 10)]) for _ in range(800)], "field=severity:integer")
out = T.apply(pts, "severity", "hexbin", {"stat": "sum"})
hexes = out["layer"]
check(hexes.featureCount() > 10 and sum(f["n"] for f in hexes.getFeatures()) == sum(f["severity"] for f in
                                                                                     pts.getFeatures()),
      "hexbin sums the weights")
render([hexes], "hexbin")
T.undo(pts)
T.apply(pts, "severity", "heatmap")
check(isinstance(pts.renderer(), QgsHeatmapRenderer), "heatmap")
render([pts], "heatmap")
T.undo(pts)

cities = mem("Point", "cities", [(QgsGeometry.fromPointXY(QgsPointXY(X0 + x, Y0 + y)), [cid]) for cid, x, y in
                                 [("A", 0, 0), ("B", 9000, 1000), ("C", 4000, 8000), ("D", 9000, 9000)]],
             "field=code:string")
od = QgsVectorLayer("None?field=o:string&field=d:string&field=trips:double", "od", "memory")
for o, d, v in [("A", "B", 500), ("B", "A", 200), ("A", "C", 50), ("C", "D", 900), ("X", "A", 3)]:
    f = QgsFeature(od.fields())
    f.setAttributes([o, d, v])
    od.dataProvider().addFeature(f)
out = T.apply(cities, "trips", "flow", {"od_layer": od, "id_field": "code", "from_field": "o", "to_field": "d"})
check(out["layer"].featureCount() == 4, "flow: 4 matched OD pairs drawn, unknown origin skipped")
render([out["layer"], cities], "flow")
T.undo(cities)

out = T.apply(districts, "year", "small_multiples")
lay = out["layout"]
check(lay is not None and len([i for i in lay.items() if i.__class__.__name__ == "QgsLayoutItemMap"]) == 4,
      "small multiples: 4 maps")
QgsLayoutExporter(lay).exportToImage(os.path.join(OUT, "small_multiples.png"),
                                     QgsLayoutExporter.ImageExportSettings())
T.undo(districts)
check(style_xml(districts) == original, "final undo leaves the layer as it was")

# ---- the dialog builds headless and follows the layer -------------------------------------------------------------
from biruni.map_studio import thematic_dialog  # noqa: E402

dlg = thematic_dialog.ThematicDialog(None, districts)
dlg.field_box.setField("population")
dlg.refresh()
check(dlg.cards.count() >= 3 and dlg.cards.item(0).data(256) == "proportional", "dialog lists recommendation cards")
dlg.apply()
check(T.has_backup(districts) and "✓" in dlg.status.text(), "dialog applies")
dlg.undo()
check(style_xml(districts) == original, "dialog undo")
dlg.hist.grab().save(os.path.join(OUT, "dialog_hist.png"))
dlg.deleteLater()

print("PNG:", OUT)
print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
P.clear()
app.exitQgis()
sys.exit(1 if failures else 0)
