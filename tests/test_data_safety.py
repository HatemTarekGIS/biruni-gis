r"""Fixes that keep data from being changed without the user knowing (the review of 2026-11-03):
the modify tools keep surveyed heights (Z), the vertex table writes only the vertices the user typed,
"Next number" gives features made together their own numbers, a restore keeps the project file first,
a crash leaves a log, the problem-report log receives the messages, and a local server's port cannot be
taken by a second server.

Run:  engine\bin\python-qgis.bat tests\test_data_safety.py
"""
import os
import subprocess
import sys
import tempfile
import textwrap

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.join(HERE, "..", "app", "plugins")
sys.path.insert(0, PLUGINS)

from qgis.core import (Qgis, QgsApplication, QgsFeature, QgsGeometry, QgsMessageLog,  # noqa: E402
                       QgsVectorLayer, QgsVectorLayerUtils)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.PyQt.QtWidgets import QTableWidget  # noqa: E402

from biruni import crashlog, rules  # noqa: E402
from biruni.editing import modify_geom as mg  # noqa: E402
from biruni.editing.modify_pane import ModifyFeaturesPane  # noqa: E402
from biruni.local_server import ExclusiveServer  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def heights(g):
    if isinstance(g, tuple):
        g = g[0]
    return g.constGet().is3D() and all(v.z() == v.z() and v.z() != 0 for v in g.vertices())


# ---- 1. heights ----------------------------------------------------------------------------------------
poly = QgsGeometry.fromWkt("POLYGON Z((0 0 10,10 0 11,10 0.001 11,10 10 12,0 10 13,0 0 10))")
line = QgsGeometry.fromWkt("LINESTRING Z(0 0 5,5 0 6,5.0001 0 6,10 5 7)")
line2 = QgsGeometry.fromWkt("LINESTRING Z(10.05 5 9,20 5 9)")
tools = {
    "remove duplicate vertices": lambda: mg.remove_duplicate_vertices(poly, 0.01),
    "remove spikes": lambda: mg.remove_spikes(poly, 1),
    "align to an edge": lambda: mg.align_edge(poly, QgsGeometry.fromWkt("LINESTRING(-1 -0.05, 11 -0.05)"), 0.1),
    "round a line corner": lambda: mg.fillet(QgsGeometry.fromWkt("LINESTRING Z(0 0 1,10 0 2,10 10 3)"), 1, 2),
    "round a polygon corner": lambda: mg.fillet(poly, 3, 1),
    "two-point cut": lambda: mg.two_point_cut(poly, (5, 0), (5, 10))[0],
    "extend": lambda: mg.extend_line(line, QgsGeometry.fromWkt("LINESTRING(15 0,15 20)")),
    "close line gaps": lambda: list(mg.close_line_gaps([line, line2], 0.2).values())[0],
}
for name, fn in tools.items():
    check(heights(fn()), f"{name} keeps the heights (Z)")
kept = mg.remove_duplicate_vertices(poly, 0.01)[0]
check([v.z() for v in kept.vertices()] == [10, 11, 12, 13, 10], "an untouched vertex keeps its own height exactly")
mid = mg.two_point_cut(poly, (5, 0), (5, 10))[0]
check(any(abs(v.z() - 10.5) < 1e-9 for v in mid.vertices()), "a new vertex gets the height between its neighbours")
flat2d = QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))")
check(not mg.remove_spikes(flat2d, 1)[0].constGet().is3D(), "a 2D feature stays 2D")

# ---- 2. the vertex table ---------------------------------------------------------------------------------
lyr = QgsVectorLayer("Polygon?crs=EPSG:32638&field=n:integer", "p", "memory")
f = QgsFeature(lyr.fields())
f.setGeometry(QgsGeometry.fromWkt("POLYGON((500000.123456 2700000.987654,500010.5 2700000,500010 2700010.25,"
                                  "500000.123456 2700000.987654))"))
