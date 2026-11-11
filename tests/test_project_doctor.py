"""Project safety and housekeeping: who is using a file, the project doctor (broken paths, where data is
used, renaming a project), the unsaved-edits guard, steps to script/model, and placing an image by hand.

Run:  engine\\bin\\python-qgis.bat tests\\test_project_doctor.py
"""
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_CFG = tempfile.mkdtemp(prefix="biruni_cfg_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = _CFG

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsFillSymbol, QgsGeometry,  # noqa: E402
                       QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProcessingModelAlgorithm,
                       QgsProcessingModelChildParameterSource, QgsProject, QgsRasterLayer, QgsRectangle,
                       QgsSingleSymbolRenderer, QgsVectorFileWriter, QgsVectorLayer)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from qgis.gui import QgsMapCanvas, QgsMapMouseEvent, QgsMessageBar  # noqa: E402
from qgis.PyQt.QtCore import QEvent, QPoint, Qt  # noqa: E402

from biruni import edit_guard, file_locks, history, project_doctor as doc, quick_align, session_script  # noqa: E402
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.project import create_folder_structure, copy_vector_into_gpkg  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


TMP = tempfile.mkdtemp(prefix="biruni_doctor_")
UTM = QgsCoordinateReferenceSystem("EPSG:32637")
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()


def write_layer(path, geom_type, wkts, driver, layer_name=None):
    lyr = QgsVectorLayer(f"{geom_type}?crs=EPSG:32637&field=name:string(20)", "x", "memory")
    feats = []
    for i, w in enumerate(wkts):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(w))
        f.setAttributes([f"f{i}"])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    if layer_name:
        opts.layerName = layer_name
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path, QgsProject.instance().transformContext(), opts)
    assert err == QgsVectorFileWriter.WriterError.NoError, msg
    return path


def sq(x, y, s=100):
    return f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))"


class FakeIface:
    def __init__(self, canvas=None):
        self.bar = QgsMessageBar()
        self.canvas = canvas
        self.opened = []

    def messageBar(self):  # noqa: N802
        return self.bar

    def mainWindow(self):  # noqa: N802
        return None

    def activeLayer(self):  # noqa: N802
        return None

    def layerTreeView(self):  # noqa: N802
        return None

    def mapCanvas(self):  # noqa: N802
        return self.canvas

    def newProject(self, prompt=True):  # noqa: N802
        QgsProject.instance().clear()
        return True

    def addProject(self, path):  # noqa: N802
        self.opened.append(path)
        return QgsProject.instance().read(path)


class FakePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.project = None


# =====================================================================================================
# 1. Who is using this file?
# =====================================================================================================
d1 = os.path.join(TMP, "locks")
os.makedirs(d1)
shp = write_layer(os.path.join(d1, "roads.shp"), "LineString", ["LINESTRING(0 0,10 10)"], "ESRI Shapefile")
files = file_locks.dataset_files(shp)
exts = sorted(os.path.splitext(f)[1].lower() for f in files)
check({".shp", ".shx", ".dbf", ".prj"} <= set(exts), f"shapefile dataset files {exts}")
check(sorted(file_locks.dataset_files(shp.replace(".shp", ".dbf"))) == sorted(files), "any sidecar names the dataset")
gp = write_layer(os.path.join(d1, "db.gpkg"), "Point", ["POINT(1 1)"], "GPKG", "pts")
open(gp + "-wal", "wb").close()
gfiles = file_locks.dataset_files(gp + "|layername=pts")
check(gp in gfiles and gp + "-wal" in gfiles, "GeoPackage with its journal, layer suffix ignored")
gdb = os.path.join(d1, "city.gdb")
os.makedirs(gdb)
for f in ("a00000001.gdbtable", "a00000001.gdbtablx", "_gdb.MYPC.1234.5678.sr.lock",
          "parcels.OFFICE-7.99.1.ed.lock"):
    open(os.path.join(gdb, f), "wb").close()
check(len(file_locks.dataset_files(gdb)) == 4, "every file of a .gdb folder")
locks = file_locks.lock_files(os.path.join(gdb, "parcels"))
kinds = sorted((lk["kind"], lk["who"]) for lk in locks)
check(len(locks) == 2 and any("OFFICE-7" in w and "99" in w for _k, w in kinds) and
      {k for k, _w in kinds} == {"reading", "editing"}, f"FileGDB lock files read {kinds}")

