"""The peer review lottery: exact sampling plans, exact bounds that cover, a draw anyone can re-draw, nobody
reviewing their own work, verdicts written and the results computed.

Run:  engine\\bin\\python-qgis.bat tests\\test_peer_review.py
"""
import os
import random
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsProject, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.peer_review import sampling, store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- the hypergeometric law --------------------------------------------------------------------------------------
N, D, n = 50, 7, 12
tot = sum(sampling.pmf(x, N, D, n) for x in range(0, n + 1))
check(abs(tot - 1) < 1e-12, f"the probabilities add up to one ({tot:.15f})")
mean = sum(x * sampling.pmf(x, N, D, n) for x in range(0, n + 1))
check(abs(mean - n * D / N) < 1e-9, f"the mean is n·D/N ({mean:.6f})")
check(abs(sampling.pmf(0, 10, 3, 2) - (7 * 6) / (10 * 9)) < 1e-12, "P(no wrong one in 2 of 10 with 3 wrong) = 42/90")

# ---- plans -------------------------------------------------------------------------------------------------------
p = sampling.plan(1000, 0.01, 0.05, 0.05, 0.10)
check(p["pa_aql"] >= 0.95 and p["pa_ltpd"] <= 0.10, f"plan for 1000: n={p['n']} c={p['c']} meets both risks "
      f"({p['pa_aql']:.3f}, {p['pa_ltpd']:.3f})")
# the binomial plan for AQL 1% / LTPD 5% / 5% / 10% is n=132, c=3 (the classic table value); the exact plan for a
# finite lot needs a few less
check(110 <= p["n"] <= 135 and p["c"] == 3, f"close to the classic n=132 c=3 (got n={p['n']} c={p['c']})")
m = p["n"] - 1
c = 0
while sampling.cdf(c, 1000, p["d1"], m) < 0.95:
    c += 1
check(sampling.cdf(c, 1000, p["d2"], m) > 0.10, f"{m} is not enough: the plan is the smallest")
ns = [sampling.plan(N_, 0.01, 0.05)["n"] for N_ in (100, 300, 1000, 5000, 20000)]
check(ns == sorted(ns), f"bigger lots need samples at least as big: {ns}")
check(ns[-1] - ns[-2] <= 3, "and the sample levels off for very big lots (it hardly depends on N)")
tight = sampling.plan(1000, 0.01, 0.03)["n"]
check(tight > p["n"], f"a tighter limit needs a bigger sample ({tight} > {p['n']})")
small = sampling.plan(8, 0.01, 0.05)
check(small["all"] and small["n"] == 8, "a lot of 8 is reviewed whole")
try:
    sampling.plan(100, 0.05, 0.05)
    check(False, "a limit equal to the acceptable rate is refused")
except sampling.PlanError:
    check(True, "a limit equal to the acceptable rate is refused")

# ---- bounds that cover ------------------------------------------------------------------------------------------
b = sampling.bounds(500, 80, 0)
check(b["lo"] == 0 and b["upper"] > 0 and b["upper_rate"] < 0.05, f"0 wrong of 80 from 500: at most {b['upper']} "
      f"({100 * b['upper_rate']:.1f}%)")
b2 = sampling.bounds(200, 200, 5)
check(b2["upper"] == 5 and b2["lo"] == 5 and b2["hi"] == 5, "a lot reviewed whole is known exactly")
rng = random.Random(7)
for N_, D_, n_ in ((400, 12, 60), (150, 30, 40), (1000, 3, 120)):
    lot = [1] * D_ + [0] * (N_ - D_)
    hits_up = hits_two = 0
    T = 1500
    for _ in range(T):
        x = sum(rng.sample(lot, n_))
        bb = sampling.bounds(N_, n_, x)
        hits_up += D_ <= bb["upper"]
        hits_two += bb["lo"] <= D_ <= bb["hi"]
    check(hits_up / T >= 0.95 and hits_two / T >= 0.95,
          f"N={N_} D={D_} n={n_}: the bounds cover the true count ({hits_up / T:.3f} one-sided, "
          f"{hits_two / T:.3f} two-sided, at least 0.95)")
st = sampling.stratified([(300, 50, 2), (200, 40, 0), (100, 100, 4)])
check(abs(st["est"] - (300 * 2 / 50 + 0 + 4)) < 1e-9 and st["lo"] <= st["est"] <= st["hi"] and st["upper"] >= 16,
      f"the whole delivery: estimate {st['est']:.1f}, interval {st['lo']}-{st['hi']}")
