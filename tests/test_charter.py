r"""The Data Charter (charter/): fields, value lists, kinds, rules, linked choices, learning a charter from the
data, health, the data dictionary, charter files, the phone form, the change log and the window.

Run:  engine\bin\python-qgis.bat tests\test_charter.py
      (set BIRUNI_SHOTS=<folder> to keep the pictures of the window)
"""
import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsFeature, QgsFieldConstraints, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProject, QgsVectorFileWriter, QgsVectorLayer, QgsVectorLayerUtils)

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()
from qgis.gui import QgsGui  # noqa: E402

QgsGui.editorWidgetRegistry().initEditors()

from biruni import i18n  # noqa: E402

i18n.init("en_US")
import biruni.charter  # noqa: E402,F401  - importing the package does nothing

from biruni.charter import apply as ca  # noqa: E402
from biruni.charter import dictionary as cd  # noqa: E402
from biruni.charter import health as ch  # noqa: E402
from biruni.charter import learn as cl  # noqa: E402
from biruni.charter import model as cm  # noqa: E402
from biruni.charter import travel as ct  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()
project = QgsProject.instance()


def gpkg_layer(name, spec, rows, geom="Point"):
    mem = QgsVectorLayer(f"{geom}?crs=EPSG:32638&" + "&".join(f"field={s}" for s in spec), name, "memory")
    feats = []
    for n, r in enumerate(rows):
        f = QgsFeature(mem.fields())
        f.setAttributes(list(r))
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(500000 + n * 10, 2700000 + n * 7)))
        feats.append(f)
    mem.dataProvider().addFeatures(feats)
    path = os.path.join(work, name + ".gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName, opts.layerName = "GPKG", name
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, project.transformContext(), opts)
    assert err == QgsVectorFileWriter.WriterError.NoError, msg
    lyr = QgsVectorLayer(f"{path}|layername={name}", name, "ogr")
    assert lyr.isValid()
    project.addMapLayer(lyr)
    return lyr


def widget(layer, name):
    s = layer.editorWidgetSetup(layer.fields().indexOf(name))
    return s.type(), s.config()


# ---- 1. fields: add / rename / alias / required / default / unit / order on a GeoPackage -------------------
parcels = gpkg_layer("parcels", ["code:integer", "name:string(50)", "use:string(20)", "area:double", "floors:integer",
                                 "tmp:string"],
                     [(1, "Ali", "res", 300.5, 2, "x"), (2, "Omar", "com", 410.0, 4, "y"), (3, "Huda", "res", 280.0, 1, "")])
charter, seeded = cm.read_charter(parcels)
check(not seeded and [f["name"] for f in charter["fields"]][:2] == ["fid", "code"], "a new charter starts from the layer")
spec = {f["name"]: f for f in charter["fields"]}
spec["name"]["name"] = "owner"
spec["name"].update(alias_ar="المالك", alias_en="Owner")
spec["use"].update(alias_ar="الاستخدام", alias_en="Land use", required=True, desc="What the parcel is used for")
spec["floors"].update(default="1", alias_ar="عدد الأدوار", alias_en="Floors")
spec["area"].update(auto="area", unit="m²", alias_ar="المساحة", alias_en="Area")
charter["fields"].append(cm.field_spec("price", orig="", type="decimal", unit="SAR", alias_ar="السعر",
                                       alias_en="Price"))
charter["fields"].append(cm.field_spec("notes", orig="", type="text", length=100))
charter["fields"] = [f for f in charter["fields"] if f["name"] != "tmp"]
# notes before price: the order follows the grid
charter["fields"][-2], charter["fields"][-1] = charter["fields"][-1], charter["fields"][-2]
stored, text = ca.apply_charter(parcels, charter, {}, deleted=["tmp"], section="fields")
names = [f.name() for f in parcels.fields()]
check("owner" in names and "name" not in names and "tmp" not in names and "price" in names and "notes" in names,
      f"fields added, renamed and deleted in the GeoPackage ({names})")
check(names.index("notes") < names.index("price"), "new fields in the order of the grid")
vals = {f["code"]: f["owner"] for f in parcels.getFeatures()}
check(vals == {1: "Ali", 2: "Omar", 3: "Huda"}, "renaming keeps the values")
check(parcels.fields().field("notes").length() == 100, "text length of a new field")
i_use = parcels.fields().indexOf("use")
check(parcels.attributeAlias(i_use) == "Land use", "English alias in English")
check(parcels.attributeAlias(parcels.fields().indexOf("price")) == "Price (SAR)", "the unit is shown with the alias")
check(bool(parcels.fields().at(i_use).constraints().constraints() & QgsFieldConstraints.Constraint.ConstraintNotNull),
      "required = not null constraint")
