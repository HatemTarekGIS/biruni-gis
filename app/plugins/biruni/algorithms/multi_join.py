from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterString, QgsProcessingUtils)
from qgis.PyQt.QtCore import QMetaType

from .. import arabic_text
from ..i18n import tr
from .base import BiruniAlgorithm

# how values are compared
METHODS = ["Exactly", "Ignoring case and extra spaces", "Ignoring case, spaces and Arabic letter forms (أ/ا, ة/ه, ى/ي)"]
# what to do with several matches
MODES = ["Take the first match (one row per feature)", "One row per match", "Count the matches and list their values"]
LIST_SEP = " | "


def _text(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v)
    return None if s == "NULL" else s


def make_key(values, method):
    """A comparable key from several values; None when one of them is empty."""
    out = []
    for v in values:
        s = _text(v)
        if s is None:
            return None
        if method == 1:
            s = " ".join(s.split()).lower()
        elif method == 2:
            s = arabic_text.normalise(s)
        if s == "":
            return None
        out.append(s)
    return tuple(out)


class MultiFieldJoin(BiruniAlgorithm):
    """Join attributes on a composite key (two to four fields): district + plan +
    parcel number, road + chainage … QGIS and ArcGIS Pro join on one field only."""

    ID = "multijoin"
    NAME = "Join attributes by several fields"
    GROUP = "Join & attributes"
    GROUP_ID = "join"
    HELP = ("Copies fields from a second layer or table where several fields match at once - for example district, "
            "plan number and parcel number together. Pick the fields of the input and the matching fields of the "
            "other table in the same order. Matching can ignore case, extra spaces and the different ways Arabic "
            "letters are written. When a feature has several matches, take the first, make one row per match, or "
            "count them and list their values. Features without a match are listed in a second output.")

    INPUT, FIELDS, JOIN, JOIN_FIELDS, COPY, METHOD, MODE, PREFIX, DISCARD, OUTPUT, UNMATCHED = (
        "INPUT", "FIELDS", "JOIN", "JOIN_FIELDS", "COPY", "METHOD", "MODE", "PREFIX", "DISCARD", "OUTPUT",
        "UNMATCHED")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Input layer"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS, tr("Key fields of the input (in order)"), parentLayerParameterName=self.INPUT,
            allowMultiple=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.JOIN, tr("Layer or table to join"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(
            self.JOIN_FIELDS, tr("Matching fields of the joined table (same order)"),
            parentLayerParameterName=self.JOIN, allowMultiple=True))
        self.addParameter(QgsProcessingParameterField(
            self.COPY, tr("Fields to copy (empty = all)"), parentLayerParameterName=self.JOIN, allowMultiple=True,
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, tr("Compare values"), [tr(m) for m in METHODS], defaultValue=1))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, tr("When there are several matches"), [tr(m) for m in MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.PREFIX, tr("Prefix for the copied fields"), defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DISCARD, tr("Leave out features without a match"), defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Joined layer")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.UNMATCHED, tr("Features without a match"), Qgis.ProcessingSourceType.Vector, optional=True,
            createByDefault=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        join = self.parameterAsSource(parameters, self.JOIN, context)
        if source is None or join is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT if source is None
                                                                  else self.JOIN))
        keys = self.parameterAsStrings(parameters, self.FIELDS, context)
        jkeys = self.parameterAsStrings(parameters, self.JOIN_FIELDS, context)
        if not keys or len(keys) != len(jkeys):
            raise QgsProcessingException(tr("Choose the same number of key fields on both sides (one to four)."))
        if len(keys) > 4:
            raise QgsProcessingException(tr("Choose one to four pairs of fields."))
        copy = self.parameterAsStrings(parameters, self.COPY, context) or [f.name() for f in join.fields()]
        method = self.parameterAsEnum(parameters, self.METHOD, context)
        mode = self.parameterAsEnum(parameters, self.MODE, context)
        prefix = self.parameterAsString(parameters, self.PREFIX, context) or ""
        discard = self.parameterAsBoolean(parameters, self.DISCARD, context)

        jf = join.fields()
        copy_idx = [jf.indexOf(n) for n in copy if jf.indexOf(n) >= 0]
        add = QgsFields()
        if mode == 2:
            add.append(QgsField(f"{prefix}match_count", QMetaType.Type.Int))
            for i in copy_idx:
                add.append(QgsField(prefix + jf.at(i).name(), QMetaType.Type.QString))
        else:
            for i in copy_idx:
                f = QgsField(jf.at(i))
                f.setName(prefix + f.name())
                add.append(f)
        out_fields = QgsProcessingUtils.combineFields(source.fields(), add)

        # index the joined table
        jidx = [jf.indexOf(n) for n in jkeys]
        table = {}
        for feat in join.getFeatures(QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry)):
            if feedback.isCanceled():
                return {}
            attrs = feat.attributes()
            k = make_key([attrs[i] for i in jidx], method)
            if k is not None:
                table.setdefault(k, []).append([attrs[i] for i in copy_idx])

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, out_fields, source.wkbType(),
                                          source.sourceCrs())
        usink, udest = self.parameterAsSink(parameters, self.UNMATCHED, context, source.fields(),
                                            source.wkbType(), source.sourceCrs())
        sidx = [source.fields().indexOf(n) for n in keys]
        total = source.featureCount() or 1
        matched = unmatched = rows = multi = 0
        for n, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                return {}
            attrs = feat.attributes()
            hits = table.get(make_key([attrs[i] for i in sidx], method), [])
            if hits:
                matched += 1
                multi += len(hits) > 1
            else:
                unmatched += 1
                if usink is not None:
                    usink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
                if discard:
                    continue
            if mode == 0 or not hits:
                extras = [hits[0] if hits else [None] * len(copy_idx)]
            elif mode == 1:
                extras = hits
            else:
                lists = []
                for c in range(len(copy_idx)):
                    seen = []
                    for h in hits:
                        t = _text(h[c])
                        if t is not None and t not in seen:
                            seen.append(t)
                    lists.append(LIST_SEP.join(seen) if seen else None)
                extras = [[len(hits)] + lists]
            for extra in extras:
                out = QgsFeature(out_fields)
                out.setGeometry(feat.geometry())
                out.setAttributes(list(attrs) + list(extra))
                sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                rows += 1
            feedback.setProgress(100 * n / total)
        feedback.pushInfo(tr("{m} features matched ({x} of them more than once), {u} without a match.").format(
            m=matched, x=multi, u=unmatched))
        result = {self.OUTPUT: dest, "MATCHED": matched, "UNMATCHED_COUNT": unmatched, "MULTIPLE": multi,
                  "ROWS": rows}
        if usink is not None:
            result[self.UNMATCHED] = udest
        return result