# Excel owner file: name in ANSI at 1.., Unicode copy length at 54, UTF-16 from 56
xlsx = os.path.join(d1, "rates.xlsx")
open(xlsx, "wb").close()
for who in ("Hatem", "حاتم طارق"):
    raw = who.encode("cp1256")
    data = bytes([len(who)]) + raw.ljust(53, b" ") + bytes([len(who), 0]) + who.encode("utf-16-le")
    data = data.ljust(165, b" ")
    with open(os.path.join(d1, "~$rates.xlsx"), "wb") as f:
        f.write(data)
    lk = [x for x in file_locks.lock_files(xlsx) if x["program"] == "Excel"]
    check(lk and lk[0]["who"] == who, f"Excel owner file names the user {who!r} -> {lk[0]['who'] if lk else None!r}")
rep = file_locks.who_is_using(xlsx)
check(any("Excel" in a or "workbook" in a for a in rep["advice"]), "Excel owner file gives advice")

# a file held open by this process and by a child process
fh = open(shp.replace(".shp", ".dbf"), "rb")
procs = file_locks.processes_using(files)
check(procs is not None and any(p["is_self"] for p in procs), f"this process is found holding the file {procs}")
fh.close()
child = subprocess.Popen([sys.executable, "-c",
                          "import sys, time; f = open(sys.argv[1], 'rb'); print('ok', flush=True); time.sleep(60)",
                          shp], stdout=subprocess.PIPE, text=True)
try:
    child.stdout.readline()
    procs = file_locks.processes_using(files)
    mine = [p for p in procs or [] if p["pid"] == child.pid]
    check(mine and not mine[0]["is_self"], f"another program holding the file is found {mine}")
    if mine:
        check(file_locks.program_key(mine[0]["name"]) == "qgis" and "window" in file_locks.advice_for(mine[0]),
              f"advice for a Python/QGIS process ({mine[0]['name']})")
finally:
    child.kill()
    child.wait()
time.sleep(0.3)
procs = file_locks.processes_using(files)
check(procs == [], f"nobody holds the file once it is closed {procs}")
check(any("No program" in a for a in file_locks.who_is_using(shp)["advice"]), "advice when nobody holds it")
check(file_locks.program_key("EXCEL") == "excel" and file_locks.program_key("ArcGISPro") == "arcgis"
      and file_locks.program_key("OneDrive") == "sync" and file_locks.program_key("notepad") == "",
      "program names are recognised")

con = sqlite3.connect(gp, timeout=0)
con.execute("BEGIN IMMEDIATE")
check(file_locks.sqlite_busy(gp), "a GeoPackage being written by another connection is busy")
con.rollback()
con.close()
check(not file_locks.sqlite_busy(gp), "and free afterwards")
dlg = file_locks.LocksDialog(FakePlugin(FakeIface()), xlsx)
check(dlg.tree.topLevelItemCount() >= 1 and dlg.note.text(), "the dialog lists the Excel owner")

# =====================================================================================================
# 2. Project doctor
# =====================================================================================================
root, qgz, gpkg = create_folder_structure(TMP, "Alpha")
data_shp = write_layer(os.path.join(root, "data", "parcels.shp"), "Polygon", [sq(0, 0), sq(200, 0)], "ESRI Shapefile")
prj = QgsProject.instance()
prj.clear()
prj.setFileName(qgz)
lp = QgsVectorLayer(data_shp, "parcels", "ogr")
lp.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({"color": "12,34,56"})))
prj.addMapLayer(lp)
mem = QgsVectorLayer("Point?crs=EPSG:32637&field=n:int", "wells", "memory")
f = QgsFeature(mem.fields())
f.setGeometry(QgsGeometry.fromWkt("POINT(5 5)"))
mem.dataProvider().addFeatures([f])
prj.addMapLayer(mem)
copy_vector_into_gpkg(mem, gpkg)  # the layer now lives in Alpha.gpkg
nocrs = QgsVectorLayer("Point?field=n:int", "no crs", "memory")
nocrs.setCrs(QgsCoordinateReferenceSystem())
prj.addMapLayer(nocrs)
prj.setTitle("Alpha")
prj.setFilePathStorage(__import__("qgis.core", fromlist=["Qgis"]).Qgis.FilePathType.Relative)
check(prj.write(qgz), "project written")
hv = history.make_version(root, qgz, gpkg, note="first")