i_fl = parcels.fields().indexOf("floors")
check(parcels.defaultValueDefinition(i_fl).expression() == "1", "a fixed default value")
check("$area" in parcels.defaultValueDefinition(parcels.fields().indexOf("area")).expression(),
      "automatic area from the attribute rules")
f = QgsVectorLayerUtils.createFeature(parcels, QgsGeometry(), {})
check(f["floors"] == 1, "a new feature gets the default")
check(os.path.isdir(os.path.join(work, ".biruni-backup")), "the GeoPackage was backed up before the columns changed")
check(cm.stored_charter(parcels)["fields"][0]["name"] == "fid" and
      cm.find_field(cm.stored_charter(parcels), "use")["desc"] == "What the parcel is used for",
      "the charter is kept on the layer")
# bilingual aliases follow the interface language
i18n.init("ar")
ca.sync_language()
check(parcels.attributeAlias(i_use) == "الاستخدام", "Arabic interface: the Arabic alias")
i18n.init("en_US")
ca.sync_language()
check(parcels.attributeAlias(i_use) == "Land use", "back to English")
# a text file cannot change its columns
csv = os.path.join(work, "points.csv")
with open(csv, "w", encoding="utf-8") as fh:
    fh.write("id,x,y\n1,1,1\n")
csv_layer = QgsVectorLayer(f"file:///{csv.replace(os.sep, '/')}?delimiter=,&xField=x&yField=y", "points", "delimitedtext")
ok, why = ca.can_alter(csv_layer)
check(not ok and "GeoPackage" in why, "a CSV cannot change its columns: explained, with the GeoPackage way out")
project.addMapLayer(csv_layer)
gp = ca.rebuild_into_gpkg(csv_layer, os.path.join(work, "rebuilt.gpkg"))
check(csv_layer.providerType() == "ogr" and ca.can_alter(csv_layer)[0] and os.path.exists(gp),
      "rebuilt into a GeoPackage, the columns can change now")

# ---- 2. one value list for two layers -------------------------------------------------------------------------
landuse = cm.new_list("استخدام الأرض", "Land use", "codes", [
    {"code": "res", "ar": "سكني", "en": "Residential", "color": "#F5D76E"},
    {"code": "com", "ar": "تجاري", "en": "Commercial", "color": "#E74C3C"},
    {"code": "mix", "ar": "مختلط", "en": "Mixed"}], lid="landuse")
floors_range = cm.new_list("الأدوار", "Floors", "range", lid="floors", min=1, max=30)
c = cm.stored_charter(parcels)
cm.find_field(c, "use")["list"] = "landuse"
cm.find_field(c, "floors")["list"] = "floors"
ca.apply_charter(parcels, c, {"landuse": landuse, "floors": floors_range}, section="lists")
buildings = gpkg_layer("buildings", ["bid:integer", "use:string(10)", "height:double"],
                       [(1, "res", 6.0), (2, "com", 12.0), (3, "bad", 9.0)])
bc, _ = cm.read_charter(buildings)
cm.find_field(bc, "use")["list"] = "landuse"
ca.apply_charter(buildings, bc, cm.project_lists(), section="lists")
for lyr in (parcels, buildings):
    t, cfg = widget(lyr, "use")
    check(t == "ValueMap" and cfg["map"] == [{"Residential": "res"}, {"Commercial": "com"}, {"Mixed": "mix"}],
          f"{lyr.name()}: the list as a value map with English labels")
    check("'res', 'com', 'mix'" in lyr.constraintExpression(lyr.fields().indexOf("use")),
          f"{lyr.name()}: the list is checked by a constraint")
check(sorted(f for _l, f in [(l.name(), f) for l, f in cm.list_usages("landuse")]) == ["use", "use"],
      "the list knows the two layers that use it")
t, cfg = widget(parcels, "floors")
check(t == "Range" and cfg["Min"] == 1 and cfg["Max"] == 30, "a range list as a range widget")
bad = [ft for ft in buildings.getFeatures() if ft["use"] == "bad"][0]
ok, errs = QgsVectorLayerUtils.validateAttribute(buildings, bad, buildings.fields().indexOf("use"))
check(not ok, "a value outside the list breaks the constraint")
i18n.init("ar")
ca.sync_language()
check(widget(buildings, "use")[1]["map"][0] == {"سكني": "res"}, "Arabic interface: Arabic labels in the list")
i18n.init("en_US")
project.setDirty(False)
ca.sync_language()
check(not project.isDirty(), "following the language does not mark the project as changed")

