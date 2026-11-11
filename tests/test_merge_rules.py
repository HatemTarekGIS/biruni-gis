r"""The room's signed merge constitution and escrowed numbers (team/rules.py, idea I06 first piece), off screen.

The rules one by one (keep = today, last, authority, max, min, sum, ask); a constitution or grant that is unsigned,
altered or signed by a non-admin is ignored; the real host decision path (Hub.decide_op) with three members
editing one parcel at once converges identically on two hosts (the same changes decided by the original host and
by a member who took over); numbering from granted ranges, offline on three computers, never repeats after the
merge; a number outside its author's range or used twice is flagged; with no constitution the results are today's.

Run:  engine\bin\python-qgis.bat tests\test_merge_rules.py
"""
import os
import sys
import tempfile
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.secure import crypto  # noqa: E402
from biruni.team import code as codes, features, merge, rules  # noqa: E402
from biruni.team.hub import Hub, sign_body  # noqa: E402
from biruni.team.store import Store  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ---- people -----------------------------------------------------------------------------------------------
def person(name, n, rank):
    seed = (f"{n:02x}" * 32)
    return {"id": f"{n:x}" * 32, "name": name, "seed": seed, "pub": crypto.public_key(bytes.fromhex(seed)).hex(),
            "rank": rank, "rights": {}, "token": None, "state": "in"}


admin, mona, sami, tarek = person("Admin", 1, 0), person("Mona", 2, 1), person("Sami", 3, 2), person("Tarek", 4, 3)
people = {p["id"]: p for p in (admin, mona, sami, tarek)}
lookup = people.get

# ---- the rules on their own -------------------------------------------------------------------------------
B = {"a": {"owner": "x", "area": 100, "count": 5, "note": "n"}}


def mf(mine, theirs, rule, field, mr=99, tr_=99):
    m = {"a": dict(B["a"], **mine)}
    t = {"a": dict(B["a"], **theirs)}
    return merge.merge_feature(B, m, t, rules={field: rule} if rule else None, mine_rank=mr, theirs_rank=tr_)


st, cf = mf({"owner": "M"}, {"owner": "T"}, None, "owner")
check(st["a"]["owner"] == "T" and cf and cf[0]["field"] == "owner", "no rule: the current value stays and the author is asked (today)")
st2, cf2 = mf({"owner": "M"}, {"owner": "T"}, "keep", "owner")
check(st2 == st and len(cf2) == len(cf), "keep is exactly today's behaviour")
st, cf = mf({"owner": "M"}, {"owner": "T"}, "last", "owner")
check(st["a"]["owner"] == "M" and not cf, "last: the change decided last wins, nobody asked")
st, cf = mf({"owner": "M"}, {"owner": "T"}, "authority", "owner", mr=1, tr_=2)
check(st["a"]["owner"] == "M" and not cf, "authority: the higher rank (lower number) wins - mine")
st, cf = mf({"owner": "M"}, {"owner": "T"}, "authority", "owner", mr=3, tr_=1)
check(st["a"]["owner"] == "T" and not cf, "authority: the higher rank wins - theirs")
st, cf = mf({"area": 120}, {"area": 90}, "max", "area")
check(st["a"]["area"] == 120 and not cf, "max: the larger number")
st, cf = mf({"area": 120}, {"area": 90}, "min", "area")
check(st["a"]["area"] == 90 and not cf, "min: the smaller number")
st, cf = mf({"count": 7}, {"count": 9}, "sum", "count")
check(st["a"]["count"] == 11 and not cf, "sum: both changes add up (5 +2 +4 = 11)")
st, cf = mf({"area": "big"}, {"area": 90}, "max", "area")
check(st["a"]["area"] == 90 and cf, "max on values that are not numbers: kept and asked")
st, cf = mf({"note": "M"}, {"owner": "T"}, "ask", "note")
check(st["a"]["note"] == "n" and cf and cf[0]["field"] == "note", "ask: a field changed while the feature changed is never joined by itself")
st, cf = mf({"note": "M"}, {"owner": "T"}, None, "note")
check(st["a"]["note"] == "M" and not cf, "... without ask the same change joins (today)")


