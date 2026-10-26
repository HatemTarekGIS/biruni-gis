"""Dev probe: list widgets whose class name mentions Locator, and widgets with an
explicitly-set palette, then quit."""
import os

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import QApplication, QWidget
from qgis.utils import iface

OUT = os.environ.get("BIRUNI_PROBE_OUT", ".")


def run():
    mw = iface.mainWindow()
    lines = []
    for w in mw.findChildren(QWidget):
        cls = w.metaObject().className()
        if "ocator" in cls:
            p = w.parentWidget()
            lines.append(f"LOC {cls} name={w.objectName()} parent={p.metaObject().className() if p else None}/{p.objectName() if p else None} py={type(w).__name__}")
    pal_cls = {}
    for w in QApplication.allWidgets():
        if w.testAttribute(Qt.WidgetAttribute.WA_SetPalette):
            c = w.metaObject().className()
            pal_cls[c] = pal_cls.get(c, 0) + 1
    lines.append("PALETTE-SET " + ", ".join(f"{k}:{v}" for k, v in sorted(pal_cls.items())))
    with open(os.path.join(OUT, "locator_probe.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    QTimer.singleShot(200, QApplication.instance().quit)


QTimer.singleShot(5000, run)
