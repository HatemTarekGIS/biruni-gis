r"""Workspaces (workbench.py): a smaller ribbon for the surveyor, the engineering office, the student and the GIS
analyst. Every name in the definitions is a real command, "Everything" hides nothing, the core commands stay in
every workspace, the ribbon built with a workspace leaves out the hidden buttons and the groups and tabs left
empty, and the search still finds (and runs) a hidden button.

Run:  engine\bin\python-qgis.bat tests\test_workbench.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))

from qgis.core import QgsApplication  # noqa: E402

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], True)
app.initQgis()
from qgis.PyQt.QtCore import QCoreApplication, QEvent  # noqa: E402
from qgis.PyQt.QtWidgets import QFrame, QMenu  # noqa: E402

from biruni import i18n, workbench  # noqa: E402
from biruni.commands import CATALOG  # noqa: E402
from biruni.learn import locator  # noqa: E402
from biruni.plugin import BiruniPlugin  # noqa: E402
from biruni.ribbon import Ribbon, RibbonButton, RibbonGroup  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- the definitions ---------------------------------------------------------------------------------------
TABS = ("home", "data", "edit", "analysis", "quality", "share", "thesis", "view", "plugins")
for name, wb in workbench.WORKBENCHES.items():
    unknown = sorted(c for c in workbench.command_ids(name) if c not in CATALOG)
    check(not unknown, f"{name}: every command it names is in the catalog {unknown or ''}")
    bad = [t for t in wb.tabs if t not in TABS] + [g for g in wb.groups if g.split("/")[0] not in TABS]
    check(not bad, f"{name}: every tab and group it names is a ribbon tab {bad or ''}")
check(workbench.Hidden("all").nothing(), "«Everything» hides nothing")
check(workbench.CORE <= set(CATALOG), "the core commands are in the catalog")
for name in workbench.WORKBENCHES:
    hid = workbench.Hidden(name)
    kept = all(not hid.button(t, "Any", cid) for cid in workbench.CORE for t in TABS)
    check(kept, f"{name}: no core command is hidden, even in a hidden tab")
check(workbench.Hidden("no such workspace").nothing(), "an unknown workspace falls back to «Everything»")


# ---- the ribbon built with a workspace (the app's own build_ribbon, on a stub plugin) ----------------------
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
        self.ran.append(c)


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


def rebuild(rib, name):
    rib.use_workbench(name)
    plugin.build_ribbon(rib)
    rib.keep_shown_tab()
    rib.refresh_theme()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)  # the old buttons are gone
    app.processEvents()


def shown(rib):
    """{tab: {group title: [command ids]}} of the tabs and groups on the ribbon."""
    out = {}
    for key, page in rib.pages.items():
        if rib.tab_button(key).isHidden():
            continue
        for grp in page.findChildren(RibbonGroup):
            out.setdefault(key, {})[grp.title] = [b.cmd.id for b in grp.findChildren(RibbonButton)]
    return out


def separators(rib, key):
    return len([f for f in rib.pages[key].findChildren(QFrame) if f.objectName() == "RibbonSep"])


plugin = _Plugin()
rib = Ribbon(plugin)
rebuild(rib, "all")
full = shown(rib)
full_ids = {c for groups in full.values() for cs in groups.values() for c in cs}
full_buttons = len(rib.buttons)
widths = {"all": {k: rib.pages[k].sizeHint().width() for k in full}}
check("thesis" in full and full_buttons > 250, f"«Everything» builds the whole ribbon ({full_buttons} buttons)")
total_all = sum(widths["all"].values())
print("     " + "  ".join(f"{k} {w}" for k, w in widths["all"].items()), flush=True)

for name, wb in workbench.WORKBENCHES.items():
    if name == "all":
        continue
    rebuild(rib, name)
    hid = workbench.Hidden(name)
    now = shown(rib)
    ids = {c for groups in now.values() for cs in groups.values() for c in cs}
    leaked = sorted({c for k, groups in now.items() for g, cs in groups.items() for c in cs if hid.button(k, g, c)})
    check(not leaked, f"{name}: no hidden button is on the ribbon {leaked[:5] or ''}")
    missing = sorted((workbench.CORE & full_ids) - ids)
    check(not missing, f"{name}: the core commands are all there {missing or ''}")
    empty = [f"{k}/{g}" for k, groups in now.items() for g, cs in groups.items() if not cs]
    check(not empty, f"{name}: no empty group is shown {empty or ''}")
    for t in wb.tabs:
        check(t not in now and rib.tab_button(t).isHidden(), f"{name}: the tab «{t}» is not shown")
    for g in wb.groups:
        tab, title = g.split("/", 1)
        left = now.get(tab, {}).get(title, [])
        check(set(left) <= workbench.CORE, f"{name}: the group «{g}» is gone (or keeps only core buttons)")
    check(all(separators(rib, k) == len(now[k]) - 1 for k in now), f"{name}: one separator between groups")
    check(not rib.tabs.checkedButton().isHidden(), f"{name}: the open tab is one that is shown")
    widths[name] = {k: rib.pages[k].sizeHint().width() for k in now}
    total = sum(widths[name].values())
    # (0.62: the astrolabe and the shapefile with arcs are on every workspace's ribbon - with them the office
    # workspace is 60-61 % of the full ribbon, still about half)
    check(total < 0.62 * total_all, f"{name}: the ribbon is about half as wide ({total} of {total_all} px, "
                                   f"{len(rib.buttons)} of {full_buttons} buttons)")
    print("     " + "  ".join(f"{k} {w}" for k, w in widths[name].items()), flush=True)

# the thesis tab hidden while open: another tab opens; back to everything, every tab comes back in its place
rib.select_tab("thesis")
rebuild(rib, "surveyor")
check(rib.tabs.checkedButton() is rib.tab_button("home"), "a hidden open tab gives way to the first tab")
rebuild(rib, "all")
check(shown(rib) == full, "«Everything» brings every tab, group and button back, in order")
check([k for k in rib._tab_keys if not rib.tab_button(k).isHidden()] == list(full), "… the tabs in their order")

# ---- the choice is remembered, and shown in the header icon's tooltip and menu ------------------------------
workbench.set_current("surveyor")
check(workbench.current() == "surveyor", "the workspace is kept in the settings")
workbench.set_current("bogus")
check(workbench.current() == "all", "an unknown name keeps «Everything»")
rib.use_workbench("student")
check(i18n.tr("Student / researcher") in rib.workbench_btn.toolTip(), "the header icon's tooltip names it")
m = QMenu()
plugin.shell = type("S", (), {"ribbon": rib})()
plugin.fill_workbench_menu(m)
items = [a for a in m.actions() if a.isCheckable()]
check(len(items) == len(workbench.WORKBENCHES) and [a.text() for a in items if a.isChecked()] ==
      [i18n.tr("Student / researcher")], "the menu lists every workspace, the current one checked")

# ---- hidden buttons are still found by the search, and run from it ------------------------------------------
rebuild(rib, "surveyor")
on_ribbon = {b.cmd.id for b in rib.buttons}
check("ss_hotspots" not in on_ribbon, "«Hot Spots» is not on the surveyor's ribbon")
for code in ("en", "ar"):
    i18n.init(code)
    locator._COMMANDS.clear()
    res = locator.search_commands(i18n.tr("Hot Spots"))
    check(any(cid == "ss_hotspots" for cid, _l, _s in res), f"{code}: … but the search finds it")
from qgis.core import QgsLocatorResult  # noqa: E402

flt = locator.ArcGISNamesFilter(plugin)  # the search box's filter for the ribbon's buttons
flt.triggerResult(QgsLocatorResult(flt, res[0][1], {"target": "ss_hotspots"}))
check(plugin.runner.ran[-1] is CATALOG["ss_hotspots"], "… and it runs from there")
i18n.init("en")
locator._COMMANDS.clear()
hidden_ids = sorted(full_ids - on_ribbon)
found = [c for c in hidden_ids
         if c in {r[0] for r in locator.search_commands(CATALOG[c].label.replace("\n", " "), n=400)}]
check(found == hidden_ids, f"every hidden button is found by its name ({len(found)}/{len(hidden_ids)})")

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
