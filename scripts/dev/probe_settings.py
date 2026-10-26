"""Dev probe: find settings-tree keys and welcome-screen widget, then quit."""
import json
import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QApplication, QWidget
from qgis.core import QgsSettingsTree
from qgis.utils import iface

OUT = os.environ.get("BIRUNI_PROBE_OUT", os.path.join(os.environ.get("TEMP", "."), "biruni-probe"))
os.makedirs(OUT, exist_ok=True)
WANT = ("launch", "welcome", "news", "check-version", "locale", "theme", "splash", "migrat", "first-run", "tips")


def walk(node, acc):
    for e in node.childrenSettings():
        k = e.definitionKey()
        if any(w in k.lower() for w in WANT):
            acc.append({"key": k, "desc": e.description(), "default": str(e.defaultValueAsVariant())})
    for c in node.childrenNodes():
        walk(c, acc)


def run():
    acc = []
    walk(QgsSettingsTree.treeRoot(), acc)
    mw = iface.mainWindow()
    widgets = []
    for w in mw.findChildren(QWidget):
        cls = w.metaObject().className()
        if "Welcome" in cls or "Quick" in cls:
            p = w.parentWidget()
            widgets.append({"cls": cls, "name": w.objectName(), "visible": w.isVisible(),
                            "parent": p.metaObject().className() if p else None,
                            "parentName": p.objectName() if p else None, "geom": str(w.geometry())})
    with open(os.path.join(OUT, "settings_probe.json"), "w", encoding="utf-8") as f:
        json.dump({"keys": acc, "widgets": widgets}, f, ensure_ascii=False, indent=1)
    QTimer.singleShot(200, QApplication.instance().quit)


QTimer.singleShot(5000, run)