rep = doc.health_report(prj, root)
check(rep["relative"] and "no crs" in rep["no_crs"] and "no crs" in rep["temporary"] and not rep["broken"],
      f"health report {rep}")
doc.set_relative_paths(False, prj)
check(not doc.health_report(prj)["relative"], "paths can be switched to absolute")
doc.set_relative_paths(True, prj)
prj.write(qgz)

# move the data away, reopen: the layer is broken; the doctor finds it in the new folder
prj.clear()
moved = os.path.join(TMP, "elsewhere", "survey", "data")
os.makedirs(moved)
for fn in os.listdir(os.path.join(root, "data")):
    shutil.move(os.path.join(root, "data", fn), os.path.join(moved, fn))
decoy = os.path.join(TMP, "old", "parcels.shp")  # a different file with the same name
os.makedirs(os.path.dirname(decoy))
write_layer(decoy, "Polygon", [sq(0, 0, 5)], "ESRI Shapefile")
prj.clear()
prj.read(qgz)
broken = doc.broken_layers(prj)
check([b["name"] for b in broken] == ["parcels"], f"one broken layer {[b['name'] for b in broken]}")
fixes = doc.suggest_fixes(broken, [TMP])
check(fixes and os.path.normcase(fixes[0]["best"]) == os.path.normcase(os.path.join(moved, "parcels.shp")) and
      fixes[0]["alternatives"], f"best match shares the most path parts {fixes[0]['best'] if fixes else None}")
fixed, failed = doc.fix_all(fixes)
lyr = prj.mapLayersByName("parcels")[0]
check(fixed == ["parcels"] and not failed and lyr.isValid() and lyr.featureCount() == 2, "the layer is fixed")
check(lyr.renderer().symbol().color().name() == "#0c2238", "its style is kept")
check(not doc.broken_layers(prj), "nothing broken any more")
prj.write(qgz)

# where is this data used?
other_dir = os.path.join(TMP, "projects", "Other")
os.makedirs(other_dir)
prj2 = QgsProject()
prj2.setFilePathStorage(__import__("qgis.core", fromlist=["Qgis"]).Qgis.FilePathType.Absolute)
prj2.addMapLayer(QgsVectorLayer(os.path.join(moved, "parcels.shp"), "my parcels", "ogr"))
prj2.addMapLayer(QgsVectorLayer(gp + "|layername=pts", "points", "ogr"))
prj2.write(os.path.join(other_dir, "Other.qgs"))
hits = doc.where_used(os.path.join(moved, "parcels.shp"), [TMP])
projs = sorted(os.path.basename(h["project"]) for h in hits)
check(projs == ["Alpha.qgz", "Other.qgs"], f"both projects use the shapefile {projs}")
check(len(doc.where_used(os.path.join(moved, "parcels.dbf"), [TMP])) == 2, "a sidecar finds the same users")
check([h["layer"] for h in doc.where_used(os.path.dirname(moved), [TMP])].count("my parcels") == 1,
      "a folder finds the layers inside it")
check([h["layer"] for h in doc.where_used(gp, [TMP])] == ["points"], "a GeoPackage file is found")
check(not doc.where_used(decoy, [TMP]), "a file with the same name elsewhere is not counted")
old_place = os.path.join(root, "data", "parcels.shp")
hist_hits = doc.where_used(old_place, [TMP], include_history=True)
check(not doc.where_used(old_place, [TMP]) and len(hist_hits) == 1 and ".history" in hist_hits[0]["project"],
      f"old versions in the history can be included (relative to the project folder) {hist_hits}")

# rename the project (closed)
prj.clear()
try:
    doc.rename_project_files(qgz, "a/b")
    check(False, "bad name refused")
except doc.RenameError:
    check(True, "bad name refused")
os.makedirs(os.path.join(TMP, "Taken"))
open(os.path.join(TMP, "Taken", "x.txt"), "w").close()
try:
    doc.rename_project_files(qgz, "Taken")
    check(False, "existing folder refused")
except doc.RenameError:
    check(os.path.exists(qgz) and os.path.isdir(root), "existing folder refused, nothing changed")
lockcon = sqlite3.connect(gpkg)
lockcon.execute("SELECT 1").fetchall()
lock_fh = open(gpkg, "rb")
try:
    doc.rename_project_files(qgz, "Beta")
    check(False, "a project still open somewhere is refused")
