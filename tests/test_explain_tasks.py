"""Error explainer (explain.py, explain_rules.py) and task search (learn/tasks.py).

Run:  engine\\bin\\python-qgis.bat tests\\test_explain_tasks.py
"""
import os
import re
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import Qgis, QgsApplication, QgsMessageLog  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402,F401
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni import explain  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.commands import CATALOG  # noqa: E402
from biruni.explain_rules import RULES  # noqa: E402
from biruni.learn import tasks  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


registry = QgsApplication.processingRegistry()


def target_exists(t):
    return t in CATALOG or (":" in t and registry.algorithmById(t) is not None)


# ---- rules ----------------------------------------------------------------------------------------
ids = [r["id"] for r in RULES]
check(len(RULES) >= 60, f"{len(RULES)} rules")
check(len(ids) == len(set(ids)), "rule ids are unique")
wrong = []
for r in RULES:
    if not r["samples"]:
        wrong.append(f"{r['id']}: no sample")
    for s in r["samples"]:
        got = explain.find(s)
        if got is None or got["id"] != r["id"]:
            wrong.append(f"{r['id']}: {s[:70]!r} -> {got['id'] if got else None}")
check(not wrong, "every sample is explained by its own rule" + ("\n     " + "\n     ".join(wrong) if wrong else ""))
missing_actions = [r["id"] for r in RULES if r["action"] and r["action"] != "folder" and not target_exists(r["action"])]
check(not missing_actions, f"every action exists {missing_actions}")
check(all(r["title"] and r["cause"] and r["fix"] for r in RULES), "every rule has a title, a cause and a fix")

benign = [
    "Project saved", "Loading layer roads", "Algorithm Buffer finished", "Results: {'OUTPUT': 'memory:'}",
    "Execution completed in 0.12 seconds", "Loaded 404 features", "Plugin biruni loaded", "Rendering took 0.5 s",
    "Timestamp: 12:30", "Network request finished successfully", "CRS: EPSG:32637", "Using 4 threads",
    "Downloading tiles 50%", "Saved to D:/data/out.gpkg", "Selected features only", "Input layer: parcels",
    "تم حفظ المشروع", "جاري تحميل الطبقة", "Layer added: roads (1250 features)", "Opening D:/x/a.shp",
    "Processing algorithm…", "Starting algorithm 'Buffer'", "GDAL version 3.11", "Converted 12 features",
    "Snapping enabled", "Coordinate system set to EPSG:4326", "403 parcels exported", "Zoomed to layer",
]
noisy = [(b, explain.find(b)["id"]) for b in benign if explain.find(b) is not None]
check(not noisy, f"ordinary messages are not explained {noisy}")
check(explain.path_in("ERROR 4: Permission denied: 'C:/Program Files/out.shp'") == "C:/Program Files/out.shp",
      "the file path is found in a message")
check(explain.path_in("[WinError 32] used by another process: 'C:\\data\\parcels.shp'") == "C:\\data\\parcels.shp",
      "a Windows path with backslashes")
check(explain.find("<b>Layer</b> 'roads' could not be loaded.")["id"] == "layer_invalid", "HTML is ignored")
e = explain.explain_text("sqlite3_step() failed: database is locked")
check(e and e["id"] == "db_locked" and e["title"] and e["fix"], "explain_text gives title and fix")

# ---- the explainer: rate limit, levels, message log -------------------------------------------------
plugin = types.SimpleNamespace(iface=None, mw=None, runner=None)
ex = explain.Explainer(plugin)
shown = []
ex._announce = lambda entry: shown.append(entry["rule"]["id"])
explain.BAR_GAP_SECONDS = 0
ex.handle("sqlite3_step() failed: database is locked")
ex.handle("sqlite3_step() failed: database is locked")
ex.handle("ERROR 1: database is locked")
check(shown == ["db_locked"], f"the same kind of error is announced once a minute ({shown})")
check(ex.history[0]["count"] == 1 and ex.history[1]["count"] == 2, "repeats are counted in the history")
ex.handle("std::bad_alloc")
check(shown == ["db_locked", "out_of_memory"], "another kind of error is announced")
check(ex.handle("Project saved") is None and len(ex.history) == 3, "unknown messages are not kept")
explain.BAR_GAP_SECONDS = 5
ex.handle("Proxy authentication required")
check(shown[-1] == "out_of_memory" and ex.history[0]["rule"]["id"] == "proxy",
      "announcements a moment apart are held back, still kept in the history")
explain.BAR_GAP_SECONDS = 0
rec = ex.recent(2)
check(len(rec) == 2 and rec[0]["id"] == "proxy" and rec[0]["title"], "recent() for the Learn panel")

ex2 = explain.Explainer(plugin).install()
ex2._announce = lambda entry: None
QgsMessageLog.logMessage("Ring Self-intersection at or near point 1 2", "GEOS", Qgis.MessageLevel.Warning)
QgsMessageLog.logMessage("Too few points in geometry component", "GEOS", Qgis.MessageLevel.Info)
QgsMessageLog.logMessage("Feature (3) from “p” has invalid geometry.", "Processing", Qgis.MessageLevel.Critical)
for _ in range(20):
    app.processEvents()
got = [h["rule"]["id"] for h in ex2.history]
check(got == ["geom_invalid", "geom_self_intersection"], f"the message log is heard, info level ignored ({got})")
ex2.uninstall()
QgsMessageLog.logMessage("database is locked", "OGR", Qgis.MessageLevel.Critical)
app.processEvents()
check(len(ex2.history) == 2, "uninstall stops listening")