# ---- signatures -------------------------------------------------------------------------------------------
class S:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def put(self, k, v):
        self.d[k] = v


s = S()
s.put(rules.CKEY, rules.make_constitution(admin["seed"], admin["id"], {"L1": {"owner": "last"}}, 1))
check(rules.layer_rules(s, lookup, "L1") == {"owner": "last"}, "a constitution signed by the admin is read")
bad = rules.make_constitution(admin["seed"], admin["id"], {"L1": {"owner": "last"}}, 1)
bad["doc"]["layers"]["L1"]["owner"] = "authority"
s.put(rules.CKEY, bad)
check(rules.layer_rules(s, lookup, "L1") == {}, "an altered constitution is ignored")
s.put(rules.CKEY, rules.make_constitution(mona["seed"], mona["id"], {"L1": {"owner": "last"}}, 1))
check(rules.layer_rules(s, lookup, "L1") == {}, "a constitution signed by a member who is not the admin is ignored")
forged = rules.make_constitution(mona["seed"], admin["id"], {"L1": {"owner": "last"}}, 1)
s.put(rules.CKEY, forged)
check(rules.layer_rules(s, lookup, "L1") == {}, "a constitution in the admin's name with another key is ignored")
s.put(rules.CKEY, {"doc": {"kind": "constitution", "by": admin["id"], "layers": {"L1": {"owner": "last"}}}})
check(rules.layer_rules(s, lookup, "L1") == {}, "an unsigned constitution is ignored")
g_ok = rules.make_grant(admin["seed"], admin["id"], "L1", "parcel_no", mona["id"], 1000, 1009)
g_bad = rules.make_grant(mona["seed"], mona["id"], "L1", "parcel_no", mona["id"], 1, 5000)
g_alt = rules.make_grant(admin["seed"], admin["id"], "L1", "parcel_no", sami["id"], 2000, 2009)
g_alt["doc"]["hi"] = 9999
s.put(rules.GKEY, [g_ok, g_bad, g_alt])
gs = rules.grants(s, lookup, layer="L1")
check(len(gs) == 1 and gs[0]["member"] == mona["id"], "only the admin's untouched grants count (self-granted and altered ones ignored)")

# ---- the real host decision path ----------------------------------------------------------------------------
root = tempfile.mkdtemp()


def make_layer(name):
    path = os.path.join(root, name + ".gpkg")
    mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string&field=area:double&field=count:integer"
                         "&field=parcel_no:integer", "Parcels", "memory")
    f = QgsFeature(mem.fields())
    f.setAttributes(["x", 100.0, 5, 1])
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))
    mem.dataProvider().addFeatures([f])
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName, o.layerName = "GPKG", "parcels"
    QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), o)
    lyr = QgsVectorLayer(path + "|layername=parcels", "Parcels", "ogr")
    assert lyr.isValid()
    return lyr


def make_hub(name, me, constitution=None, grants=None):
    st = Store(os.path.join(root, name + ".sqlite"))
    for p in (admin, mona, sami, tarek):
        if p["id"] != me["id"]:
            st.save_member(dict(p))
    if constitution is not None:
        st.put(rules.CKEY, constitution)
    if grants is not None:
        st.put(rules.GKEY, grants)
    lyr = make_layer(name)
    hub = Hub(st, codes.RoomKeys(codes.new_code()), {"L1": lyr}, dict(me, rights={"L1": "edit"}))
    return hub, lyr


def op_of(who, fid, base_state, state, base_ver=0):
    op = {"uid": uuid.uuid4().hex, "layer": "L1", "fid": fid, "kind": "put", "base_ver": base_ver,
          "base": base_state, "state": state, "author": who["id"], "name": who["name"], "ts": 1.0}
    op["sig"] = crypto.sign(bytes.fromhex(who["seed"]), sign_body(op)).hex()
    return op