except doc.RenameError as e:
    check(os.path.exists(qgz), f"a project still open somewhere is refused ({e})")
lock_fh.close()
lockcon.close()
new_qgz = doc.rename_project_files(qgz, "Beta")
new_root = os.path.join(TMP, "Beta")
check(os.path.normcase(new_qgz) == os.path.normcase(os.path.join(new_root, "Beta.qgz")) and not os.path.exists(root)
      and os.path.exists(os.path.join(new_root, "Beta.gpkg")) and not os.path.exists(os.path.join(new_root, "Alpha.gpkg")),
      "folder, project file and database renamed")
prj.read(new_qgz)
names = {lyr.name(): lyr for lyr in prj.mapLayers().values()}
check(names["wells"].isValid() and names["wells"].featureCount() == 1 and "Beta.gpkg" in names["wells"].source(),
      f"the database layer opens from Beta.gpkg ({names['wells'].source()})")
check(names["parcels"].isValid(), "other data still found")
check(prj.title() == "Beta", f"title follows the name ({prj.title()})")
vdir = os.path.join(new_root, ".history", os.path.basename(hv))
check(os.path.exists(os.path.join(vdir, "Beta.qgz")) and os.path.exists(os.path.join(vdir, "Beta.gpkg")),
      "history versions renamed too")
prj.clear()
history.restore_files(vdir, new_qgz, os.path.join(new_root, "Beta.gpkg"))
prj.read(new_qgz)
w = prj.mapLayersByName("wells")
check(w and w[0].isValid(), "a version restored after renaming opens")
prj.clear()

# rename the open project through the interface
prj.read(new_qgz)
iface = FakeIface()
gamma = doc.rename_open_project(iface, "Gamma")
check(os.path.exists(gamma) and iface.opened == [gamma] and QgsProject.instance().fileName().endswith("Gamma.qgz"),
      "the open project is saved, closed, renamed and opened again")
lyr = QgsProject.instance().mapLayersByName("wells")[0]
lyr.startEditing()
lyr.addFeature(QgsFeature(lyr.fields()))
try:
    doc.rename_open_project(iface, "Delta")
    check(False, "renaming with unsaved edits is refused")
except doc.RenameError:
    check(True, "renaming with unsaved edits is refused")
lyr.rollBack()
dd = __import__("biruni.project_doctor_dialog", fromlist=["x"]).ProjectDoctorDialog(FakePlugin(FakeIface()), "broken")
check(dd.tabs.count() == 4 and dd.tabs.currentIndex() == 1 and "✔" in dd.ov_label.text(), "doctor window builds")
QgsProject.instance().clear()

# =====================================================================================================
# 3. Saving the project does not save layer edits
# =====================================================================================================
prj.clear()
eg_path = write_layer(os.path.join(TMP, "edit.gpkg"), "Point", ["POINT(0 0)"], "GPKG", "pts")
el = QgsVectorLayer(eg_path + "|layername=pts", "survey points", "ogr")
prj.addMapLayer(el)
el.startEditing()
nf = QgsFeature(el.fields())
nf.setGeometry(QgsGeometry.fromWkt("POINT(3 3)"))
el.addFeature(nf)
check(edit_guard.unsaved_edit_layers() == [el], "a layer with unsaved edits is found")
iface = FakeIface()
guard = edit_guard.install(FakePlugin(iface))
prj.write(os.path.join(TMP, "edits.qgz"))
items = iface.bar.items()
check(len(items) == 1 and "survey points" in edit_guard.message_text([el]), "saving the project warns about the edits")
saved, failed = edit_guard.save_all([el])
check(saved == ["survey points"] and not failed and not el.isModified() and el.isEditable(),
      "Save all edits commits and keeps editing on")
check(guard.check() is None, "no warning once the edits are saved")
edit_guard.uninstall()
el.rollBack()
prj.clear()

# =====================================================================================================
# 4. Steps to a script and a model
# =====================================================================================================
sdir = os.path.join(TMP, "steps")
os.makedirs(sdir)
src = write_layer(os.path.join(sdir, "parcels.gpkg"), "Polygon", [sq(0, 0), sq(500, 0), sq(0, 500)], "GPKG", "parcels")
src_uri = src + "|layername=parcels"
out1 = os.path.join(sdir, "buffered.gpkg")
out2 = os.path.join(sdir, "centres.gpkg")
from qgis.gui import QgsGui  # noqa: E402
from qgis.PyQt.QtCore import QDateTime  # noqa: E402

