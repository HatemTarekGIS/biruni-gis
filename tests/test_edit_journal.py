r"""Unsaved edits that survive a crash or a power cut (edit_journal.py).

Run:  engine\bin\python-qgis.bat tests\test_edit_journal.py

The crash is real: a second Python process makes the edits, lets the journal write them and then dies with
os._exit (no commit, no clean shutdown); this process opens the project again and restores them.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsFeature, QgsField, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, Qt, QTime  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], False)
app.initQgis()
from biruni import edit_journal as ej  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def square(x, y, s=10):
    return QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x+s} {y},{x+s} {y+s},{x} {y+s},{x} {y}))")


def make_data(folder):
    """A project folder: parcels.gpkg (10 polygons), roads.shp (5 lines), demo.qgz."""
    os.makedirs(folder, exist_ok=True)
    mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=name:string(40)&field=area:double&field=day:date",
                         "parcels", "memory")
    feats = []
    for i in range(10):
        f = QgsFeature(mem.fields())
        f.setGeometry(square(i * 20, 0))
        f.setAttributes([f"P{i + 1}", 100.0 + i, QDate(2026, 1, i + 1)])
        feats.append(f)
    mem.dataProvider().addFeatures(feats)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "parcels"
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, os.path.join(folder, "parcels.gpkg"),
                                              QgsProject.instance().transformContext(), opts)
    lines = QgsVectorLayer("LineString?crs=EPSG:32638&field=road:string(30)&field=lanes:integer", "roads", "memory")
    feats = []
    for i in range(5):
        f = QgsFeature(lines.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"LINESTRING({i * 10} 50, {i * 10 + 8} 60)"))
        f.setAttributes([f"شارع {i + 1}", 2])
        feats.append(f)
    lines.dataProvider().addFeatures(feats)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    QgsVectorFileWriter.writeAsVectorFormatV3(lines, os.path.join(folder, "roads.shp"),
                                              QgsProject.instance().transformContext(), opts)
    prj = QgsProject.instance()
    prj.clear()
    prj.addMapLayer(QgsVectorLayer(os.path.join(folder, "parcels.gpkg") + "|layername=parcels", "parcels", "ogr"))
    prj.addMapLayer(QgsVectorLayer(os.path.join(folder, "roads.shp"), "roads", "ogr"))
    prj.write(os.path.join(folder, "demo.qgz"))
    prj.clear()
    return os.path.join(folder, "demo.qgz")


NEW_DT = QDateTime(QDate(2026, 10, 29), QTime(13, 45, 7), Qt.TimeSpec.UTC)


def writer(folder):
    """Process A: edit, let the journal write, crash."""
    prj = QgsProject.instance()
    qgz = os.path.join(folder, "demo.qgz")
    prj.read(qgz)
    journal = ej.EditJournal(quiet_ms=60_000).install()
    parcels = prj.mapLayersByName("parcels")[0]
    roads = prj.mapLayersByName("roads")[0]
    parcels.startEditing()
    roads.startEditing()
    # fields first, so the new features carry them
    parcels.addAttribute(QgsField("note", QMetaType.Type.QString, "", 50))
    parcels.addAttribute(QgsField("seen", QMetaType.Type.QDateTime))
    names = parcels.fields().names()
    new = []
    for k, (name, day) in enumerate((("NEW-A", QDate(2026, 10, 1)), ("NEW-B", None), ("جديد ج", QDate(2026, 10, 3)))):
        f = QgsFeature(parcels.fields())
        f.setGeometry(square(500 + k * 20, 100))
        vals = {"name": name, "area": 1.5 + k, "day": day, "note": "added", "seen": NEW_DT if k == 0 else None}
        for n, v in vals.items():
            f.setAttribute(names.index(n), v)
        new.append(f)
    parcels.addFeatures(new)
    parcels.changeGeometry(1, square(-100, -100, 30))
    parcels.changeAttributeValue(2, names.index("name"), "P2 renamed")
    parcels.changeAttributeValue(2, names.index("area"), 999.25)
    parcels.deleteFeature(3)
    parcels.changeAttributeValue(4, names.index("note"), "مراجعة")
    # an edit that is made and undone must not come back
    parcels.changeAttributeValue(5, names.index("name"), "temporary")
    parcels.undoStack().undo()
    rn = roads.fields().names()
    for k in range(2):
        f = QgsFeature(roads.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"LINESTRING({100 + k} 0, {110 + k} 5)"))
        f.setAttributes([f"طريق {k}", 4])
        roads.addFeatures([f])
    roads.deleteFeature(0)
    roads.changeAttributeValue(1, rn.index("lanes"), 6)
    journal.flush()
    print("JOURNAL", journal.file.path, flush=True)
    os._exit(0)      # a crash: no commit, no clean exit, nothing more is written


if len(sys.argv) > 2 and sys.argv[1] == "writer":
    writer(sys.argv[2])

# ---- values ------------------------------------------------------------------------------------------------
vals = [None, True, 7, -3.25, "نص", QDate(2026, 2, 3), NEW_DT, QTime(1, 2, 3, 400), b"\x00\x01ab",
        [1, "a", QDate(2020, 1, 1)], {"k": 1.5}, float("inf")]
back = [ej.decode(ej.encode(v)) for v in vals]
import json  # noqa: E402

json.dumps([ej.encode(v) for v in vals])
check(back[0] is None and back[1] is True and back[2] == 7 and back[3] == -3.25 and back[4] == "نص",
      "plain values survive")
check(back[5] == vals[5] and back[6] == NEW_DT and back[7] == vals[7] and back[8] == vals[8],
      "dates, date-times, times and bytes survive")
check(back[9][2] == QDate(2020, 1, 1) and back[10] == {"k": 1.5} and back[11] == float("inf"), "lists, maps, inf")
from qgis.core import NULL  # noqa: E402

check(ej.encode(NULL) is None, "QGIS NULL is stored as null")

# ---- crash in another process, restore here ---------------------------------------------------------------------
root = tempfile.mkdtemp()
folder = os.path.join(root, "Demo")
qgz = make_data(folder)
t0 = time.perf_counter()
proc = subprocess.run([sys.executable, os.path.abspath(__file__), "writer", folder], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=300)
check(proc.returncode == 0 and "JOURNAL" in proc.stdout,
      f"the editing process wrote its journal and died ({proc.returncode}) {proc.stderr[-300:]}")
recovery_dir = os.path.join(folder, ".biruni", "recovery")
files = [f for f in os.listdir(recovery_dir) if f.endswith(ej.SUFFIX)] if os.path.isdir(recovery_dir) else []
check(len(files) == 1, f"one journal left by the crash {files}")

prj = QgsProject.instance()
prj.read(qgz)
jpath = ej.check_journal(qgz)
check(jpath is not None, "the journal is found when the project is opened again")
entries = ej.read_journal(jpath)
n, added, changed, deleted = ej.summary(entries)
check((n, added, changed, deleted) == (2, 5, 4, 2), f"summary: 2 layers, 5 new, 4 changed, 2 deleted {(n, added, changed, deleted)}")
check("note" in ej.details_text(entries), "details list the added fields")
# a second copy of the folder, changed by "another program", for the conflict test below
folder2 = os.path.join(root, "Demo2")
shutil.copytree(folder, folder2)

report = ej.restore(jpath, prj)
parcels = prj.mapLayersByName("parcels")[0]
roads = prj.mapLayersByName("roads")[0]
check(parcels.isEditable() and parcels.isModified() and roads.isModified(), "edits are back, unsaved")
check(parcels.undoStack().count() == 1 and roads.undoStack().count() == 1, "one undo step per layer")
names = parcels.fields().names()
check("note" in names and "seen" in names, "added fields are back")
feats = {f["name"]: f for f in parcels.getFeatures()}
check(parcels.featureCount() == 12, f"10 + 3 new - 1 deleted = 12 ({parcels.featureCount()})")
check(3 not in [f.id() for f in parcels.getFeatures()], "the deleted parcel stays deleted")
check(parcels.getFeature(1).geometry().equals(square(-100, -100, 30)), "changed geometry is back")
check(parcels.getFeature(2)["name"] == "P2 renamed" and parcels.getFeature(2)["area"] == 999.25,
      "changed values are back")
check(parcels.getFeature(4)["note"] == "مراجعة", "a value in an added field is back")
check(parcels.getFeature(5)["name"] == "P5", "an undone edit does not come back")
check({"NEW-A", "NEW-B", "جديد ج"} <= set(feats), "new features are back with Arabic names")
check(feats["NEW-A"]["day"] == QDate(2026, 10, 1) and feats["NEW-A"]["seen"] == NEW_DT and
      feats["NEW-A"]["area"] == 1.5, "new features keep dates, date-times and numbers")
check(feats["NEW-B"]["day"] in (None, NULL) or not feats["NEW-B"]["day"], "a NULL date stays NULL")
check(feats["جديد ج"].geometry().equals(square(540, 100)), "new feature geometry is back")
rf = {f["road"]: f for f in roads.getFeatures()}
check(roads.featureCount() == 6 and "طريق 0" in rf and "طريق 1" in rf and 0 not in [f.id() for f in roads.getFeatures()]
      and roads.getFeature(1)["lanes"] == 6, f"shapefile edits are back ({roads.featureCount()})")
check(not report["skipped"] and not report["missing"], f"nothing skipped {report}")
check("2" in ej.report_text(report), "a readable report")
# the restored edits really save
check(parcels.commitChanges() and roads.commitChanges(), "the restored edits can be saved")
check(QgsVectorLayer(os.path.join(folder, "parcels.gpkg") + "|layername=parcels", "x", "ogr").featureCount() == 12,
      "and they are in the file")
ej.discard_left(qgz)
check(ej.check_journal(qgz) is None, "answered journals are gone")
prj.clear()

# ---- the data changed by another program since the edits ------------------------------------------------------
gpkg2 = os.path.join(folder2, "parcels.gpkg")
other = QgsVectorLayer(gpkg2 + "|layername=parcels", "other", "ogr")
other.dataProvider().deleteFeatures([2])       # a feature the journal changed is gone
del other
prj.read(os.path.join(folder2, "demo.qgz"))
jpath2 = ej.check_journal(os.path.join(folder2, "demo.qgz"))
check(jpath2 is not None, "the copied project finds its journal (layers matched by id: the paths differ)")
report2 = ej.restore(jpath2, prj)
text2 = ej.report_text(report2)
check(any("another program" in s for s in report2["skipped"]), f"the outside change is reported: {text2}")
check(any("no longer" in s for s in report2["skipped"]), "the edit of the vanished feature is skipped")
p2 = prj.mapLayersByName("parcels")[0]
check(p2.getFeature(1).geometry().equals(square(-100, -100, 30)), "the other edits still come back")
p2.rollBack()
prj.mapLayersByName("roads")[0].rollBack()
prj.clear()

# ---- damaged files are ignored ---------------------------------------------------------------------------------
bad = ej.journal_path(qgz, session="0123456789abXX")
os.makedirs(os.path.dirname(bad), exist_ok=True)
with open(bad, "wb") as f:
    f.write(b"this is not a database" * 50)
check(ej.read_journal(bad) == [] and ej.check_journal(qgz) is None, "a damaged journal is ignored")
os.remove(bad)
good = os.path.join(root, "copy.sqlite")
shutil.copy(jpath2, good)
size = os.path.getsize(good)
with open(good, "r+b") as f:
    f.truncate(size // 3)
check(isinstance(ej.read_journal(good), list), "a journal cut short by a power failure does not break anything")

# ---- this session's journal: written while editing, removed when the edits are saved or dropped -----------------
prj.read(qgz)
temp_dir = tempfile.mkdtemp()
journal = ej.EditJournal(temp_dir=temp_dir, quiet_ms=200).install()
lyr = prj.mapLayersByName("parcels")[0]
check(journal.flush() == 0.0 and not os.path.exists(journal.current_path()), "no editing, no file (no I/O when idle)")
lyr.startEditing()
lyr.changeAttributeValue(1, lyr.fields().indexOf("name"), "quiet")
deadline = time.time() + 3
while time.time() < deadline and not os.path.exists(journal.current_path()):
    app.processEvents()
    time.sleep(0.02)
check(os.path.exists(journal.current_path()), "written a moment after the last change (debounce)")
check(ej.check_journal(qgz) is None, "this session's own journal is never offered back to it")
lyr.commitChanges()
check(not os.path.exists(journal.current_path()), "saving the edits removes the journal")
lyr.startEditing()
lyr.deleteFeature(2)
journal.flush()
check(os.path.exists(journal.current_path()), "edits after the save are journaled again")
lyr.rollBack()
check(not os.path.exists(journal.current_path()), "discarding the edits removes the journal")
lyr.startEditing()
lyr.deleteFeature(2)
journal.flush()
lyr.rollBack()
prj.write()
check(not any(f.endswith(ej.SUFFIX) for f in os.listdir(recovery_dir)), "a saved project with nothing in edit mode has no journal")

# ---- big buffer: written in steps, the next small write stays fast --------------------------------------------
lyr.startEditing()
fields = lyr.fields()
big = []
for i in range(20000):
    f = QgsFeature(fields)
    f.setGeometry(square(i % 200 * 12, 1000 + i // 200 * 12, 10))
    f.setAttribute(fields.indexOf("name"), f"B{i}")
    f.setAttribute(fields.indexOf("area"), float(i))
    big.append(f)
lyr.addFeatures(big)
steps = []
t_all = time.perf_counter()
while any(st.fids or st.full for st in journal.states.values()):
    steps.append(journal.flush(limit=ej.CHUNK))
t = time.perf_counter() - t_all
lyr.changeAttributeValue(1, lyr.fields().indexOf("name"), "one more")
t_small = journal.flush()
print(f"     journal write: 20,000 new features {t * 1000:.0f} ms in {len(steps)} steps "
      f"(longest {max(steps) * 1000:.0f} ms); one more change {t_small * 1000:.1f} ms")
check(t < 5.0, f"20,000 new features written in {t:.2f} s")
check(max(steps) < 0.25, f"no single step blocks the window for long ({max(steps) * 1000:.0f} ms)")
check(t_small < 0.1, f"a single change after that is written in {t_small * 1000:.1f} ms")
check(ej.summary(ej.read_journal(journal.current_path()))[1] == 20000, "all 20,000 are in the journal")
lyr.rollBack()

# ---- closing the project the normal way removes it; uninstall leaves no file ------------------------------------
lyr.startEditing()
lyr.deleteFeature(2)
journal.flush()
path_before = journal.current_path()
lyr.rollBack()
journal.uninstall()
check(not os.path.exists(path_before), "a normal exit leaves no journal")
prj.clear()

# ---- autosave offers project copy + edits in one go --------------------------------------------------------------
from biruni import autosave  # noqa: E402

folder3 = os.path.join(root, "Demo3")
shutil.copytree(folder2, folder3)
# Demo2's journal was restored and rolled back but not discarded: rename it as an earlier session of Demo3
q3 = os.path.join(folder3, "demo.qgz")
check(ej.check_journal(q3) is not None, "another project folder carries its own journal")
saver = autosave.AutoSaver(None, temp_dir=temp_dir)
found = saver.offer_recovery(q3)
check(found is not None and found.endswith(ej.SUFFIX), "autosave's recovery offer includes the unsaved edits")
prj.read(q3)
rep = saver.restore_all(None, found, q3)
check(isinstance(rep, dict) and prj.mapLayersByName("parcels")[0].isModified(), "restore from the offer puts them back")
check(ej.check_journal(q3) is None, "and the journal is answered")
for l in prj.mapLayers().values():
    if l.isEditable():
        l.rollBack()
prj.clear()

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
app.exitQgis()
sys.exit(1 if failures else 0)