def run(hub, lyr, seq):
    """seq: [(who, {field: value})] - all started from the same version of parcel 1 (edited at once)."""
    fid = next(lyr.getFeatures()).id()
    base = features.stored_state(lyr, fid)
    res = []
    for who, change in seq:
        st_ = {"g": base["g"], "a": dict(base["a"], **change)}
        res.append(hub.apply_remote(op_of(who, fid, base, st_, base_ver=0), hub._member(who["id"])))
    return features.stored_state(lyr, fid)["a"], res


EDITS = [(mona, {"owner": "Mona's", "area": 120.0, "count": 7}),
         (sami, {"owner": "Sami's", "area": 90.0, "count": 9}),
         (tarek, {"owner": "Tarek's", "area": 105.0, "count": 6})]
c1 = rules.make_constitution(admin["seed"], admin["id"],
                             {"L1": {"owner": "authority", "area": "max", "count": "sum"}}, 1)
h1, l1 = make_hub("host1", admin, constitution=c1)
a1, r1 = run(h1, l1, EDITS)
check(a1["owner"] == "Mona's" and a1["area"] == 120.0 and a1["count"] == 5 + 2 + 4 + 1,
      f"three members edit one parcel at once: authority, max and sum decide ({a1['owner']}, {a1['area']}, {a1['count']})")
check(all(not r.get("conflicts") for r in r1), "... and nobody is asked")
# the same changes decided by a member who took over as host (another computer, the same signed rules)
h2, l2 = make_hub("host2", mona, constitution=c1)
a2, _r2 = run(h2, l2, EDITS)
check(a2 == a1, "a member who takes over as host decides the same changes the same way (identical attributes)")
# default: no constitution = today's results
h3, l3 = make_hub("host3", admin)
a3, r3 = run(h3, l3, EDITS)
check(a3["owner"] == "Mona's" and a3["count"] == 7 and any(r.get("conflicts") for r in r3[1:]),
      "with no constitution the room behaves as today (first writer kept, the others asked)")
h4, l4 = make_hub("host4", admin, constitution=forged)
a4, _r4 = run(h4, l4, EDITS)
check(a4 == a3, "a forged constitution changes nothing")

# ---- escrowed numbers -------------------------------------------------------------------------------------
grants_ = [rules.make_grant(admin["seed"], admin["id"], "L1", "parcel_no", p["id"], lo, lo + 9)
           for p, lo in ((mona, 1000), (sami, 2000), (tarek, 3000))]
gs = S()
gs.put(rules.GKEY, grants_)
taken = []
for p in (mona, sami, tarek):          # three computers, offline, each numbers 10 new parcels from its own range
    docs = rules.grants(gs, lookup, layer="L1", field="parcel_no", member=p["id"])
    used = set()
    for _ in range(10):
        n = rules.next_number(docs, used)
        used.add(n)
        taken.append(n)
check(len(taken) == 30 and len(set(taken)) == 30, "30 parcels numbered offline on three computers: no number repeats")
check(rules.next_number(rules.grants(gs, lookup, member=mona["id"]), set(range(1000, 1010))) is None,
      "a used-up range gives no number (the member is warned)")
h5, l5 = make_hub("host5", admin, grants=grants_)


def add_op(who, fid, number):
    stt = {"g": features.stored_state(l5, next(l5.getFeatures()).id())["g"],
           "a": {"owner": who["name"], "area": 1.0, "count": 1, "parcel_no": number}}
    return h5.apply_remote(op_of(who, fid, None, stt), h5._member(who["id"]))


r_ok = add_op(mona, 50_000_001, 1000)
r_out = add_op(sami, 50_000_002, 1005)          # Sami's range is 2000-2009
r_dup = add_op(tarek, 50_000_003, 1000)          # in nobody's... and already used by Mona
check(r_ok.get("status") == "ok" and not r_ok.get("flags"), "a number from its author's range goes in unflagged")
check(any("outside your range" in f for f in r_out.get("flags", [])), "a number outside its author's range is flagged")
check(any("already used" in f for f in r_dup.get("flags", [])), "a number used twice is flagged")
notes = [e.get("note") or "" for e in h5.store.last(10)]
check(any("already used" in n for n in notes), "the flag is written in the stream's note")

