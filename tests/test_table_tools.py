"""Table tools: field order (table view and data), highlighting, natural sort,
type-ahead, the multi-field join and saved selections.

Run:  engine\\bin\\python-qgis.bat tests\\test_table_tools.py
"""
import os
import shutil
import sys
import tempfile

from qgis.core import (QgsApplication, QgsConditionalStyle, QgsEditorWidgetSetup, QgsExpression,
                       QgsExpressionContext, QgsExpressionContextUtils, QgsFeature, QgsField, QgsGeometry,
                       QgsProcessingContext, QgsProcessingFeedback, QgsProject, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QMetaType

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni import selection_sets as ss  # noqa: E402
from biruni import table_tools as tt  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp(prefix="biruni_tt_")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


ROWS = [("قطعة ١", 7, 1.5, QDate(2024, 1, 2), "POINT(10 10)"),
        ("plot B", 3, 2.25, QDate(2024, 3, 4), "POINT(20 15)"),
        (None, 9, None, None, "POINT(30 5)"),
        ("الروضة", 3, 4.0, QDate(2025, 6, 7), "POINT(40 40)")]


def memory_layer(name="mem"):
    lyr = QgsVectorLayer("Point?crs=EPSG:32637&field=a:string(40)&field=b:integer&field=c:double&field=d:date",
                         name, "memory")
    feats = []
    for r in ROWS:
        f = QgsFeature(lyr.fields())
        f.setAttributes(list(r[:4]))
        f.setGeometry(QgsGeometry.fromWkt(r[4]))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def file_layer(driver, ext, name):
    mem = memory_layer()
    path = os.path.join(TMP, f"{name}.{ext}")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.fileEncoding = "UTF-8"
    opts.layerName = name
    err = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsProject.instance().transformContext(), opts)
    assert err[0] == QgsVectorFileWriter.WriterError.NoError, err
    uri = f"{path}|layername={name}" if driver == "GPKG" else path
    lyr = QgsVectorLayer(uri, name, "ogr")
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def snapshot(lyr):
    """{fid: (geometry wkt, {field: value})} over the data fields."""
    names = tt.provider_fields(lyr)[0]
    return {f.id(): (f.geometry().asWkt(1), {n: f[n] for n in names if n != "fid"}) for f in lyr.getFeatures()}


def data_order(lyr):
    return [f.name() for f in lyr.dataProvider().fields()]


def rewrite_case(lyr, label):
    lyr.setFieldAlias(lyr.fields().indexOf("b"), "Floors")
    lyr.setEditorWidgetSetup(lyr.fields().indexOf("b"), QgsEditorWidgetSetup("Range", {"Min": 0, "Max": 50}))
    lyr.conditionalStyles().setFieldStyles("c", [QgsConditionalStyle("@value > 2")])
    before = snapshot(lyr)
    count = lyr.featureCount()
    res = tt.rewrite_fields(lyr, ["c", "a", "d", "b"], {"a": "name_a"})
    order = [n for n in data_order(lyr) if n != "fid"]
    check(res.changed and order == ["c", "name_a", "d", "b"], f"{label}: data order rewritten {order}")
    after = snapshot(lyr)
    same = set(before) == set(after) and all(
        before[k][0] == after[k][0] and before[k][1]["b"] == after[k][1]["b"]
        and before[k][1]["c"] == after[k][1]["c"] and before[k][1]["a"] == after[k][1]["name_a"]
        and before[k][1]["d"] == after[k][1]["d"] for k in before)
    check(same and lyr.featureCount() == count and lyr.isValid(),
          f"{label}: fids, geometries and values kept (Arabic text included)")
    i = lyr.fields().indexOf("b")
    check(lyr.attributeAlias(i) == "Floors" and lyr.editorWidgetSetup(i).type() == "Range"
          and len(lyr.conditionalStyles().fieldStyles("c")) == 1, f"{label}: alias, widget and styles carried over")
    check(tt.table_order(lyr)[0][:5] == [n for n in data_order(lyr)][:5], f"{label}: attribute table follows")
    return before, res


