import os
import sys
import traceback

from qgis.core import QgsApplication

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app", "plugins"))
app = QgsApplication([], False)
app.initQgis()
from biruni.algorithms import ALGORITHMS  # noqa: E402

for cls in ALGORITHMS:
    try:
        a = cls()
        a.initAlgorithm({})
        b = a.createInstance()
        print("OK", cls.__name__, type(b))
        c = a.create({})
        print("   create ->", c)
    except Exception:
        print("FAIL", cls.__name__)
        traceback.print_exc()
app.exitQgis()