lyr.dataProvider().addFeatures([f])
fid = next(lyr.getFeatures()).id()
table = QTableWidget(0, 2)
ModifyFeaturesPane._fill_vertices(None, table, {lyr: [fid]})
check(ModifyFeaturesPane._vertex_moves(None, table) == {}, "an untouched table moves no vertex (no rounding)")
table.item(1, 0).setText("500011")
moves = ModifyFeaturesPane._vertex_moves(None, table)
check(list(moves) == [1] and moves[1] == (500011.0, 2700000.0), "only the typed vertex moves")
table.item(1, 0).setText("500010.500")
check(ModifyFeaturesPane._vertex_moves(None, table) == {}, "the shown text typed again is no move")
table.item(0, 0).setText("500000.2")
m = ModifyFeaturesPane._vertex_moves(None, table)
check(abs(m[0][1] - 2700000.987654) < 1e-9, "the other coordinate of a typed vertex keeps its full value")

# ---- 3. next number ---------------------------------------------------------------------------------------
lyr = QgsVectorLayer("Point?crs=EPSG:32638&field=no:integer&field=code:string", "pts", "memory")
r = rules.read_rules(lyr)
r["no"].auto = "number"
r["code"].auto = "number"
rules.apply_rules(lyr, r)
check(rules.number_fields(lyr) == [0, 1], "the next-number fields are found")
lyr.startEditing()
feats = [QgsVectorLayerUtils.createFeature(lyr, QgsGeometry.fromWkt(f"POINT({i} 0)"), {}) for i in range(3)]
check(len({ft["no"] for ft in feats}) == 1, "(the engine gives features made together the same number)")
rules.renumber(lyr, feats)
lyr.addFeatures(feats)
check(sorted(ft["no"] for ft in lyr.getFeatures()) == [1, 2, 3], "three features made together: 1, 2, 3")
more = [QgsVectorLayerUtils.createFeature(lyr, QgsGeometry.fromWkt("POINT(9 9)"), {0: 2}) for _ in range(2)]
rules.renumber(lyr, more)
lyr.addFeatures(more)
check(sorted(ft["no"] for ft in lyr.getFeatures()) == [1, 2, 3, 4, 5],
      "copies of a feature get new numbers, counting the unsaved ones")
check(sorted(ft["code"] for ft in lyr.getFeatures()) == ["1", "2", "3", "4", "5"], "a text field is numbered too")

# ---- 4. the crash log ---------------------------------------------------------------------------------------
where = tempfile.mkdtemp()
script = textwrap.dedent(f"""
    import os, sys, ctypes
    sys.path.insert(0, {PLUGINS!r})
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from qgis.core import QgsApplication
    app = QgsApplication([], False)
    from biruni import crashlog
    crashlog.start({where!r})
    crashlog.note("drawing a parcel")
    ctypes.string_at(0)   # a native crash
""")
p = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=120)
check(p.returncode != 0, "(the test process crashed in native code)")
found = crashlog.start(where)
text = open(found, encoding="utf-8", errors="replace").read() if found else ""
check(bool(found) and "drawing a parcel" in text, "the next start finds the crash and what was logged before it")
check("access violation" in text.lower() or "fatal" in text.lower(), "the crash itself is in the log, with the stack")
crashlog.stop()
check(crashlog.start(where) is None, "a normal exit is not taken for a crash")
crashlog.stop()
check(crashlog.kept_logs(where) == [found], "the crash log is kept for problem reports")

# ---- 5. problem reports receive the messages -------------------------------------------------------------
from biruni.support import Support  # noqa: E402


class _P:
    iface = None


sup = Support(_P())
sup.install()
QgsMessageLog.logMessage("a message for the report", "test", Qgis.MessageLevel.Warning)
app.processEvents()
check(any("a message for the report" in ln for ln in sup.log), "the problem report's log receives the messages")
sup.unload()

# ---- 6. a local server's port is its own ---------------------------------------------------------------
from http.server import BaseHTTPRequestHandler  # noqa: E402

a = ExclusiveServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
port = a.server_address[1]
try:
    b = ExclusiveServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    b.server_close()
    check(False, "a second server cannot take a port in use")
except OSError:
    check(True, "a second server cannot take a port in use")
a.server_close()

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