# ---- 3. kinds ---------------------------------------------------------------------------------------------------
fac = gpkg_layer("facilities", ["type:string", "floors:integer", "beds:integer", "kind:string", "name:string"],
                 [("primary", 2, None, "school", "A"), ("clinic", 5, 20, "hospital", "B"),
                  ("clinic", 2, None, "school", "C"), ("general", 4, None, "hospital", "D")])
fc, _ = cm.read_charter(fac)
fc["kind_field"] = "kind"
fc["kinds"] = [
    {"code": "school", "ar": "مدرسة", "en": "School", "color": "#2563EB", "defaults": {"floors": "2"},
     "lists": {"type": ["primary", "secondary"]}, "required": []},
    {"code": "hospital", "ar": "مستشفى", "en": "Hospital", "color": "#DC2626", "defaults": {"floors": "5"},
     "lists": {"type": ["general", "clinic"]}, "required": ["beds"]}]
lists = cm.project_lists()
lists["factype"] = cm.new_list("نوع المنشأة", "Facility type", "codes", [
    {"code": "primary", "ar": "ابتدائي", "en": "Primary"}, {"code": "secondary", "ar": "ثانوي", "en": "Secondary"},
    {"code": "general", "ar": "عام", "en": "General"}, {"code": "clinic", "ar": "عيادة", "en": "Clinic"}], lid="factype")
cm.find_field(fc, "type")["list"] = "factype"
ca.apply_charter(fac, fc, lists, section="kinds")
i_fl = fac.fields().indexOf("floors")
check(fac.defaultValueDefinition(i_fl).expression() == "CASE WHEN \"kind\" = 'school' THEN 2 WHEN \"kind\" = "
      "'hospital' THEN 5 ELSE NULL END", "a default value per kind")
k_idx = fac.fields().indexOf("kind")
f_school = QgsVectorLayerUtils.createFeature(fac, QgsGeometry(), {k_idx: "school"})
f_hosp = QgsVectorLayerUtils.createFeature(fac, QgsGeometry(), {k_idx: "hospital"})
check(f_school["floors"] == 2 and f_hosp["floors"] == 5, "createFeature gets the default of the kind")
t, cfg = widget(fac, "type")
check(t == "ValueRelation" and "current_value('kind') = 'school' THEN \"code\" IN ('primary', 'secondary')"
      in cfg["FilterExpression"] and "\"list\" = 'factype'" in cfg["FilterExpression"],
      "the allowed list per kind: a value relation filtered on current_value('kind')")
check(cfg["Value"] == "label_en" and cfg["Key"] == "code" and project.mapLayer(cfg["Layer"]) is not None,
      "the value relation points to the lookup table of the project")
t, cfg = widget(fac, "kind")
check(t == "ValueMap" and cfg["map"] == [{"School": "school"}, {"Hospital": "hospital"}], "the kind field lists the kinds")
bad = [ft for ft in fac.getFeatures() if ft["name"] == "C"][0]
check(not QgsVectorLayerUtils.validateAttribute(fac, bad, fac.fields().indexOf("type"))[0],
      "a clinic is not allowed for a school")
good = [ft for ft in fac.getFeatures() if ft["name"] == "A"][0]
check(QgsVectorLayerUtils.validateAttribute(fac, good, fac.fields().indexOf("type"))[0], "a primary school is fine")
nobeds = [ft for ft in fac.getFeatures() if ft["name"] == "D"][0]
check(not QgsVectorLayerUtils.validateAttribute(fac, nobeds, fac.fields().indexOf("beds"))[0] and
      QgsVectorLayerUtils.validateAttribute(fac, good, fac.fields().indexOf("beds"))[0],
      "beds required for a hospital only")
r = fac.renderer()
i18n.init("ar")
ca.sync_language()
check([c.label() for c in fac.renderer().categories()][:2] == ["مدرسة", "مستشفى"] and
      fac.renderer().categories()[0].symbol().color().name() == "#2563eb", "kind labels follow the language, colours kept")
i18n.init("en_US")
ca.sync_language()
r = fac.renderer()
check(r.type() == "categorizedSymbol" and r.classAttribute() == "kind" and
      [c.label() for c in r.categories()][:2] == ["School", "Hospital"] and
      r.categories()[0].symbol().color().name() == "#2563eb", "a symbol per kind")

