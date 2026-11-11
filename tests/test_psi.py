"""Private match by files: A and B find the parcels they both have, neither sees the other's list.

Parcel numbers written differently (أ / ا, ة / ه, Arabic-Indic digits, 12 and "12.0") still meet; the files hold no
number; B learns only how many A has (unless A sends file 3); a file altered, an answer to another match, a match
finished twice: refused. By place, a parcel drawn 0.3 m apart still meets.

Run:  engine\\bin\\python-qgis.bat tests\\test_psi.py
"""
import gzip
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

app = QgsApplication([], True)
app.initQgis()

from biruni.psi import core  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
A_DIR, B_DIR = os.path.join(root, "a"), os.path.join(root, "b")     # the two computers
AR = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def layer(name, rows, dx=0.0, crs="EPSG:32638"):
    lyr = QgsVectorLayer(f"Polygon?crs={crs}&field=plan:string&field=no:string&field=n:double", name, "memory")
    feats = []
    for plan, no, k in rows:
        f = QgsFeature(lyr.fields())
        x, y = (k % 40) * 30.0 + dx, (k // 40) * 30.0
        f.setAttributes([plan, no, float(k)])
        f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 20} {y},{x + 20} {y + 20},{x} {y + 20},{x} {y}))"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


# A: plan «أم الحمام» parcels 1..300; B: «ام الحمام» written its own way, parcels 201..500 (100 in common)
a_rows = [("مخطط أم الحمام", str(k), k) for k in range(1, 301)]
b_rows = [("مخطط ام الحمام" if k % 2 else "مخطط  أمّ الحمام", str(k).translate(AR) if k % 3 == 0 else str(k), k)
          for k in range(201, 501)]
la, lb = layer("Parcels A", a_rows), layer("Parcels B", b_rows)

check(core.value_key("مدرسة أبو بكر") == core.value_key("مدرسه ابو بكر "), "ة / ه and أ / ا are one key")
check(core.value_key("١٢") == core.value_key(12) == core.value_key(12.0) == core.value_key("12.0"),
      "١٢, 12, 12.0 and \"12.0\" are one key")