# ---- GeoPackage -----------------------------------------------------------------
gp = file_layer("GPKG", "gpkg", "parcels")
gp.saveStyleToDatabase("biruni_test_style", "", True, "")
before, res = rewrite_case(gp, "GeoPackage")
check(res.backup and os.path.isfile(os.path.join(res.backup["dir"], "parcels.gpkg")), "GeoPackage: backup copied")
check(data_order(gp)[0] == "fid", "GeoPackage: fid stays first")
_, _, styles, _, _ = gp.listStylesInDatabase()
check("biruni_test_style" in list(styles), "GeoPackage: styles saved in the file survive")
check(len(tt.backups(gp)) == 1, "GeoPackage: backup remembered on the layer")
tt.restore_backup(gp, tt.backups(gp)[0])
check([n for n in data_order(gp) if n != "fid"] == ["a", "b", "c", "d"] and snapshot(gp) == before,
      "GeoPackage: restore puts back the order and the values")
# a second rewrite after the restore works on the reopened file
res2 = tt.rewrite_fields(gp, ["d", "c", "b", "a"])
check([n for n in data_order(gp) if n != "fid"] == ["d", "c", "b", "a"], "GeoPackage: rewrite after restore")

# ---- shapefile ------------------------------------------------------------------
sh = file_layer("ESRI Shapefile", "shp", "plots")
before_sh, res = rewrite_case(sh, "Shapefile")
check(sorted(os.listdir(res.backup["dir"])) and any(n.endswith(".dbf") for n in os.listdir(res.backup["dir"])),
      "Shapefile: backup holds the .dbf")
try:
    tt.rewrite_fields(sh, ["c"], {"c": "اسم_طويل_جدا"})
    check(False, "Shapefile: names over 10 bytes refused")
except RuntimeError:
    check(True, "Shapefile: names over 10 bytes refused")
tt.restore_backup(sh, tt.backups(sh)[-1])
check([n for n in data_order(sh)] == ["a", "b", "c", "d"] and snapshot(sh) == before_sh,
      "Shapefile: restore puts back the order and the values")

# ---- memory ---------------------------------------------------------------------
mem = memory_layer("scratch")
QgsProject.instance().addMapLayer(mem)
mem_before, res = rewrite_case(mem, "Memory")
tt.undo_rewrite(mem, res)
check(data_order(mem) == ["a", "b", "c", "d"] and snapshot(mem) == mem_before, "Memory: undo puts it back")
check(tt.rewrite_fields(mem, ["a", "b", "c", "d"]).changed is False, "No change when the order is the same")

# ---- refusals and the table-only order -------------------------------------------
mem.startEditing()
ok, why = tt.can_rewrite(mem)
check(not ok and why, "Refused while the layer is being edited")
mem.rollBack()
try:
    tt.rewrite_fields(mem, ["a"], {"a": "b"})
    check(False, "Duplicate names refused")
except RuntimeError:
    check(True, "Duplicate names refused")
tt.view_order(mem, ["d", "a"], hidden={"c"})
names, hidden = tt.table_order(mem)
check(names[:2] == ["d", "a"] and hidden == {"c"} and data_order(mem) == ["a", "b", "c", "d"],
      "Table-only order and hidden columns, data untouched")
csv = QgsVectorLayer(os.path.join(TMP, "x.csv") + "?type=csv", "csv", "delimitedtext")
check(not tt.can_rewrite(csv)[0], "Refused for data that cannot change fields")

# ---- highlighting ------------------------------------------------------------------
hl = memory_layer("hl")
QgsProject.instance().addMapLayer(hl)


