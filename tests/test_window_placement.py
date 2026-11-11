r"""Panes open beside the map and windows open in the middle (commands.tab_beside, centering.py).

Run:  engine\bin\python-qgis.bat tests\test_window_placement.py   (no window is shown on the screen)
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()

from qgis.core import QgsApplication  # noqa: E402
from qgis.PyQt.QtCore import QRect, Qt  # noqa: E402
from qgis.PyQt.QtWidgets import QDialog, QDockWidget, QMainWindow, QWidget  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
from biruni import centering  # noqa: E402
from biruni.commands import tab_beside  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


mw = QMainWindow()
mw.setCentralWidget(QWidget())
avail0 = app.primaryScreen().availableGeometry()  # the offscreen screen is small: fit the main window in it
mw.resize(int(avail0.width() * 0.8), int(avail0.height() * 0.8))
mw.move(avail0.left() + 20, avail0.top() + 20)
mw.show()
browser = QDockWidget("Browser", mw)
browser.setObjectName("Browser")
mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, browser)
toolbox = QDockWidget("Toolbox", mw)
toolbox.setObjectName("BiruniToolbox")
mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, toolbox)
browser.show()
toolbox.show()
app.processEvents()
toolbox.setFloating(True)  # what the user had: the toolbox left floating
app.processEvents()

pane = QDockWidget("Field photos", mw)
tab_beside(mw, pane, toolbox)
pane.show()
app.processEvents()
check(not pane.isFloating() and mw.dockWidgetArea(pane) == Qt.DockWidgetArea.RightDockWidgetArea,
      "a pane opened while the toolbox floats is docked at the right, not floating with it")
check(pane in mw.tabifiedDockWidgets(browser), "it becomes a tab beside the Browser instead")

toolbox.setFloating(False)
mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, toolbox)
toolbox.show()
app.processEvents()
pane2 = QDockWidget("Map stops", mw)
tab_beside(mw, pane2, toolbox)
pane2.show()
app.processEvents()
check(pane2 in mw.tabifiedDockWidgets(toolbox), "with the toolbox docked, a new pane is its tab")

pane2.setFloating(True)
tab_beside(mw, pane2, toolbox)
check(not pane2.isFloating(), "a pane left floating comes back beside the map")

screens = [QRect(0, 0, 1920, 1080)]
check(centering._badly_placed(QRect(0, 0, 400, 300), screens), "a window in the screen's corner is badly placed")
check(centering._badly_placed(QRect(3000, 200, 400, 300), screens), "a window off the screen is badly placed")
check(not centering._badly_placed(QRect(700, 300, 400, 300), screens), "a window in the middle is fine")

dlg = QDialog(mw)
dlg.resize(500, 400)
dlg.move(0, 0)
dlg.show()
centering.centre(dlg, mw)
c, m = dlg.frameGeometry().center(), mw.frameGeometry().center()
check(abs(c.x() - m.x()) <= 2 and abs(c.y() - m.y()) <= 2, f"a dialog is centred on the main window ({c} vs {m})")
avail = (mw.screen() or app.primaryScreen()).availableGeometry()
big = QDialog(mw)
big.resize(avail.width() + 500, avail.height() + 500)
big.show()
centering.centre(big, mw)
check(avail.contains(big.frameGeometry()) or big.frameGeometry().width() <= avail.width(),
      "a dialog bigger than the screen is fitted to it")

# the Vault takes the classic Browser's place (the Catalog arrangement)
from biruni import commands  # noqa: E402

vault = QDockWidget("Vault", mw)
vault.setObjectName("BiruniVault")


class _FakeVault:
    @staticmethod
    def open_dock(plugin):
        tab_beside(mw, vault, toolbox)
        vault.show()
        return vault


class _Plugin:
    class iface:  # noqa: N801
        @staticmethod
        def mainWindow():  # noqa: N802
            return mw


real_mod = commands._mod
commands._mod = lambda name: _FakeVault if name == "vault" else real_mod(name)
browser.show()
app.processEvents()
got = commands.vault_for_browser(_Plugin())
app.processEvents()
check(got is vault and vault.isVisible() and not browser.isVisible() and not vault.isFloating(),
      "at start the Vault shows where the Browser was, and the Browser is hidden")
check(mw.dockWidgetArea(vault) == Qt.DockWidgetArea.RightDockWidgetArea, "the Vault is at the right")
vault.hide()
check(commands.vault_for_browser(_Plugin()) is None and not vault.isVisible(),
      "someone who closed both keeps a clean screen at start")
check(commands.vault_for_browser(_Plugin(), force=True) is vault and vault.isVisible(),
      "Standard Panel Layout always brings the Vault")
commands._mod = real_mod

print("ALL PASS" if not failures else f"{len(failures)} FAILED")
app.exitQgis()
sys.exit(1 if failures else 0)