# the stratified (Bonferroni) bound also covers
lots = [(300, 12, 50), (200, 5, 40)]
T, hit = 800, 0
for _ in range(T):
    obs = []
    for N_, D_, n_ in lots:
        obs.append((N_, n_, sum(rng.sample([1] * D_ + [0] * (N_ - D_), n_))))
    hit += 17 <= sampling.stratified(obs)["upper"]
check(hit / T >= 0.95, f"the delivery's upper bound covers ({hit / T:.3f})")

# ---- the lottery --------------------------------------------------------------------------------------------------
lyr = QgsVectorLayer("Polygon?crs=EPSG:32638&field=pid:integer&field=use:string", "Parcels", "memory")
feats = []
for i in range(300):
    f = QgsFeature(lyr.fields())
    x, y = 500000 + (i % 20) * 30, 2700000 + (i // 20) * 30
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 25} {y},{x + 25} {y + 25},{x} {y + 25},{x} {y}))"))
    f.setAttributes([i, "res" if i % 3 else "com"])
    feats.append(f)
lyr.dataProvider().addFeatures(feats)
fids = sorted(f.id() for f in lyr.getFeatures())
people = [("a1", "Amal"), ("b2", "Badr"), ("c3", "Camelia")]
authors = {fid: people[k % 3] for k, fid in enumerate(fids[:270])}   # the last 30: before the room
reviewers = people + [("d4", "Dina")]
pend = store.commit(lyr, authors, reviewers, 0.01, 0.10, secret="00" * 16)
check(len(pend["lots"]) == 4, "four lots: three authors and the features made before the room")
check(pend["names"][store.lot_code("00" * 16, "a1")] == "Amal", "the names behind the codes are kept apart")
d1 = store.reveal(pend, "zaytoun 41 nakhl")
d2 = store.reveal(pend, "zaytoun 41 nakhl")
check(d1["sample"] == d2["sample"], "the same commitment and salt give the same sample")
d3 = store.reveal(pend, "zaytoun 42 nakhl")
check(d3["sample"] != d1["sample"], "another salt gives another sample")
codes_of = {store.lot_code("00" * 16, i): i for i, _n in reviewers}
own = [it for it in d1["sample"] if codes_of.get(it[3]) is not None and it[3] == it[2]]
check(not own, "nobody reviews their own features")
loads = {}
for it in d1["sample"]:
    loads[it[3]] = loads.get(it[3], 0) + 1
check(max(loads.values()) - min(loads.values()) <= 1 and "" not in loads,
      f"the loads are level and everything is given: {sorted(loads.values())}")
for code, fids_ in pend["lots"].items():
    got = sum(1 for it in d1["sample"] if it[2] == code)
    check(got == pend["plans"][code]["n"], f"lot {code}: {got} drawn = the plan's {pend['plans'][code]['n']}")
ok, probs, again = store.verify_draw(store.record_of(d1), lyr)
check(ok and not probs and again == d1["sample"], f"the kept record re-draws the same sample ({probs})")
forged = store.record_of(d1)
forged["body"] = dict(forged["body"], items=[it for it in forged["body"]["items"] if it[0] != fids[0]])
ok2, probs2, _s = store.verify_draw(forged)
check(not ok2 and "commitment" in probs2, "a record with a feature left out after the commitment is caught")
pend_b = store.commit(lyr, authors, reviewers, 0.01, 0.10, secret="00" * 16)
check(pend_b["commitment"] == pend["commitment"], "the commitment is a function of the data and the plan")
lyr.dataProvider().changeAttributeValues({fids[5]: {1: "ind"}})
pend_c = store.commit(lyr, authors, reviewers, 0.01, 0.10, secret="00" * 16)
check(pend_c["commitment"] != pend["commitment"], "one changed value changes the commitment")
_ok3, probs3, _s = store.verify_draw(store.record_of(d1), lyr)
check(any(p.startswith("changed:1") for p in probs3), f"verifying with the layer names the changed feature {probs3}")
lyr.dataProvider().changeAttributeValues({fids[5]: {1: "com" if 5 % 3 == 0 else "res"}})

solo = store.commit(lyr, {fid: ("a1", "Amal") for fid in fids}, [("a1", "Amal")], 0.01, 0.10)
s = store.reveal(solo, "x")
check(all(it[3] == "" for it in s["sample"]), "with only the author as reviewer nothing is given (no self review)")