reg = QgsGui.historyProviderRegistry()
start = QDateTime.currentDateTime().addSecs(-1)
for alg_id, params in (("native:buffer", {"INPUT": src_uri, "DISTANCE": 10.0, "SEGMENTS": 5, "OUTPUT": out1}),
                       ("native:centroids", {"INPUT": out1, "ALL_PARTS": False, "OUTPUT": out2})):
    alg = QgsApplication.processingRegistry().algorithmById(alg_id)
    res = processing.run(alg_id, params, context=ctx, feedback=fb)
    reg.addEntry("processing", {"python_command": alg.asPythonCommand(params, ctx), "algorithm_id": alg_id,
                                "parameters": alg.asMap(params, ctx), "results": {k: v for k, v in res.items()}})
steps = session_script.history_entries(start.toPyDateTime())
check([s["alg"] for s in steps] == ["native:buffer", "native:centroids"], f"history read in order {[s['alg'] for s in steps]}")
check(steps and steps[0]["inputs"].get("DISTANCE") == 10.0, f"parameters read {steps[0]['inputs'] if steps else None}")
script = session_script.make_script(steps)
check("PARCELS = " in script and "step1['OUTPUT']" in script and "processing.runAndLoadResults('native:centroids'" in script
      and "processing.run('native:buffer'" in script, "script links the second tool to the first")
src2 = write_layer(os.path.join(sdir, "parcels2.gpkg"), "Polygon", [sq(0, 0), sq(900, 900)], "GPKG", "parcels")


def swap(text, old, new):
    for o, n in ((old, new), (old.replace("\\", "/"), new.replace("\\", "/")),
                 (old.replace("\\", "\\\\"), new.replace("\\", "\\\\"))):
        text = text.replace(o, n)
    return text


script2 = swap(swap(swap(script, src, src2), out1, out1.replace("buffered", "buffered2")),
               out2, out2.replace("centres", "centres2"))
check(src2.replace("\\", "/") in script2 and "buffered2" in script2 and "centres2" in script2,
      "input and output paths are plain values in the script")
ns = {}
exec(compile(script2.replace("processing.runAndLoadResults", "processing.run"), "steps.py", "exec"), ns)
res_layer = ns["step2"]["OUTPUT"]
res_layer = res_layer if isinstance(res_layer, QgsVectorLayer) else QgsVectorLayer(res_layer, "r", "ogr")
check(res_layer.featureCount() == 2, f"the script runs on another input ({res_layer.featureCount()} centroids)")
model = session_script.make_model(steps, "My steps")
mpath = session_script.save_model(model, os.path.join(sdir, "my_steps"))
m2 = QgsProcessingModelAlgorithm()
check(m2.fromFile(mpath) and len(m2.childAlgorithms()) == 2, "model written and read back")
second = [c for c in m2.childAlgorithms().values() if c.algorithmId() == "native:centroids"][0]
srcs = second.parameterSources()["INPUT"]
from qgis.core import Qgis  # noqa: E402

check(srcs[0].source() == Qgis.ProcessingModelChildParameterSource.ChildOutput, "the model chains the tools")
params = {d.name(): d for d in m2.parameterDefinitions()}
inp = [n for n in params if n == "parcels"]
dest = [d.name() for d in m2.destinationParameterDefinitions()]
check(inp and len(dest) == 1, f"one input and one output in the model {list(params)} {dest}")
mres = processing.run(m2, {"parcels": src2 + "|layername=parcels", dest[0]: "TEMPORARY_OUTPUT"}, context=ctx,
                      feedback=fb)
outl = ctx.getMapLayer(mres[dest[0]]) if isinstance(mres[dest[0]], str) else mres[dest[0]]
check(outl is not None and outl.featureCount() == 2, "the model runs")
sd = session_script.SessionScriptDialog(FakePlugin(FakeIface()))
check(sd.list.count() >= 2, "steps window lists the recorded tools")

# =====================================================================================================
# 5. Placing an image by hand
# =====================================================================================================
P = quick_align.Placement
pl = P(200, 100, 2 * complex(math.cos(math.radians(30)), math.sin(math.radians(30))), complex(500000, 2700000))
gt = pl.geotransform()
for c, r in ((0, 0), (37.5, 81.25), (200, 100)):
    x, y = pl.to_map(c, r)
    check(abs(x - (gt[0] + c * gt[1] + r * gt[2])) < 1e-6 and abs(y - (gt[3] + c * gt[4] + r * gt[5])) < 1e-6,
          f"geotransform agrees at pixel {c},{r}")