dlg = ex.show_dialog(ex.history[0])
from qgis.PyQt.QtWidgets import QPushButton  # noqa: E402

labels = [b.text() for b in dlg.findChildren(QPushButton)]
check(any("Copy" in t for t in labels) and any(t == ex.action_label("options") for t in labels),
      f"the dialog has the fix and copy buttons ({labels})")
dlg.close()
check(ex.action_label("native:fixgeometries") and ex.action_label("folder"), "action labels")

# ---- tasks ---------------------------------------------------------------------------------------
bad_targets = sorted({t for t, _en, _ar in tasks.TASKS if not target_exists(t)})
check(not bad_targets, f"every task target exists {bad_targets}")
check(len(tasks.TASKS) >= 120, f"{len(tasks.TASKS)} tasks")
queries = [
    ("احسب المسافة بين النقط والطرق", "native:joinbynearest"),
    ("عايز اعرف مساحة القطع", "biruni:geometryattributes"),
    ("حول الاكسل لنقط", "smart_table"),
    ("اقص طبقة بحدود", "native:clip"),
    ("ادمج طبقتين", "native:mergevectorlayers"),
    ("ازاي احسب طول الخطوط", "biruni:geometryattributes"),
    ("عايز اعمل بافر حوالين المدارس", "native:buffer"),
    ("نطاق 50 متر حول الطريق", "native:buffer"),
    ("اربط ملف اكسل بالطبقة", "native:joinattributestable"),
    ("كام نقطة في كل حي", "native:countpointsinpolygon"),
    ("حول ملف الاوتوكاد لشيب فايل", "cad_to_gis"),
    ("غير نظام الاحداثيات للطبقة", "native:reprojectlayer"),
    ("الطبقة في مكان غلط", "biruni:guesscrs"),
    ("مش عارف الطبقة على انهي اسقاط", "biruni:guesscrs"),
    ("ارسم قطعة من الصك", "biruni:deedtoparcel"),
    ("قسم الارض لاجزاء متساوية", "biruni:subdividepolygon"),
    ("صلح الاشكال الغلط", "native:fixgeometries"),
    ("القطع راكبة على بعض", "biruni:overlapsgaps"),
    ("اطبع الخريطة", "map_sheet"),
    ("ابعت المشروع لحد تاني", "package"),
    ("نزل صورة قمر صناعي", "biruni:downloadsentinel2"),
    ("خطوط كنتور من الارتفاعات", "gdal:contour"),
    ("مجاري السيول والاودية", "biruni:streamsbasins"),
    ("رجع نسخة قديمة من المشروع", "history_versions"),
    ("حصر كميات البردورات", "biruni:quantities"),
    ("قارن نسختين من الطبقة", "biruni:comparelayers"),
    ("رقم القطع بالترتيب", "biruni:numberfeatures"),
    ("اعمل خريطة ويب", "web_map"),
    ("مواقع من شات الواتساب", "biruni:whatsapplocations"),
    ("صورة مخطط سكانر لقطع", "biruni:scantoparcels"),
    ("الوضع الليلي", "theme"),
    ("امسح العناصر المكررة", "native:deleteduplicategeometries"),
    ("نقطة في منتصف كل قطعة", "native:centroids"),
    ("اقرب مستشفى لكل بيت", "closest_facility"),
    ("حول الشيب فايل لجيوباكدج", "export_layer"),
    ("calculate the area of parcels", "biruni:geometryattributes"),
    ("excel to points", "smart_table"),
    ("clip a layer by a boundary", "native:clip"),
    ("merge two layers", "native:mergevectorlayers"),
    ("distance from points to the nearest road", "native:joinbynearest"),
    ("change the coordinate system", "native:reprojectlayer"),
    ("which coordinate system is this", "biruni:guesscrs"),
    ("print a map to pdf", "map_sheet"),
    ("contour lines", "gdal:contour"),
    ("compare two versions of a layer", "biruni:comparelayers"),
    ("convert autocad dxf to shapefile", "cad_to_gis"),
]
misses = []
for q, want in queries:
    top = [t for _s, t, _p in tasks.search(q, 3)]
    if want not in top:
        misses.append(f"{q!r}: {top}")
check(not misses, f"{len(queries) - len(misses)}/{len(queries)} task queries find their tool in the top 3"
      + ("\n     " + "\n     ".join(misses) if misses else ""))
check(tasks.search("") == [] and tasks.search("عايز ازاي") == [], "empty or stop-word queries give nothing")
check(tasks.search("xyzzy qwerty") == [], "nonsense gives nothing")

# the search box: task results come with the ArcGIS names filter
from biruni.learn.locator import ArcGISNamesFilter  # noqa: E402
from qgis.core import QgsFeedback, QgsLocatorContext  # noqa: E402

flt = ArcGISNamesFilter(types.SimpleNamespace())
results = []
flt.resultFetched.connect(lambda r: results.append(r))
flt.fetchResults("حول الاكسل لنقط", QgsLocatorContext(), QgsFeedback())
check(any("→" in r.displayString and r.group == "What do you want to do?" for r in results),
      f"the search box shows tasks ({[r.displayString for r in results][:3]})")
results.clear()
flt.fetchResults("Buffer", QgsLocatorContext(), QgsFeedback())
check(any(r.group == "ArcGIS tool names" for r in results), "ArcGIS names still found")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
