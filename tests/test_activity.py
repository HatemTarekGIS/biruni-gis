"""Activity store + Excel report tests.

Run:  engine\\bin\\python-qgis.bat tests\\test_activity.py
"""
import os
import sys
import tempfile

from qgis.core import QgsApplication

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
app = QgsApplication([], False)
app.initQgis()
from openpyxl import load_workbook  # noqa: E402

from biruni import project as bp  # noqa: E402
from biruni.activity import ActivityStore  # noqa: E402
from biruni.activity_report import build_stats, write_report  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


tmp = tempfile.mkdtemp(prefix="biruni-act-")
root, qgz, gpkg = bp.create_folder_structure(tmp, "Survey")
store = ActivityStore(gpkg)


def ev(ts, session, user, cat, action, target="", count=1, tid=""):
    return (ts, session, user, "PC1", cat, action, target, tid, count, "")


day1 = [ev("2026-10-20 09:00:00", "s1", "hatem", "session", "start", count=0),
        ev("2026-10-20 09:05:00", "s1", "hatem", "layer", "added", "Parcels"),
        ev("2026-10-20 09:30:00", "s1", "hatem", "edit", "features_added", "Parcels", 12),
        ev("2026-10-20 10:00:00", "s1", "hatem", "edit", "geometries_changed", "Parcels", 5),
        ev("2026-10-20 10:10:00", "s1", "hatem", "tool", "run", "Buffer", tid="native:buffer"),
        ev("2026-10-20 10:20:00", "s1", "hatem", "edit", "features_deleted", "Roads", 2),
        ev("2026-10-20 11:00:00", "s1", "hatem", "project", "saved", "Survey.qgz")]
store.write(day1, ("s1", "hatem", "PC1", "2026-10-20 09:00:00", "2026-10-20 11:30:00", 95, "Biruni"),
            {("s1", "2026-10-20"): 95})
day2 = [ev("2026-10-21 13:00:00", "s2", "sara", "edit", "attributes_changed", "Parcels", 40),
        ev("2026-10-21 13:20:00", "s2", "sara", "tool", "run", "Buffer", tid="native:buffer"),
        ev("2026-10-21 13:25:00", "s2", "sara", "tool", "run", "Find overlaps and gaps", tid="biruni:overlapsgaps")]
store.write(day2, ("s2", "sara", "PC2", "2026-10-21 13:00:00", None, 30, "Biruni"), {("s2", "2026-10-21"): 30})
store.write([], ("s2", "sara", "PC2", "2026-10-21 13:00:00", None, 45, "Biruni"), {("s2", "2026-10-21"): 45})

events, sessions, active = store.read()
check(len(events) == 10, f"10 events stored, got {len(events)}")
check(len(sessions) == 2 and sessions[1]["active_minutes"] == 45, "session row upserted")
check(sum(a["minutes"] for a in active) == 140, "active minutes upserted per day (95 + 45)")

s = build_stats(events, sessions, active)
check(s["totals"] == {"features_added": 12, "geometries_changed": 5, "attributes_changed": 40,
                      "features_deleted": 2}, f"edit totals {s['totals']}")
check(s["layers"][0][0] == "Parcels", "most edited layer is Parcels")
check(s["tools"][0][:3] == ("Buffer", "native:buffer", 2), f"top tool {s['tools'][0]}")
check([u for u, _ in s["by_user"]] == ["hatem", "sara"], "users ordered by active time")

xlsx = os.path.join(root, "exports", "report.xlsx")
write_report(xlsx, "Survey", events, sessions, active)
wb = load_workbook(xlsx)
check(wb.sheetnames == ["Summary", "Daily", "Layers", "Tools", "Users", "Sessions", "Log"],
      f"sheets {wb.sheetnames}")
summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(min_row=4, max_row=16, max_col=2, values_only=True)}
check(summary.get("Total edits") == 59, f"summary total edits {summary.get('Total edits')}")
check(summary.get("Active working time (h:mm)") == "2:20", f"active time {summary.get('Active working time (h:mm)')}")
check(len(wb["Summary"]._charts) == 2, "two charts on the summary")
check(wb["Log"].max_row == 11, "log sheet has every event")

# the GeoPackage must still open as a GeoPackage after our tables were added
from qgis.core import QgsVectorLayer  # noqa: E402

lyr = QgsVectorLayer(gpkg + "|layername=biruni_project", "info", "ogr")
check(lyr.isValid(), "project database still valid")

app.exitQgis()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED"))
sys.exit(1 if failures else 0)