# ---- who made each feature, from a team room's record --------------------------------------------------------------
from biruni import team  # noqa: E402
from biruni.team.store import Store  # noqa: E402

tst = Store(os.path.join(tempfile.mkdtemp(), "room.sqlite"))
seq = 0
for fid, who, kind in ((fids[0], "a1", "put"), (fids[0], "b2", "put"), (fids[1], "c3", "put"), (fids[1], "a1", "field"),
                       (fids[2], "b2", "put")):
    seq += 1
    tst.append({"uid": f"u{seq}", "epoch": 1, "author": who, "name": who.upper(), "ts": seq, "layer": "L1",
                "fid": fid, "kind": kind, "state": None, "prev": None, "ver": seq, "sig": ""})


class FakeNode:
    store = tst

    def key_of(self, layer):
        return "L1" if layer is lyr else None


team.set_current(FakeNode())
ra = store.room_authors(lyr)
check(ra == {fids[0]: ("b2", "B2"), fids[1]: ("c3", "C3"), fids[2]: ("b2", "B2")},
      "the room's record: the last one who changed each feature (a new field is not a change of the feature)")
other = QgsVectorLayer("Point?crs=EPSG:4326", "x", "memory")
check(store.room_authors(other) is None, "a layer not in the room has no record")
team.set_current(None)
tst.close()

# ---- the tables, the verdicts and the results ---------------------------------------------------------------------
gpkg = os.path.join(tempfile.mkdtemp(), "reviews.gpkg")
rev, drw = store.ensure_tables(gpkg, QgsCoordinateReferenceSystem("EPSG:32638"))
check(rev.isValid() and drw.isValid(), "the reviews and draws tables are made")
store.write_draw(rev, drw, d1, lyr)
rec = store.draws_in(drw)[0]
check(rec["id"] == d1["id"] and rec["seed"] == d1["seed"] and "secret" not in rec,
      "the draw's public record is kept without its secret")
rows = store.rows_of(rev, d1["id"])
check(len(rows) == len(d1["sample"]) and all(r["reviewer"] for r in rows), f"{len(rows)} review rows, each with "
      "its reviewer's name")
check(all(r["lot"].startswith("L-") and not any(n in r["lot"] for n in ("a1", "b2", "c3", "Amal", "Badr"))
          for r in rows),
      "the rows name lots by code, not their authors")
check(all(not g.isEmpty() for g in (f.geometry() for f in rev.getFeatures())), "each row has a point on its feature")
mine = store.rows_of(rev, d1["id"], reviewer="Dina")
check(mine and all(r["reviewer"] == "Dina" for r in mine), f"Dina's list: {len(mine)}")
lot_a = store.lot_code("00" * 16, "a1")
lot_b = store.lot_code("00" * 16, "b2")
plan_a = pend["plans"][lot_a]
for r in rows:
    if r["lot"] == lot_a:
        store.set_verdict(rev, r["row"], "ok")
errs = [r for r in rows if r["lot"] == lot_b][:pend["plans"][lot_b]["c"] + 1]
for r in errs:
    store.set_verdict(rev, r["row"], "error", "Shape", "a corner is missing")
try:
    store.set_verdict(rev, rows[0]["row"], "maybe")
    check(False, "an unknown verdict is refused")
except ValueError:
    check(True, "an unknown verdict is refused")
res = store.results(rev, rec, store.names_of(d1["id"]))
by = {r["lot"]: r for r in res["lots"]}
check(by[lot_a]["decision"] == "accept" and by[lot_a]["errors"] == 0 and by[lot_a]["done"] == plan_a["n"],
      f"Amal's lot: all {plan_a['n']} reviewed, none wrong, accepted")
ua = sampling.upper(by[lot_a]["N"], by[lot_a]["done"], 0, 0.10)
check(ua < plan_a["d2"], f"and at most {ua} of {by[lot_a]['N']} wrong with 90% confidence: below the limit "
      f"({plan_a['d2']}), as the plan promised; at 95%: {by[lot_a]['bounds']['upper']}")
check(by[lot_b]["decision"] == "reject" and by[lot_b]["categories"] == {"Shape": len(errs)},
      f"Badr's lot: {len(errs)} errors > {pend['plans'][lot_b]['c']} allowed: rejected before the end, by category")
