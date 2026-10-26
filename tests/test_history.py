"""Tests for project history (versions, retention, restore keeping the activity log).

Run:  engine\\bin\\python-qgis.bat tests\\test_history.py
"""
import datetime as dt
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsVectorLayer  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from biruni import history  # noqa: E402
from biruni.activity import ActivityStore  # noqa: E402
from biruni.project import copy_vector_into_gpkg, create_folder_structure  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp()
root, qgz, gpkg = create_folder_structure(tmp, "Demo")
with open(qgz, "wb") as f:
    f.write(b"v1")
pts = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "points", "memory")
f = QgsFeature(pts.fields())
f.setGeometry(QgsGeometry.fromWkt("POINT(31 30)"))
f.setAttributes(["first"])
pts.dataProvider().addFeatures([f])
copy_vector_into_gpkg(pts, gpkg)


def count(table="points"):
    con = sqlite3.connect(gpkg)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


ActivityStore(gpkg).write([("2026-10-24 10:00:00", "s1", "u", "m", "edit", "features_added", "points", "", 1, "")],
                          None, {})
v1 = history.make_version(root, qgz, gpkg, note="first")
versions = history.list_versions(root)
check(len(versions) == 1 and versions[0]["note"] == "first" and "points" in versions[0]["tables"],
      "version stored with note and tables")
check(os.path.exists(os.path.join(v1, "Demo.gpkg")) and os.path.exists(os.path.join(v1, "Demo.qgz")),
      "version holds the project file and database")

# change the project after the version
lyr = QgsVectorLayer(f"{gpkg}|layername=points", "p", "ogr")
lyr.startEditing()
for k in range(3):
    g = QgsFeature(lyr.fields())
    g.setGeometry(QgsGeometry.fromWkt(f"POINT({32 + k} 30)"))
    lyr.addFeature(g)
lyr.commitChanges()
del lyr
with open(qgz, "wb") as f:
    f.write(b"v2")
ActivityStore(gpkg).write([("2026-10-24 11:00:00", "s2", "u", "m", "edit", "features_added", "points", "", 3, "")],
                          None, {})
check(count() == 4, "live database has 4 points before restoring")

before = history.make_version(root, qgz, gpkg, note="before", manual=True)
history.restore_files(v1, qgz, gpkg, keep_activity_from=os.path.join(before, "Demo.gpkg"))
check(count() == 1, "restored database is back to 1 point")
check(open(qgz, "rb").read() == b"v1", "restored project file")
events, _s, _a = ActivityStore(gpkg).read()
check(len(events) == 2 and events[-1]["count"] == 3, "activity log not rolled back by the restore")

# retention
now = dt.datetime(2026, 10, 24, 12, 0, 0)
fresh = tempfile.mkdtemp()
r2, q2, g2 = create_folder_structure(fresh, "Keep")
with open(q2, "wb") as f:
    f.write(b"x")
stamps = [now - dt.timedelta(hours=h) for h in (1, 2, 5)]                       # last 24 h: all kept
stamps += [now - dt.timedelta(days=3, hours=h) for h in (1, 2, 3)]               # same day: 1 kept
stamps += [now - dt.timedelta(days=10, hours=h) for h in (1, 4)]                 # same day: 1 kept
stamps += [now - dt.timedelta(days=60, hours=h) for h in (0, 24)]                # same ISO week: 1 kept
for s in stamps:
    history.make_version(r2, q2, g2, now=s)
history.make_version(r2, q2, g2, note="hand", manual=True, now=now - dt.timedelta(days=5, hours=1))
history.make_version(r2, q2, g2, now=now - dt.timedelta(days=5, hours=2))
removed = history.prune(r2, now)
left = history.list_versions(r2)
check(len(left) == 3 + 1 + 1 + 1 + 2, f"retention keeps 8 versions ({len(left)} left, {removed} removed)")
check(any(v.get("note") == "hand" for v in left), "versions saved by hand are kept")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
