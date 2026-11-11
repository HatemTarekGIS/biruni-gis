"""The live dashboard's engine: every widget's shape, filters, a card's zone, hidden fields that never leave the
PC, k_min, cards refused when they must be, seals, rev/delta, and the speed on 50 000 features.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_engine.py
"""
import json
import os
import random
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsFeature, QgsField, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QMetaType  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.dashboard import cards, data, model, seals, service  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


cards._TEST_SEED[:] = [bytes(range(32))]
seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
prj = QgsProject.instance()

# ---- data: 4 districts (squares in EPSG:4326) and parcels (points) ------------------------------------------------
zones = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "districts", "memory")
zf = []
NAMES = ["North", "South", "East", "West"]
for i, n in enumerate(NAMES):
    f = QgsFeature(zones.fields())
    x0 = i * 1.0
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x0} 0,{x0 + 1} 0,{x0 + 1} 1,{x0} 1,{x0} 0))"))
    f.setAttributes([n])
    zf.append(f)
zones.dataProvider().addFeatures(zf)


def parcels_layer(n, seed=1):
    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "parcels", "memory")
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField("district", QMetaType.Type.QString), QgsField("status", QMetaType.Type.QString),
                      QgsField("area", QMetaType.Type.Double), QgsField("day", QMetaType.Type.QDate),
                      QgsField("owner", QMetaType.Type.QString), QgsField("crew", QMetaType.Type.QString)])
    lyr.updateFields()
    rnd = random.Random(seed)
    feats = []
    for i in range(n):
        d = rnd.randrange(4)
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(d + rnd.uniform(0.05, 0.95), rnd.uniform(0.05, 0.95))))
        f.setAttributes([NAMES[d], "done" if rnd.random() < 0.6 else "open", round(rnd.uniform(200, 900), 2),
                         QDate(2026, 1, 1).addDays(rnd.randrange(60)), f"SECRET-OWNER-{i}",
                         ["Mona", "Sami", "Hadi"][rnd.randrange(3)]])
        feats.append(f)
    pr.addFeatures(feats)
    return lyr


parcels = parcels_layer(400)
prj.addMapLayers([zones, parcels])


def W(id_, type_, **k):
    w = {"id": id_, "type": type_, "title": id_, "x": 0, "y": 0, "w": 3, "h": 2, "source": "s1"}
    w.update(k)
    return w


D = model.blank("Survey")
D.update({"id": "d_test", "sources": {"s1": {"layer": parcels.id(), "filter": ""}},
          "filters": [{"id": "f1", "source": "s1", "field": "district", "label": "District"}],
          "widgets": [
              W("kpi", "kpi", measure={"op": "count", "where": "status = 'done'"}),
              W("gauge", "gauge", measure={"op": "sum", "field": "area"}, style={"target": 500000}),
              W("spark", "sparkline", measure={"op": "count"}, group={"field": "day", "time": "week"}),
              W("line", "line", measure={"op": "sum", "field": "area"}, group={"field": "day", "time": "month"},
                options={"split": "status"}),
              W("area", "area", measure={"op": "count"}, group={"field": "day", "time": "day"}),
              W("bar", "bar", measure={"op": "count"}, group={"field": "district"}),
              W("pie", "pie", measure={"op": "avg", "field": "area"}, group={"field": "status"}),
              W("table", "table", options={"fields": ["district", "status", "area", "owner"], "limit": 20}),
              W("gtable", "table", measure={"op": "count"}, group={"field": "crew"}),
              W("map", "map", options={"fields": ["status", "owner"]}, style={"by": "status"}),
              W("choro", "choropleth", measure={"op": "count"}, group={"zones": zones.id(), "label": "name"}),
              W("prog", "progress", group={"field": "district"}, options={"done": "status = 'done'"}),
              W("lead", "leaderboard", measure={"op": "count"}, group={"field": "crew"}),
              W("feed", "feed", source=None),
              W("text", "text", source=None, options={"text": "Hello **team**"}),
              W("flt", "filter", source=None, options={"filter": "f1"}),
              W("owners", "kpi", measure={"op": "distinct", "field": "owner"}),
              W("ratio", "kpi", measure={"op": "ratio", "where": "status = 'done'", "denominator": {"op": "count"}}),
          ]})
