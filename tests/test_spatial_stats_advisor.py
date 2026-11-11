r"""Spatial statistics advisor («مستشار الإحصاء المكاني»): questions, the data checks before a tool runs,
the prefilled tool parameters and the window (built offscreen, never shown).

Run:  engine\bin\python-qgis.bat tests\test_spatial_stats_advisor.py

Tools of the toolbox that are not registered are SKIPPED, not failed.
"""
import math
import os
import random
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsFeature, QgsField, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtCore import QDate, QMetaType  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import i18n  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.spatial_stats import advisor as adv  # noqa: E402
from biruni.spatial_stats import advisor_dialog as ad  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def skip(msg):
    print("SKIP " + msg)


def make(kind, crs, rows, fields, geoms):
    """rows: list of attribute lists; fields: [(name, QMetaType)]; geoms: WKT list."""
    lyr = QgsVectorLayer(f"{kind}?crs={crs}", "t", "memory")
    lyr.dataProvider().addAttributes([QgsField(n, t) for n, t in fields])
    lyr.updateFields()
    feats = []
    for attrs, wkt in zip(rows, geoms):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        f.setAttributes(attrs)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def squares(n, ncol=6, s=100.0):
    return [f"POLYGON(({500000 + (i % ncol) * s} {2700000 + (i // ncol) * s},{500000 + (i % ncol + 1) * s} "
            f"{2700000 + (i // ncol) * s},{500000 + (i % ncol + 1) * s} {2700000 + (i // ncol + 1) * s},"
            f"{500000 + (i % ncol) * s} {2700000 + (i // ncol + 1) * s},{500000 + (i % ncol) * s} "
            f"{2700000 + (i // ncol) * s}))" for i in range(n)]


def has(found, level, *words):
    return any(lv == level and all(w.lower() in m.lower() for w in words) for lv, m in found)


D = QMetaType.Type.Double
rnd = random.Random(3)

# ---- questions -----------------------------------------------------------------------------------------
qs = adv.questions()
ids = [q["id"] for q in qs]
check(len(qs) == 12 and len(set(ids)) == 12, f"12 questions with distinct ids ({ids})")
check(all(q["ar"] and q["en"] and q["tools"] for q in qs), "every question has Arabic and English words and tools")
check(all(t.startswith("biruni:ss") for q in qs for t in q["tools"]), "every tool is a biruni spatial stats id")
check(adv.question("explain")["tools"] == ["biruni:ssols", "biruni:ssgwr"], "explain: OLS first, then GWR")
check(adv.question("explain")["field2"] == "numeric_multi" and adv.question("time")["field2"] == "date"
      and adv.question("colocation")["field2"] == "category" and adv.question("clustered")["geometry"] == "point",
      "questions say what they need (explanatory list, date, category, points)")
for q in qs:
    for t in q["tools"]:
        if adv.registered(t):
            check(t in adv.tools_for(q["id"]), f"{q['id']}: {t} registered and offered")
        else:
            skip(f"{q['id']}: {t} not registered - not offered")
            check(t not in adv.tools_for(q["id"]), f"{q['id']}: unregistered {t} is not offered")
check(all(adv.tools_for(q["id"]) for q in adv.available_questions()), "available questions all have a tool")

# ---- inspect -------------------------------------------------------------------------------------------
check(has(adv.inspect(None, question_id="similar"), "stop", "vector layer"), "no layer: stop")
poly18 = make("Polygon", "EPSG:32637", [[float(i % 7)] for i in range(18)], [("v", D)], squares(18))
found = adv.inspect(poly18, "v", "similar")
check(has(found, "warn", "18 polygons", "little power"), "18 polygons: Moran's I will have little power")
check(has(found, "ok", "projected"), "projected CRS is fine")
check(found[-1][0] == "warn", "verdict: ready, with warnings")

pts_geo = make("Point", "EPSG:4326", [[rnd.random()] for _ in range(50)], [("v", D)],
               [f"POINT({46 + rnd.random() * 0.1} {24 + rnd.random() * 0.1})" for _ in range(50)])
