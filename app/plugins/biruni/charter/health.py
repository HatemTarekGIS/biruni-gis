"""Data health: how many features break which rule of the charter, and a score out of 100.

``HealthWatcher`` recomputes after edits (debounced) - it exists only while the charter window is open.
"""
from collections import Counter, defaultdict

from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils, QgsFeatureRequest
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from . import model
from .apply import checks

MAX_FIDS = 5000


class HealthItem:
    def __init__(self, check):
        self.id, self.field, self.title = check.id, check.field, check.title
        self.level, self.kind = check.level, check.kind
        self.fids = []

    @property
    def count(self):
        return len(self.fids)


class HealthReport:
    def __init__(self, total, items, bad_errors, bad_warnings, errors=()):
        self.total = total
        self.items = items                  # HealthItem with at least one feature, worst first
        self.bad_errors = bad_errors        # features breaking at least one error rule
        self.bad_warnings = bad_warnings    # features breaking only warnings
        self.errors = list(errors)          # rules that could not be evaluated (text)

    @property
    def score(self):
        if not self.total:
            return 100
        return max(0, round(100 * (1 - (self.bad_errors + 0.5 * self.bad_warnings) / self.total)))

    def by_field(self):
        out = defaultdict(int)
        for it in self.items:
            out[it.field] += it.count
        return dict(out)

    def summary(self):
        return {"total": self.total, "score": self.score, "errors": self.bad_errors, "warnings": self.bad_warnings,
                "items": [(it.title, it.field, it.level, it.count) for it in self.items]}


def evaluate(layer, charter=None, lists=None, limit=500000):
    """The health of a layer against its charter (the stored one by default)."""
    charter = charter or model.stored_charter(layer) or model.new_charter()
    lists = model.project_lists() if lists is None else lists
    all_checks = checks(charter, lists, layer.fields())
    ctx = QgsExpressionContext()
    ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    compiled, errors, items = [], [], []
    for ch in all_checks:
        it = HealthItem(ch)
        items.append(it)
        if ch.kind == "unique":
            compiled.append((it, None))
            continue
        e = QgsExpression(ch.expr)
        if e.hasParserError():
            errors.append(f"{ch.title}: {e.parserErrorString()}")
            continue
        e.prepare(ctx)
        compiled.append((it, e))
    unique_fields = [it.field for it, e in compiled if e is None]
    seen = {f: Counter() for f in unique_fields}
    first = {f: {} for f in unique_fields}
    bad_err, bad_warn = set(), set()
    total = 0
    need_geom = any(e is not None and e.needsGeometry() for _it, e in compiled)
    req = QgsFeatureRequest()
    if not need_geom:
        req.setFlags(QgsFeatureRequest.Flag.NoGeometry)
    for feat in layer.getFeatures(req):
        total += 1
        ctx.setFeature(feat)
        for it, e in compiled:
            if e is None:
                v = feat.attribute(it.field)
                if v is None or (hasattr(v, "isNull") and v.isNull()) or str(v).strip() == "":
                    continue
                key = str(v)
                seen[it.field][key] += 1
                if seen[it.field][key] == 1:
                    first[it.field][key] = feat.id()
                    continue
                if seen[it.field][key] == 2:
                    it.fids.append(first[it.field][key])
                    bad_err.add(first[it.field][key])
                ok = False
            else:
                r = e.evaluate(ctx)
                if e.hasEvalError():
                    ok = True
                else:
                    ok = bool(r) and not (hasattr(r, "isNull") and r.isNull())
            if not ok:
                if len(it.fids) < MAX_FIDS:
                    it.fids.append(feat.id())
                (bad_err if it.level == "error" else bad_warn).add(feat.id())
        if total >= limit:
            break
    for it, e in compiled:
        if e is not None and e.hasEvalError() and not it.fids:
            errors.append(f"{it.title}: {e.evalErrorString()}")
    bad_warn -= bad_err
    shown = sorted((it for it in items if it.fids), key=lambda it: (it.level != "error", -it.count))
    return HealthReport(total, shown, len(bad_err), len(bad_warn), errors)


class HealthWatcher(QObject):
    """Recomputes the health of one layer after its edits settle; emits ``updated(report)``."""

    updated = pyqtSignal(object)

    def __init__(self, layer, delay=700, parent=None):
        super().__init__(parent)
        self.layer = layer
        self.report = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay)
        self._timer.timeout.connect(self.refresh)
        self._signals = []
        for name in ("featureAdded", "featureDeleted", "attributeValueChanged", "geometryChanged",
                     "afterCommitChanges", "afterRollBack", "updatedFields", "dataChanged"):
            sig = getattr(layer, name, None)
            if sig is not None:
                sig.connect(self.schedule)
                self._signals.append(sig)

    def schedule(self, *_):
        self._timer.start()

    def refresh(self):
        try:
            self.report = evaluate(self.layer)
        except RuntimeError:  # the layer was deleted
            self.stop()
            return None
        self.updated.emit(self.report)
        return self.report

    def stop(self):
        self._timer.stop()
        for sig in self._signals:
            try:
                sig.disconnect(self.schedule)
            except (TypeError, RuntimeError):
                pass
        self._signals = []
