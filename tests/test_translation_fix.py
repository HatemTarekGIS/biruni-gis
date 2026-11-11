"""scripts/fix_qm.py: the %1 placeholders of the QGIS Arabic translation are repaired at build time.

Run:  engine\\bin\\python-qgis.bat tests\\test_translation_fix.py
"""
import os
import shutil
import sys
import tempfile

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QTranslator

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

app = QgsApplication([], False)
app.initQgis()
import fix_qm  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


repair = fix_qm.repair
check(repair("%1 [optional]", "1% [اختياري]") == "%1 [اختياري]", "1% -> %1")
check(repair("Could not write to %1", "لا يمكن الكتابة إلى٪ 1") == "لا يمكن الكتابة إلى%1", "Arabic percent sign and space")
check(repair("%1: Not a raster layer.", "1%: ليست طبقة النقطية.") == "%1: ليست طبقة النقطية.", "1%: at the start")
check(repair("Map %1", "خريطة ٪1") == "خريطة %1", "٪1")
check(repair("<p>%1: %2</p>\n", "<p>1%: 2%</p>\n") == "<p>%1: %2</p>\n", "two placeholders")
check(repair("Library name is %1\n", "اسم المكتبة%\n1") == "Library name is %1\n", "beyond repair: English")
check(repair("Delete %1", "حذف %1") is None, "a correct translation is left alone")

src = os.path.join(QgsApplication.i18nPath(), "qgis_ar.qm")
tmp = tempfile.mkdtemp()
qm = os.path.join(tmp, "qgis_ar.qm")
shutil.copy(src, qm)
before = QTranslator()
before.load(src)
first = fix_qm.fix_file(qm)
print(f"     {first} translations repaired in the copy (0 if the engine copy is already repaired)")
check(fix_qm.fix_file(qm) == 0, "running it again changes nothing")
after = QTranslator()
check(after.load(qm), "the repaired file loads")
check(after.translate("QObject", "%1 [optional]") == "%1 [اختياري]", "the optional-parameter label is repaired")
check(after.translate("QObject", "Delete %1") == "حذف %1", "'حذف 1%' -> 'حذف %1'")
samples = [("QObject", "Error"), ("QObject", "Warning"), ("QObject", "Cancel"), ("QObject", "Layers"),
           ("QObject", "Map %1 of %2"), ("QObject", "Name")]
check(all(after.translate(c, s) == before.translate(c, s) for c, s in samples),
      f"other translations unchanged ({[after.translate(c, s) for c, s in samples]})")
# every message of the file is still found, unchanged unless it was repaired
blocks = {b[0]: b[1] for b in fix_qm.read_blocks(open(src, "rb").read())}
msgs, i, records = blocks[fix_qm.MESSAGES], 0, []
while i < len(msgs):
    rec = {}
    while msgs[i] != fix_qm.END:
        tag = msgs[i]
        i += 1
        if tag == fix_qm.OBSOLETE1:
            i += 4
            continue
        n = int.from_bytes(msgs[i:i + 4], "big")
        i += 4
        if n == 0xFFFFFFFF:
            continue
        if tag in (fix_qm.SOURCE, fix_qm.CONTEXT, fix_qm.COMMENT):
            rec[tag] = msgs[i:i + n].decode("utf-8", "replace")
        i += n
    i += 1
    if fix_qm.SOURCE in rec and all(x.isascii() for x in rec.values()):  # PyQt passes them as ASCII
        records.append((rec.get(fix_qm.CONTEXT, ""), rec[fix_qm.SOURCE], rec.get(fix_qm.COMMENT) or None))
changed = [r for r in records if after.translate(*r) != before.translate(*r)]
check(len(records) > 10000 and all("%" in s for _, s, _ in changed) and len(changed) <= first,
      f"all {len(records)} messages still found; {len(changed)} changed, all with placeholders")
check(after.translate("QObject", "Library name is %1\n") in ("", "Library name is %1\n"),
      "a message beyond repair falls back to English")

print("ALL PASS" if not failures else f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
