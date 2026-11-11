r"""Review & Save: the unsaved changes of a layer listed one by one, and only the chosen ones saved - the others
kept as unsaved edits or dropped; nothing lost when a layer refuses the save.

Run:  engine\bin\python-qgis.bat tests\test_review_save.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsProject, QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.PyQt.QtCore import Qt  # noqa: E402

from biruni.editing import review_save as rs  # noqa: E402
from biruni.editing.review_save_dialog import ReviewDialog  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def make(name):
    path = os.path.join(tempfile.mkdtemp(), name + ".gpkg")
    mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", name, "memory")
    feats = []
    for i, owner in enumerate(("A", "B", "C")):
        f = QgsFeature(mem.fields())
        f.setAttributes([owner])
        x = i * 20
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 10} 0,{x + 10} 10,{x} 10,{x} 0))"))
        feats.append(f)
    mem.dataProvider().addFeatures(feats)
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = "GPKG"
    o.layerName = name
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), o)
    lyr = QgsVectorLayer(f"{path}|layername={name}", name, "ogr")
    lyr.setDisplayExpression('"owner"')
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def edit(lyr):
    """One of each: add D, reshape A, re-value B, delete C."""
    ids = {f["owner"]: f.id() for f in lyr.getFeatures()}
    lyr.startEditing()
    f = QgsFeature(lyr.fields())
    f.setAttributes([None, "D"])
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((100 0,110 0,110 10,100 10,100 0))"))
    lyr.addFeature(f)
    lyr.changeGeometry(ids["A"], QgsGeometry.fromWkt("POLYGON((0 0,15 0,15 10,0 10,0 0))"))
    lyr.changeAttributeValue(ids["B"], lyr.fields().indexOf("owner"), "B2")
    lyr.deleteFeature(ids["C"])
    return ids


def stored(lyr):
    """What is on disk: {owner: area}."""
    fresh = QgsVectorLayer(lyr.source(), "check", "ogr")
    return {f["owner"]: round(f.geometry().area(), 3) for f in fresh.getFeatures()}


lyr = make("parcels")
ids = edit(lyr)
ch = rs.changes(lyr)
check([c.kind for c in ch] == ["add", "geometry", "attributes", "delete"], f"the four changes are listed ({[c.kind for c in ch]})")
text = [rs.describe(c) for c in ch]
check("100.00 m²" in text[0] and "100.00" in text[1] and "150.00" in text[1] and "B → B2" in text[2]
      and "C" in text[3], f"each change says what changed, with areas and values: {text}")
check(rs.editing_layers() == [lyr], "the layers with unsaved edits are found")

keep = {c.key for c in ch if c.kind in ("add", "geometry")}
saved, left = rs.save(lyr, keep, "keep")
disk = stored(lyr)
check((saved, left) == (2, 2), f"two saved, two left ({saved}, {left})")
check(disk == {"A": 150.0, "B": 100.0, "C": 100.0, "D": 100.0},
      f"only the ticked changes are on disk: A reshaped, D added; B and C as they were ({disk})")
rest = rs.changes(lyr)
check(lyr.isEditable() and [c.kind for c in rest] == ["attributes", "delete"],
      f"the unticked changes are still unsaved edits ({[c.kind for c in rest]})")
saved, left = rs.save(lyr, {c.key for c in rest}, "keep")
check(stored(lyr) == {"A": 150.0, "B2": 100.0, "D": 100.0} and not lyr.isModified(),
      "saved later, they reach the disk too")

lyr2 = make("lots")
edit(lyr2)
ch = rs.changes(lyr2)
rs.save(lyr2, {ch[1].key}, "drop")
check(stored(lyr2) == {"A": 150.0, "B": 100.0, "C": 100.0} and not lyr2.isModified(),
      "'discard them' drops the unticked changes")

# a layer whose fields changed is saved whole or not at all
lyr3 = make("fields")
edit(lyr3)
from qgis.core import QgsField  # noqa: E402
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

lyr3.addAttribute(QgsField("note", QMetaType.Type.QString))
ch = rs.changes(lyr3)
try:
    rs.save(lyr3, {ch[0].key}, "keep")
    check(False, "a layer with new fields cannot be saved in part")
except rs.SaveRefused:
    check(lyr3.isModified() and len(rs.changes(lyr3)) == 4, "a layer with new fields cannot be saved in part (nothing lost)")
lyr3.rollBack()

# a refused save puts every change back
lyr4 = make("refuse")
edit(lyr4)
real = lyr4.commitChanges
lyr4.commitChanges = lambda *a: False          # e.g. the file is locked by another program
n_before = len(rs.changes(lyr4))
try:
    rs.save(lyr4, {rs.changes(lyr4)[0].key}, "keep")
    check(False, "a refused save is reported")
except rs.SaveRefused as e:
    check(len(rs.changes(lyr4)) == n_before, f"a refused save is reported and every change is still there ({e})")
lyr4.commitChanges = real
lyr4.rollBack()

# the window
lyr5 = make("window")
edit(lyr5)
dlg = ReviewDialog(None, [lyr5])
check(len(dlg.items) == 4 and len(dlg.ticked()) == 4, "the window lists the changes, all ticked")
top = dlg.tree.topLevelItem(0)
top.child(3).setCheckState(0, Qt.CheckState.Unchecked)       # keep the delete for later
dlg.tree.setCurrentItem(top.child(1))
check(not dlg.after_band.asGeometry().isEmpty() and not dlg.before_band.asGeometry().isEmpty(),
      "a reshaped feature shows both its shapes on the map")
check("3" in dlg.summary.text() and "4" in dlg.summary.text(), f"the count follows the ticks: {dlg.summary.text()}")
dlg.save()
check(dlg.saved == (3, 1) and "C" in stored(lyr5) and lyr5.isModified(), "Save writes the ticked ones only")
lyr5.rollBack()

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