check(by[lot_a]["name"] == "Amal", "names show on the computer that drew")
check(res["decision"] == "reject", "the delivery is rejected while a lot is")
check(res["total"]["x"] == len(errs) and res["total"]["N"] >= by[lot_a]["N"], "the delivery adds its lots up")
txt = store.csv_text(res)
check(txt.count("\n") == len(res["lots"]) + 2 and "reject" in txt, "the CSV report has a row per lot and the total")

# ---- the windows ------------------------------------------------------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni.peer_review import ui  # noqa: E402

said = []
ui.QMessageBox.information = lambda *a, **k: said.append(a[2] if len(a) > 2 else "")


class Bar:
    def pushMessage(self, *a, **k):
        said.append(a[1] if len(a) > 1 else "")


class Iface:
    def __init__(self):
        self.w = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.active = None

    def mainWindow(self):
        return self.w

    def mapCanvas(self):
        return self.canvas

    def activeLayer(self):
        return self.active

    def setActiveLayer(self, l):
        self.active = l

    def messageBar(self):
        return Bar()

    def addDockWidget(self, area, dock):
        self.w.addDockWidget(area, dock)


class Plugin:
    iface = Iface()


folder = tempfile.mkdtemp()
prj = QgsProject.instance()
prj.setFileName(os.path.join(folder, "Delivery.qgz"))
prj.write()
lyr.setName("Parcels")
prj.addMapLayer(lyr)
Plugin.iface.active = lyr
dlg = ui.draw_ui(Plugin())
check(dlg is not None and dlg.layer.currentText() == "Parcels" and not dlg.by_room.isEnabled(),
      "the draw window opens on the active layer; no room, so the room's record is off")
dlg.by_field.setChecked(True)
dlg.field.setCurrentIndex(dlg.field.findText("use"))
dlg.reviewers.setPlainText(chr(10).join(["res", "com", "Dina"]))
dlg.seal()
check(dlg.pending is not None and dlg.table.rowCount() == 2 and dlg.draw_btn.isEnabled() and
      dlg.pending["commitment"] in dlg.commitment.text(), "sealed: one row a person and the commitment shown")
dlg.salt.setText("tamr hindi")
dlg.draw()
rev, drw = ui.tables()
check(rev is not None and drw is not None and os.path.isfile(os.path.join(folder, "Delivery_team.gpkg")),
      "the tables are in the map's GeoPackage and on the map")
recs = store.draws_in(drw)
rows = store.rows_of(rev, recs[0]["id"])
names = store.names_of(recs[0]["id"])
lot_name = {r["lot"]: names.get(r["lot"]) for r in rows}
check(rows and all(lot_name[r["lot"]] != r["reviewer"] for r in rows), f"{len(rows)} drawn; nobody got their own lot")
dock = ui.review_ui(Plugin())
check(dock is not None and dock.me.count() == 3, "the review window lists the draw's reviewers")
dock.me.setCurrentIndex(dock.me.findText("Dina"))
n_dina = len(dock.rows)
check(n_dina > 0 and "Dina" not in dock.where.text() and "res" not in dock.where.text().split("»")[0],
      f"Dina has {n_dina} to review and the window does not say whose: {dock.where.text()[:60]}")
dock._verdict("ok")
dock.cat.setCurrentIndex(dock.cat.findData("Position"))
dock.note.setText("shifted 2 m")
dock._verdict("error")
mine = store.rows_of(rev, recs[0]["id"], reviewer="Dina")
check(sorted(r["verdict"] for r in mine if r["verdict"]) == ["error", "ok"] and
      any(r["category"] == "Position" and r["note"] == "shifted 2 m" for r in mine), "two verdicts kept, with the kind")
check(dock.i == 2 or n_dina <= 2, "the window moved on to the next one")
res_d = ui.results_ui(Plugin())
check(res_d is not None and res_d.table.rowCount() == 2 and "%" in res_d.summary.text(),
      "the results window: " + res_d.summary.text()[:90])
said.clear()
res_d.verify()
check(said and "fair" in said[-1], "checking the draw from the window says it is fair")
page = ui.report_html(res_d.res, res_d._record())
check(page.startswith("<!doctype html>") and recs[0]["seed"] in page and "Parcels" in page,
      "the report page holds the table, the commitment, the salt and the seed")
res_d.show_map()
check(rev.subsetString().startswith('"draw"') and isinstance(rev.renderer(), ui.QgsCategorizedSymbolRenderer),
      "shown on the map, coloured by verdict")

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nALL PASSED")
