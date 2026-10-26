r"""Tests for the timesheet built from the activity log.

Run:  engine\bin\python-qgis.bat tests\test_timesheet.py
"""
import datetime as dt
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

app = QgsApplication([], False)
app.initQgis()

from biruni import timesheet  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def ev(ts, cat, action, target="", count=1, tid=""):
    return {"ts": ts, "session": "s", "user": "u", "machine": "m", "category": cat, "action": action,
            "target": target, "target_id": tid, "count": count, "details": ""}


events = [ev("2026-10-20 09:00:00", "edit", "features_added", "Parcels", 40),
          ev("2026-10-20 10:00:00", "edit", "attributes_changed", "Parcels", 12),
          ev("2026-10-20 11:00:00", "tool", "run", "Buffer", tid="native:buffer"),
          ev("2026-10-20 11:30:00", "tool", "run", "Buffer", tid="native:buffer"),
          ev("2026-10-21 09:00:00", "edit", "geometries_changed", "Roads", 7),
          ev("2026-10-22 09:00:00", "gps", "track_recorded", "Track 1")]
active = [{"session": "s", "day": "2026-10-20", "minutes": 130}, {"session": "s", "day": "2026-10-21", "minutes": 47},
          {"session": "s", "day": "2026-10-22", "minutes": 0}]
rows = timesheet.daily_work(events, active)
check([r[0].isoformat() for r in rows] == ["2026-10-20", "2026-10-21"], "days with work (empty GPS-only day "
                                                                         "without minutes dropped)")
check(rows[0][1] == 130 and rows[0][2] == 130, "minutes kept without rounding")
check("Parcels: 40 added, 12 attribute edits" in rows[0][3] and "Buffer ×2" in rows[0][3], f"work described: {rows[0][3]}")
rows15 = timesheet.daily_work(events, active, round_to=15)
check([r[2] for r in rows15] == [135, 60], "rounded up to 15 minutes per day")
only = timesheet.daily_work(events, active, start=dt.date(2026, 10, 21))
check(len(only) == 1 and only[0][0] == dt.date(2026, 10, 21), "date range filter")

path = os.path.join(tempfile.mkdtemp(), "ts.xlsx")
timesheet.write_timesheet(path, "Demo", rows15, "ACME", "Hatem", 25.0, "USD", "Thanks")
import openpyxl  # noqa: E402

ws = openpyxl.load_workbook(path).active
cells = {c.value for row in ws.iter_rows() for c in row if c.value is not None}
check("ACME" in cells and "Demo" in cells and 25.0 in cells, "header filled")
formulas = [c.value for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
check(any("SUM(D" in f for f in formulas) and any("*$B$7" in f for f in formulas), f"live formulas {formulas[:3]}")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
