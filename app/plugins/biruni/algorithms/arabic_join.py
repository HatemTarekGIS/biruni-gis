from collections import Counter, defaultdict

from qgis.core import (Qgis, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterNumber, QgsProcessingParameterString)
from qgis.PyQt.QtCore import QMetaType

from ..arabic_text import normalise, similarity_keys, trigrams
from ..i18n import tr
from .base import BiruniAlgorithm

CANDIDATES = 15          # best trigram-sharing rows scored per value
DOUBT_MARGIN = 0.07      # a fuzzy match this close to the threshold is listed for review
AMBIGUOUS_GAP = 0.02     # ... and so is one whose runner-up is this close


def _text(value):
    if value is None:
        return ""
    s = str(value)
    return "" if s == "NULL" else s.strip()


class NameIndex:
    """The values of the join table, ready for exact, normalised and fuzzy lookups."""

    def __init__(self, values):
        self.values = values
        self.raw = defaultdict(list)
        self.norm = defaultdict(list)
        self.keys, self.tris = [], []
        postings = defaultdict(list)
        for i, v in enumerate(values):
            key = normalise(v, True)
            tri = trigrams(key, True)
            self.keys.append(key)
            self.tris.append(tri)
            if not key:
                continue
            self.raw[v.casefold()].append(i)
            self.norm[key.replace(" ", "")].append(i)
            for t in tri:
                postings[t].append(i)
        limit = max(200, len(values) // 5)
        self.postings = postings
        self.rare = {t: p for t, p in postings.items() if len(p) <= limit}

    def candidates(self, value):
        """[(score, row, type)] best first: exact, normalised, then fuzzy."""
        out, seen = [], set()
        for i in self.raw.get(value.casefold(), []):
            out.append((1.0, i, "exact"))
            seen.add(i)
        key = normalise(value, True)
        if not key:
            return out
        for i in self.norm.get(key.replace(" ", ""), []):
            if i not in seen:
                out.append((1.0, i, "normalised"))
                seen.add(i)
        tri = trigrams(key, True)
        counts = Counter()
        source = self.rare if any(t in self.rare for t in tri) else self.postings
        for t in tri:
            counts.update(source.get(t, ()))
        fuzzy = []
        for i, _ in counts.most_common(CANDIDATES + len(seen)):
            if i in seen:
                continue
            fuzzy.append((similarity_keys(key, self.keys[i], tri, self.tris[i]), i, "fuzzy"))
        fuzzy.sort(key=lambda x: -x[0])
        return out + fuzzy[:CANDIDATES]


class ArabicJoin(BiruniAlgorithm):
    ID = "arabicjoin"
    NAME = "Join by similar Arabic names"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Joins a table to a layer by names that are written differently: «الروضه» / «الروضة» / «حي الروضة», "
            "«عبدالله» / «عبد الله», hamza and alef forms, ى / ي, tashkeel, Arabic-Indic digits, extra spaces and "
            "Latin capitals. Exact matches are taken first, then names that are equal once normalised, then the "
            "most similar name when its score reaches the threshold (1 = identical). Every row gets match_type "
            "(exact, normalised, fuzzy, none), match_score and the matched name; names that found nothing, or a "
            "doubtful match, are listed with their three best candidates for review. With one-to-one, each row "
            "of the table is used once.")

    INPUT, FIELD, JOIN, JOIN_FIELD, FIELDS, THRESHOLD, ONE_TO_ONE, PREFIX, OUTPUT, REVIEW = (
        "INPUT", "FIELD", "JOIN", "JOIN_FIELD", "FIELDS", "THRESHOLD", "ONE_TO_ONE", "PREFIX", "OUTPUT", "REVIEW")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(self.FIELD, tr("Name field"), parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.JOIN, tr("Table to join"), [Qgis.ProcessingSourceType.Vector]))
        self.addParameter(QgsProcessingParameterField(
            self.JOIN_FIELD, tr("Name field of the table"), parentLayerParameterName=self.JOIN))
        self.addParameter(QgsProcessingParameterField(
            self.FIELDS, tr("Fields to copy (none = all)"), parentLayerParameterName=self.JOIN,
            allowMultiple=True, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD, tr("Lowest similarity accepted (0-1)"), Qgis.ProcessingNumberParameterType.Double, 0.85,
            minValue=0.0, maxValue=1.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ONE_TO_ONE, tr("Use each row of the table once (one-to-one)"), False))
        self.addParameter(QgsProcessingParameterString(self.PREFIX, tr("Prefix for the copied fields"), "",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Joined layer")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.REVIEW, tr("Names to review"), Qgis.ProcessingSourceType.Vector))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        table = self.parameterAsSource(parameters, self.JOIN, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        if table is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.JOIN))
        field = self.parameterAsString(parameters, self.FIELD, context)
        jfield = self.parameterAsString(parameters, self.JOIN_FIELD, context)
        wanted = self.parameterAsStrings(parameters, self.FIELDS, context)
        threshold = self.parameterAsDouble(parameters, self.THRESHOLD, context)
        one_to_one = self.parameterAsBool(parameters, self.ONE_TO_ONE, context)
        prefix = self.parameterAsString(parameters, self.PREFIX, context) or ""

        jfields = table.fields()
        copy_idx = [jfields.lookupField(n) for n in wanted] if wanted else list(range(jfields.count()))
        copy_idx = [i for i in copy_idx if i >= 0]
        out_fields = QgsFields(source.fields())
        used = {f.name().lower() for f in out_fields}
        for i in copy_idx:
            f = QgsField(jfields.at(i))
            name = prefix + f.name()
            base, k = name, 2
            while name.lower() in used:
                name = f"{base}_{k}"
                k += 1
            used.add(name.lower())
            f.setName(name)
            out_fields.append(f)
        extra = [("matched_value", QMetaType.Type.QString), ("match_score", QMetaType.Type.Double),
                 ("match_type", QMetaType.Type.QString)]
        for name, t in extra:
            out_fields.append(QgsField(name, t))

        feedback.pushInfo(tr("Reading the table…"))
        jidx = jfields.lookupField(jfield)
        jrows, jvalues = [], []
        for jf in table.getFeatures(QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry)):
            attrs = jf.attributes()
            jrows.append([attrs[i] for i in copy_idx])
            jvalues.append(_text(attrs[jidx]))
        index = NameIndex(jvalues)

        feedback.pushInfo(tr("Matching names…"))
        fidx = source.fields().lookupField(field)
        feats = list(source.getFeatures())
        cache, cands = {}, []
        total = len(feats) or 1
        for n, f in enumerate(feats):
            if feedback.isCanceled():
                return {}
            v = _text(f.attributes()[fidx])
            if v not in cache:
                cache[v] = index.candidates(v) if v else []
            cands.append(cache[v])
            feedback.setProgress(80 * n / total)

        chosen = [None] * len(feats)
        if one_to_one:
            rank = {"exact": 0, "normalised": 1, "fuzzy": 2}
            pairs = [(rank[t], -s, n, i, t, s) for n, cl in enumerate(cands) for s, i, t in cl
                     if t != "fuzzy" or s >= threshold]
            pairs.sort()
            taken = set()
            for _, _, n, i, t, s in pairs:
                if chosen[n] is None and i not in taken:
                    chosen[n] = (s, i, t)
                    taken.add(i)
        else:
            for n, cl in enumerate(cands):
                if cl and (cl[0][2] != "fuzzy" or cl[0][0] >= threshold):
                    chosen[n] = cl[0]

        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, out_fields, source.wkbType(),
                                          source.sourceCrs())
        rf = QgsFields()
        for name, t in [("feature_id", QMetaType.Type.LongLong), ("value", QMetaType.Type.QString),
                        ("normalised", QMetaType.Type.QString), ("status", QMetaType.Type.QString)] + \
                [(f"{k}_{j}", t) for j in (1, 2, 3)
                 for k, t in (("candidate", QMetaType.Type.QString), ("score", QMetaType.Type.Double))]:
            rf.append(QgsField(name, t))
        rsink, rdest = self.parameterAsSink(parameters, self.REVIEW, context, rf, Qgis.WkbType.NoGeometry)

        stats = Counter()
        blank = [None] * len(copy_idx)
        for n, f in enumerate(feats):
            c = chosen[n]
            out = QgsFeature(out_fields)
            out.setGeometry(f.geometry())
            if c:
                s, i, t = c
                out.setAttributes(f.attributes() + jrows[i] + [jvalues[i], round(s, 4), t])
            else:
                t = "none"
                out.setAttributes(f.attributes() + blank + [None, None, t])
            stats[t] += 1
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            status = None
            if t == "none":
                status = "no match"
            elif t == "fuzzy":
                others = [x for x in cands[n] if x[1] != c[1]]
                if c[0] < threshold + DOUBT_MARGIN:
                    status = "doubtful"
                elif others and c[0] - others[0][0] <= AMBIGUOUS_GAP:
                    status = "ambiguous"
            if status:
                v = _text(f.attributes()[fidx])
                row = [f.id(), v, normalise(v, True), status]
                for s, i, _ in (cands[n] + [(None, None, None)] * 3)[:3]:
                    row += [jvalues[i] if i is not None else None, round(s, 4) if s is not None else None]
                rf_feat = QgsFeature(rf)
                rf_feat.setAttributes(row)
                rsink.addFeature(rf_feat, QgsFeatureSink.Flag.FastInsert)
                stats["review"] += 1
        feedback.setProgress(100)
        feedback.pushInfo(tr("{e} exact, {n} normalised, {f} similar, {u} without a match; {r} to review.").format(
            e=stats["exact"], n=stats["normalised"], f=stats["fuzzy"], u=stats["none"], r=stats["review"]))
        return {self.OUTPUT: dest, self.REVIEW: rdest, "EXACT": stats["exact"], "NORMALISED": stats["normalised"],
                "FUZZY": stats["fuzzy"], "UNMATCHED": stats["none"], "REVIEW_COUNT": stats["review"]}