check(abs(pl.rotation - 30) < 1e-9 and abs(pl.pixel_size - 2) < 1e-12, "rotation and pixel size read back")
cc, rr = pl.to_pixel(*pl.to_map(12.3, 45.6))
check(abs(cc - 12.3) < 1e-9 and abs(rr - 45.6) < 1e-9, "map to pixel is the inverse")
pairs = [((0, 0), pl.to_map(0, 0)), ((200, 100), pl.to_map(200, 100))]
fit = P.from_pairs(200, 100, pairs)
check(abs(fit.a - pl.a) < 1e-9 and abs(fit.b - pl.b) < 1e-6, "two point pairs give the placement exactly")
noisy = [((c, r), (pl.to_map(c, r)[0] + dx, pl.to_map(c, r)[1] + dy))
         for (c, r), (dx, dy) in (((0, 0), (0.3, -0.2)), ((200, 0), (-0.2, 0.1)), ((200, 100), (0.1, 0.3)),
                                  ((0, 100), (-0.2, -0.2)))]
fit = P.from_pairs(200, 100, noisy)
check(max(fit.residuals(noisy)) < 0.6 and abs(fit.rotation - 30) < 0.3, "four noisy pairs: least-squares fit")
q = pl.copy()
cx, cy = q.centre()
q.rotate(90, cx, cy)
check(abs(complex(*q.centre()) - complex(cx, cy)) < 1e-6 and abs(q.rotation - 120) < 1e-9, "turning keeps the centre")
q.scale(0.5, cx, cy)
check(abs(q.pixel_size - 1) < 1e-12 and abs(complex(*q.centre()) - complex(cx, cy)) < 1e-6, "resizing keeps the centre")
q.move(10, -5)
check(abs(complex(*q.centre()) - complex(cx + 10, cy - 5)) < 1e-6, "moving")
q.set_rotation(0)
q.set_pixel_size(3)
check(abs(q.rotation) < 1e-9 and abs(q.pixel_size - 3) < 1e-12, "typed rotation and pixel size")

# a picture with a red dot at pixel (49..51, 24..26)
from osgeo import gdal  # noqa: E402
import numpy as np  # noqa: E402

img = os.path.join(TMP, "scan.png")
arr = np.full((3, 100, 200), 255, dtype=np.uint8)
arr[1:, 24:27, 49:52] = 0
mem_ds = gdal.GetDriverByName("MEM").Create("", 200, 100, 3, gdal.GDT_Byte)
for b in range(3):
    mem_ds.GetRasterBand(b + 1).WriteArray(arr[b])
gdal.GetDriverByName("PNG").CreateCopy(img, mem_ds)
mem_ds = None
check(quick_align.prepare_image(img)[1:] == (200, 100), "image size read")
for label, placement in (("turned 30°", pl), ("north-up", P(200, 100, 2 + 0j, complex(500000, 2700000)))):
    out = os.path.join(TMP, f"placed_{label[:2]}.tif")
    quick_align.export_geotiff(img, placement, UTM.toWkt(), out, north_up=True, world_file=True)
    ds = gdal.Open(out)
    ogt = ds.GetGeoTransform()
    red = ds.GetRasterBand(1).ReadAsArray().astype(int)
    green = ds.GetRasterBand(2).ReadAsArray().astype(int)
    ys, xs = np.where((red > 200) & (green < 100))
    if len(xs):
        mc, mr = xs.mean() + 0.5, ys.mean() + 0.5
        mx, my = ogt[0] + mc * ogt[1] + mr * ogt[2], ogt[3] + mc * ogt[4] + mr * ogt[5]
        ex, ey = placement.to_map(50.5, 25.5)
        err = math.hypot(mx - ex, my - ey)
    else:
        err = 1e9
    check(err < 3 and abs(ogt[2]) < 1e-9 and os.path.exists(out[:-4] + ".tfw") and
          QgsCoordinateReferenceSystem(ds.GetProjection()).authid() == "EPSG:32637",
          f"{label}: the dot lands where it should ({err:.2f} m), north-up GeoTIFF with world file")
    ds = None