# ---- 4. linked choices --------------------------------------------------------------------------------------------
addr = gpkg_layer("addresses", ["gov:string", "district:string", "street:string"],
                  [("Riyadh", "Olaya", "a"), ("Riyadh", "Rawdah", "b"), ("Jeddah", "Rawdah", "c"), (None, "Olaya", "d")])
ac, _ = cm.read_charter(addr)
lists = cm.project_lists()
lists["gov"] = cm.new_list("المنطقة", "Governorate", "codes", [{"code": "Riyadh", "ar": "الرياض", "en": "Riyadh"},
                                                              {"code": "Jeddah", "ar": "جدة", "en": "Jeddah"}], lid="gov")
lists["district"] = cm.new_list("الحي", "District", "codes", [{"code": "Olaya", "ar": "العليا", "en": "Olaya"},
                                                             {"code": "Malaz", "ar": "الملز", "en": "Malaz"},
                                                             {"code": "Rawdah", "ar": "الروضة", "en": "Rawdah"}],
                                lid="district")
cm.find_field(ac, "gov")["list"] = "gov"
cm.find_field(ac, "district")["list"] = "district"
ac["links"] = [{"id": "Kgovdist", "parents": ["gov"], "child": "district",
                "pairs": {"Riyadh": ["Olaya", "Malaz"], "Jeddah": ["Rawdah"]}}]
ca.apply_charter(addr, ac, lists, section="links")
t, cfg = widget(addr, "district")
check(t == "ValueRelation" and cfg["FilterExpression"] ==
      "\"list\" = 'Kgovdist' AND \"parent\" = coalesce(to_string(current_value('gov')), '')",
      "the district list follows current_value('gov')")
look = project.mapLayer(cfg["Layer"])
rows = sorted((f["parent"], f["code"]) for f in look.getFeatures() if f["list"] == "Kgovdist")
check(look is not None and look.customProperty(ca.CHOICES_PROP) and
      rows == [("Jeddah", "Rawdah"), ("Riyadh", "Malaz"), ("Riyadh", "Olaya")],
      "the lookup table holds the pairs")
check(look.providerType() == "ogr" and look.source().lower().endswith("layername=" + ca.CHOICES_TABLE),
      "the lookup table is kept in a GeoPackage (travels to QField)")
check([f["label_ar"] for f in look.getFeatures() if f["list"] == "Kgovdist" and f["code"] == "Olaya"] == ["العليا"],
      "the lookup table has the Arabic labels")
r2 = [ft for ft in addr.getFeatures() if ft["street"] == "b"][0]
check(not QgsVectorLayerUtils.validateAttribute(addr, r2, addr.fields().indexOf("district"))[0],
      "Rawdah is not a district of Riyadh")
r1 = [ft for ft in addr.getFeatures() if ft["street"] == "a"][0]
check(QgsVectorLayerUtils.validateAttribute(addr, r1, addr.fields().indexOf("district"))[0], "Olaya is")

# ---- 5. validation rules ------------------------------------------------------------------------------------------
c = cm.stored_charter(parcels)
c["rules"] = [{"id": "R1", "expr": "\"floors\" <= 3 OR \"use\" <> 'res'", "ar": "السكني ٣ أدوار بالكتير",
               "en": "Residential: at most 3 floors", "level": "error", "field": ""},
              {"id": "R2", "expr": "\"area\" >= 290", "ar": "مساحة صغيرة", "en": "Small parcel", "level": "warning",
               "field": ""}]
ca.apply_charter(parcels, c, cm.project_lists(), section="rules")
parcels.startEditing()
for ft in parcels.getFeatures():
    if ft["code"] == 1:
        parcels.changeAttributeValue(ft.id(), parcels.fields().indexOf("floors"), 5)
parcels.commitChanges()
i_fl = parcels.fields().indexOf("floors")
check("Residential: at most 3 floors" in parcels.constraintDescription(i_fl), "the rule's message on its field")
f1 = [ft for ft in parcels.getFeatures() if ft["code"] == 1][0]
check(not QgsVectorLayerUtils.validateAttribute(parcels, f1, i_fl)[0], "a rule across fields is checked in the form")
rep = ch.evaluate(parcels)
by = {it.id: it for it in rep.items}
check(by.get("rule:R1") is not None and by["rule:R1"].count == 1 and by["rule:R1"].level == "error",
      "health: one feature breaks the rule")
check(by.get("rule:R2") is not None and by["rule:R2"].count == 1 and by["rule:R2"].level == "warning",
      "health: one warning")
check(rep.total == 3 and rep.bad_errors == 1 and rep.bad_warnings == 1 and rep.score == 50,
      f"health score ({rep.score})")
