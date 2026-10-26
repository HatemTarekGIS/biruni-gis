import sys

from qgis.core import QgsApplication

app = QgsApplication([], False)
app.initQgis()
sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
alg = QgsApplication.processingRegistry().algorithmById("native:dxfexport")
for p in alg.parameterDefinitions():
    extra = ""
    if hasattr(p, "options"):
        opts = p.options()
        extra = f" options[{len(opts)}]={opts[:5]}… utf8_index={[i for i, o in enumerate(opts) if 'UTF' in o.upper()][:3]}"
    print(p.name(), type(p).__name__, repr(p.defaultValue()), extra)
app.exitQgis()