check(core.value_key("012") != core.value_key("12"), "\"012\" and \"12\" stay apart")
x = core.to_group("s", "k")
check(pow(x, (core.P - 1) // 2, core.P) == 1, "a key lands among the squares of the group")

f1, f2, f3 = (os.path.join(root, "m" + core.SUFFIX[i]) for i in (1, 2, 3))
t0 = time.time()
s = core.start(la, {"mode": "fields", "fields": ["plan", "no"]}, f1, folder=A_DIR)
check(s.mine == 300 and os.path.exists(f1), f"A starts: {s.mine} keys")
d = core.describe(f1)
check(d["count"] == 300 and d["key"]["fields"] == ["plan", "no"], "B sees what A asks for: the fields, how many keys")
r2 = core.answer(f1, lb, f2, ["plan", "no"], folder=B_DIR)
check(r2.theirs == 300 and r2.mine == 300, "B answers: it learns only that A has 300 keys")
r = core.finish(f2, folder=A_DIR, path3=f3)
took = time.time() - t0
want_a = sorted(f.id() for f in la.getFeatures() if 201 <= int(f["no"]) <= 300)
check(r.fids == want_a and r.common == 100, f"A finds exactly the 100 in common ({len(r.fids)})")
print(f"     {r.mine + r.theirs} keys each way in {took:.1f} s ({(600 * 2) / took:.0f} raisings a second)")
rb = core.finish_answer(f3, folder=B_DIR)
want_b = sorted(f.id() for f in lb.getFeatures() if f["n"] <= 300)
check(rb.fids == want_b, f"with file 3 B finds its own 100 ({len(rb.fids)})")


def text_of(p):
    with gzip.open(p, "rb") as fh:
        return fh.read().decode("utf-8")


raw = text_of(f1) + text_of(f2) + text_of(f3)
check("الحمام" not in raw and '"250"' not in raw and "Parcels" not in raw.replace("Parcels A", ""),
      "no parcel number or plan name in any of the files")
st = json.load(open(os.path.join(A_DIR, s.session + "-start.json"), encoding="utf-8"))
check("secret" not in st and st["common"] == want_a, "A's secret power is forgotten once finished, the answer kept")
stb = json.load(open(os.path.join(B_DIR, s.session + "-answer.json"), encoding="utf-8"))
check("secret" not in stb, "B never keeps a secret")
try:
    core.finish(f2, folder=A_DIR)
    check(False, "finished twice")
except core.PsiError as e:
    check(str(e) == "finished", "a match finished twice: refused")

# an altered file, an answer to another match, the wrong step
doc = json.loads(text_of(f2))
doc["items"][0] = doc["items"][1]
bad = os.path.join(root, "bad" + core.SUFFIX[2])
with gzip.open(bad, "wb") as fh:
    fh.write(json.dumps(doc).encode("utf-8"))
try:
    core.finish(bad, folder=A_DIR)
    check(False, "an altered file")
except core.PsiError as e:
    check(str(e) == "altered", "file 2 altered on the way: refused")
s2 = core.start(la, {"mode": "fields", "fields": ["plan", "no"]}, os.path.join(root, "n" + core.SUFFIX[1]),
                folder=A_DIR)
other = os.path.join(root, "o" + core.SUFFIX[2])
core.answer(os.path.join(root, "n" + core.SUFFIX[1]), lb, other, ["plan", "no"], folder=B_DIR)
od = json.loads(text_of(other))
od["session"] = s.session              # passed off as the answer to the first match
core.write_file({k: v for k, v in od.items() if k != "check"}, other)
st_a = json.load(open(os.path.join(A_DIR, s.session + "-start.json"), encoding="utf-8"))
st_a["secret"] = "1" * 64              # (as if the first were not finished yet)
json.dump(st_a, open(os.path.join(A_DIR, s.session + "-start.json"), "w", encoding="utf-8"))
try:
    core.finish(other, folder=A_DIR)
    check(False, "an answer to another file")
except core.PsiError as e:
    check(str(e) == "not ours", "an answer to another file 1: refused")
try:
    core.finish(f1, folder=A_DIR)
    check(False, "file 1 as file 2")
except core.PsiError as e:
    check(str(e) == "format", "file 1 given where file 2 is wanted: refused")
try:
    core.finish(f2, folder=os.path.join(root, "elsewhere"))
    check(False, "finished on another computer")
except core.PsiError as e:
    check(str(e) == "session", "finished on a computer that did not start it: refused")
try:
    core.answer(f1, lb, f2, ["plan"], folder=B_DIR)
    check(False, "one field for two")
except core.PsiError as e:
    check(str(e) == "fields", "one field for A's two: refused")

# only the selected features
la.selectByIds([f.id() for f in la.getFeatures() if int(f["no"]) <= 210])
s3 = core.start(la, {"mode": "fields", "fields": ["no"]}, os.path.join(root, "sel" + core.SUFFIX[1]),
                fids=la.selectedFeatureIds(), folder=A_DIR)
core.answer(os.path.join(root, "sel" + core.SUFFIX[1]), lb, os.path.join(root, "sel" + core.SUFFIX[2]), ["no"],
            folder=B_DIR)
r3 = core.finish(os.path.join(root, "sel" + core.SUFFIX[2]), folder=A_DIR)
check(s3.mine == 210 and len(r3.fids) == 10, f"only A's selected 210: the 10 in common ({len(r3.fids)})")

# by place: B's parcels drawn 0.3 m apart, in another coordinate system
lc = layer("Parcels C", [("", "", k) for k in range(250, 350)], dx=0.3)
lc_geo = QgsVectorLayer("Polygon?crs=EPSG:4326&field=plan:string", "Parcels C geo", "memory")
from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem  # noqa: E402
tr_ = QgsCoordinateTransform(lc.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
fs = []
for f in lc.getFeatures():
    g = QgsGeometry(f.geometry())
    g.transform(tr_)
    nf = QgsFeature(lc_geo.fields())
    nf.setGeometry(g)
    fs.append(nf)
lc_geo.dataProvider().addFeatures(fs)
la.removeSelection()
p1, p2 = os.path.join(root, "shape" + core.SUFFIX[1]), os.path.join(root, "shape" + core.SUFFIX[2])
spec = core.shape_spec(la, 1.0)
core.start(la, spec, p1, folder=A_DIR)
core.answer(p1, lc_geo, p2, folder=B_DIR)
r4 = core.finish(p2, folder=A_DIR)
want = sorted(f.id() for f in la.getFeatures() if 250 <= f["n"] <= 300)
check(r4.fids == want, f"by place, 0.3 m apart and in degrees on the other side: the 51 in common ({len(r4.fids)})")
lfar = layer("Parcels far", [("", "", k) for k in range(250, 350)], dx=2.5)
core.start(la, spec, p1, folder=A_DIR)
core.answer(p1, lfar, p2, folder=B_DIR)
check(core.finish(p2, folder=A_DIR).fids == [], "2.5 m apart (beyond the grid): no match")

# ---- the dialog -------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class Bar:
    def pushMessage(self, *a, **k):
        pass


class Iface:
    def __init__(self):
        self.w = QWidget()

    def mainWindow(self):
        return self.w

    def activeLayer(self):
        return la

    def messageBar(self):
        return Bar()


class Plugin:
    iface = Iface()


from biruni.psi import ui  # noqa: E402

dlg = ui.open_ui(Plugin())
check(dlg is not None and dlg.s_layer.currentText() == "Parcels A", "the dialog opens on the active layer")
check(dlg.start(os.path.join(root, "ui" + core.SUFFIX[1])) is None and "Tick" in dlg.out.text(), "no field ticked: said so")
dlg.set_start_fields(["no"])
rs = dlg.start(os.path.join(root, "ui" + core.SUFFIX[1]))
check(rs is not None and "300" in dlg.out.text(), "file 1 from the dialog: " + dlg.out.text()[:60])
dlg.open_file1(os.path.join(root, "ui" + core.SUFFIX[1]))
dlg.a_layer.setCurrentIndex(dlg.a_layer.findText("Parcels B"))
check(len(dlg.a_combos) == 1 and dlg.a_combos[0].currentText() == "no", "B is asked for one field, «no» guessed")
ra = dlg.answer(os.path.join(root, "ui" + core.SUFFIX[2]))
check(ra is not None and "300" in dlg.out.text(), "file 2 from the dialog")
dlg.f_tell.setChecked(True)
rf = dlg.finish(os.path.join(root, "ui" + core.SUFFIX[2]))
check(rf is not None and la.selectedFeatureCount() == 100 and os.path.exists(os.path.join(root, "ui" + core.SUFFIX[3])),
      "A finishes: its 100 selected, file 3 made")
rb2 = dlg.finish(os.path.join(root, "ui" + core.SUFFIX[3]))
check(rb2 is not None and lb.selectedFeatureCount() == 100, "B finishes from file 3: its 100 selected")
check(dlg.finish(os.path.join(root, "ui" + core.SUFFIX[2])) is None and "already" in dlg.out.text(),
      "finishing again: said so")

print()
print("FAILED: %d" % len(failures) if failures else "ALL PASSED")
app.exitQgis()
sys.exit(1 if failures else 0)