found = adv.inspect(pts_geo, None, "clustered")
check(has(found, "ok", "degrees", "UTM"), "geographic CRS: measured in the local UTM zone")
check(has(found, "ok", "50 points"), "50 points are enough")
check(found[-1][0] == "ok", "clean points: ready")

check(has(adv.inspect(poly18, None, "clustered"), "stop", "about points"), "polygons for a point question: stop")
pts3 = make("Point", "EPSG:32637", [[1.0]] * 3, [("v", D)], ["POINT(0 0)", "POINT(10 0)", "POINT(0 10)"])
check(has(adv.inspect(pts3, None, "clustered"), "stop", "only 3 points"), "3 points: stop")
check(has(adv.inspect(pts_geo, "v", "rates"), "stop", "areas (polygons)"), "points for rates: stop")

n = 60
geoms = squares(n, 10)
flat = make("Polygon", "EPSG:32637", [[5.0] for _ in range(n)], [("v", D)], geoms)
check(has(adv.inspect(flat, "v", "similar"), "stop", "same value"), "constant field: stop")
zeros = make("Polygon", "EPSG:32637", [[float(rnd.choice([0, 0, 0, 0, 1, 2]))] for _ in range(n)], [("c", D)], geoms)
check(has(adv.inspect(zeros, "c", "hotspots"), "warn", "mostly zeros"), "zero-inflated count: warned")
skewed = make("Polygon", "EPSG:32637", [[math.exp(rnd.gauss(0, 1.5))] for _ in range(n)], [("s", D)], geoms)
check(has(adv.inspect(skewed, "s", "similar"), "warn", "skewed", "log"), "skewed values: log transform suggested")
nulls = make("Polygon", "EPSG:32637", [[None if i % 10 == 0 else float(i)] for i in range(n)], [("v", D)], geoms)
check(has(adv.inspect(nulls, "v", "similar"), "warn", "6 of 60", "no value"), "empty values counted")
check(has(adv.inspect(nulls, None, "similar"), "stop", "choose the numeric field"), "missing field: stop")
check(has(adv.inspect(nulls, None, "hotspots"), "stop", "for polygons"), "polygon hot spots need a value")
dups = make("Point", "EPSG:32637", [[1.0]] * 40, [("v", D)],
            [f"POINT({(i % 30) * 10} {(i % 30) * 7})" for i in range(40)])
check(has(adv.inspect(dups, None, "clustered"), "warn", "10 features", "same spot"), "duplicate locations warned")

dates = make("Point", "EPSG:32637", [[QDate(2024, 1, 1).addDays(i * 3), "A" if i % 3 else "B"] for i in range(120)],
             [("day", QMetaType.Type.QDate), ("kind", QMetaType.Type.QString)],
             [f"POINT({rnd.random() * 1000} {rnd.random() * 1000})" for _ in range(120)])
check(has(adv.inspect(dates, None, "time", "day"), "ok", "dates cover"), "date field read")
check(has(adv.inspect(dates, None, "time"), "stop", "date field"), "no date field: stop")
check(has(adv.inspect(dates, None, "colocation", "kind"), "ok", "2 categories"), "two categories found")
one = make("Point", "EPSG:32637", [["A"]] * 30, [("kind", QMetaType.Type.QString)],
           [f"POINT({i} {i * 2})" for i in range(30)])
check(has(adv.inspect(one, None, "colocation", "kind"), "stop", "fewer than two"), "one category: stop")

reg = make("Polygon", "EPSG:32637", [[float(i + rnd.random()), float(i), float(rnd.random())] for i in range(40)],
           [("y", D), ("x1", D), ("x2", D)], squares(40, 8))
check(has(adv.inspect(reg, "y", "explain", []), "stop", "explanatory"), "explain without explanatory: stop")
found = adv.inspect(reg, "y", "explain", ["x1", "x2"])
check(has(found, "warn", "fewer than 50", "GWR") and adv.ready(found), "40 features: GWR will be weak, OLS fine")
popl = make("Polygon", "EPSG:32637", [[float(rnd.randint(0, 9)), float(rnd.choice([50, 200, 5000]))] for _ in range(40)],
            [("cases", D), ("pop", D)], squares(40, 8))
