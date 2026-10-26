import sys

from qgis.core import QgsApplication, QgsVectorLayer

app = QgsApplication([], False)
app.initQgis()
v = QgsVectorLayer("Point?crs=EPSG:4326", "x", "memory")
for sig in ("committedFeaturesAdded", "committedFeaturesRemoved", "committedGeometriesChanges",
            "committedAttributeValuesChanges", "committedAttributesAdded", "committedAttributesDeleted",
            "afterRollBack", "editingStarted", "editingStopped", "featureAdded", "geometryChanged",
            "attributeValueChanged", "featureDeleted"):
    print("OK  " if hasattr(v, sig) else "FAIL", sig)
try:
    from qgis.gui import QgsGui

    print("OK   QgsGui.historyProviderRegistry", hasattr(QgsGui, "historyProviderRegistry"))
    from qgis.gui import QgsHistoryProviderRegistry

    print("OK   entryAdded", hasattr(QgsHistoryProviderRegistry, "entryAdded"))
except Exception as e:
    print("FAIL history", e)
for mod in ("openpyxl", "xlsxwriter", "pandas"):
    try:
        m = __import__(mod)
        print("OK  ", mod, getattr(m, "__version__", ""))
    except Exception as e:
        print("FAIL", mod, e)
app.exitQgis()