def matches(lyr, field, fid, row=False):
    ctx = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
    feat = lyr.getFeature(fid)
    ctx.setFeature(feat)
    styles = lyr.conditionalStyles().rowStyles() if row else lyr.conditionalStyles().fieldStyles(field)
    value = None if row else feat[field]
    return [s.name() for s in QgsConditionalStyle.matchingConditionalStyles(styles, value, ctx)]


fids = [f.id() for f in hl.getFeatures()]
tt.highlight_empty(hl, ["a", "c"])
check(matches(hl, "a", fids[2]) and not matches(hl, "a", fids[0]), "Empty cells highlighted")
tt.highlight_duplicates(hl, "b")
check(matches(hl, "b", fids[1]) and matches(hl, "b", fids[3]) and not matches(hl, "b", fids[0]),
      "Duplicates highlighted (3 twice)")
tt.highlight_range(hl, "b", 4, 8)
check(any("range" in n or tt.STYLE_TAG in n for n in matches(hl, "b", fids[2])) and len(matches(hl, "b", fids[0])) == 0,
      "Outside the range highlighted (9 > 8, 7 inside)")
lo, hi = tt.colour_scale(hl, "c", 5)
check(lo == 1.5 and hi == 4.0 and len(matches(hl, "c", fids[3])) == 1 and len(matches(hl, "c", fids[0])) == 1,
      "Colour scale: every number falls in exactly one band, the maximum included")
n, more = tt.colour_rows_by(hl, "b")
check(n == 3 and not more and len(matches(hl, None, fids[1], row=True)) == 1, "Rows coloured by value")
foreign = QgsConditionalStyle("@value = 1")
foreign.setName("mine")
hl.conditionalStyles().setFieldStyles("d", [foreign])
removed = tt.clear_highlighting(hl)
check(removed >= 5 and not tt.highlighting(hl) and len(hl.conditionalStyles().fieldStyles("d")) == 1,
      "Clear removes only Biruni highlighting")

# ---- natural sort ----------------------------------------------------------------
values = ["plot 10", "Plot 2", "plot 1", "قطعة ١٠", "قطعة ٢", "قطعه 3", None]
ordered = sorted([v for v in values if v], key=tt.natural_key_value)
check(ordered[:3] == ["plot 1", "Plot 2", "plot 10"] and ordered[3:] == ["قطعة ٢", "قطعه 3", "قطعة ١٠"],
      f"Natural order: {ordered}")
tt.register_functions()
e = QgsExpression("natural_key('a12') > natural_key('a9')")
check(bool(e.evaluate()) and not e.hasEvalError(), "natural_key() in expressions")
e2 = QgsExpression("arabic_normalise('أحمد  ١٢')")
check(e2.evaluate() == "احمد 12", "arabic_normalise() in expressions")
tt.sort_naturally(hl, "a")
check(hl.attributeTableConfig().sortExpression() == 'natural_key("a")', "Attribute table sorted naturally")

# ---- type-ahead ------------------------------------------------------------------
ia = hl.fields().indexOf("a")
hl.setEditorWidgetSetup(ia, QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True}))
check(tt.enable_autocomplete(hl, ["a", "b"]) == 2 and tt.autocomplete_fields(hl) == ["a", "b"],
      "Type-ahead on two fields")
check(hl.editorWidgetSetup(ia).config().get("Editable") is True, "Type-ahead widget is editable")
tt.disable_autocomplete(hl, ["a"])
check(hl.editorWidgetSetup(ia).type() == "TextEdit" and hl.editorWidgetSetup(ia).config().get("IsMultiline"),
      "Type-ahead off restores the previous widget and its settings")
tt.disable_autocomplete(hl)
check(tt.autocomplete_fields(hl) == [], "All type-ahead off")

# ---- multi-field join --------------------------------------------------------------
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()
parcels = QgsVectorLayer("Point?crs=EPSG:32637&field=district:string&field=plan:integer&field=no:string",
                         "parcels", "memory")