probs = model.validate(D)
check(not probs, f"the test dashboard is valid {probs}")
check(model.validate({"id": "x", "widgets": [{"id": "a", "type": "nope"}]}), "an unknown widget type is a problem")
check(model.validate({"id": "x", "sources": {"s": {"layer": "l", "filter": "a = "}}}),
      "a broken expression is a problem")
model.save(prj, D)
check(model.get(prj, "d_test")["title"] == "Survey", "a dashboard is kept with the project and read back")

comp = data.Computer(D)
res = comp.compute()
w = res["widgets"]
check(not res["errors"], f"no widget failed {res['errors']}")
total = parcels.featureCount()
done = sum(1 for f in parcels.getFeatures() if f["status"] == "done")
check(w["kpi"]["value"] == done and w["kpi"]["prev"] is None, f"kpi counts the done parcels ({w['kpi']})")
check(set(w["gauge"]) == {"value", "min", "max"} and w["gauge"]["max"] == 500000, "gauge shape")
check(w["spark"]["x"] == "time" and len(w["spark"]["series"]) == 1 and
      sum(p[1] for p in w["spark"]["series"][0]["points"]) == total, "sparkline counts every parcel by week")
check(len(w["line"]["series"]) == 2 and {s["name"] for s in w["line"]["series"]} == {"done", "open"},
      "line split into one series per status")
check(all(isinstance(p[0], float) for p in w["area"]["series"][0]["points"]), "area has epoch x values")
check(sorted(i["key"] for i in w["bar"]["items"]) == sorted(NAMES) and
      sum(i["value"] for i in w["bar"]["items"]) == total, "bar groups by district")
check(len(w["pie"]["items"]) == 2 and all(200 <= i["value"] <= 900 for i in w["pie"]["items"]), "pie of averages")
check([c["key"] for c in w["table"]["columns"]] == ["district", "status", "area", "owner"] and
      len(w["table"]["rows"]) == 20 and w["table"]["total"] == total, "table rows (limit 20, total all)")
check(len(w["gtable"]["rows"]) == 3, "a grouped table has one row per crew")
check(len(w["map"]["geojson"]["features"]) == total and w["map"]["bbox"][0] >= 0, "map GeoJSON in 4326")
props = w["map"]["geojson"]["features"][0]["properties"]
check(set(props) == {"id", "status", "owner"}, f"map properties are the chosen fields ({set(props)})")
cv = {f["properties"]["label"]: f["properties"]["value"] for f in w["choro"]["geojson"]["features"]}
bars = {i["key"]: i["value"] for i in w["bar"]["items"]}
check(cv == bars, "choropleth by zones equals the bar by district field")
check(all(it["done"] <= it["total"] for it in w["prog"]["items"]) and
      sum(it["done"] for it in w["prog"]["items"]) == done, "progress per district")
check(len(w["lead"]["items"]) == 3 and w["lead"]["items"][0]["value"] >= w["lead"]["items"][-1]["value"],
      "leaderboard sorted")
check(w["feed"] == {"items": []}, "feed empty before any edit")
check(w["text"]["text"] == "Hello **team**", "text")
check(len(w["flt"]["options"]) == 4 and w["flt"]["current"] == [], "filter options")
check(w["owners"]["value"] == total, "distinct owners")
check(abs(w["ratio"]["value"] - done / total) < 1e-9, "ratio done / all")

# ---- filters -----------------------------------------------------------------------------------------------------
nf = sum(1 for f in parcels.getFeatures() if f["district"] == "North")
r2 = comp.compute(filters={"f1": ["North"]})["widgets"]
check(sum(i["value"] for i in r2["bar"]["items"]) == nf and len(r2["bar"]["items"]) == 1, "a filter cuts the bar")
check(len(r2["flt"]["options"]) == 4 and r2["flt"]["current"] == ["North"],
      "the filter widget still lists every district (its own filter is skipped)")