# the live tool on a map canvas
canvas = QgsMapCanvas()
canvas.resize(800, 600)
canvas.setDestinationCrs(UTM)
canvas.setExtent(QgsRectangle(500000, 2700000, 501600, 2701200))
canvas.refresh()
iface = FakeIface(canvas)
al = quick_align.QuickAligner(iface, img)
check(al.layer is not None and al.layer.isValid() and QgsProject.instance().mapLayer(al.layer.id()),
      "the see-through image is on the map")
panel = quick_align.AlignPanel(al)
before = al.placement.copy()
m2p = canvas.getCoordinateTransform()


def px(x, y):
    return QPoint(int(round(x)), int(round(y)))


def mouse(kind, x, y, mods=Qt.KeyboardModifier.NoModifier):
    return QgsMapMouseEvent(canvas, kind, px(x, y), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods)


def at(x, y):
    p = m2p.toMapCoordinates(px(x, y))
    return complex(p.x(), p.y())


al.tool.canvasPressEvent(mouse(QEvent.Type.MouseButtonPress, 300, 300))
al.tool.canvasMoveEvent(mouse(QEvent.Type.MouseMove, 340, 280))
al.tool.canvasReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 400, 250))
shift = complex(*al.placement.centre()) - complex(*before.centre())
check(abs(shift - (at(400, 250) - at(300, 300))) < 1e-6 and abs(al.placement.a - before.a) < 1e-12,
      "dragging moves the image by the mouse distance")
cx, cy = al.placement.centre()
c0 = complex(cx, cy)
cpx = m2p.transform(cx, cy)
a, b = at(cpx.x() + 100, cpx.y()), at(cpx.x(), cpx.y() - 100)
expected_turn = math.degrees(math.atan2((b - c0).imag, (b - c0).real) - math.atan2((a - c0).imag, (a - c0).real))
al.tool.canvasPressEvent(mouse(QEvent.Type.MouseButtonPress, cpx.x() + 100, cpx.y(), Qt.KeyboardModifier.ShiftModifier))
al.tool.canvasReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, cpx.x(), cpx.y() - 100,
                                 Qt.KeyboardModifier.ShiftModifier))
check(abs(al.placement.rotation - expected_turn) < 1e-6 and abs(expected_turn - 90) < 1 and
      abs(panel.rot.value() - al.placement.rotation) < 0.001,
      f"Shift+drag turns it ({al.placement.rotation:.2f}°) and the panel follows")
size0 = al.placement.pixel_size
a, b = at(cpx.x() + 100, cpx.y()), at(cpx.x() + 200, cpx.y())
al.tool.canvasPressEvent(mouse(QEvent.Type.MouseButtonPress, cpx.x() + 100, cpx.y(), Qt.KeyboardModifier.ControlModifier))
al.tool.canvasReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, cpx.x() + 200, cpx.y(),
                                 Qt.KeyboardModifier.ControlModifier))
check(abs(al.placement.pixel_size / size0 - abs(b - c0) / abs(a - c0)) < 1e-9 and
      abs(al.placement.pixel_size / size0 - 2) < 0.05, "Ctrl+drag resizes it")
# two points: the image's pixel (0,0) and (200,100) to two chosen map points
targets = [(500100.0, 2701100.0), (500700.0, 2700800.0)]
al.panel_set_two_point(True)
for (c, r), (tx, ty) in zip(((0, 0), (200, 100)), targets):
    sx, sy = al.placement.to_map(c, r)
    sp, tp = m2p.transform(sx, sy), m2p.transform(tx, ty)
    al.tool.canvasPressEvent(mouse(QEvent.Type.MouseButtonPress, sp.x(), sp.y()))
    al.tool.canvasPressEvent(mouse(QEvent.Type.MouseButtonPress, tp.x(), tp.y()))
ok = all(math.hypot(al.placement.to_map(c, r)[0] - tx, al.placement.to_map(c, r)[1] - ty) < 3 * canvas.mapUnitsPerPixel()
         for (c, r), (tx, ty) in zip(((0, 0), (200, 100)), targets))
check(ok and "2" in panel.pairs_label.text(), "two point pairs snap the image into place")
outp = os.path.join(TMP, "final.tif")
al.save(outp)
check(os.path.exists(outp) and QgsProject.instance().mapLayersByName("final") and al.layer is None,
      "saved as GeoTIFF, added to the map, the see-through copy removed")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
