"""Biruni GIS Processing provider: general-purpose tools that QGIS lacks or
hides. Algorithms only depend on qgis.core so they also run in qgis_process.

Every module of this package is scanned: each BiruniAlgorithm subclass with an
ID becomes a tool, so a new tool is just a new file."""
import importlib
import inspect
import os
import pkgutil

from qgis.core import QgsMessageLog, QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .base import BiruniAlgorithm


def algorithm_classes():
    found = []
    for mod in sorted(m.name for m in pkgutil.iter_modules(__path__)):
        if mod == "base":
            continue
        try:
            module = importlib.import_module(f"{__name__}.{mod}")
        except Exception as e:  # one broken tool must not take the others down
            QgsMessageLog.logMessage(f"{mod}: {e}", "Biruni GIS")
            continue
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BiruniAlgorithm) and cls.ID and cls.__module__ == module.__name__:
                found.append(cls)
    return found


class BiruniProvider(QgsProcessingProvider):
    def id(self):
        return "biruni"

    def name(self):
        return "Biruni GIS"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "branding", "logo.svg"))

    def loadAlgorithms(self):  # noqa: N802
        for cls in algorithm_classes():
            self.addAlgorithm(cls())