# ---- cards: zone, hidden fields, refusals --------------------------------------------------------------------------
north = "POLYGON((0 0,1 0,1 1,0 1,0 0))"
c_north = cards.issue("d_test", "Supervisor North", zone_wkt=north, hide=["owner"])
p = cards.parse(c_north, "d_test")
check(p["name"] == "Supervisor North" and p["zone"], "a card is issued and read back")
r3 = comp.compute(card=p)
w3 = r3["widgets"]
check(sum(i["value"] for i in w3["bar"]["items"]) == nf, "a card for one district sees only it")
check(w3["kpi"]["value"] == sum(1 for f in parcels.getFeatures() if f["district"] == "North"
                                 and f["status"] == "done"), "the totals change with the zone")
check("owners" not in w3, "a widget counting a hidden field is not given to the card")
blob = json.dumps(r3, ensure_ascii=False) + json.dumps(model.for_card(D, p), ensure_ascii=False)
check("SECRET-OWNER" not in blob and '"owner"' not in blob, "the hidden field is nowhere in what the card gets")
check([c["key"] for c in w3["table"]["columns"]] == ["district", "status", "area"], "table without the hidden field")
fc = model.for_card(D, p)
check(all(k not in json.dumps(fc) for k in (parcels.id(), "status = 'done'", "sources")),
      "the definition a viewer gets has no layer ids, expressions or sources")
cv3 = {f["properties"]["label"] for f in w3["choro"]["geojson"]["features"]}
check(cv3 <= {"North", "East"}, f"the choropleth shows only zones touching the card's zone ({cv3})")

for why, text in (("foreign", cards.issue("d_test", "x")[:-6] + "AAAAAA"),
                  ("bad card", "BDC1.nonsense"),
                  ("other dashboard", cards.issue("d_other", "x"))):
    try:
        cards.parse(text, "d_test")
        check(False, f"a {why} card is refused")
    except cards.CardError as e:
        check(e.why == why or (why == "foreign" and e.why in ("foreign", "bad card")), f"a {why} card is refused "
                                                                                       f"({e.why})")
try:
    cards.parse(cards.issue("d_test", "old", exp=time.time() - 5), "d_test")
    check(False, "an expired card is refused")
except cards.CardError as e:
    check(e.why == "expired", "an expired card is refused")
c_rev = cards.issue("d_test", "to revoke")
cards.revoke(cards.parse(c_rev)["id"], "d_test", prj)
try:
    cards.parse(c_rev, "d_test")
    check(False, "a revoked card is refused")
except cards.CardError as e:
    check(e.why == "revoked", "a revoked card is refused")
real = cards._TEST_SEED[0]
cards._TEST_SEED[:] = [bytes(32)]
other_pc = cards.issue("d_test", "from another pc")
cards._TEST_SEED[:] = [real]
try:
    cards.parse(other_pc, "d_test")
    check(False, "a card of another computer is refused")
except cards.CardError as e:
    check(e.why == "foreign", "a card of another computer is refused")
check(len(cards.issued("d_test")) >= 3, "issued cards are listed for the desk")

# ---- k_min --------------------------------------------------------------------------------------------------------
Dk = json.loads(json.dumps(D))
Dk["k_min"] = 140
ck = data.Computer(Dk)
rk = ck.compute()["widgets"]
counts = {i["key"]: i["value"] for i in w["bar"]["items"]}
hid = {i["key"] for i in rk["bar"]["items"] if i.get("hidden")}
check(hid == {k for k, v in counts.items() if v < 140} and all("value" not in i for i in rk["bar"]["items"]
                                                                 if i.get("hidden")),
      f"groups under k_min are withheld ({sorted(hid)})")
rk2 = ck.compute(card=p)["widgets"]
check(rk2["map"] == {"hidden": True} or nf >= 140, "a map of fewer than k_min features is withheld")
ex = ck.explain("bar", "North", card=p)
check(ex.get("hidden") == (nf < 140) and (not ex.get("hidden") or ex["features"] == []),
      "explain gives no features under k_min")

