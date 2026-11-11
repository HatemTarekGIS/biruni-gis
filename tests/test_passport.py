"""The quality passport: the rules of a delivery and how the data kept them, sealed in the certificate's code.

Run:  engine\\bin\\python-qgis.bat tests\\test_passport.py
"""
import copy
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

import json  # noqa: E402

from biruni.fingerprint import core, passport  # noqa: E402
from biruni.georules import core as gr  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


lyr = QgsVectorLayer("Polygon?crs=EPSG:32638&field=id:integer", "Parcels", "memory")
feats = []
for i in range(10):
    f = QgsFeature(lyr.fields())
    f.setAttributes([i])
    x = i * 20.0 if i != 9 else 175.0          # the last one overlaps its neighbour by 5 x 20 m
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} 0,{x + 20} 0,{x + 20} 20,{x} 20,{x} 0))"))
    feats.append(f)
lyr.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(lyr)
streets = QgsVectorLayer("LineString?crs=EPSG:32638", "Streets", "memory")
rules = [gr.rule("no_overlap", min_area=0.01), gr.rule("min_area", min=100), gr.rule("inside", layer="Blocks")]
lyr.setCustomProperty(gr.PROP, json.dumps(gr.document(lyr, rules)))

cert = core.certificate([("Parcels", core.build(lyr)), ("Streets", core.build(streets))])
plain = cert["code"]
cert = passport.attach(cert, [lyr, streets])
e = cert["layers"][0]
res = {r["kind"]: r["broken"] for r in e["passport"]["rules"]}
check(res == {"no_overlap": 1, "min_area": 0, "inside": None},
      f"the passport says what was found: {res} (a rule naming a layer not there: not checked)")
check(cert["layers"][1]["passport"] == {"rules": []}, "a layer without rules has an empty passport, so the code covers all")
check(cert["code"] != plain and passport.verify(cert) is None, "the code covers the passport, and checks")

bad = copy.deepcopy(cert)
bad["layers"][0]["passport"]["rules"][0]["broken"] = 0
check(passport.verify(bad) == "passport", "a passport made better afterwards: caught")
bad["layers"][0]["quality"] = passport.seal_of(bad["layers"][0]["passport"])
check(passport.verify(bad) == "code", "…even with its seal made again: the code is not the one sent")

check(core.check(lyr, e)["same"], "the certificate still checks the data")
lyr.startEditing()
last = [f for f in lyr.getFeatures()][-1]
lyr.changeGeometry(last.id(), QgsGeometry.fromWkt("POLYGON((200 0,220 0,220 20,200 20,200 0))"))
lyr.commitChanges()
again = {r["kind"]: n for r, n in passport.again(lyr, e)}
check(again["no_overlap"] == 0, f"checked again on the data now: the overlap is gone ({again})")
check(passport.attach(core.certificate([("Streets", core.build(streets))]), [streets]).get("layers")[0].get("passport")
      is None, "no layer with rules: no passport, the certificate as it was")

from biruni.fingerprint import ui  # noqa: E402

lines = ui.passport_lines(cert, {"Parcels": lyr})
check(any("broken by 1 of 10 at delivery" in l and "(now 0)" in l for l in lines)
      and any("not checked" in l for l in lines), "the passport in words, checked again on the data: " + lines[0][:70])

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
