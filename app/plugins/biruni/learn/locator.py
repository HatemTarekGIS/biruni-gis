"""Search box filter: type an ArcGIS Pro tool name (or a Biruni one) and run
the matching Biruni tool. Works without a prefix; the prefix "ag" lists only
the ArcGIS names."""
from qgis.core import QgsApplication, QgsLocatorFilter, QgsLocatorResult

from ..i18n import tr
from .dictionary import TERMS


class ArcGISNamesFilter(QgsLocatorFilter):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def clone(self):
        return ArcGISNamesFilter(self.plugin)

    def name(self):
        return "biruni_arcgis_names"

    def displayName(self):  # noqa: N802
        return tr("ArcGIS tool names")

    def prefix(self):
        return "ag"

    def priority(self):
        return QgsLocatorFilter.Priority.High

    def flags(self):
        return QgsLocatorFilter.Flag.FlagFast

    def fetchResults(self, text, context, feedback):  # noqa: N802
        q = text.strip().lower()
        if len(q) < 3:
            return
        for name, target, note in TERMS:
            low = name.lower()
            if q in low or all(w in low for w in q.split()):
                r = QgsLocatorResult(self, f"{name}  →  {self._target_label(target)}", {"target": target})
                r.description = tr(note) if note else tr("ArcGIS Pro name")
                r.score = 1.0 if low.startswith(q) else 0.6
                r.group = tr("ArcGIS tool names")
                self.resultFetched.emit(r)

    def _target_label(self, target):
        from ..commands import CATALOG

        if target in CATALOG:
            return tr(CATALOG[target].label).replace("\n", " ")
        alg = QgsApplication.processingRegistry().algorithmById(target)
        return alg.displayName() if alg else target

    def triggerResult(self, result):  # noqa: N802
        data = result.userData() if callable(getattr(result, "userData", None)) else result.userData
        target = data["target"]
        from ..commands import CATALOG

        if target in CATALOG:
            self.plugin.runner.execute(CATALOG[target])
        else:
            self.plugin.runner.open_algorithm(target)