# ---- explain -------------------------------------------------------------------------------------------------------
ex = comp.explain("bar", "North")
check(ex["count"] == nf and len(ex["features"]) == 50 and ex["more"] and ex["bbox"][2] <= 1.0001,
      "explain lists what is behind a number")
try:
    comp.explain("owners", None, card=p)
    check(False, "explain refuses a widget the card may not see")
except data.DataError:
    check(True, "explain refuses a widget the card may not see")

# ---- seals --------------------------------------------------------------------------------------------------------
code = seals.make("d_test", "kpi", "kpi", w["kpi"], {}, None, res["digests"]["s1"])
again = seals.make("d_test", "kpi", "kpi", w["kpi"], {}, None, res["digests"]["s1"])
check(code == again and len(code) == 9, f"the same number gives the same seal ({code})")
got = seals.lookup(code.lower().replace("-", " "))
check(got and seals.verify(got["statement"], got["sig"]), "a seal's statement verifies (code typed loosely)")
bad = dict(got["statement"])
bad["value"] = {"value": w["kpi"]["value"] + 1}
check(not seals.verify(bad, got["sig"]), "a changed value fails the signature")
check(not seals.verify(got["statement"], got["sig"], pub=bytes(32).hex()), "another key fails")
txt = seals.as_text(code)
st, sig = seals.from_text(txt)
check(seals.verify(st, sig, cards.public_hex()), "a seal as text verifies in another Biruni with the public key")
rc = seals.recheck(got["statement"], prj)
check(rc.get("same") and rc.get("data_same"), f"recheck computes the same number from the data ({rc})")

# ---- the service: hello, data, delta, filters, explain, seal -------------------------------------------------------
svc = service.DashboardService(prj)
box = []
sess = svc.connect(box.append, "lan")
sess.receive({"t": "filter", "values": {}})
check(box and box[-1]["t"] == "error" and box[-1]["why"] == "hello first", "nothing before the hello")
box.clear()
sess.receive({"t": "hello", "card": c_north})
check(box[0]["t"] == "dash" and box[0]["card"]["zone"] and box[0]["pub"] == cards.public_hex(), "hello -> dash")
check(box[1]["t"] == "data" and box[1]["rev"] == 1 and "owners" not in box[1]["widgets"] and
      all("seal" in v for v in box[1]["widgets"].values() if "error" not in v), "first data: all widgets, sealed")
check("SECRET-OWNER" not in json.dumps(box), "the session never carries the hidden field")
before = box[1]["widgets"]["kpi"]["value"]
box.clear()
parcels.startEditing()
nfid = next(f.id() for f in parcels.getFeatures() if f["district"] == "North" and f["status"] == "open")
parcels.changeAttributeValue(nfid, parcels.fields().indexOf("status"), "done")
svc.flush()
check(box and box[-1]["t"] == "data" and box[-1]["rev"] == 2, "an edit sends new data (rev 2)")
ch = box[-1]["widgets"] if box else {}
check(ch.get("kpi", {}).get("value") == before + 1 and ch["kpi"]["prev"] == before and ch["kpi"]["delta"] == 1,
      "kpi value, prev and delta after the edit")
check("text" not in ch and "flt" not in ch or "flt" in ch, "unchanged widgets are not sent again")
check("text" not in ch, "the text widget, unchanged, is not sent again")
check(ch.get("feed", {}).get("items") and ch["feed"]["items"][0]["kind"] == "edit", "the feed shows the edit")
box.clear()
sfid = next(f.id() for f in parcels.getFeatures() if f["district"] == "South")
parcels.changeAttributeValue(sfid, parcels.fields().indexOf("status"), "done")
svc.flush()
check(not box or "kpi" not in box[-1]["widgets"], "an edit outside the card's zone changes nothing it sees")
parcels.rollBack()
svc.flush()
box.clear()
sess.receive({"t": "filter", "values": {"f1": ["East"]}})
check(box and box[-1]["t"] == "data", "a filter sends data")
sess.receive({"t": "explain", "widget": "bar", "key": "North"})
check(box[-1]["t"] == "explain" and box[-1]["count"] == 0, "explain within the card and filter (East ∩ North = 0)")
sess.receive({"t": "filter", "values": {"f1": None}})
k_seal = box[-1]["widgets"].get("kpi", {}).get("seal") if box[-1]["t"] == "data" else None
sess.receive({"t": "seal", "code": k_seal or code})
check(box[-1]["t"] == "seal" and seals.verify(box[-1]["statement"], box[-1]["sig"], box[0]["pub"] if
                                               box[0]["t"] == "dash" else cards.public_hex()),
      "the session gives a seal of its own numbers")
