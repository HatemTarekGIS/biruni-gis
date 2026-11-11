"""Tests for «Clean my table» and «Find spelling variants».

Run:  engine\\bin\\python-qgis.bat tests\\test_clean_spell.py
"""
import os
import random
import sys
import tempfile
import time

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.algorithms.clean_table import clean_text, detect_kind, parse_number_text  # noqa: E402
from biruni.algorithms.spell_values import group_spellings, read_reference  # noqa: E402
from biruni.arabic_text import latin_skeleton  # noqa: E402

provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
failures = []
TMP = tempfile.mkdtemp()


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def null(v):
    return v is None or (hasattr(v, "isNull") and v.isNull())


def table(fields, rows, geom="None", name="t"):
    spec = "&".join(f"field={n}:{t}" for n, t in fields)
    lyr = QgsVectorLayer(f"{geom}?{spec}" + ("&crs=EPSG:32637" if geom != "None" else ""), name, "memory")
    feats = []
    for i, r in enumerate(rows):
        f = QgsFeature(lyr.fields())
        f.setAttributes(list(r))
        if geom == "Point":
            f.setGeometry(QgsGeometry.fromWkt(f"POINT({i} {i})"))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def out(value):
    return ctx.getMapLayer(value) if isinstance(value, str) else value


def values(lyr, field):
    return [f[field] for f in lyr.getFeatures()]


ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = QgsProcessingFeedback()

# ---- clean_text: the rules one by one -----------------------------------------------------------------
check(clean_text("  الملك   فهد ") == ("الملك فهد", ["spaces"]), "trim and collapse spaces")
check(clean_text("‏شارع​ العليا‎") == ("شارع العليا", ["invisible"]), "direction marks, zero width")
check(clean_text("فهـــد") == ("فهد", ["invisible"]), "tatweel removed")
check(clean_text("a b") == ("a b", ["spaces"]), "no-break space")
check(clean_text("قطعة ١٢٣") == ("قطعة 123", ["digits"]), "Arabic-Indic digits")
check(clean_text("٢٠٫٥") == ("20.5", ["digits"]), "Arabic decimal mark")
check(clean_text("مدرسة أحمد", letters=True) == ("مدرسه احمد", ["letters"]), "letter forms, when asked")
check(clean_text("مدرسة أحمد")[0] == "مدرسة أحمد", "letter forms kept by default")
check(clean_text("king FAHD road", case=3) == ("King Fahd Road", ["case"]), "Title Case")
check(clean_text("Riyadh الرياض", case=1) == ("RIYADH الرياض", ["case"]), "UPPER leaves Arabic alone")
for ph in ("-", "N/A", "null", "لا يوجد", "  ", "", "—"):
    check(clean_text(ph)[0] is None, f"placeholder {ph!r} -> NULL")
check(clean_text("0")[0] == "0" and clean_text("0", zero=True)[0] is None, "zero -> NULL only when asked")
check(clean_text("Al-Malik") == ("Al-Malik", []), "a clean value is untouched")

# ---- types hidden in text ---------------------------------------------------------------------------
check(parse_number_text("1,250.5") == 1250.5 and parse_number_text("٢٠٫٥") == 20.5
      and parse_number_text("-3") == -3 and parse_number_text("12 م") is None, "numbers written as text")
check(detect_kind(["1", "2", "٣", "40"])[0] == "integer", "integer column (Arabic digits too)")
check(detect_kind(["1.5", "2,25", "3"])[0] == "decimal", "decimal column")
check(detect_kind(["0012", "0100", "0001"])[0] == "code", "leading zeros = codes")
kind, _, opts = detect_kind(["25/12/2023", "01/02/2024", "13/01/2024"])
check(kind == "date" and opts["day_first"], "dd/mm/yyyy dates")
kind, _, opts = detect_kind(["12/25/2023", "01/02/2024"])
check(kind == "date" and not opts["day_first"], "mm/dd/yyyy when a second part is above 12")
check(detect_kind(["2024-01-31", "٢٠٢٤-٠٢-٠١"])[0] == "date", "ISO dates, Arabic digits")
check(detect_kind(["15/09/1445", "01/01/1446"])[0] == "hijri date", "Hijri dates are recognised")
check(detect_kind(["45000", "45001"], "survey_date")[0] == "excel date", "Excel day numbers in a date field")
check(detect_kind(["45000", "45001"], "parcel")[0] == "integer", "... but plain numbers elsewhere")
check(detect_kind(["شارع", "1", "2"])[0] is None, "mixed text is left alone")