rep = ch.evaluate(buildings)
check([(it.id, it.count) for it in rep.items] == [("list:use", 1)], "health: a value outside its list")
rep = ch.evaluate(fac)
ids = {it.id: it.count for it in rep.items}
check(ids.get("kindlist:type") == 1 and ids.get("kindreq:beds") == 1, f"health: kinds ({ids})")
rep = ch.evaluate(addr)
check({it.id: it.count for it in rep.items} == {"link:Kgovdist": 1}, "health: linked choices")

# ---- 6. learn the charter from messy data --------------------------------------------------------------------------
import random  # noqa: E402

rnd = random.Random(7)
rows = []
for n in range(40):
    kind = ["مدرسة", "مستشفى", "مسجد"][n % 3]
    if n in (3, 9):
        kind = "مدرسه"
    if n == 15:
        kind = " مدرسة "
    status = "ABC"[rnd.randrange(3)]
    height = 10 + (n % 7) * 1.5
    if n == 20:
        height = 900.0
    rows.append((f"F{n:03d}", kind, None, status, height, f"Name {n}"))
for i, r in enumerate(rows):
    canon = r[1].strip().replace("مدرسه", "مدرسة")
    t = {"مدرسة": ["ابتدائي", "متوسط", "ثانوي"], "مستشفى": ["عام", "تخصصي"], "مسجد": ["جامع", "مصلى"]}[canon]
    rows[i] = (r[0], r[1], t[(i // 3) % len(t)], r[3], r[4], r[5])
messy = QgsVectorLayer("Point?crs=EPSG:4326&field=uid:string&field=kind:string&field=type:string&field=status:string"
                       "&field=height:double&field=name:string", "messy", "memory")
feats = []
for r in rows:
    ft = QgsFeature(messy.fields())
    ft.setAttributes(list(r))
    ft.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(31, 30)))
    feats.append(ft)
messy.dataProvider().addFeatures(feats)
project.addMapLayer(messy)
props = cl.infer(messy, {})
if os.environ.get("CHARTER_DEBUG"):
    for p in props:
        print("  PROPOSAL", p.kind, p.field, round(p.confidence, 2), p.checked, p.title, "|", p.detail[:120])
kinds = {}
for p in props:
    kinds.setdefault(p.kind, []).append(p)
merges = kinds.get("merge", [])
check(len(merges) == 1 and merges[0].field == "kind" and merges[0].data["canonical"] == "مدرسة" and
      sorted(merges[0].data["values"]) == sorted(["مدرسه", " مدرسة "]),
      "spelling variants «مدرسه» and « مدرسة » merge into «مدرسة»")
lst = {p.field: p for p in kinds.get("list", [])}
check(set(lst) == {"kind", "type", "status"} and sorted(v for v, _c in lst["status"].data["items"]) == ["A", "B", "C"] and
      [v for v, _c in lst["kind"].data["items"]] == ["مدرسة", "مستشفى", "مسجد"],
      f"code-like fields become value lists ({sorted(lst)})")
rng = {p.field: p for p in kinds.get("range", [])}
check(set(rng) == {"height"} and rng["height"].data["outliers"] == [900.0] and rng["height"].data["max"] < 900 and
      len(rng["height"].fids) == 1, "the outlier 900 is flagged, the range is proposed without it")
req = {p.field for p in kinds.get("required", [])}
check(req == {"uid", "kind", "type", "status", "height", "name"}, f"fields never empty: required? ({sorted(req)})")
uni = {p.field: p for p in kinds.get("unique", [])}
check(set(uni) == {"uid", "name"} and uni["uid"].checked and not uni["name"].checked,
      "unique-looking fields: «uid» ticked, «name» offered")
kp = kinds.get("kind", [])
check(len(kp) == 1 and kp[0].data["kind_field"] == "kind" and kp[0].data["child"] == "type" and
      sorted(kp[0].data["pairs"]["مدرسة"]) == sorted(["ابتدائي", "متوسط", "ثانوي"]) and kp[0].confidence > 0.6,
      "kind decides type: allowed values per kind")
check(not kinds.get("link"), "no false linked choices (status is random)")
undo_before = messy.undoStack().count() if messy.undoStack() else 0
accepted, new_lists, changed = cl.accept(messy, props, lists={}, fix_data=True)
check(changed == 3, f"3 spellings fixed ({changed})")
check(messy.isEditable() and messy.undoStack().count() == undo_before + 1, "in ONE undo step")
vals = sorted({ft["kind"] for ft in messy.getFeatures()})
check(vals == ["مدرسة", "مستشفى", "مسجد"], "the data now has one spelling")
messy.undoStack().undo()
check(sorted({ft["kind"] for ft in messy.getFeatures()}) == sorted({"مدرسة", "مدرسه", " مدرسة ", "مستشفى", "مسجد"}),
      "undo puts the old spellings back")
messy.undoStack().redo()
check(accepted["kind_field"] == "kind" and len(accepted["kinds"]) == 3 and
      sorted(cm.find_kind(accepted, "مدرسة")["lists"]["type"]) == sorted(["ابتدائي", "متوسط", "ثانوي"]),
      "accepted: kinds with their allowed types")
check(cm.find_field(accepted, "uid")["unique"] and not cm.find_field(accepted, "name")["unique"] and
      cm.find_field(accepted, "kind")["required"], "accepted: unique and required")
hl = new_lists[cm.find_field(accepted, "height")["list"]]
check(hl["kind"] == "range" and hl["max"] < 900, "accepted: the range list")
messy.commitChanges()
ca.apply_charter(messy, accepted, new_lists, section="learn")
rep = ch.evaluate(messy)
check({it.id: it.count for it in rep.items}.get("range:height") == 1, "the outlier shows in the health")

# ---- 7. the health watcher ------------------------------------------------------------------------------------------
watch = ch.HealthWatcher(buildings, delay=10)
got = []
watch.updated.connect(got.append)
buildings.startEditing()
for ft in buildings.getFeatures():
    if ft["use"] == "bad":
        buildings.changeAttributeValue(ft.id(), buildings.fields().indexOf("use"), "res")
for _ in range(50):
    app.processEvents()
    if got:
        break
    import time
    time.sleep(0.02)
check(got and got[-1].items == [] and got[-1].score == 100, "the health follows the edits (debounced)")
buildings.rollBack()
watch.stop()

# ---- 8. the data dictionary -----------------------------------------------------------------------------------------
html_path = cd.write_html(os.path.join(work, "dictionary.html"))
doc = open(html_path, encoding="utf-8").read()
for needle, what in (("Data dictionary", "English title"), ("قاموس البيانات", "Arabic title"),
                     ("الاستخدام", "Arabic alias"), ("Land use", "English alias"), ("What the parcel is used for",
                                                                                    "description"),
                     ("SAR", "unit"), ("Residential", "English list label"), ("سكني", "Arabic list label"),
                     ("مستشفى", "Arabic kind"), ("Hospital", "English kind"), ("Residential: at most 3 floors",
                                                                               "rule message"),
                     ("السكني ٣ أدوار بالكتير", "Arabic rule message"), ("الاختيارات المترابطة", "linked choices"),
                     ("Riyadh", "pairs"), ("صحة البيانات", "health"), ("buildings.use, parcels.use", "list usage"),
                     ("سجل التغييرات", "change log"), ("charset=utf-8", "Word-readable encoding")):
    check(needle in doc, f"dictionary: {what}")

# ---- 9. export / import round trip --------------------------------------------------------------------------------
path = os.path.join(work, "parcels.charter.json")
ct.export_charter(parcels, path)
doc = json.load(open(path, encoding="utf-8"))
check(doc["format"] == "biruni-charter" and set(doc["lists"]) == {"landuse", "floors"}, "the charter file")
other = gpkg_layer("parcels_2026", ["CODE:integer", "Owner:string", "USE:string", "area:double", "floors:integer"],
                   [(9, "Sara", "res", 500.0, 2)])
c2, lists2, src = ct.read_charter_file(path)
mapping = ct.match_fields(c2, other)
check(src == "parcels" and mapping["use"] == "USE" and mapping["code"] == "CODE" and mapping["owner"] == "Owner" and
      mapping["notes"] == "", f"fields matched by name ({mapping})")
stored2, text2 = ct.apply_file_to_layer(path, other)
names2 = [f.name() for f in other.fields()]
check("notes" in names2 and "price" in names2, "missing fields were added")
check(widget(other, "USE")[0] == "ValueMap" and other.attributeAlias(other.fields().indexOf("USE")) == "Land use",
      "the list and the alias came along")
check("\"USE\" <> 'res'" in other.constraintExpression(other.fields().indexOf("floors")), "rules renamed to the fields")
check(cm.stored_charter(other)["fields"] and json.dumps(ct.charter_document(other)["lists"]["landuse"], sort_keys=True)
      == json.dumps(landuse, sort_keys=True), "the round trip keeps the list")

# ---- 10. the phone form ----------------------------------------------------------------------------------------------
from biruni.field_photos.server import clean_form  # noqa: E402

form = ct.phone_form(cm.stored_charter(parcels))
check(form == clean_form(form), "the phone form is in field_photos.server.clean_form format")
q = {f["name"]: f for f in form}
check(q["use"]["type"] == "choice" and q["use"]["choices"] == ["Residential", "Commercial", "Mixed"] and q["use"]["req"],
      "a list becomes a required choice")
check(q["floors"]["type"] == "integer" and q["floors"]["min"] == 1 and q["floors"]["max"] == 30, "a range: limits")
check(q["price"]["type"] == "number" and q["price"]["label"] == "Price (SAR)", "a decimal: number, label with unit")
check("area" not in q and "fid" not in q, "automatic fields and the key are not asked")
form_k = ct.phone_form(cm.stored_charter(fac))
check([f["choices"] for f in form_k if f["label"] == "kind"] == [["School", "Hospital"]], "the kinds as a choice")

# ---- 11. the change log ----------------------------------------------------------------------------------------------
log = cm.read_log()
if os.environ.get("CHARTER_DEBUG"):
    for e in log:
        print("  LOG", e)
check(len(log) >= 6 and any("owner" in e["what"] and "renamed" in e["what"] for e in log) and
      all(e.get("time") and "layer" in e for e in log), f"the change log ({len(log)} lines)")
check(any(e["section"] == "lists" and "Land use" in e["what"] for e in log), "the log names the lists")

# ---- 12. the window (offscreen pictures: look at them) ---------------------------------------------------------------
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMainWindow  # noqa: E402

from biruni.theme import TOKENS  # noqa: E402

SHOTS = os.environ.get("BIRUNI_SHOTS") or os.path.join(work, "shots")
os.makedirs(SHOTS, exist_ok=True)


class Bar:
    def __getattr__(self, name):
        return lambda *a, **k: None


class Iface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.active = None

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return Bar()

    def activeLayer(self):
        return self.active


class Theme:
    mode = "light"

    def tokens(self):
        return TOKENS[self.mode]

    def on_change(self, fn):
        pass


class Plugin:
    def __init__(self):
        self.iface = Iface()
        self.theme = Theme()


from biruni.charter import dialog as cw  # noqa: E402

from qgis.PyQt.QtGui import QFont  # noqa: E402

QApplication.setFont(QFont("Segoe UI", 9))  # the offscreen platform picks an odd font otherwise
plugin = Plugin()
plugin.iface.active = parcels


def shoot(win, name):
    win.resize(1280, 760)
    for _ in range(3):
        app.processEvents()
    path = os.path.join(SHOTS, name + ".png")
    win.grab().save(path)
    return os.path.getsize(path) > 10000


w = cw.open_charter(plugin, "fields")
w.ask = False
check(w.layer is parcels and w.badge.text().startswith("Health ") and "%" in w.badge.text(), "the window: layer and badge")
check(shoot(w, "en_light_fields"), "picture: fields (English, light)")
fp = w.pages["fields"]
n_rows = fp.table.rowCount()
fp.add_field()
fp.table.item(fp.table.rowCount() - 1, 0).setText("survey_date")
fp.table.cellWidget(fp.table.rowCount() - 1, 3).setCurrentIndex(4)  # date
fp.table.item(fp.table.rowCount() - 1, 2).setText("Survey date")
check(w.apply() and parcels.fields().indexOf("survey_date") >= 0 and
      parcels.fields().field("survey_date").type() in (14, 16), "the grid adds a date field")
check(fp.table.rowCount() == n_rows + 1, "the grid shows it after Apply")
w.show_section("lists")
check(shoot(w, "en_light_lists"), "picture: value lists")
lp = w.pages["lists"]
lid = lp.list_from_field("owner")
check(lid in w.lists and len(w.lists[lid]["items"]) == 3, "a list made from a field's values")
w.load_layer(fac)
w.show_section("kinds")
check(w.pages["kinds"].kinds.rowCount() == 2 and w.pages["kinds"].rules.rowCount() >= 3, "kinds and their rules")
check(shoot(w, "en_light_kinds"), "picture: kinds")
w.load_layer(parcels)
w.show_section("rules")
counts = w.pages["rules"].count()
check(counts.get("rule:R1") == 1, "rules: count the features that break a rule")
check(shoot(w, "en_light_rules"), "picture: rules")
w.load_layer(addr)
w.show_section("links")
check(w.pages["links"].pairs.rowCount() == 2, "linked choices shown")
w.pages["links"].fill_from_data()
check(w.pages["links"].pairs.rowCount() == 2 and "Olaya" in w.pages["links"].pairs.item(0, 1).text() + w.pages["links"].pairs.item(1, 1).text(),
      "fill the pairs from the data")
check(shoot(w, "en_light_links"), "picture: linked choices")
w.load_layer(messy)
w.show_section("learn")
props = w.pages["learn"].scan()
check(len(props) >= 1 and w.pages["learn"].table.rowCount() == len(props), f"learn: proposals in the table ({len(props)})")
check(shoot(w, "en_light_learn"), "picture: learn from the data")
w.show_section("health")
check(shoot(w, "en_light_health"), "picture: health")
w.load_layer(parcels)
w.show_section("health")
check(w.pages["health"].table.rowCount() >= 2, "health table")
w.pages["health"]._select(0, 0)
check(parcels.selectedFeatureCount() >= 1, "clicking a problem selects its features")
w.show_section("share")
check("questions" in w.pages["share"].form_info.text(), "the phone form summary")
w.next_path = [os.path.join(work, "ui_dict.html")]
check(w.pages["share"].dictionary() and os.path.getsize(os.path.join(work, "ui_dict.html")) > 3000,
      "the dictionary from the window")
form = w.pages["share"].use_phone_form()
from qgis.core import QgsSettings  # noqa: E402

check(json.loads(QgsSettings().value("biruni/field-photos/form")) == form, "the phone form handed to Field Photos")
check(shoot(w, "en_light_share"), "picture: dictionary and sharing")
w.show_section("log")
check(w.pages["log"].table.rowCount() >= 5, "the change log in the window")
check(shoot(w, "en_light_log"), "picture: change log")
from biruni.theme import build_palette  # noqa: E402

plugin.theme.mode = "dark"
QApplication.setPalette(build_palette(TOKENS["dark"]))
w.refresh_theme()
w.show_section("fields")
check(shoot(w, "en_dark_fields"), "picture: fields (dark)")
w.load_layer(messy)
w.show_section("learn")
w.pages["learn"].scan()
check(shoot(w, "en_dark_learn"), "picture: learn (dark)")
w.close()
QApplication.setPalette(build_palette(TOKENS["light"]))
for _ in range(3):
    app.processEvents()
fresh = messy.clone()
fresh.setName("survey")
fresh.removeCustomProperty(cm.PROP)
project.addMapLayer(fresh)
fresh.startEditing()
for ft in fresh.getFeatures():
    if ft["uid"] in ("F003", "F009"):
        fresh.changeAttributeValue(ft.id(), fresh.fields().indexOf("kind"), "مدرسه")
fresh.commitChanges()
# Arabic, right to left (BIRUNI_EXTRA_I18N: a {"English": [ar, …]} file of strings not merged into ar.json yet)
i18n.init("ar")
if os.environ.get("BIRUNI_EXTRA_I18N"):
    i18n._strings.update({k: v[0] for k, v in json.load(open(os.environ["BIRUNI_EXTRA_I18N"], encoding="utf-8")).items()})
ca.sync_language()
check(QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft, "Arabic: right to left")
plugin.theme.mode = "light"
w = cw.open_charter(plugin, "fields", parcels)
w.ask = False
check(w.layoutDirection() == Qt.LayoutDirection.RightToLeft, "the window is right to left")
check(shoot(w, "ar_light_fields"), "picture: fields (Arabic)")
w.load_layer(fac)
w.show_section("kinds")
check(shoot(w, "ar_light_kinds"), "picture: kinds (Arabic)")
w.load_layer(fresh)
w.show_section("learn")
w.pages["learn"].scan()
check(shoot(w, "ar_light_learn"), "picture: learn (Arabic)")
w.pages["learn"].accept()
fc2 = cm.stored_charter(fresh)
check(fc2 and fc2["kind_field"] == "kind" and fresh.isModified() and
      sorted({ft["kind"] for ft in fresh.getFeatures()}) == ["مدرسة", "مستشفى", "مسجد"] and
      widget(fresh, "type")[0] == "ValueRelation", "accepting in the window: charter applied, spellings fixed (unsaved)")
fresh.rollBack()
w.load_layer(parcels)
w.show_section("health")
check(shoot(w, "ar_light_health"), "picture: health (Arabic)")
w.show_section("share")
check(shoot(w, "ar_light_share"), "picture: share (Arabic)")
w.close()
i18n.init("en_US")
print("pictures in", SHOTS)

project.clear()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED: " + "; ".join(failures))
app.exitQgis()
sys.exit(1 if failures else 0)