# a member's own computer, offline: new parcels drawn there take numbers from its own range
from biruni.team.node import Node  # noqa: E402

mst = Store(os.path.join(root, "mona.sqlite"))
mst.put("roster", [{k: p[k] for k in ("id", "name", "pub", "rank", "state")} for p in (admin, mona, sami, tarek)])
mst.put(rules.GKEY, grants_)
mst.put("block", [9_000_000, 9_000_100])
node = Node(mst, codes.RoomKeys(codes.new_code()), {"id": mona["id"], "seed": mona["seed"], "name": "Mona"})
ml = make_layer("mona_copy")
node.layers = {"L1": ml}
warns = []
node.status.connect(lambda lvl, text: warns.append(text))
ml.startEditing()
for i in range(3):
    nf = QgsFeature(ml.fields())
    nf.setAttribute("owner", f"new {i}")
    nf.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({20 + i * 20} 0,{30 + i * 20} 0,{30 + i * 20} 10,{20 + i * 20} 10,"
                                       f"{20 + i * 20} 0))"))
    ml.addFeature(nf)
node._assign_numbers(ml)
nums = sorted(f["parcel_no"] for f in ml.getFeatures() if str(f["owner"]).startswith("new"))
check(nums == [1000, 1001, 1002], f"parcels drawn offline on Mona's computer take 1000, 1001, 1002 ({nums})")
check(node.numbers_left("L1", "parcel_no") == 7, "seven numbers are left in her range")
for i in range(3):
    nf = QgsFeature(ml.fields())
    nf.setGeometry(QgsGeometry.fromWkt("POLYGON((0 20,5 20,5 25,0 25,0 20))"))
    ml.addFeature(nf)
node._assign_numbers(ml)
check(any("numbers left" in w for w in warns), "a member is warned when few numbers are left")
ml.rollBack()

# the dialogs, built off screen: read-only for a member who is not the admin
from biruni.team import rules_ui  # noqa: E402

mst.put(rules.CKEY, c1)
rd, nd = rules_ui.RulesDialog(node), rules_ui.NumbersDialog(node)
check(not rd.edit and rd.table.rowCount() >= 4 and rd.rules.get("L1", {}).get("area") == "max",
      "the merge rules show to a member, read-only, with the signed rules")
check(not nd.edit and nd.table.rowCount() == 3, "the number ranges show to a member, read-only (three grants)")

# ---- [I06 second piece] shared-node geometry merge & shrinking conflicts ------------------------------
# 1. Shrinking conflict: two members move the same corner to different positions; independent corners merge
p_base = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"
p_mine = "POLYGON((0 0, 10 0, 11 11, 0 12, 0 0))"     # moved (10, 10) -> (11, 11) AND (0, 10) -> (0, 12)
p_theirs = "POLYGON((0 0, 10 0, 12 12, 0 10, 0 0))"   # moved (10, 10) -> (12, 12)
b_hex = bytes(QgsGeometry.fromWkt(p_base).asWkb()).hex()
m_hex = bytes(QgsGeometry.fromWkt(p_mine).asWkb()).hex()
t_hex = bytes(QgsGeometry.fromWkt(p_theirs).asWkb()).hex()

merged_hex, ok, node_c = merge.merge_geometry_nodes(b_hex, m_hex, t_hex)
check(not ok and len(node_c) == 1, "disagreement shrinks to exactly one node conflict")
check(node_c[0]["base"][:2] == (10.0, 10.0) and node_c[0]["mine"][:2] == (11.0, 11.0) and node_c[0]["theirs"][:2] == (12.0, 12.0),
      "the node conflict records the contested corner coordinates")
mg = QgsGeometry()
mg.fromWkb(bytes.fromhex(merged_hex))
pts_merged = [(round(p.x(), 2), round(p.y(), 2)) for p in mg.vertices()]
check((0.0, 12.0) in pts_merged and (12.0, 12.0) in pts_merged,
      "uncontested corner (0, 12) was merged while contested corner kept theirs (12, 12)")