# ---- cleantable into a new layer ---------------------------------------------------------------------
rows = [
    ("  الملك   فهد ", "١٢٣", "25/12/2023", "N/A", "شمال، الرياض", "Riyadh", "a"),
    ("‏العليا", "45", "01/02/2024", "300", "جنوب,جدة", "Jeddah", "b"),
    ("العليا", "45", "01/02/2024", "300", "جنوب,جدة", "Jeddah", "b"),
    ("لا يوجد", "x", "13/01/2024", "-", None, None, "c"),
]
lyr = table([("name", "string"), ("num", "string"), ("when", "string"), ("price", "string"), ("place", "string"),
             ("city", "string"), ("code", "string")], rows, geom="Point", name="messy")
res = processing.run("biruni:cleantable", {
    "INPUT": lyr, "CONVERT": True, "SPLIT_FIELD": "place", "SPLIT_SEP": ",", "MERGE_FIELDS": ["name", "city"],
    "MERGE_SEP": " - ", "MERGE_NAME": "label", "DUP_FIELDS": ["name", "num", "when"],
    "OUTPUT": "TEMPORARY_OUTPUT", "CHANGES": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
o = out(res["OUTPUT"])
feats = list(o.getFeatures())
names = [f.name() for f in o.fields()]
check(o.featureCount() == 4 and o.geometryType() == lyr.geometryType(), "same rows and geometry")
check([f["name"] for f in feats] == ["الملك فهد", "العليا", "العليا", None], f"names cleaned {[f['name'] for f in feats]}")
check([f["num"] for f in feats] == ["123", "45", "45", "x"], "digits")
check(null(feats[0]["price"]) and null(feats[3]["price"]), "placeholders to NULL")
check(res["TYPES"].get("when") == "date" and res["TYPES"].get("price") == "integer"
      and res["TYPES"].get("num") is None, f"types found {res['TYPES']}")
check("when_date" in names and feats[0]["when_date"] == QDate(2023, 12, 25)
      and feats[3]["when_date"] == QDate(2024, 1, 13), "date field added")
check("price_num" in names and feats[1]["price_num"] == 300 and null(feats[0]["price_num"]), "number field added")
check(feats[0]["place_1"] == "شمال" and feats[0]["place_2"] == "الرياض" and feats[1]["place_2"] == "جدة",
      "split by , and Arabic ،")
check(feats[0]["label"] == "الملك فهد - Riyadh" and feats[3]["label"] is None or feats[3]["label"] == "",
      f"merged field {[f['label'] for f in feats]}")
check([f["dup_group"] for f in feats][1:3] == [1, 1] and null(feats[0]["dup_group"]),
      f"duplicates found after cleaning {[f['dup_group'] for f in feats]}")
ch = out(res["CHANGES"])
rules = {(c["fid"], c["field"]): c["rule"] for c in ch.getFeatures()}
check(rules.get((1, "name")) == "spaces" and rules.get((2, "name")) == "invisible"
      and rules.get((1, "price")) == "empty", f"changes table {sorted(rules.items())[:6]}")
check(res["RULE_COUNTS"]["duplicate"] == 2 and res["RULE_COUNTS"]["date"] == 4, f"counts {res['RULE_COUNTS']}")
check(lyr.getFeature(1)["name"] == "  الملك   فهد " and not lyr.isEditable(), "the input is untouched")

# ---- cleantable in place: undo and discard bring the values back ------------------------------------
orig = values(lyr, "name")
res = processing.run("biruni:cleantable", {"INPUT": lyr, "IN_PLACE": True, "CONVERT": True,
                                           "CHANGES": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
check(res["IN_PLACE"] and lyr.isEditable(), "in place: the layer is in an edit session")
check(values(lyr, "name") == ["الملك فهد", "العليا", "العليا", None], "in place: values changed in the buffer")
check(lyr.fields().lookupField("when_date") >= 0 and lyr.getFeature(1)["when_date"] == QDate(2023, 12, 25),
      "in place: converted field added")
check(lyr.dataProvider().fields().lookupField("when_date") < 0, "in place: nothing saved yet")
lyr.undoStack().undo()
check(values(lyr, "name") == orig and lyr.fields().lookupField("when_date") < 0, "Ctrl+Z undoes everything at once")
lyr.undoStack().redo()
check(values(lyr, "name")[0] == "الملك فهد", "redo")
lyr.rollBack()
check(values(lyr, "name") == orig and not lyr.isEditable(), "discarding the edits restores the layer")

# a table without geometry, numeric zero
tbl = table([("n", "integer"), ("s", "string")], [(0, " a "), (5, "b"), (0, " a ")])
res = processing.run("biruni:cleantable", {"INPUT": tbl, "FIELDS": ["n", "s"], "ZERO": True,
                                           "OUTPUT": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
o = out(res["OUTPUT"])
check(values(o, "n")[0] is None or null(values(o, "n")[0]), "numeric zero -> NULL when asked")
check(values(o, "s") == ["a", "b", "a"] and values(o, "dup_group") == [1, None, 1]
      or [null(x) for x in values(o, "dup_group")] == [False, True, False], "table without geometry")

# ---- spelling variants: grouping ----------------------------------------------------------------------
counts = {"الملك فهد": 40, "الملك فهـد": 3, "الملك  فهد": 2, "الملك فهد ": 1, "شارع الملك فهد": 4,
          "الملك فهض": 1, "Al Malik Fahd": 5, "الملك عبدالعزيز": 30, "الملك عبد العزيز": 6, "العليا": 20,
          "العلياء": 2, "شارع 15": 5, "شارع 16": 4, "King Fahd Rd": 2, "King Fahd Road": 3}
groups = group_spellings(counts, 0.85)
by = {g[0]: {m[0]: m for m in g[2]} for g in groups}
fahd = by.get("الملك فهد", {})
check(set(fahd) >= {"الملك فهـد", "الملك  فهد", "الملك فهد ", "شارع الملك فهد", "الملك فهض"},
      f"«الملك فهد» group {sorted(fahd)}")
check(fahd.get("الملك فهـد", (0, 0, 0, ""))[3] == "normalised" and fahd.get("الملك فهض", (0, 0, 0, ""))[3] == "typo",
      "kinds: normalised / typo (a rare spelling one letter away)")
check(not any("السلام" in g and "السلامة" in g
              for g in ({m[0] for m in grp[2]} for grp in group_spellings({"السلام": 10, "السلامة": 8}, 0.85))),
      "two common names one letter apart stay apart («السلام» / «السلامة»)")
check(not any("حطين" in g and "حطيم" in g
              for g in ({m[0] for m in grp[2]} for grp in group_spellings({"حطين": 10, "حطيم": 9}, 0.85))),
      "... also when the letter is swapped («حطين» / «حطيم»)")
check("Al Malik Fahd" not in fahd, "Latin spelling separate unless asked")
check("الملك عبد العزيز" in by.get("الملك عبدالعزيز", {}), "عبدالعزيز / عبد العزيز")
check("العلياء" in by.get("العليا", {}), "hamza at the end")
check(not any("شارع 16" in g and "شارع 15" in g for g in by.values()), "different numbers are never grouped")
check("King Fahd Rd" in by.get("King Fahd Road", {}), "Latin typo / abbreviation")
groups = group_spellings(counts, 0.85, translit=True)
by = {g[0]: {m[0]: m for m in g[2]} for g in groups}
check(by.get("الملك فهد", {}).get("Al Malik Fahd", (0, 0, 0, ""))[3] == "transliteration",
      "Arabic and Latin spellings grouped when asked")
check(latin_skeleton("محمد") == latin_skeleton("Mohammed") and latin_skeleton("عبدالله") == latin_skeleton("Abdullah")
      and latin_skeleton("الغامدي") == latin_skeleton("Al Ghamdi") and latin_skeleton("أحمد") == latin_skeleton("Ahmed"),
      "sound skeletons")
check(latin_skeleton("خالد") != latin_skeleton("محمد"), "different names, different skeletons")

# with a reference list
groups = group_spellings({"الملك فهد": 2, "الملك فهـد": 5, "طريق الامير سلطان": 3, "حي النخيل": 1},
                         0.85, reference=["الملك فهد", "الأمير سلطان"])
by = {g[0]: (g[1], {m[0]: m for m in g[2]}) for g in groups}
check(by.get("الملك فهد", (False, {}))[0] and "الملك فهـد" in by["الملك فهد"][1],
      "the list's spelling wins over the most frequent one")
check("طريق الامير سلطان" in by.get("الأمير سلطان", (False, {}))[1], "snapped to the list")
check(by.get("حي النخيل", (True,))[0] is False, "a name not in the list is its own group")

# ---- spellvalues algorithm, reference file and apply -------------------------------------------------
streets = table([("street", "string"), ("n", "integer")],
                [("الملك فهد", 1), ("الملك فهـد", 2), ("الملك فهد ", 3), ("الملك فهد", 4), ("العليا", 5),
                 ("العلياء", 6), ("العليا", 7), ("التحلية", 8), (None, 9), ("شارع 15", 10), ("شارع 16", 11)],
                name="streets")
res = processing.run("biruni:spellvalues", {"INPUT": streets, "FIELD": "street", "OUTPUT": "TEMPORARY_OUTPUT"},
                     context=ctx, feedback=fb)
rep = out(res["OUTPUT"])
rows = {(f["value"]): f for f in rep.getFeatures()}
check(res["GROUPS"] == 2 and res["VARIANTS"] == 3 and res["CHANGED"] == 0, f"report counts {res}")
check(rows["الملك فهـد"]["suggested"] == "الملك فهد" and rows["الملك فهد"]["kind"] == "suggested"
      and rows["العلياء"]["suggested"] == "العليا", "report rows")
check("التحلية" not in rows and "شارع 15" not in rows, "values without variants are not listed")
check(not streets.isEditable(), "nothing changed without «apply»")

ref_csv = os.path.join(TMP, "names.csv")
with open(ref_csv, "w", encoding="utf-8-sig") as fh:
    fh.write("id,الاسم\n1,طريق الملك فهد\n2,شارع العليا العام\n3,التحلية\n")
check(read_reference(ref_csv, "الاسم") == ["طريق الملك فهد", "شارع العليا العام", "التحلية"], "list from a CSV column")
ref_txt = os.path.join(TMP, "names.txt")
with open(ref_txt, "w", encoding="utf-8") as fh:
    fh.write("الملك فهد\nالعليا\n\nالتحلية\n")
check(read_reference(ref_txt) == ["الملك فهد", "العليا", "التحلية"], "list from a text file")
import openpyxl  # noqa: E402

ref_xlsx = os.path.join(TMP, "names.xlsx")
wb = openpyxl.Workbook()
wb.active.append(["الاسم"])
for n in ("الملك فهد", "العليا", "التحلية"):
    wb.active.append([n])
wb.save(ref_xlsx)
check(read_reference(ref_xlsx) == ["الملك فهد", "العليا", "التحلية"], "list from Excel (header dropped)")

before = values(streets, "street")
res = processing.run("biruni:spellvalues", {"INPUT": streets, "FIELD": "street", "REFERENCE": ref_txt,
                                            "APPLY": True, "OUTPUT": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
after = values(streets, "street")
check(streets.isEditable() and res["CHANGED"] == 3, f"applied in an edit session ({res['CHANGED']} changed)")
check(after[:7] == ["الملك فهد"] * 4 + ["العليا"] * 3 and after[7] == "التحلية" and after[9] == "شارع 15",
      f"values after apply {after}")
rep = out(res["OUTPUT"])
notes = [f["note"] for f in rep.getFeatures() if f["value"] == "شارع 15"]
check(res["NOT_IN_LIST"] >= 2 and notes and "list" in (notes[0] or "").lower() or notes and notes[0],
      f"names not in the list are flagged ({notes})")
streets.undoStack().undo()
check(values(streets, "street") == before, "Ctrl+Z restores the spellings")
streets.rollBack()

# ---- speed: 50,000 rows -------------------------------------------------------------------------------
random.seed(7)
base = [f"حي {w} {i}" for i, w in enumerate(["النخيل", "الروضة", "العليا", "الملز", "السليمانية"] * 400)]
noisy = []
for i in range(50000):
    b = base[i % len(base)]
    r = random.random()
    noisy.append(b.replace("ة", "ه") if r < 0.1 else b + " " if r < 0.2 else b.replace("ي", "ى") if r < 0.25 else b)
big = table([("name", "string")], [(v,) for v in noisy], name="big")
t0 = time.time()
res = processing.run("biruni:spellvalues", {"INPUT": big, "FIELD": "name", "OUTPUT": "TEMPORARY_OUTPUT"},
                     context=ctx, feedback=fb)
dt_spell = time.time() - t0
t0 = time.time()
res2 = processing.run("biruni:cleantable", {"INPUT": big, "OUTPUT": "TEMPORARY_OUTPUT"}, context=ctx, feedback=fb)
dt_clean = time.time() - t0
print(f"     50,000 rows: spelling {dt_spell:.1f} s ({res['GROUPS']} groups), cleaning {dt_clean:.1f} s")
check(dt_spell < 60 and dt_clean < 60, "50,000 rows in reasonable time")
check(res["GROUPS"] <= 2000, f"numbered names stay apart ({res['GROUPS']} groups)")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