for d, p, n in (("الروضة", 101, "12"), ("الروضة", 101, "13"), ("Al Nakheel", 202, "5"), ("السلام", 303, "1"),
                (None, 101, "12")):
    f = QgsFeature(parcels.fields())
    f.setAttributes([d, p, n])
    f.setGeometry(QgsGeometry.fromWkt("POINT(1 1)"))
    parcels.dataProvider().addFeatures([f])
owners = QgsVectorLayer("None?field=hay:string&field=mukhattat:string&field=qeta:integer&field=owner:string",
                        "owners", "memory")
for h, m, q, o in (("الروضه", "101", 12, "أحمد"), ("الروضة ", "101", 13, "سارة"), ("الروضة", "101", 13, "خالد"),
                   ("al  nakheel", "202", 5, "Omar")):
    f = QgsFeature(owners.fields())
    f.setAttributes([h, m, q, o])
    owners.dataProvider().addFeatures([f])
QgsProject.instance().addMapLayers([parcels, owners])
base = {"INPUT": parcels, "FIELDS": ["district", "plan", "no"], "JOIN": owners,
        "JOIN_FIELDS": ["hay", "mukhattat", "qeta"], "COPY": ["owner"], "PREFIX": "",
        "OUTPUT": "memory:", "UNMATCHED": "memory:"}


def run_join(**kw):
    params = dict(base)
    params.update(kw)
    res = processing.run("biruni:multijoin", params, context=ctx, feedback=fb)
    out = res["OUTPUT"] if not isinstance(res["OUTPUT"], str) else ctx.getMapLayer(res["OUTPUT"])
    return res, out


res, out = run_join(METHOD=2, MODE=0)
owners_by = {(f["district"], f["no"]): f["owner"] for f in out.getFeatures()}
check(res["MATCHED"] == 3 and res["UNMATCHED_COUNT"] == 2 and owners_by[("الروضة", "12")] == "أحمد"
      and owners_by[("Al Nakheel", "5")] == "Omar", "Join on 3 fields, Arabic forms and case ignored")
um = res["UNMATCHED"] if not isinstance(res["UNMATCHED"], str) else ctx.getMapLayer(res["UNMATCHED"])
check(um.featureCount() == 2, "Unmatched features listed (no match + empty key)")
res, out = run_join(METHOD=0, MODE=0)
check(res["MATCHED"] == 1, "Exact matching is strict (only الروضة/101/13 matches exactly)")
res, out = run_join(METHOD=2, MODE=1)
check(res["ROWS"] == 6 and res["MULTIPLE"] == 1, "One row per match (13 has two owners)")
res, out = run_join(METHOD=2, MODE=2)
row = [f for f in out.getFeatures() if f["no"] == "13"][0]
check(row["match_count"] == 2 and row["owner"] == "سارة | خالد", "Count and list the matches")
res, out = run_join(METHOD=2, MODE=0, DISCARD=True)
check(out.featureCount() == 3, "Features without a match left out")
try:
    run_join(JOIN_FIELDS=["hay", "mukhattat"])
    check(False, "Unequal key lists refused")
except Exception:  # noqa: BLE001
    check(True, "Unequal key lists refused")

# live join
tkey, jkey = tt.create_live_join(parcels, owners, [("district", "hay"), ("plan", "mukhattat"), ("no", "qeta")],
                                 fields=["owner"], prefix="j_", normalise=True)
vals = {(f["district"], f["no"]): f["j_owner"] for f in parcels.getFeatures()}
check(vals[("الروضة", "12")] == "أحمد" and vals[("Al Nakheel", "5")] == "Omar" and vals[(None, "12")] is None,
      f"Live join on three fields: {vals}")
owners.startEditing()
o = [f for f in owners.getFeatures() if f["owner"] == "Omar"][0]
owners.changeAttributeValue(o.id(), owners.fields().indexOf("owner"), "Omar Ali")
owners.commitChanges()
parcels.dataProvider().reloadData()
for j in parcels.vectorJoins():
    pass
