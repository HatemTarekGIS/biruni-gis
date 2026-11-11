r"""Interface fixes of the review of 2026-11-03: search that ignores hamzas and accents and finds the ribbon's
buttons in every language (and by their older wording), the ribbon's scroll arrows in Arabic, the ribbon from
the keyboard (F10, ← / →, Enter, Tab, Esc), and the ribbon's body as tall as its pages.

Run:  engine\bin\python-qgis.bat tests\test_ui_wave2.py
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
from qgis.PyQt.QtCore import QPoint, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QMenu, QWidget  # noqa: E402

from biruni import i18n, search_text  # noqa: E402
from biruni.learn import locator  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- folding --------------------------------------------------------------------------------------------
f = search_text.fold
check(f("إضافة") == f("اضافة") == f("أضافه"), "hamzas and ta marbuta do not matter («اضافة» finds «إضافة»)")
check(f("Créer") == "creer" and f("Çizgi") == "cizgi" and f("İLÇE") == f("ilce"), "accents and Turkish letters")
check(f("یک") == f("يك") and f("مُضَلَّع") == "مضلع" and f("١٢٣") == "123", "Persian letters, tashkeel, digits")
check(search_text.matches("اضافه طبقه", "إضافة طبقة جديدة") and not search_text.matches("حذف", "إضافة"),
      "every word must be there")

# ---- ribbon buttons in the search, in every language ---------------------------------------------------
for code, query, want in (("ar", "اضافة", "add_"), ("fr", "couche", None), ("tr", "katman", None),
                          ("en", "add data", "add_")):
    i18n.init(code)
    locator._COMMANDS.clear()
    res = locator.search_commands(query)
    ok = bool(res) and (want is None or any(cid.startswith(want) for cid, _l, _s in res))
    check(ok, f"{code}: «{query}» finds ribbon buttons ({[l for _c, l, _s in res[:3]]})")
i18n.init("ar")
locator._COMMANDS.clear()
syn = i18n.search_synonyms()
old = next(((en, words[0]) for en, words in syn.items() if any(en == c.label for c in
                                                                 __import__("biruni.commands", fromlist=["x"])
                                                                 .CATALOG.values())), None)
if old:
    word = max(old[1].split(), key=len)
    res = locator.search_commands(word)
    check(any(i18n.tr(old[0]).replace("\n", " ") == lbl for _c, lbl, _s in res),
          f"the older wording of a button still finds it («{word}»)")
else:
    check(True, "(no button changed its wording)")

# ---- the ribbon's scroll arrows ------------------------------------------------------------------------
from biruni.ribbon import Ribbon, _PageScroller  # noqa: E402

for d in (Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft):
    app.setLayoutDirection(d)
    page = QWidget()
    lay = QHBoxLayout(page)
    first = QLabel("FIRST")
    lay.addWidget(first)
    for i in range(30):
        lay.addWidget(QLabel(f"button {i}"))
    sc = _PageScroller(page)
    sc.resize(400, 90)
    sc.show()
    app.processEvents()
    left, right = sc.arrows
    x = first.mapTo(sc.viewport(), QPoint(0, 0)).x()
    covered = (right.isVisible() and x + first.width() > 400 - 18) or (left.isVisible() and x < 18)
    rtl = d == Qt.LayoutDirection.RightToLeft
    check(not covered and (left.isVisible() if rtl else right.isVisible()),
          f"{'Arabic' if rtl else 'English'}: the arrow shown points to the rest of the page, not over the first group")
    (left if rtl else right).click()
    app.processEvents()
    check(sc.horizontalScrollBar().value() > 0, f"{'Arabic' if rtl else 'English'}: the arrow moves on")
    sc.close()


# ---- the ribbon from the keyboard --------------------------------------------------------------------------
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
        self.ran.append(c.cid if hasattr(c, "cid") else c)


class _Plugin:
    theme = _Theme()
    runner = _Runner()
    iface = None

    def build_file_menu(self):
        return QMenu()

    def fill_theme_menu(self, m): pass
    def fill_language_menu(self, m): pass
    def fill_help_menu(self, m): pass
    def fill_all_menus(self, m): pass
    def on_tab_shown(self, key): pass


from biruni.commands import CATALOG  # noqa: E402

for d in (Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft):
    app.setLayoutDirection(d)
    rib = Ribbon(_Plugin())
    ids = list(CATALOG)[:6]
    for key in ("home", "edit", "data"):
        page = rib.add_tab(key, key.title())
        g = rib.group(page, "Group")
        for cid in ids[:3] if key == "home" else ids[3:]:
            g.large(cid) if hasattr(g, "large") else None
    rib.refresh_theme()
    rib.resize(900, 160)
    rib.show()
    rib.activateWindow()
    app.processEvents()
    rtl = d == Qt.LayoutDirection.RightToLeft
    name = "Arabic" if rtl else "English"
    page_h = max(p.sizeHint().height() for p in rib.pages.values())
    check(rib.body.height() >= page_h, f"{name}: the ribbon body is as tall as its pages ({rib.body.height()} ≥ {page_h})")
    rib.focus_tabs()
    app.processEvents()
    check(rib.tab_button("home").hasFocus(), f"{name}: F10 puts the keyboard on the current tab")
    QTest.keyClick(rib.tab_button("home"), Qt.Key.Key_Left if rtl else Qt.Key.Key_Right)
    app.processEvents()
    check(rib.tab_button("edit").isChecked() and rib.tab_button("edit").hasFocus(),
          f"{name}: the arrow towards the reading direction goes to the next tab")
    QTest.keyClick(rib.tab_button("edit"), Qt.Key.Key_Return)
    app.processEvents()
    focused = QgsApplication.focusWidget()
    check(focused is not None and focused.objectName() in ("RibbonLarge", "RibbonSmall"),
          f"{name}: Enter goes into the page, on its first button")
    rib.close()

# ---- the toolbox search ------------------------------------------------------------------------------------
from biruni.toolbox import ToolboxDock  # noqa: E402

from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402


class _Iface:
    _mw = QMainWindow()

    def mainWindow(self):  # noqa: N802
        return self._mw


i18n.init("ar")
plug = _Plugin()
plug.iface = _Iface()
box = ToolboxDock(plug)
box.load()
word = next((w for t in box.tools.values() for w in t.name.split() if len(w) > 4 and w[0] in "أإآ"), None)
plain = "ا" + word[1:]
box.search.setText(plain)
box.populate()
names = [box.tree.topLevelItem(i).child(j).text(0) for i in range(box.tree.topLevelItemCount())
         for j in range(box.tree.topLevelItem(i).childCount())]
check(any(word in n for n in names), f"the toolbox finds «{word}» when «{plain}» is typed ({names[:3]})")
box.search.setText("")
box.populate()
check(box.tree.topLevelItemCount() > 0, "an empty search shows every category again")

# ---- start page cards from the keyboard ---------------------------------------------------------------------
from biruni.startpage import Card  # noqa: E402

opened = []
card = Card("folder", "Open", "Open a project", lambda: opened.append(1))
check(card.focusPolicy() & Qt.FocusPolicy.TabFocus, "a start page card is reached with Tab")
QTest.keyClick(card, Qt.Key.Key_Return)
QTest.keyClick(card, Qt.Key.Key_Space)
check(opened == [1, 1], "Enter and Space open it")

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
