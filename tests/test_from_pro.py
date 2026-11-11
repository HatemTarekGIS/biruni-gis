r"""The «Coming from ArcGIS Pro?» window, off screen: the start-up time is measured and kept; the yearly cost adds up
and is remembered; the speed test runs the real tools on 20,000 points; cards open their commands and cards whose
command does not exist are left out; the command, ribbon buttons and start-page link are wired; Arabic.

Run:  engine\bin\python-qgis.bat tests\test_from_pro.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")  # Processing, as inside the app
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

from biruni import from_pro, i18n  # noqa: E402
from biruni.commands import CATALOG  # noqa: E402
from biruni.theme import TOKENS  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


class _Theme:
    def tokens(self):
        return TOKENS["light"]


class _Runner:
    def __init__(self):
        self.ran = []

    def execute(self, cmd):
        self.ran.append(cmd.id)


class _Plugin:
    def __init__(self):
        self.mw = QMainWindow()
        self.theme = _Theme()
        self.runner = _Runner()


age = from_pro.record_start()
check(age is not None and 0 < age < 600, f"start-up time measured from process creation ({age})")
check(from_pro.last_start() == round(age, 2), "start-up time is kept for the window")

p = _Plugin()
d = from_pro.open_dialog(p, show=False)
check(d.tabs.count() == 4, "four tabs: keep your work, costs, speed, only here")
check("open_arcgis" in d.card_ids and "history_versions" in d.card_ids, "cards for existing commands are shown")
check(all(c in CATALOG for c in d.card_ids), "no card for a command that does not exist")

for cn, pr in d.cost_inputs.values():
    cn.setValue(0)
d.cost_inputs["pro"][0].setValue(3)
d.cost_inputs["pro"][1].setValue(2000)
d.cost_inputs["credits"][0].setValue(10)
d.cost_inputs["credits"][1].setValue(120)
check(d.yearly_cost() == 3 * 2000 + 10 * 120, "yearly cost adds up")
check("7,200" in d.cost_year.text(), f"saving shown: {d.cost_year.text()}")
check("21,600" in d.cost_more.text() and "36,000" in d.cost_more.text(), "3- and 5-year totals")
d2 = from_pro.open_dialog(p, show=False)
check(d2.yearly_cost() == 7200, "the numbers typed are remembered")

d.pro_start.setValue(45)
check("×" in d.compare.text(), f"comparison with the Pro time typed: {d.compare.text()}")

d.run_benchmark()
rows = getattr(d, "bench_rows", None)
check(rows and [r[0] for r in rows] == ["create", "buffer", "dissolve", "join"], "speed test ran all four steps")
check(rows and all(r[1] >= 0 for r in rows), "times measured")
check(rows and "20,000" in rows[1][2] and "12" in rows[2][2], f"buffers and dissolve results right: {rows and rows[1:3]}")
print("   ", d.bench_label.text().replace("\n", " | "))

from biruni.from_pro import _Card  # noqa: E402

cards = d.findChildren(_Card)
cards[0]._on_click()
check(p.runner.ran and p.runner.ran[-1] in CATALOG, "a card opens its command")

src = open(os.path.join(HERE, "..", "app", "plugins", "biruni", "plugin.py"), encoding="utf-8").read()
check(src.count('g.large("from_pro")') == 2, "two ribbon buttons (Home > Learn, View > Help)")
check("record_start" in src, "start-up time recorded when ready")
check(CATALOG["from_pro"].label == "Coming from\nArcGIS Pro?", "command label has a real line break")
check(os.path.exists(os.path.join(HERE, "..", "app", "plugins", "biruni", "resources", "icons",
                                  CATALOG["from_pro"].icon + ".svg")), "icon exists")
sp = open(os.path.join(HERE, "..", "app", "plugins", "biruni", "startpage.py"), encoding="utf-8").read()
check('CATALOG["from_pro"]' in sp, "start page link")

i18n.init("ar")
d3 = from_pro.open_dialog(p, show=False)
check(d3.layoutDirection().name == "RightToLeft", "right to left in Arabic")
check(d3.windowTitle() != "Coming from ArcGIS Pro?", f"Arabic title: {d3.windowTitle()}")
check("QGIS" not in open(from_pro.__file__, encoding="utf-8").read().replace("qgis.", ""), "no engine name shown")

print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