parcels.reload()
vals = {(f["district"], f["no"]): f["j_owner"] for f in parcels.getFeatures()}
check(vals[("Al Nakheel", "5")] in ("Omar Ali", "Omar"), "Live join follows edits (after a refresh)")
check(tt.remove_live_joins(parcels) == 1 and parcels.fields().indexOf(tkey) < 0
      and owners.fields().indexOf(jkey) < 0 and not parcels.vectorJoins(), "Live join removed with its key fields")

# ---- saved selections and history ----------------------------------------------------
ss.install()
sel = memory_layer("sel")
QgsProject.instance().addMapLayer(sel)
ids = sorted(f.id() for f in sel.getFeatures())
sel.selectByIds(ids[:2])
sel.selectByIds(ids[2:])
sel.selectByIds([ids[0]])
check(ss.go(sel, -1) and set(sel.selectedFeatureIds()) == set(ids[2:]), "Select previous")
check(ss.go(sel, -1) and set(sel.selectedFeatureIds()) == set(ids[:2]), "Select previous again")
check(ss.go(sel, 1) and set(sel.selectedFeatureIds()) == set(ids[2:]), "Select next")
sel.selectByIds([ids[3]])
check(not ss.can_go(sel, 1), "A new selection drops the redo branch")
for _ in range(30):
    sel.selectByIds([ids[_ % 4]]) if _ % 2 else sel.removeSelection()
check(len(ss._history[sel.id()]["stack"]) <= ss.HISTORY_SIZE, "History capped at 20")
sel.selectByIds(ids[:3])
ss.save_set(sel, "north")
ss.save_set(sel, "big floors", expression='"b" > 5')
sel.selectByIds([ids[3]])
check(ss.restore_set(sel, "north", "add") == 4, "Saved set added to the selection")
check(ss.restore_set(sel, "north", "remove") == 1 and sel.selectedFeatureIds() == [ids[3]],
      "Saved set removed from the selection")
check(ss.restore_set(sel, "big floors") == 2, "Expression set evaluated again (7 and 9)")
sel.selectByIds(ids[:2])
check(ss.restore_set(sel, "big floors", "intersect") == 1, "Keep only the common ones")
path = os.path.join(TMP, "sets.qgz")
QgsProject.instance().write(path)
QgsProject.instance().clear()
QgsProject.instance().read(path)
again = [l for l in QgsProject.instance().mapLayers().values() if l.name() == "sel"]
check(again and [s["name"] for s in ss.sets(again[0])] == ["north", "big floors"], "Saved sets kept in the project")
check(ss.delete_set(again[0], "north") == 1 and len(ss.sets(again[0])) == 1, "Saved set deleted")
ss.uninstall()

# ---- dialog builds offscreen ------------------------------------------------------------
from qgis.PyQt.QtWidgets import QWidget  # noqa: E402


class FakeIface:
    def __init__(self):
        self.w = QWidget()

    def mainWindow(self):
        return self.w

    def messageBar(self):
        return None


class FakePlugin:
    iface = FakeIface()


from biruni.table_tools_dialog import TableToolsDialog  # noqa: E402

lyr = memory_layer("dlg")
QgsProject.instance().addMapLayer(lyr)
dlg = TableToolsDialog(FakePlugin(), lyr, 1)
check(dlg.tree.topLevelItemCount() == 4 and dlg.tabs.count() == 3 and dlg.tabs.currentIndex() == 1,
      "Dialog builds with the fields and three tabs")
dlg.tree.setCurrentItem(dlg.tree.topLevelItem(3))
dlg.tree.topLevelItem(3).setSelected(True)
dlg._move(-1)
check([dlg.tree.topLevelItem(i).text(0) for i in range(4)] == ["a", "b", "d", "c"], "Dialog: move a field up")
dlg._sort_az()
check([dlg.tree.topLevelItem(i).text(0) for i in range(4)] == ["a", "b", "c", "d"], "Dialog: sort A-Z")

shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
