"""Search box filter: type an ArcGIS Pro tool name (or a Biruni one), or what
you want to do in plain words (learn/tasks.py), and run the matching Biruni
tool. Works without a prefix; the prefix "ag" lists only the ArcGIS names."""
from qgis.core import QgsApplication, QgsLocatorFilter, QgsLocatorResult

from .. import search_text
from ..i18n import tr
from .dictionary import TERMS
from .tasks import search as search_tasks

_COMMANDS = {}   # language -> [(command id, shown label, folded label + English + synonyms, folded label)]


def search_commands(text, n=8):
    """[(command id, label, score)] of the buttons whose name holds every word typed, best first. Hamzas,
    accents and Persian letters do not matter; the older wording of a button is kept as a synonym."""
    from .. import i18n
    from ..commands import CATALOG

    words = search_text.fold(text).split()
    if not words or len("".join(words)) < 2:
        return []
    index = _COMMANDS.get(i18n.lang())
    if index is None:
        syn = i18n.search_synonyms()
        index = _COMMANDS[i18n.lang()] = []
        for cid, cmd in CATALOG.items():
            shown = tr(cmd.label).replace("\n", " ")
            extra = " ".join(syn.get(cmd.label, []))
            index.append((cid, shown, search_text.fold(f"{shown} {cmd.label} {extra}"), search_text.fold(shown)))
    out = []
    for cid, shown, hay, own in index:
        if all(w in hay for w in words):
            out.append((cid, shown, 0.9 if own.startswith(words[0]) else 0.7))
    out.sort(key=lambda x: (-x[2], len(x[1])))
    return out[:n]


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
        if getattr(context, "usingPrefix", False):  # "ag …" asks for ArcGIS names only
            return
        # the ribbon's own buttons, by their name in the program's language and in English
        for cid, label, score in search_commands(text):
            r = QgsLocatorResult(self, label, {"target": cid})
            r.description = tr("Button")
            r.score = score
            r.group = tr("Buttons")
            self.resultFetched.emit(r)
        # everyday tasks in plain words ("احسب المسافة بين النقط والطرق")
        for score, target, phrase in search_tasks(text, 5):
            if ":" in target and not self._alg_exists(target):
                continue
            r = QgsLocatorResult(self, f"{phrase}  →  {self._target_label(target)}", {"target": target})
            r.description = tr("Task")
            r.score = min(score, 0.95)
            r.group = tr("What do you want to do?")
            self.resultFetched.emit(r)

    @staticmethod
    def _alg_exists(target):
        return QgsApplication.processingRegistry().algorithmById(target) is not None

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
