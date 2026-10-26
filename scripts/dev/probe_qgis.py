"""Dev probe: dump QGIS main-window structure (actions, toolbars, docks, central
widget) to JSON + screenshot, then quit. Run via:  qgis-bin.exe --code probe_qgis.py
Output folder: env BIRUNI_PROBE_OUT (default: %TEMP%\\biruni-probe)."""
import json
import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QAction, QApplication, QDockWidget, QToolBar, QWidget
from qgis.core import Qgis, QgsApplication, QgsSettings
from qgis.utils import iface

OUT = os.environ.get("BIRUNI_PROBE_OUT", os.path.join(os.environ.get("TEMP", "."), "biruni-probe"))
os.makedirs(OUT, exist_ok=True)


def tree(w, depth=0, maxdepth=4):
    node = {"cls": type(w).__name__, "name": w.objectName(), "visible": w.isVisible()}
    if depth < maxdepth:
        kids = [c for c in w.children() if isinstance(c, QWidget)]
        if kids:
            node["children"] = [tree(c, depth + 1, maxdepth) for c in kids]
    return node


def menu_dump(menu):
    items = []
    for a in menu.actions():
        if a.isSeparator():
            items.append("---")
        elif a.menu():
            items.append({"menu": a.text(), "name": a.menu().objectName(), "items": menu_dump(a.menu())})
        else:
            items.append({"text": a.text(), "name": a.objectName()})
    return items


def dump():
    mw = iface.mainWindow()
    data = {
        "version": Qgis.version(),
        "style": QApplication.style().objectName(),
        "uiThemes": QgsApplication.uiThemes(),
        "themesFolder": QgsApplication.defaultThemesFolder(),
        "customThemes": QgsApplication.customThemesFolder() if hasattr(QgsApplication, "customThemesFolder") else None,
        "settingsDir": QgsApplication.qgisSettingsDirPath(),
        "windowTitle": mw.windowTitle(),
        "toolbars": [
            {"name": t.objectName(), "title": t.windowTitle(), "visible": t.isVisible(),
             "actions": [a.objectName() or a.text() for a in t.actions()]}
            for t in mw.findChildren(QToolBar)
        ],
        "docks": [{"name": d.objectName(), "title": d.windowTitle(), "visible": d.isVisible()} for d in mw.findChildren(QDockWidget)],
        "menus": [{"menu": a.text(), "name": a.menu().objectName(), "items": menu_dump(a.menu())} for a in mw.menuBar().actions() if a.menu()],
        "actions": sorted({a.objectName() for a in mw.findChildren(QAction) if a.objectName()}),
        "central": tree(mw.centralWidget(), maxdepth=5),
        "settingsKeys": [k for k in QgsSettings().allKeys() if any(s in k.lower() for s in ("launch", "welcome", "migrat", "theme", "locale", "news", "splash", "version"))],
    }
    with open(os.path.join(OUT, "qgis_structure.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    mw.grab().save(os.path.join(OUT, "main_window.png"))
    QTimer.singleShot(300, QApplication.instance().quit)


QTimer.singleShot(int(os.environ.get("BIRUNI_PROBE_DELAY", "6000")), dump)
