r"""The ribbon folds instead of scrolling: when a page is wider than the window its groups become one button
each - the group used least first - and the button opens the whole group in a panel. The scroll arrows are only
for a window too narrow even for the folded groups.

Run:  engine\bin\python-qgis.bat tests\test_ribbon_fold.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.path.isdir(r"C:\Windows\Fonts"):   # real fonts: the widths measured are those of the program
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication, QgsSettings  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

from biruni.plugin import BiruniPlugin  # noqa: E402
from biruni.ribbon import USES_KEY, Ribbon, RibbonButton  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


class _Theme:
    def tokens(self):
        class _All(dict):
            def __missing__(self, _k):
                return "#808080"
        return _All()


class _Runner:
    ran = []

    def available(self, _c):
        return True

    def qaction(self, _n):
        return None

    def dock(self, _n):
        return None

    def execute(self, c):
        self.ran.append(c.id)


class _Plugin:
    theme = _Theme()
    runner = _Runner()
    iface = None
    shell = None

    def build_file_menu(self):
        return QMenu()

    def fill_theme_menu(self, m): pass
    def fill_language_menu(self, m): pass
    def fill_help_menu(self, m): pass
    def fill_all_menus(self, m): pass
    def on_tab_shown(self, key): pass
    def build_plugins_tab(self, r=None): pass

    fill_workbench_menu = BiruniPlugin.fill_workbench_menu
    build_ribbon = BiruniPlugin.build_ribbon


def settle():
    for _ in range(6):
        app.processEvents()


plugin = _Plugin()
rib = Ribbon(plugin)
rib.use_workbench("all")
plugin.build_ribbon(rib)
rib.keep_shown_tab()
rib.refresh_theme()
rib.select_tab("home")
home = rib.scroller(rib.pages["home"])
groups = home.groups()
by = {g.title: g for g in groups}
full = sum(g.sizeHint().width() for g in groups)


def state():
    return [g.title for g in home.groups() if g.folded]


def fits():
    bar = home.horizontalScrollBar()
    return bar.maximum() == 0 and not any(a.isVisible() for a in home.arrows)


# ---- wide enough: nothing folds ------------------------------------------------------------------------------
rib.resize(full + 400, 150)
rib.show()
settle()
check(state() == [] and fits(), f"a window wide enough shows every group open ({len(groups)} groups, {full} px)")

# ---- narrower: the groups used least fold, the page fits, no arrow ----------------------------------------------
rib.uses.clear()
rib.uses.update({"select": 50, "identify": 30, "pan": 20, "new_project": 9, "add_data": 5})
rib.resize(int(full * .72), 150)
settle()
folded = state()
check(folded and fits(), f"at 72 % of the width {len(folded)} groups fold and the page fits without an arrow {folded}")
check("Selection" not in folded and "Inquiry" not in folded, "the groups used most stay open")
check(all(by[t].fold_btn.isVisible() and not by[t].body.isVisible() for t in folded),
      "a folded group is one button with its name")
check(all(tr_title in by[t].fold_btn.text() for t in folded for tr_title in [t]), "… named after the group")
order_now = [g.title for g in sorted(groups, key=lambda g: (g.uses(), -groups.index(g)))]
check(folded == [t for t in [g.title for g in groups] if t in set(order_now[:len(folded)])] or set(folded) <= set(order_now[:len(folded) + 2]),
      "they are the ones used least")

# the user starts using a folded group a lot: it opens, another one folds instead
first = folded[0]
cid = by[first].body.findChildren(RibbonButton)[0].cmd.id
rib.uses[cid] = 500
home.refit()
settle()
check(first not in state() and fits(), f"«{first}», now used most, is open again and the page still fits ({state()})")
rib.uses[cid] = 0
home.refit()
settle()

# ---- the folded group's panel -----------------------------------------------------------------------------------
g = by[state()[0]]
buttons = g.body.findChildren(RibbonButton)
QTest.mouseClick(g.fold_btn, Qt.MouseButton.LeftButton)
settle()
panel = g._panel
check(panel is not None and panel.isVisible() and g.body.isVisible() and g.body.parentWidget() is panel,
      f"its button opens the whole group «{g.title}» in a panel ({len(buttons)} buttons)")
plain = next(b for b in buttons if b.menu() is None)
before = len(_Runner.ran)
QTest.mouseClick(plain, Qt.MouseButton.LeftButton)
settle()
check(_Runner.ran[before:] == [plain.cmd.id], f"a button in the panel runs its command ({plain.cmd.id})")
check(g._panel is None and g.body.parentWidget() is g and not g.body.isVisible() and g.fold_btn.isVisible(),
      "the panel closes and the group is one button again")
check(rib.uses.get(plain.cmd.id) == 1 and plain.cmd.id in (QgsSettings().value(USES_KEY, "") or ""),
      "the use is counted and kept for the next session")
check(set(b.cmd.id for b in g.findChildren(RibbonButton)) == set(b.cmd.id for b in buttons), "its buttons are all still there")

# ---- wide again: everything opens --------------------------------------------------------------------------------
rib.resize(full + 400, 150)
settle()
check(state() == [] and all(x.body.isVisible() for x in groups), "wide again: every group opens")

# ---- far too narrow: everything folds, and only then the arrows --------------------------------------------------
rib.resize(260, 150)
settle()
multi = [x for x in groups if len(x.body.findChildren(RibbonButton)) > 1]
check(fits() or all(x.folded for x in multi),
      f"the narrowest window folds until the page fits, or everything that can fold ({len(state())} of {len(multi)})")
check((home.horizontalScrollBar().maximum() > 0) == any(a.isVisible() for a in home.arrows),
      "the arrows show only when even the folded groups do not fit")

# ---- every page of the ribbon fits a laptop screen ---------------------------------------------------------------
rib.resize(1366, 150)
settle()
bad = []
for key in rib.pages:
    if rib.tab_button(key).isHidden():
        continue
    rib.select_tab(key)
    settle()
    sc = rib.scroller(rib.pages[key])
    if sc.horizontalScrollBar().maximum() > 0:
        bad.append(f"{key} (+{sc.horizontalScrollBar().maximum()} px, {len([x for x in sc.groups() if x.folded])}/{len(sc.groups())} folded)")
check(not bad, f"at 1366 px no page of the ribbon needs an arrow {bad or ''}")

if os.environ.get("BIRUNI_SHOT"):   # one picture of a folded page, for a look
    from biruni.theme import TOKENS

    _Theme.tokens = lambda self: TOKENS["light"]
    rib.resize(1500, 150)
    rib.select_tab("home")
    rib.refresh_theme()
    settle()
    rib.grab().save(os.environ["BIRUNI_SHOT"])

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