# 2. Moving a common corner reaches all neighbours on the host
h_sh, l_sh = make_hub("host_shared", admin, constitution=c1, grants=grants_)
wkt_grid = [
    "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
    "POLYGON((10 0, 20 0, 20 10, 10 10, 10 0))",
    "POLYGON((0 10, 10 10, 10 20, 0 20, 0 10))",
    "POLYGON((10 10, 20 10, 20 20, 10 20, 10 10))"
]
l_sh.startEditing()
for i, w in enumerate(wkt_grid):
    f = QgsFeature(l_sh.fields())
    f.setAttribute("owner", f"orig_{i+1}")
    f.setAttribute("parcel_no", 100 + i)
    f.setGeometry(QgsGeometry.fromWkt(w))
    l_sh.addFeature(f)
l_sh.commitChanges(False)
fids_grid = sorted(f.id() for f in l_sh.getFeatures())
b_st1 = features.stored_state(l_sh, fids_grid[0])

# Mona moves the common corner (10, 10) -> (11, 11) in parcel 1:
p1_new_wkt = "POLYGON((0 0, 10 0, 11 11, 0 10, 0 0))"
p1_new_st = {"g": bytes(QgsGeometry.fromWkt(p1_new_wkt).asWkb()).hex(), "a": dict(b_st1["a"], owner="Mona")}
op1 = op_of(mona, fids_grid[0], b_st1, p1_new_st)
r_p1 = h_sh.apply_remote(op1, h_sh._member(mona["id"]))
check(r_p1.get("status") in ("ok", "merged"), "Mona's corner edit applied")

def corners_of(fid):
    g = features.stored_state(l_sh, fid)["g"]
    qg = QgsGeometry()
    qg.fromWkb(bytes.fromhex(g))
    return [(round(v.x(), 2), round(v.y(), 2)) for v in qg.vertices()]

check(all((11.0, 11.0) in corners_of(fid) for fid in fids_grid),
      "moving common corner in parcel 1 reached all 3 untouched neighbours")
notes_sh = [e.get("note") or "" for e in h_sh.store.last(10)]
check(any("shared-node" in n for n in notes_sh), "shared-node entries recorded in stream")

# 3. Pinning: member with restricted area moving corner touching neighbour outside area gets pinned
from biruni.team import area  # noqa: E402
area_p1 = area.make([QgsGeometry.fromWkt("POLYGON((-1 -1, 11 -1, 11 11, -1 11, -1 -1))")], l_sh.crs())
h_sh.store.save_member(dict(mona, rights={"L1": "edit", area.KEY: area_p1}))
h_sh._areas._cache.clear()

cur_p1 = features.stored_state(l_sh, fids_grid[0])
p1_ext_wkt = "POLYGON((0 0, 10 0, 10.5 10.5, 0 10, 0 0))"
p1_ext_st = {"g": bytes(QgsGeometry.fromWkt(p1_ext_wkt).asWkb()).hex(), "a": dict(cur_p1["a"])}
op_ext = op_of(mona, fids_grid[0], cur_p1, p1_ext_st)
r_ext = h_sh.apply_remote(op_ext, h_sh._member(mona["id"]))
check(any("pinned shared corner" in f for f in r_ext.get("flags", [])),
      "shared corner touching neighbour outside member's area was pinned (red-team fix)")

# 4. Carrying constitution & grants inside pigeon & folder packs
from biruni.pigeon import core as p_core  # noqa: E402
from biruni.team import folder as f_core  # noqa: E402
import types  # noqa: E402

host_node = types.SimpleNamespace(store=h_sh.store, hub=h_sh, roster=h_sh.roster(), me_id=admin["id"], ident={"name": "Admin"}, since=0, epoch=1, keys=types.SimpleNamespace(tag="room_test"))
down_packs = p_core._down_packs(host_node)
check(bool(down_packs) and "settings" in down_packs[0] and "constitution" in down_packs[0]["settings"],
      "pigeon down pack carries signed merge constitution and grants")