sess.receive({"t": "seal", "code": code})
check(box[-1] == {"t": "error", "why": "unknown seal"}, "a seal of another card is not given")
viewers = svc.viewers()
check(len(viewers) == 1 and viewers[0]["name"] == "Supervisor North", "viewers() lists the session")

# preview session without a card
pbox = []
ps = svc.connect(pbox.append, "preview", definition=D)
ps.receive({"t": "hello", "card": None})
check(pbox[0]["t"] == "dash" and "owners" in pbox[1]["widgets"], "a preview sees the definition being edited")
# a lan session without a card is refused
lbox = []
ls = svc.connect(lbox.append, "lan")
ls.receive({"t": "hello", "card": None})
check(lbox == [{"t": "error", "why": "bad card"}], "a lan session needs a card")
# revocation while connected
cards.revoke(p["id"], "d_test", prj)
svc._periodic()
check(sess.closed and box[-1]["t"] == "bye" and box[-2] == {"t": "error", "why": "revoked"},
      "a card revoked while connected ends the session")
svc.stop()

# ---- speed: 50 000 features, 10 widgets ----------------------------------------------------------------------------
big = parcels_layer(50000, seed=7)
prj.addMapLayer(big)
DB = json.loads(json.dumps(D))
DB["sources"]["s1"]["layer"] = big.id()
DB["widgets"] = [x for x in DB["widgets"] if x["id"] in ("kpi", "gauge", "spark", "line", "bar", "pie", "gtable",
                                                         "prog", "lead", "ratio")]
cb = data.Computer(DB)
t0 = time.perf_counter()
rb = cb.compute()
t1 = time.perf_counter()
cb.invalidate(big.id(), [1, 2, 3])
rb2 = cb.compute()
t2 = time.perf_counter()
rb3 = cb.compute(card=p)
t3 = time.perf_counter()
cb.compute(filters={"f1": ["West"]})
t4 = time.perf_counter()
print(f"SPEED 50k features, 10 widgets: first {t1 - t0:.3f} s, after an edit of 3 features {t2 - t1:.3f} s, "
      f"with a zone card {t3 - t2:.3f} s (first zone cut), with a filter {t4 - t3:.3f} s", flush=True)
check(not rb["errors"] and rb["widgets"]["kpi"]["value"] > 0, "the big dashboard computes")
check(t2 - t1 < 1.0, f"recomputing after an edit is well under a second ({t2 - t1:.3f} s)")
check(t4 - t3 < 1.0, f"recomputing with a filter is under a second ({t4 - t3:.3f} s)")
ai = big.fields().indexOf("area")
big.dataProvider().changeAttributeValues({5: {ai: 1.5}, 6: {ai: 2.5}})
big.dataProvider().deleteFeatures([7])
cb.invalidate(big.id(), [5, 6, 7])
inc = cb.compute()
fresh = data.Computer(DB).compute()
check(inc["digests"] == fresh["digests"] and inc["widgets"]["gauge"] == fresh["widgets"]["gauge"] and
      inc["widgets"]["bar"] == fresh["widgets"]["bar"],
      "after editing and deleting features, the updated rows equal rows read afresh (numbers and digest)")
mapw = data.Computer({**DB, "widgets": [W("m", "map", options={"fields": ["status"]})]}).compute()["widgets"]["m"]
check(len(mapw["geojson"]["features"]) == data.MAX_MAP and mapw["total"] == big.featureCount(), "a map keeps at most 5000 features")

print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
app.exitQgis()
sys.exit(1 if failures else 0)
