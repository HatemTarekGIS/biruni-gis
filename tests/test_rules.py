r"""Tests for attribute rules.

Run:  engine\bin\python-qgis.bat tests\test_rules.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer,  # noqa: E402
                       QgsVectorLayerUtils)

app = QgsApplication([], False)
app.initQgis()

from biruni import rules  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


lyr = QgsVectorLayer("Polygon?crs=EPSG:32636&field=code:integer&field=use:string&field=floors:integer"
                     "&field=area:double&field=owner:string&field=created:datetime", "parcels", "memory")
QgsProject.instance().addMapLayer(lyr)
rules.apply_rules(lyr, {
    "code": rules.FieldRule(required=True, unique=True, auto="number"),
    "use": rules.FieldRule(required=True, values=["سكني", "تجاري", "Mixed"]),
    "floors": rules.FieldRule(minimum=1, maximum=30),
    "area": rules.FieldRule(auto="area"),
    "created": rules.FieldRule(auto="created"),
})
back = rules.read_rules(lyr)
check(back["code"].required and back["code"].unique and back["code"].auto == "number", "required + unique + numbering read back")
check(back["use"].values == ["سكني", "تجاري", "Mixed"], "allowed values read back (Arabic kept)")
check(back["floors"].minimum == 1 and back["floors"].maximum == 30, "range read back")
check(back["area"].auto == "area" and back["created"].auto == "created", "automatic values read back")
check(back["owner"].is_empty(), "untouched field has no rules")


def feature(code, use, floors):
    f = QgsVectorLayerUtils.createFeature(lyr, QgsGeometry.fromWkt(
        "POLYGON((0 0, 20 0, 20 30, 0 30, 0 0))"), {})
    if code is not None:
        f["code"] = code
    f["use"] = use
    f["floors"] = floors
    return f


def valid(f, name):
    return QgsVectorLayerUtils.validateAttribute(lyr, f, lyr.fields().indexFromName(name))[0]


lyr.startEditing()
f1 = feature(None, "سكني", 4)
check(f1["code"] == 1, "next number starts at 1")
check(abs(f1["area"] - 600) < 1e-6, "area filled automatically (600 m²)")
check(not f1["created"] is None, "creation date filled")
check(valid(f1, "use") and valid(f1, "floors"), "valid feature passes")
lyr.addFeature(f1)
lyr.commitChanges()
lyr.startEditing()
f2 = feature(None, "صناعي", 45)
check(f2["code"] == 2, "next number continues (2)")
check(not valid(f2, "use"), "value outside the list rejected")
check(not valid(f2, "floors"), "value above the range rejected")
f3 = feature(1, "تجاري", 2)
check(not valid(f3, "code"), "duplicate code rejected")
f4 = feature(5, None, None)
check(not valid(f4, "use") and valid(f4, "floors"), "required empty rejected, optional empty allowed")
lyr.rollBack()

# geometry change keeps the area current
lyr.startEditing()
fid = next(lyr.getFeatures()).id()
lyr.changeGeometry(fid, QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"))
lyr.commitChanges()
check(abs(lyr.getFeature(fid)["area"] - 100) < 1e-6, "area updated after reshaping (100 m²)")

# removing rules
rules.apply_rules(lyr, {"floors": rules.FieldRule(), "use": rules.FieldRule()})
back = rules.read_rules(lyr)
check(back["floors"].is_empty() and back["use"].is_empty(), "rules removed cleanly")
check(lyr.constraintExpression(lyr.fields().indexFromName("use")) == "", "constraint expression cleared")

lyr.startEditing()
lyr.changeAttributeValue(fid, lyr.fields().indexFromName("floors"), 99)
lyr.commitChanges()
rules.apply_rules(lyr, {"floors": rules.FieldRule(maximum=30)})
check(len(rules.problems(lyr)) == 1, "existing features that break a new rule are found")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