dummy_node = types.SimpleNamespace(store=Store(os.path.join(root, "dummy.sqlite")),
                                   since=0, roster=[], me_id="x"*32, epoch=1,
                                   _keep_settings=lambda s: None, changed=types.SimpleNamespace(emit=lambda: None),
                                   apply_entries=lambda es: None)
dummy_node.store.put("roster", h_sh.roster())
saved_s = {}
dummy_node._keep_settings = lambda s: saved_s.update(s)
rep_p = {"applied": 0, "conflicts": 0, "waiting": 0}
p_core._member_take(dummy_node, down_packs[0], rep_p)
check(rules.CKEY in saved_s, "member taking pigeon pack inherits room constitution")

# 5. Randomised convergence test on shared-node merge
h_rnd1, l_rnd1 = make_hub("host_rnd1", admin)
h_rnd2, l_rnd2 = make_hub("host_rnd2", admin)
for h, l in ((h_rnd1, l_rnd1), (h_rnd2, l_rnd2)):
    l.startEditing()
    f1 = QgsFeature(l.fields())
    f1.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"))
    f1["owner"] = "base"
    f2 = QgsFeature(l.fields())
    f2.setGeometry(QgsGeometry.fromWkt("POLYGON((10 0, 20 0, 20 10, 10 10, 10 0))"))
    f2["owner"] = "base"
    l.addFeatures([f1, f2])
    l.commitChanges(False)

f1_id1 = next(f.id() for f in l_rnd1.getFeatures() if f["owner"] == "base" and f.geometry().boundingBox().xMinimum() < 5)
f1_id2 = next(f.id() for f in l_rnd2.getFeatures() if f["owner"] == "base" and f.geometry().boundingBox().xMinimum() < 5)
f2_id1 = next(f.id() for f in l_rnd1.getFeatures() if f.id() != f1_id1)
f2_id2 = next(f.id() for f in l_rnd2.getFeatures() if f.id() != f1_id2)

base_f1_1 = features.stored_state(l_rnd1, f1_id1)
base_f1_2 = features.stored_state(l_rnd2, f1_id2)

# Mona edits north edge of parcel 1; Sami edits south edge; Tarek moves common corner (10, 10) -> (10.5, 10.5)
u1_1 = op_of(mona, f1_id1, base_f1_1, {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 -1, 10 0, 10 10, 0 10, 0 -1))").asWkb()).hex(), "a": {"owner": "M"}})
u2_1 = op_of(sami, f1_id1, base_f1_1, {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 11, 0 0))").asWkb()).hex(), "a": {"owner": "S"}})
u3_1 = op_of(tarek, f1_id1, base_f1_1, {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10.5 10.5, 0 10, 0 0))").asWkb()).hex(), "a": {"owner": "T"}})

u1_2 = op_of(mona, f1_id2, base_f1_2, {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 -1, 10 0, 10 10, 0 10, 0 -1))").asWkb()).hex(), "a": {"owner": "M"}})
u2_2 = op_of(sami, f1_id2, base_f1_2, {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 11, 0 0))").asWkb()).hex(), "a": {"owner": "S"}})
u3_2 = op_of(tarek, f1_id2, base_f1_2, {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10.5 10.5, 0 10, 0 0))").asWkb()).hex(), "a": {"owner": "T"}})

for op in [u1_1, u2_1, u3_1]:
    h_rnd1.apply_remote(dict(op), h_rnd1._member(op["author"]))
for op in [u3_2, u2_2, u1_2]:
    h_rnd2.apply_remote(dict(op), h_rnd2._member(op["author"]))

geom_1_h1 = features.stored_state(l_rnd1, f1_id1)["g"]
geom_1_h2 = features.stored_state(l_rnd2, f1_id2)["g"]
geom_2_h1 = features.stored_state(l_rnd1, f2_id1)["g"]
geom_2_h2 = features.stored_state(l_rnd2, f2_id2)["g"]

check(geom_1_h1 == geom_1_h2 and geom_2_h1 == geom_2_h2,
      "randomised order convergence: both hosts reached identical geometries for both parcels")


