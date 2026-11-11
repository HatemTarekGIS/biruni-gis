r"""Closing the program with a team room open must not crash (crash report of 2026-11-03: the project deleted its
layers, then the room let go of their signals). Each case runs in its own process, three times, so a crash shows
as a failed case rather than taking the test down.

Run:  engine\bin\python-qgis.bat tests\test_team_exit.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD = r'''
import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
sys.path.insert(0, {plugins!r})
from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,
                       QgsVectorFileWriter, QgsVectorLayer)
app = QgsApplication([], True)
app.initQgis()
from biruni.team import session

root = tempfile.mkdtemp()
gpkg = os.path.join(root, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string", "Parcels", "memory")
f = QgsFeature(mem.fields())
f.setAttributes(["A"])
f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))
mem.dataProvider().addFeatures([f])
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
first = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(first)
QgsProject.instance().write(os.path.join(root, "map.qgz"))
QgsProject.instance().clear()
QgsProject.instance().read(os.path.join(root, "map.qgz"))   # as in the program: the engine makes the layers
lyr = QgsProject.instance().mapLayersByName("Parcels")[0]
node = session.open_room([lyr], "Exit test", approve=False)
for _ in range(20):
    app.processEvents()
case = {case!r}
if case == "exit":            # as the program does on exit: the room is closed when the project is about to clear
    QgsProject.instance().aboutToBeCleared.connect(lambda: session.close_room(node))
    QgsProject.instance().clear()
elif case == "layer removed":  # the layer goes first, the room is closed later
    QgsProject.instance().removeMapLayer(lyr.id())
    for _ in range(20):
        app.processEvents()
    session.close_room(node)
elif case == "late close":     # the old order: closed after the project's layers were deleted
    QgsProject.instance().clear()
    for _ in range(20):
        app.processEvents()
    churn = [QgsVectorLayer("Point?field=a:string", "x", "memory") for _ in range(300)]   # reuse the freed memory
    del churn
    session.close_room(node)
for _ in range(20):
    app.processEvents()
print("closed cleanly", flush=True)
os._exit(0)
'''

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


plugins = os.path.join(HERE, "..", "app", "plugins")
for case in ("exit", "layer removed", "late close"):
    codes = []
    for _ in range(3):
        p = subprocess.run([sys.executable, "-c", CHILD.format(plugins=plugins, case=case)], capture_output=True,
                           text=True, timeout=180)
        codes.append(p.returncode if "closed cleanly" in p.stdout else p.returncode or -1)
    check(codes == [0, 0, 0], f"{case}: the room closes without a crash ({codes})")

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