check(has(adv.inspect(popl, "cases", "rates", "pop"), "warn", "20-fold"), "rates: very unequal populations warned")

i18n._lang = "ar"
found = adv.inspect(poly18, "v", "similar")
check(any("مضلعات" in m for _, m in found), "the checklist speaks Arabic in an Arabic interface")
i18n._lang = "en"

# ---- prefilled parameters -----------------------------------------------------------------------------
p = adv.parameters("biruni:ssols", reg, "y", ["x1", "x2"], "explain")
check(p == {"INPUT": reg.id(), "DEPENDENT": "y", "EXPLANATORY": ["x1", "x2"]}, f"OLS prefilled: {p}")
p = adv.parameters("biruni:ssgwr", reg, "y", ["x1", "y", "x2"], "explain")
check(p.get("DEPENDENT") == "y" and p.get("EXPLANATORY") == ["x1", "x2"], "GWR prefilled (the value is not "
      "its own explanation)")
check(adv.parameters("biruni:doesnotexist", reg, "y") == {}, "unknown tool: nothing prefilled")
optional = [("biruni:sshotspots", zeros, "c", None, "hotspots", {"c"}),
            ("biruni:ssmoran", poly18, "v", None, "similar", {"v"}),
            ("biruni:ssrates", popl, "cases", "pop", "rates", {"cases", "pop"}),
            ("biruni:ssbivariate", reg, "x1", "x2", "bivariate", {"x1", "x2"}),
            ("biruni:ssemerging", dates, None, "day", "time", {"day"}),
            ("biruni:sscolocation", dates, None, "kind", "colocation", {"kind", "A", "B"}),
            ("biruni:ssnearest", pts_geo, None, None, "clustered", set())]
for tid, lyr, f1, f2, qid, expect in optional:
    if not adv.registered(tid):
        skip(f"{tid} not registered")
        continue
    p = adv.parameters(tid, lyr, f1, f2, qid)
    vals = {v for k, v in p.items() if isinstance(v, str) and v != lyr.id()}
    check(lyr.id() in p.values() and vals == expect, f"{tid} prefilled: {p}")

# ---- the window (offscreen, never shown) ---------------------------------------------------------------
dlg = ad.AdvisorDialog(None)
check(dlg.windowTitle() == "Spatial statistics advisor", "window title")
check(dlg.questions.count() == len(adv.available_questions()), "the window lists the questions with tools")
check(dlg.select_question("explain"), "pick 'what explains this value'")
check(dlg.tool.count() >= 1 and dlg.tool_id() == "biruni:ssols", "OLS is suggested first")
dlg.set_layer(reg)
dlg.set_fields("y", ["x1", "x2"])
check(dlg.current_parameters() == {"INPUT": reg.id(), "DEPENDENT": "y", "EXPLANATORY": ["x1", "x2"]},
      f"window prefill: {dlg.current_parameters()}")
items = [dlg.checks.item(i).text() for i in range(dlg.checks.count())]
check(any(t.startswith("⚠") and "GWR" in t for t in items) and any(t.startswith("✓") for t in items),
      "checklist shows ✓ and ⚠ marks")
check(dlg.open_btn.isEnabled(), "the tool can be opened")
check(not dlg.multi.isHidden() and dlg.field2.isHidden(), "explanatory variables shown as a check list")
if dlg.select_question("similar"):
    dlg.set_layer(poly18)
    dlg.set_fields("v")
    check(any("little power" in dlg.checks.item(i).text() for i in range(dlg.checks.count())),
          "window: 18 polygons warning")
    check(dlg.multi.isHidden(), "no explanatory list for Moran's I")
else:
    skip("'similar' has no registered tool")
dlg.deleteLater()

print("ALL PASS" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