# 6. Geographic CRS tolerance test (EPSG:4326): 1 mm is ~8.98e-9 degrees, NOT 0.001 deg (~100 m)
from qgis.core import QgsCoordinateReferenceSystem  # noqa: E402
crs_deg = QgsCoordinateReferenceSystem("EPSG:4326")
tol_deg = merge.layer_tolerance(crs_deg)
check(tol_deg < 1e-7 and tol_deg > 1e-9, f"geographic CRS tolerance is ~1 mm ({tol_deg:.2e} deg), not 100 metres")

# In EPSG:4326, neighbour vertex at 0.0001 deg (~11 m away) MUST NOT be moved by a corner moving 0.0000001 deg
g_b4326 = bytes(QgsGeometry.fromWkt("POLYGON((31.0 30.0, 31.0001 30.0, 31.0001 30.0001, 31.0 30.0001, 31.0 30.0))").asWkb()).hex()
g_n4326 = bytes(QgsGeometry.fromWkt("POLYGON((31.0 30.0, 31.0001 30.0, 31.00010001 30.00010001, 31.0 30.0001, 31.0 30.0))").asWkb()).hex()
mv4326 = merge.node_moves(g_b4326, g_n4326, tol=tol_deg)
check(len(mv4326) == 1, "corner move in geographic layer detected at sub-millimetre precision")

# 7. Matching when vertex count changes (added/removed vertex)
g_orig = bytes(QgsGeometry.fromWkt("POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))").asWkb()).hex()
# A vertex is added at (5, 0) AND corner (10, 10) is moved to (11, 11) -> 5 corners instead of 4
g_added = bytes(QgsGeometry.fromWkt("POLYGON((0 0, 5 0, 10 0, 11 11, 0 10, 0 0))").asWkb()).hex()
mv_diff = merge.node_moves(g_orig, g_added, tol=0.001)
check(any(m[0] == (10.0, 10.0) and m[1] == (11.0, 11.0) for m in mv_diff),
      "moved corner was matched and extracted even when vertex count changed")

# 8. T-junction corner on neighbour edge
g_neighbour = bytes(QgsGeometry.fromWkt("POLYGON((10 0, 20 0, 20 20, 10 20, 10 0))").asWkb()).hex()
# Point (10, 10) lies on edge (10, 0) -> (10, 20) of neighbour, not as a vertex
split_hex, split_ok = merge.insert_and_move_node_on_edge(g_neighbour, (10.0, 10.0), (11.0, 10.0), tol=0.001)
check(split_ok, "T-junction on neighbour edge detected and split")
split_g = QgsGeometry()
split_g.fromWkb(bytes.fromhex(split_hex))
check(any((round(v.x(), 1), round(v.y(), 1)) == (11.0, 10.0) for v in split_g.vertices()),
      "neighbour edge was split and moved to follow the T-junction corner")

# 9. Respecting locks (hook for future feature): a locked neighbour is never modified
h_sh.store.put("locks", {f"L1:{fids_grid[1]}": tarek["id"]})  # parcel 2 locked by Tarek
op_lock_test = op_of(mona, fids_grid[0], features.stored_state(l_sh, fids_grid[0]),
                     {"g": bytes(QgsGeometry.fromWkt("POLYGON((0 0, 10.2 0, 11 11, 0 10, 0 0))").asWkb()).hex(),
                      "a": dict(features.stored_state(l_sh, fids_grid[0])["a"])})
r_lock = h_sh.apply_remote(op_lock_test, h_sh._member(mona["id"]))
check(any("neighbour #2 is locked" in f or "is locked" in f for f in r_lock.get("flags", [])),
      "shared corner touching a locked neighbour was pinned and skipped")

# 10. Error logging: last 50 errors are kept with their timestamps
h_sh.store.put("room_errors", [])
for i in range(55):
    h_sh._log_error(f"test error {i}")
errs = h_sh.store.get("room_errors")
check(len(errs) == 50 and errs[-1]["text"] == "test error 54" and "ts" in errs[-1],
      "the room keeps exactly the last 50 errors with their timestamps")

ALL_PASSED_MSG = "\nALL PASSED"
print()
print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
sys.exit(1 if failures else 0)
