"""Standalone tests for project-folder helpers.

Run:  engine\\bin\\python-qgis.bat tests\\test_project.py
"""
import os
import sys
import tempfile

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
app = QgsApplication([], False)
app.initQgis()
from biruni import project as bp  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni-proj-")
root, qgz, gpkg = bp.create_folder_structure(tmp, "Demo")
check(all(os.path.isdir(os.path.join(root, d)) for d in bp.SUBFOLDERS), "sub-folders created")
check(os.path.exists(gpkg), "project database created")

outside = os.path.join(tmp, "outside.geojson")
with open(outside, "w", encoding="utf-8") as f:
    f.write('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"A"},'
            '"geometry":{"type":"Point","coordinates":[31.2,30.0]}}]}')
lyr = QgsVectorLayer(outside, "parcels", "ogr")
QgsProject.instance().addMapLayer(lyr)
check(bp._layer_file(lyr) is not None, f"layer file detected: {bp._layer_file(lyr)}")
check(not bp._inside(outside, root), "outside file is outside the folder")
table = bp.copy_vector_into_gpkg(lyr, gpkg)
check(lyr.isValid() and gpkg.replace("\\", "/") in lyr.source().replace("\\", "/"), f"repointed: {lyr.source()}")
check(lyr.featureCount() == 1, "features copied")

mem = QgsVectorLayer("Polygon?crs=EPSG:4326&field=n:integer", "scratch drawing", "memory")
f = QgsFeature(mem.fields())
f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 0))"))
mem.dataProvider().addFeatures([f])
t2 = bp.copy_vector_into_gpkg(mem, gpkg)
check(mem.providerType() == "ogr" and mem.featureCount() == 1, f"memory layer stored as table {t2}")
check(bp._unique_table(gpkg, "parcels") == "parcels_2", "unique table names")

app.exitQgis()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED"))
sys.exit(1 if failures else 0)
