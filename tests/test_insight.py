r"""Insight: the spatial spell checker (a value checked against where its feature lies) and the rules learned
from the data (with their support and exceptions, and broken_rules() for the drawing tool). Small layers in
memory with planted mistakes - each must be found - and the same layers clean, where nothing may be flagged.
Ends with the speed on 20,000 parcels.

Run:  engine\bin\python-qgis.bat tests\test_insight.py
"""
import os
import random
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()

from biruni.insight import learned, spellcheck  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


CRS = "EPSG:32638"
STREETS = ["الورود", "الزهور", "النخيل", "الريان", "السلام", "الفيصل", "العليا", "الملقا", "التعاون", "الربيع"]


def rect(x0, y0, x1, y1):
    return QgsGeometry.fromWkt(f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))")


def layer(kind, name, fields, rows):
    lyr = QgsVectorLayer(f"{kind}?crs={CRS}&" + "&".join(f"field={f}" for f in fields), name, "memory")
    feats = []
    for geom, attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setAttributes(attrs)
        f.setGeometry(geom)
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def build(cols=40, rows=10, dirty=True, seed=1, buildings=True, roads=True):
    """districts | parcels (cols x rows, numbered j*1000+i+1 along rows) | streets every 2 rows | buildings 3 m
    inside each parcel | road polygons 8/12/16 m wide. With dirty=True one mistake of each kind is planted;
    returns (layers, planted) where planted maps a name to (i, j) or a feature index."""
    rnd = random.Random(seed)
    QgsProject.instance().removeAllMapLayers()
    xs = [0]
    for i in range(cols):
        xs.append(xs[-1] + 25 + (i % 3) * 5)
    h = 20
    split = xs[cols // 2]
    top = rows * h
    districts = layer("Polygon", "Districts", ["name:string"],
                      [(rect(-50, -50, split, top + 50), ["الياسمين"]),
                       (rect(split, -50, xs[-1] + 50, top + 50), ["النرجس"])])
    nlines = rows // 2 + 1
    streets = layer("LineString", "Streets", ["name:string"],
                    [(QgsGeometry.fromWkt(f"LINESTRING({-20} {b * 2 * h},{xs[-1] + 20} {b * 2 * h})"),
                      [STREETS[b % len(STREETS)] + ("" if b < len(STREETS) else f" {b}")])
                     for b in range(nlines)])
    prow, brow = [], []
    for j in range(rows):
        for i in range(cols):
            x0, x1, y0 = xs[i], xs[i + 1], j * h
            g = rect(x0, y0, x1, y0 + h)
            district = "الياسمين" if x0 < split else "النرجس"
            if (i + j) % 7 == 0:
                district = "حي " + district           # written with the prefix: not a mistake
            commercial = j < 2 and i >= cols // 2
            use = "تجاري" if commercial else "سكني"
            zone = "C" if commercial else "R"
            floors = rnd.choice([6, 8]) if commercial else rnd.randint(1, 4)
            area = round(g.area(), 2)
            owner = rnd.choice(["فرد", "شركة", "جهة حكومية", "وقف"])
            prow.append([g, [j * 1000 + i + 1, district, area, use, zone, floors, owner]])
            b = 2 * h * ((j + 1) // 2)        # the street on the near side of the two-row block
            bg = rect(x0 + 3, y0 + 3, x1 - 3, y0 + h - 3)
            brow.append([bg, [STREETS[(b // (2 * h)) % len(STREETS)] + ("" if b // (2 * h) < len(STREETS)
                                                                         else f" {b // (2 * h)}")]])
    planted = {}
    if dirty:
        def at(i, j):
            return j * cols + i
        planted["inside"] = at(5, 2)
        prow[at(5, 2)][1][1] = "النرجس"                     # lies in الياسمين
        planted["inside_spelling"] = at(10, 4)
        prow[at(10, 4)][1][1] = "الياسمن"                   # misspelt
        planted["sequence"] = at(12, 6)
        prow[at(12, 6)][1][0] = 99999                       # should be 6013
        planted["duplicate"] = at(20, 9)
        prow[at(20, 9)][1][0] = 9020                        # same as its left neighbour; 9021 is missing
        planted["measure"] = at(25, 7)
        prow[at(25, 7)][1][2] = 900.0
        planted["outlier"] = at(15, 5)
        prow[at(15, 5)][1][3] = "صناعي"                     # use: none of the neighbours
        planted["zone"] = at(3, 5)
        prow[at(3, 5)][1][4] = "C"                          # residential parcel with the commercial zone
        planted["floors"] = at(7, 7)
        prow[at(7, 7)][1][5] = 9                            # residential: at most 4
        # a parcel whose top-right corner leans 2 m out: no right angle, overlaps its neighbour
        planted["skew"] = at(35, 8)
        x0, x1, y0 = xs[35], xs[36], 8 * h
        prow[at(35, 8)][0] = QgsGeometry.fromWkt(
            f"POLYGON(({x0} {y0},{x1} {y0},{x1 + 2} {y0 + h},{x0} {y0 + h},{x0} {y0}))")
        prow[at(35, 8)][1][2] = round(prow[at(35, 8)][0].area(), 2)
        if buildings:
            planted["street"] = at(8, 3)
            brow[at(8, 3)][1][0] = "التعاون"                 # a far street of the layer
            planted["street_spelling"] = at(30, 0)
            brow[at(30, 0)][1][0] = "الوورود"               # misspelt nearest street
            planted["straddle"] = at(17, 4)
            x0, x1, y0 = xs[17], xs[18], 4 * h
            brow[at(17, 4)][0] = rect(x0 + 15, y0 + 3, x1 + 10, y0 + h - 3)    # half in the next parcel
            planted["setback"] = at(22, 1)
            x0, x1, y0 = xs[22], xs[23], 1 * h
            brow[at(22, 1)][0] = rect(x0 + 0.5, y0 + 3, x1 - 3, y0 + h - 3)    # 0.5 m from the edge
    else:
        # the street name written with the word «شارع» - the same name
        brow[3][1][0] = "شارع " + brow[3][1][0]
    parcels = layer("Polygon", "Parcels", ["num:integer", "district:string", "area:double", "use:string",
                                           "zone:string", "floors:integer", "owner:string"], prow)
    out = {"districts": districts, "streets": streets, "parcels": parcels}
    if buildings:
        out["buildings"] = layer("Polygon", "Buildings", ["street:string"], brow)
    if roads:
        rrows = []
        widths = [8, 12, 16]
        for k in range(30):
            w = widths[k % 3]
            if dirty and k == 29:
                w = 10
            y = top + 200 + k * 30
            rrows.append((rect(0, y, 300, y + w), [float(w if not (dirty and k == 28) else 10.5)]))
        if dirty:
            planted["width"] = 29
            planted["width_field"] = 28
        out["roads"] = layer("Polygon", "Roads", ["width:double"], rrows)
    return out, planted


def fid_of(lyr, index):
    return sorted(f.id() for f in lyr.getFeatures())[index]


def finding(result, fid, fld, kind=None):
    return next((f for f in result.findings if f.fid == fid and f.field == fld and (kind is None or f.kind == kind)),
                None)


# ---- 1. spatial spell checker: planted mistakes ----------------------------------------------------------
L, P = build(dirty=True)
parcels, buildings = L["parcels"], L["buildings"]
res = spellcheck.check_layer(parcels)
print(f"spell check of {res.checked} parcels: {len(res.findings)} findings in {res.seconds:.2f} s")
for p in res.pairs:
    print("  pair:", p.text())
for f in res.findings:
    print("  ", f.fid, f.field, f.value, "->", f.suggestion, "|", f.text())

f = finding(res, fid_of(parcels, P["inside"]), "district", "inside")
check(f is not None and f.suggestion == "الياسمين" and "الياسمين" in f.text(),
      "inside: a parcel saying «النرجس» inside «الياسمين» is flagged with the right suggestion")
f = finding(res, fid_of(parcels, P["inside_spelling"]), "district", "inside")
check(f is not None and f.suggestion == "الياسمين" and f.reason == "inside_spelling",
      "inside: a misspelt district name is flagged as a spelling of the district it lies in")
check(any(p.kind == "inside" and p.field == "district" and p.other_field == "name" for p in res.pairs),
      "inside: the pairing district <-> Districts.name was found by itself")
check(not any(f.field == "district" and f.fid not in (fid_of(parcels, P["inside"]), fid_of(parcels, P["inside_spelling"]))
              for f in res.findings),
      "inside: «حي الياسمين» (with the prefix) is not a mistake")
f = finding(res, fid_of(parcels, P["sequence"]), "num", "sequence")
check(f is not None and f.suggestion == 6013, "sequence: 6012, 99999, 6014 along a row -> suggests 6013 (got %r)" %
      (f.suggestion if f else None))
f = finding(res, fid_of(parcels, P["duplicate"]), "num", "sequence")
check(f is not None and f.suggestion == 9021, "sequence: a repeated number between 9020 and 9022 -> suggests 9021")
check(len([f for f in res.findings if f.field == "num"]) == 2, "sequence: no other number is flagged")
f = finding(res, fid_of(parcels, P["measure"]), "area", "measure")
check(f is not None and abs(float(f.suggestion) - 600) < 0.01 and "600" in f.text(),
      "measure: area field 900 on a 600 m² parcel -> suggests 600")
check(len([f for f in res.findings if f.field == "area"]) == 1, "measure: no other area is flagged")
f = finding(res, fid_of(parcels, P["outlier"]), "use", "neighbours")
check(f is not None and f.suggestion == "سكني", "neighbours: «صناعي» among residential neighbours -> «سكني»")
f = finding(res, fid_of(parcels, P["zone"]), "zone", "neighbours")
check(f is not None and f.suggestion == "R", "neighbours: zone C among R neighbours -> R")
check(not any(f.field in ("owner", "floors") for f in res.findings),
      "neighbours: fields scattered at random (owner, floors) raise nothing")
# the commercial block's edge parcels are not outliers
check(len([f for f in res.findings if f.field == "use"]) == 1, "neighbours: the edge of the commercial block "
      "is not flagged")

bres = spellcheck.check_layer(buildings)
for f in bres.findings:
    print("  ", f.fid, f.field, f.value, "->", f.suggestion, "|", f.text())
f = finding(bres, fid_of(buildings, P["street"]), "street", "nearest")
check(f is not None and f.suggestion == "النخيل" and f.reason in ("nearest", "nearest_none"),
      "nearest: a building on «النخيل» saying «التعاون» is flagged (%s)" % (f.text() if f else "-"))
f = finding(bres, fid_of(buildings, P["street_spelling"]), "street", "nearest")
check(f is not None and f.suggestion == "الورود" and f.reason == "nearest_spelling",
      "nearest: a misspelt street name is flagged as a spelling of the nearest street")
flagged = {f.fid for f in bres.findings if f.field == "street"}
check(flagged == {fid_of(buildings, P["street"]), fid_of(buildings, P["street_spelling"])},
      "nearest: only the two planted buildings are flagged (got %d)" % len(flagged))

# accept: one click writes the suggestion, as an undoable edit
f = finding(res, fid_of(parcels, P["inside"]), "district")
n = spellcheck.accept([f])
check(n == 1 and parcels.isEditable() and parcels.getFeature(f.fid)["district"] == "الياسمين",
      "accept: the suggestion is written (layer left in edit mode)")
parcels.undoStack().undo()
check(parcels.getFeature(f.fid)["district"] == "النرجس", "accept: undo brings the old value back")
f = finding(res, fid_of(parcels, P["sequence"]), "num")
spellcheck.accept([f])
check(parcels.getFeature(f.fid)["num"] == 6013, "accept: a number suggestion is written as a number")
parcels.rollBack()

# marking the cells in the attribute table (the engine's conditional styles)
marked = spellcheck.mark_table(parcels, res.findings)
styles = parcels.conditionalStyles().fieldStyles("district")
check(marked == len(res.findings) and any(s.name() == spellcheck.MARK for s in styles),
      "mark: suspicious cells get a conditional style")
from qgis.core import QgsExpressionContext, QgsExpressionContextUtils  # noqa: E402

ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(parcels))
st = next(s for s in styles if s.name() == spellcheck.MARK)
bad = parcels.getFeature(fid_of(parcels, P["inside"]))
good = parcels.getFeature(fid_of(parcels, 0))
ctx.setFeature(bad)
m1 = st.matches(bad["district"], ctx)
ctx.setFeature(good)
m2 = st.matches(good["district"], ctx)
check(m1 and not m2, "mark: the style matches the flagged feature only")
spellcheck.clear_marks(parcels)
check(not any(s.name() == spellcheck.MARK for s in parcels.conditionalStyles().fieldStyles("district")),
      "mark: clear removes the marks")

# ---- 2. spell checker on clean data: no false alarm -------------------------------------------------------
L0, _ = build(dirty=False)
r0 = spellcheck.check_layer(L0["parcels"])
for f in r0.findings:
    print("  FALSE ALARM", f.field, f.value, "->", f.suggestion, f.text())
check(not r0.findings, "clean parcels: no finding (%d)" % len(r0.findings))
check({p.kind for p in r0.pairs} >= {"inside", "sequence", "measure"},
      "clean parcels: the pairings are still found (%s)" % sorted({p.kind for p in r0.pairs}))
rb = spellcheck.check_layer(L0["buildings"])
check(not rb.findings, "clean buildings: no finding, «شارع الورود» is «الورود» (%d)" % len(rb.findings))
check(any(p.kind == "nearest" for p in rb.pairs), "clean buildings: street <-> Streets.name found")
rr = spellcheck.check_layer(L0["roads"])
check(not rr.findings, "clean roads: no finding")

# ---- 3. rules learned from the data: planted exceptions --------------------------------------------------
L, P = build(dirty=True)
parcels, buildings, roads = L["parcels"], L["buildings"], L["roads"]
timings = {}
t0 = time.perf_counter()
rules = learned.learn(timings=timings)
print(f"learned {len(rules)} rules in {time.perf_counter() - t0:.2f} s")
for r in rules:
    print("  ", r.kind, "|", r.text(), "|", len(r.exceptions), "exceptions:",
          "; ".join(learned.Rule.note(k, v) for _f, k, v in r.exceptions[:3]))


def rule(kind, lyr, **params):
    return next((r for r in rules if r.kind == kind and r.layer_id == lyr.id()
                 and all(r.params.get(k) == v or r.values.get(k) == v for k, v in params.items())), None)


r = rule("inside", buildings, other=parcels.id())
check(r is not None and fid_of(buildings, P["straddle"]) in r.fids and len(r.fids) == 1
      and 0.99 < r.support < 1, "inside: buildings lie in a parcel (%s), the straddling one is the exception"
      % (f"{r.support:.2%}" if r else "-"))
r = rule("setback", buildings, other=parcels.id())
check(r is not None and r.params["d"] == 3 and r.fids == [fid_of(buildings, P["setback"])],
      "setback: buildings are >= 3 m from the parcel edge; the 0.5 m one breaks it (%s)" % (r.text() if r else "-"))
r = rule("no_overlap", parcels)
check(r is not None and fid_of(parcels, P["skew"]) in r.fids and len(r.fids) == 2,
      "no overlap: the leaning parcel and its neighbour are the exceptions")
r = rule("right_angles", parcels)
check(r is not None and r.fids == [fid_of(parcels, P["skew"])] and r.support > 0.99,
      "right angles: one parcel has a corner a few degrees off (%s)" % (r.exceptions[0][2] if r else "-"))
r = rule("typical", roads, field="width")
check(r is not None and r.params["centres"] == [8, 12, 16]
      and set(r.fids) == {fid_of(roads, P["width_field"]), fid_of(roads, P["width"])},
      "typical values: the width field is 8 / 12 / 16 except 10.5 and 10")
r = rule("typical_width", roads)
check(r is not None and r.params["centres"] == [8, 12, 16] and r.fids == [fid_of(roads, P["width"])],
      "typical width (measured): 8 / 12 / 16 m, the 10 m road is the exception")
r = rule("unique", parcels, field="num")
check(r is not None and set(r.fids) == {fid_of(parcels, P["duplicate"]), fid_of(parcels, P["duplicate"] - 1)},
      "unique: num is unique except the two parcels sharing 9020")
r = rule("depends", parcels, a="use", b="zone")
check(r is not None and r.fids == [fid_of(parcels, P["zone"])], "depends: use decides zone; the residential "
      "parcel in zone C is the exception")
r = rule("depends_max", parcels, b="floors")    # use and zone tell the same here: one rule is kept
check(r is not None and r.params["a"] in ("use", "zone") and r.fids == [fid_of(parcels, P["floors"])]
      and max(r.params["caps"].values()) == 8 and min(r.params["caps"].values()) == 4,
      "depends max: residential parcels have at most 4 floors; the 9-floor one breaks it")
check(not any(r.support < learned.MIN_SUPPORT for r in rules), "every reported rule has support >= 90%")
check(rule("inside", parcels) is None, "no rule without exceptions (parcels all inside districts)")

# ---- 4. broken_rules() for the drawing tool --------------------------------------------------------------
p0 = parcels.getFeature(fid_of(parcels, 0)).geometry()
bb = p0.boundingBox()
inside_ok = rect(bb.xMinimum() + 3, bb.yMinimum() + 3, bb.xMaximum() - 3, bb.yMaximum() - 3)
b0 = fid_of(buildings, 0)       # redrawing the building of that parcel
v = learned.broken_rules(buildings, inside_ok, {"street": "الورود"}, fid=b0)
check(v == [], "drawing: a building 3 m inside its parcel breaks nothing (%s)" % [x.text() for x in v])
v = learned.broken_rules(buildings, rect(bb.xMinimum() + 1, bb.yMinimum() + 3, bb.xMaximum() - 3,
                                         bb.yMaximum() - 3), fid=b0)
check([x.rule.kind for x in v] == ["setback"] and "1" in v[0].short(), "drawing: 1 m from the edge breaks "
      "the setback (%s)" % [x.text() for x in v])
v = learned.broken_rules(buildings, rect(bb.xMaximum() - 5, bb.yMinimum() + 3, bb.xMaximum() + 10,
                                         bb.yMaximum() - 3))
check("inside" in [x.rule.kind for x in v], "drawing: a building across two parcels breaks 'inside'")
v = learned.broken_rules(parcels, QgsGeometry.fromWkt(
    f"POLYGON(({bb.xMinimum()} {bb.yMinimum() - 30},{bb.xMaximum()} {bb.yMinimum() - 30},"
    f"{bb.xMaximum() + 1.5} {bb.yMinimum() - 10},{bb.xMinimum()} {bb.yMinimum() - 10},"
    f"{bb.xMinimum()} {bb.yMinimum() - 30}))"), {"use": "سكني", "zone": "C", "floors": 9, "num": 1001})
kinds = sorted(x.rule.kind for x in v)
check(set(kinds) == {"depends", "depends_max", "right_angles", "unique", "typical"},
      "drawing: a new parcel with a leaning corner, zone C, 9 floors and an existing number breaks those rules "
      "(%s)" % kinds)
print("   ", [x.text() for x in v])
v = learned.broken_rules(parcels, p0, {"num": 1}, fid=fid_of(parcels, 0))
check(v == [], "drawing: the feature being edited does not collide with itself")
moved = QgsGeometry(p0)
moved.translate(3, 0)
v = learned.broken_rules(parcels, moved, {})
check(any(x.rule.kind == "no_overlap" for x in v), "drawing: a parcel over an existing one breaks 'no overlap'")
v = learned.broken_rules(roads, rect(0, 5000, 300, 5010), {"width": 10})
check(sorted(x.rule.kind for x in v) == ["typical", "typical_width"], "drawing: a 10 m road breaks both width rules")

# ---- 5. rules on clean data: nothing to report ------------------------------------------------------------
L0, _ = build(dirty=False)
r0 = learned.learn()
for r in r0:
    print("  CLEAN RULE", r.kind, r.text(), len(r.exceptions))
check(not r0, "clean data: no rule has exceptions to show (%d)" % len(r0))

# ---- 6. the two windows (offscreen, never shown) -----------------------------------------------------
from biruni.insight.rules_dialog import RulesDialog  # noqa: E402
from biruni.insight.spell_dialog import SpellDialog  # noqa: E402

L, P = build(dirty=True)
dlg = SpellDialog(None)
dlg.layer.setLayer(L["parcels"])
dlg.run()
check(dlg.tree.topLevelItemCount() == 7 and "district" in dlg.summary.text(),
      "spell window: lists the 7 suspicious values and the pairings")
check(any(s.name() == spellcheck.MARK for s in L["parcels"].conditionalStyles().fieldStyles("district")),
      "spell window: the cells are marked in the attribute table")
row = next(i for i in range(dlg.tree.topLevelItemCount()) if dlg.tree.topLevelItem(i).text(1) == "district")
dlg.tree.topLevelItem(row).setSelected(True)
fid = dlg.selected()[0].fid
dlg.accept_selected()
check(dlg.tree.topLevelItemCount() == 6 and L["parcels"].getFeature(fid)["district"] == "الياسمين",
      "spell window: accepting writes the value and takes the row off the list")
L["parcels"].rollBack()
dlg.close()
check(not any(s.name() == spellcheck.MARK for s in L["parcels"].conditionalStyles().fieldStyles("district")),
      "spell window: closing removes the marks")
rd = RulesDialog(None)
rd.run()
check(rd.tree.topLevelItemCount() == len(rd.rules) > 0, "rules window: one row per rule (%d)" % len(rd.rules))
top = rd.tree.topLevelItem(0)
rd.tree.expandItem(top)
check(top.childCount() == len(rd.rules[0].exceptions) and "#" in top.child(0).text(0),
      "rules window: opening a rule lists its exceptions")
rd.tree.setCurrentItem(top.child(0))
r, fids = rd._current()
check(r is rd.rules[0] and fids == [rd.rules[0].exceptions[0][0]], "rules window: a row points at its feature")
rd.close()

# ---- 7. speed: 20,000 parcels ---------------------------------------------------------------------------
L, P = build(cols=200, rows=100, dirty=True, buildings=True, roads=False)
t0 = time.perf_counter()
res = spellcheck.check_layer(L["parcels"])     # against every other layer, 20,000 buildings too
t_spell = time.perf_counter() - t0
t0 = time.perf_counter()
rules = learned.learn(layers=[L["parcels"]])
t_rules = time.perf_counter() - t0
t0 = time.perf_counter()
brules = learned.learn(layers=[L["buildings"]])
t_build = time.perf_counter() - t0
t0 = time.perf_counter()
bres = spellcheck.check_layer(L["buildings"])
t_bspell = time.perf_counter() - t0
print(f"SPEED: 20,000 buildings in 20,000 parcels - rules {t_build:.1f} s ({len(brules)} rules: "
      f"{', '.join(r.kind for r in brules)}), spell check {t_bspell:.1f} s ({len(bres.findings)} findings)")
check(t_build < 20 and t_bspell < 20, "speed: 20,000 buildings against 20,000 parcels also under 20 s")
print(f"SPEED: 20,000 parcels - spell check {t_spell:.1f} s ({len(res.findings)} findings, "
      f"{len(res.pairs)} pairings), rules {t_rules:.1f} s ({len(rules)} rules)")
check(t_spell < 20, f"speed: spell check of 20,000 parcels in {t_spell:.1f} s (< 20 s)")
check(t_rules < 20, f"speed: rules of 20,000 parcels in {t_rules:.1f} s (< 20 s)")
check(finding(res, fid_of(L["parcels"], P["inside"]), "district") is not None
      and finding(res, fid_of(L["parcels"], P["measure"]), "area") is not None,
      "speed: the planted mistakes are still found at 20,000")

QgsProject.instance().removeAllMapLayers()
print("\n%d failure(s)" % len(failures))
sys.stdout.flush()
os._exit(1 if failures else 0)
