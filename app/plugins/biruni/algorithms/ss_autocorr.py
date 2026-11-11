"""Spatial statistics - autocorrelation tools: global Moran's I, local Moran (LISA), Getis-Ord Gi* hot
spots (also for bare incidents counted in hexagons), incremental spatial autocorrelation, bivariate local
Moran, the co-location quotient, emerging hot spots (space-time) and the scale check (MAUP).

Every tool checks the data first (too few features, degrees used as distances, islands, missing or
constant values, duplicate points), answers in one sentence (ANSWER), and can write an HTML report with
the numbers, the charts and what the result does not mean. Permutations use a fixed seed (SEED)."""
import math

import numpy as np
from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingLayerPostProcessorInterface, QgsProcessingOutputNumber,
                       QgsProcessingOutputString, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterString, QgsRendererCategory, QgsSymbol,
                       QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor

from ..i18n import tr
from ..spatial_stats import autocorr as ac
from ..spatial_stats import scale as sc
from ..spatial_stats import spacetime as stc
from ..spatial_stats import weights as wts
from ..spatial_stats.common import GROUP, GROUP_ID, rng, say, text_lang
from ..spatial_stats.report import write_report
from .base import BiruniAlgorithm

MIN_HARD, MIN_SOFT = 8, 30
MAX_PAIRS = 6e6  # neighbour pairs: beyond this the distance is far too large for the data

# ---- words of the explanations (Arabic / English) ------------------------------------------------------
_QUAD_WORDS = {
    ac.HH: {"en": "High among high (hot cluster)", "ar": "مرتفع وسط مرتفع (تجمّع ساخن)"},
    ac.LL: {"en": "Low among low (cold cluster)", "ar": "منخفض وسط منخفض (تجمّع بارد)"},
    ac.HL: {"en": "High among low (outlier)", "ar": "مرتفع وسط منخفض (شاذ)"},
    ac.LH: {"en": "Low among high (outlier)", "ar": "منخفض وسط مرتفع (شاذ)"},
    ac.NS: {"en": "Not significant", "ar": "غير دال إحصائيًا"},
}
_BIN_WORDS = {
    3: {"en": "Hot spot - 99% confidence", "ar": "بؤرة ساخنة - ثقة 99%"},
    2: {"en": "Hot spot - 95% confidence", "ar": "بؤرة ساخنة - ثقة 95%"},
    1: {"en": "Hot spot - 90% confidence", "ar": "بؤرة ساخنة - ثقة 90%"},
    0: {"en": "Not significant", "ar": "غير دال إحصائيًا"},
    -1: {"en": "Cold spot - 90% confidence", "ar": "بؤرة باردة - ثقة 90%"},
    -2: {"en": "Cold spot - 95% confidence", "ar": "بؤرة باردة - ثقة 95%"},
    -3: {"en": "Cold spot - 99% confidence", "ar": "بؤرة باردة - ثقة 99%"},
}
_EHSA_WORDS = {
    "new": {"en": "New {s}", "ar": "{s} جديدة"},
    "consecutive": {"en": "Consecutive {s}", "ar": "{s} متتالية"},
    "intensifying": {"en": "Intensifying {s}", "ar": "{s} تشتد"},
    "persistent": {"en": "Persistent {s}", "ar": "{s} دائمة"},
    "diminishing": {"en": "Diminishing {s}", "ar": "{s} تضعف"},
    "sporadic": {"en": "Sporadic {s}", "ar": "{s} متقطعة"},
    "oscillating": {"en": "Oscillating {s}", "ar": "{s} متذبذبة"},
    "historical": {"en": "Historical {s}", "ar": "{s} سابقة"},
}
_SIDE_WORDS = {1: {"en": "hot spot", "ar": "بؤرة ساخنة"}, -1: {"en": "cold spot", "ar": "بؤرة باردة"}}
_CAVEATS = {
    "cause": {"en": "A pattern is not an explanation: these statistics show where values cluster, not why. "
                    "Correlation in space is not causation.",
              "ar": "النمط ليس تفسيرًا: هذه الإحصاءات تبيّن أين تتجمع القيم لا لماذا. الارتباط المكاني لا يعني السببية."},
    "multiple": {"en": "Every place is tested separately, so with many places some look significant by pure "
                       "chance. The false discovery rate (FDR) correction limits this; without it, expect "
                       "about 5% false alarms.",
                 "ar": "كل مكان يُختبر وحده، ومع كثرة الأماكن يبدو بعضها دالًا بالصدفة وحدها. تصحيح معدل الاكتشاف "
                       "الكاذب (FDR) يحدّ من ذلك؛ وبدونه توقّع نحو 5% إنذارات كاذبة."},
    "edge": {"en": "Edge effects: features at the border of the study area have fewer neighbours (the data "
                   "outside it is missing), so their results are less reliable.",
             "ar": "أثر الحواف: العناصر عند حدود منطقة الدراسة جيرانها أقل (البيانات خارجها غير موجودة)، فنتائجها "
                   "أقل موثوقية."},
    "maup": {"en": "Scale and zoning (MAUP): with other units or another cell size the result can change. The "
                   "Scale check tool tells whether the conclusion holds across sizes.",
             "ar": "مشكلة الوحدة المساحية (MAUP): بوحدات أخرى أو حجم خلية آخر قد تتغير النتيجة. أداة فحص المقياس تبيّن "
                   "هل الاستنتاج ثابت عبر الأحجام."},
    "weights": {"en": "The answer depends on who counts as a neighbour. If it matters, run it again with "
                      "another neighbourhood and see whether it holds.",
                "ar": "النتيجة تتوقف على من يُعدّ جارًا. إن كانت مهمة فأعد التشغيل بجوار آخر وانظر هل تثبت."},
    "counts": {"en": "Counts follow where people and activity are: more accidents where there is more traffic. "
                     "A hot spot of counts is not automatically a high-risk place; divide by exposure "
                     "(population, traffic) for a rate when you can.",
               "ar": "الأعداد تتبع أماكن الناس والنشاط: حوادث أكثر حيث المرور أكثر. البؤرة الساخنة للأعداد ليست "
                     "بالضرورة مكانًا عالي الخطورة؛ اقسم على حجم التعرّض (السكان، المرور) لتحصل على معدل متى أمكن."},
}


def _f(v, digits=3):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    return f"{v:.{digits}f}"


def fmt_p(p):
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def fmt_dist(d):
    ar = text_lang() == "ar"
    if d >= 5000:
        return f"{d / 1000:,.2f} " + ("كم" if ar else "km")
    return f"{d:,.0f} " + ("م" if ar else "m")


def _best_p(r):
    """The permutation p-value; when every reshuffle was less extreme it only says p <= 1/(P+1), and the
    formula's p (accurate far in the tail) is used instead."""
    if r["p_sim"] is None:
        return r["p_norm"]
    floor = 1.0 / (r["sims"].size + 1)
    return min(r["p_sim"], r["p_norm"]) if r["p_sim"] <= floor + 1e-12 else r["p_sim"]


def _flag_advanced(p):
    p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
    return p


# ---- styling the output layer -----------------------------------------------------------------------------
class _Styler(QgsProcessingLayerPostProcessorInterface):
    """Categorised colours applied once the output is loaded: [(value, colour, label)]."""

    instances = []

    def __init__(self, field, cats):
        super().__init__()
        self.field, self.cats = field, cats
        _Styler.instances.append(self)

    def postProcessLayer(self, layer, context, feedback):  # noqa: N802
        if layer is None or layer.fields().indexOf(self.field) < 0:
            return
        out = []
        for value, colour, label in self.cats:
            sym = QgsSymbol.defaultSymbol(layer.geometryType())
            sym.setColor(QColor(colour))
            if layer.geometryType() == Qgis.GeometryType.Polygon:
                sym.symbolLayer(0).setStrokeColor(QColor("#ffffff"))
                sym.symbolLayer(0).setStrokeWidth(0.1)
            elif layer.geometryType() == Qgis.GeometryType.Point:
                sym.setSize(2.4)
            out.append(QgsRendererCategory(value, sym, label))
        layer.setRenderer(QgsCategorizedSymbolRenderer(self.field, out))
        layer.triggerRepaint()


def _style(context, dest, field, cats):
    if dest and context.willLoadLayerOnCompletion(dest):
        context.layerToLoadOnCompletionDetails(dest).setPostProcessor(_Styler(field, cats))


class Data:
    """The features read for an analysis: geometry in the source CRS, centres in metres."""

    def __init__(self):
        self.feats, self.geoms, self.xy, self.values = [], [], None, {}
        self.kind, self.crs, self.work, self.reprojected, self.notes = "point", None, None, False, []
        self.dropped = 0
        self.rings = None


# ---- shared plumbing -----------------------------------------------------------------------------------
class _SpatialStatsBase(BiruniAlgorithm):
    GROUP = GROUP
    GROUP_ID = GROUP_ID

    INPUT, FIELD, WEIGHTS, K, DISTANCE, PERMUTATIONS, FDR, SEED, REPORT, OUTPUT, ANSWER = (
        "INPUT", "FIELD", "WEIGHTS", "K", "DISTANCE", "PERMUTATIONS", "FDR", "SEED", "REPORT", "OUTPUT", "ANSWER")
    WEIGHT_KEYS = ["auto", "queen", "rook", "knn", "band", "inverse"]

    # parameters
    def _add_input(self, label, points_only=False):
        types = [Qgis.ProcessingSourceType.VectorPoint] if points_only else [
            Qgis.ProcessingSourceType.VectorPoint, Qgis.ProcessingSourceType.VectorPolygon]
        self.addParameter(QgsProcessingParameterFeatureSource(self.INPUT, label, types))

    def _add_field(self, name, label, optional=False):
        self.addParameter(QgsProcessingParameterField(
            name, label, parentLayerParameterName=self.INPUT, type=Qgis.ProcessingFieldParameterDataType.Numeric,
            optional=optional))

    def _add_weights(self):
        self.addParameter(QgsProcessingParameterEnum(
            self.WEIGHTS, tr("Neighbours"),
            [tr("Automatic (shared borders for polygons, 8 nearest for points)"),
             tr("Shared border or corner (queen, polygons)"), tr("Shared border (rook, polygons)"),
             tr("K nearest neighbours"), tr("Distance band"), tr("Inverse distance (within the band)")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.K, tr("Number of nearest neighbours"), Qgis.ProcessingNumberParameterType.Integer, 8, minValue=1,
            maxValue=200))
        self.addParameter(QgsProcessingParameterNumber(
            self.DISTANCE, tr("Distance band in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=0.0))

    def _add_common(self, permutations=999, fdr_option=True):
        if permutations:
            self.addParameter(QgsProcessingParameterNumber(
                self.PERMUTATIONS, tr("Permutations (random reshuffles for the p-value)"),
                Qgis.ProcessingNumberParameterType.Integer, permutations, minValue=0, maxValue=99999))
        if fdr_option:
            self.addParameter(QgsProcessingParameterBoolean(
                self.FDR, tr("Correct for testing many places at once (false discovery rate)"), True))
        self.addParameter(_flag_advanced(QgsProcessingParameterNumber(
            self.SEED, tr("Random seed (same seed = same result)"), Qgis.ProcessingNumberParameterType.Integer,
            12345, minValue=0)))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.REPORT, tr("Report (HTML)"), "HTML (*.html)", optional=True, createByDefault=False))
        self.addOutput(QgsProcessingOutputString(self.ANSWER, tr("Answer")))

    # reading and checking
    def _load(self, parameters, context, feedback, fields, points_only=False):
        src = self.parameterAsSource(parameters, self.INPUT, context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        d = Data()
        lang = text_lang()
        gtype = QgsWkbTypes.geometryType(src.wkbType())
        d.kind = "polygon" if gtype == Qgis.GeometryType.Polygon else "point"
        if points_only and d.kind != "point":
            raise QgsProcessingException(tr("This tool needs a point layer."))
        d.crs = src.sourceCrs()
        d.work, d.reprojected = wts.work_crs(d.crs, src.sourceExtent(), context)
        xf = QgsCoordinateTransform(d.crs, d.work, context.transformContext()) if d.reprojected else None
        idx = [src.fields().lookupField(f) for f in fields if f]
        xy, vals, work_geoms = [], [], []
        for f in src.getFeatures():
            if feedback.isCanceled():
                raise QgsProcessingException(tr("Cancelled."))
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                d.dropped += 1
                continue
            gw = QgsGeometry(g)
            if xf is not None:
                gw.transform(xf)
            c = gw.asPoint() if (d.kind == "point" and not gw.isMultipart()) else gw.centroid().asPoint()
            row = []
            for i in idx:
                v = f.attributes()[i]
                try:
                    row.append(float(v) if v is not None and str(v) != "NULL" else np.nan)
                except (TypeError, ValueError):
                    row.append(np.nan)
            d.feats.append(f)
            d.geoms.append(g)
            work_geoms.append(gw)
            xy.append((c.x(), c.y()))
            vals.append(row)
        d.xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        d.work_geoms = work_geoms
        vals = np.asarray(vals, dtype=float).reshape(len(xy), len(idx))
        for k, name in enumerate(f for f in fields if f):
            d.values[name] = vals[:, k]
        if d.dropped:
            self._note(d, feedback, {"en": "{n} features without geometry were left out.",
                                     "ar": "استُبعد {n} عنصرًا بلا شكل هندسي."}, n=d.dropped)
        if d.reprojected:
            self._note(d, feedback, {
                "en": "The layer is in degrees, which are not equal lengths on the ground; distances were measured "
                      "in {crs} (metres) instead.",
                "ar": "الطبقة بالدرجات وهي ليست أطوالًا متساوية على الأرض؛ لذا قيست المسافات في {crs} (بالمتر)."},
                crs=d.work.authid())
        self._check_count(d, feedback, lang)
        return d

    def _note(self, data, feedback, texts, **values):
        msg = say(texts, **values)
        data.notes.append(msg)
        feedback.pushWarning(msg)

    def _check_count(self, d, feedback, lang=None):
        n = len(d.xy)
        if n < MIN_HARD:
            raise QgsProcessingException(tr("Only {n} features: at least {m} are needed, and 30 or more for a "
                                            "trustworthy answer.").format(n=n, m=MIN_HARD))
        if n < MIN_SOFT:
            self._note(d, feedback, {"en": "Only {n} features: with fewer than 30 the result is shaky; treat it "
                                           "as a hint, not a finding.",
                                     "ar": "عدد العناصر {n} فقط: مع أقل من 30 تكون النتيجة مهتزة؛ اعتبرها مؤشرًا لا نتيجة."},
                       n=n)

    def _keep_valid(self, d, feedback, names):
        """Drop the features where a value is missing; refuse constant values."""
        ok = np.ones(len(d.xy), dtype=bool)
        for name in names:
            ok &= np.isfinite(d.values[name])
        missing = int((~ok).sum())
        if missing:
            self._note(d, feedback, {"en": "{n} features with an empty value were left out of the analysis.",
                                     "ar": "استُبعد {n} عنصرًا قيمته فارغة من التحليل."}, n=missing)
        keep = np.flatnonzero(ok)
        d.keep = keep
        d.xy_k = d.xy[keep]
        for name in names:
            v = d.values[name][keep]
            if v.size and np.ptp(v) == 0:
                raise QgsProcessingException(tr("Every value of {f} is the same: there is no pattern to find.")
                                             .format(f=name))
        d.n = keep.size
        if d.n < MIN_HARD:
            raise QgsProcessingException(tr("Only {n} features have a value: at least {m} are needed.")
                                         .format(n=d.n, m=MIN_HARD))
        return keep

    def _duplicates(self, d, feedback, xy):
        if d.kind != "point":
            return 0
        uniq = np.unique(np.round(xy, 3), axis=0)
        dup = len(xy) - len(uniq)
        if dup:
            self._note(d, feedback, {
                "en": "{n} points sit exactly on top of another point. They count as close neighbours; if they are "
                      "repeats of the same thing, remove them first.",
                "ar": "{n} نقطة تقع فوق نقطة أخرى تمامًا. تُحسب جيرانًا قريبين؛ وإن كانت تكرارًا لنفس الشيء فاحذفها أولًا."},
                n=dup)
        return dup

    def _weights(self, parameters, context, feedback, d, xy, geoms=None):
        """(sparse weights without self, description, distance used or None)."""
        mode = self.WEIGHT_KEYS[self.parameterAsEnum(parameters, self.WEIGHTS, context)]
        k = self.parameterAsInt(parameters, self.K, context)
        dist = self.parameterAsDouble(parameters, self.DISTANCE, context)
        if mode == "auto":
            mode = "queen" if d.kind == "polygon" else "knn"
        if mode in ("queen", "rook") and d.kind != "polygon":
            self._note(d, feedback, {"en": "Points share no borders: the {k} nearest neighbours were used instead.",
                                     "ar": "النقاط لا تتشارك حدودًا: استُخدم أقرب {k} جيران بدلًا من ذلك."}, k=k)
            mode = "knn"
        used = None
        if mode in ("queen", "rook"):
            w = wts.contiguity(geoms, mode == "queen", feedback)
            desc = say({"en": "polygons that share a border{c}", "ar": "المضلعات التي تتشارك حدًا{c}"},
                       c=say({"en": " or a corner (queen)", "ar": " أو ركنًا (queen)"}) if mode == "queen"
                       else " (rook)")
        elif mode == "knn":
            w = wts.knn(xy, k)
            desc = say({"en": "the {k} nearest features", "ar": "أقرب {k} عناصر"}, k=min(k, len(xy) - 1))
        else:
            auto = wts.min_threshold(xy)
            if dist <= 0:
                dist = auto
                self._note(d, feedback, {
                    "en": "Distance band chosen automatically: {d}, the smallest that gives every feature a neighbour.",
                    "ar": "اختيرت مسافة الجوار تلقائيًا: {d}، وهي أصغر مسافة تعطي كل عنصر جارًا واحدًا على الأقل."},
                    d=fmt_dist(dist))
            if wts.pair_count(xy, [dist])[0] > MAX_PAIRS:
                raise QgsProcessingException(tr("The distance band {d} makes almost every feature a neighbour of "
                                                "every other; choose a smaller distance.").format(d=fmt_dist(dist)))
            w = wts.distance_band(xy, dist, inverse=mode == "inverse")
            used = dist
            desc = say({"en": "features within {d}{inv}", "ar": "العناصر في حدود {d}{inv}"}, d=fmt_dist(dist),
                       inv=say({"en": ", weighted by 1/distance", "ar": "، بوزن 1/المسافة"}) if mode == "inverse"
                       else "")
        w, isl = wts.attach_islands(w, xy)
        if isl.size:
            self._note(d, feedback, {
                "en": "{n} features had no neighbour (islands); each was given its nearest feature as neighbour.",
                "ar": "{n} عنصرًا بلا جار (جُزر)؛ أُعطي كل منها أقرب عنصر إليه جارًا."}, n=int(isl.size))
        card = wts.cardinality(w)
        return w, desc, used, card

    def _rings(self, d, keep=None):
        """Exterior rings in metres per feature, for the report maps (None for points)."""
        if d.kind != "polygon":
            return None
        out = []
        for k in (keep if keep is not None else range(len(d.work_geoms))):
            g = d.work_geoms[k]
            parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
            out.append([np.array([(p.x(), p.y()) for p in poly[0]]) for poly in parts if poly])
        return out

    def _out_fields(self, src_fields, new):
        fields = QgsFields(src_fields)
        pos = []
        for name, typ in new:
            if fields.indexOf(name) < 0:
                fields.append(QgsField(name, typ))
            pos.append(fields.indexOf(name))
        return fields, pos

    def _write_features(self, parameters, context, d, new_fields, values_of):
        """Every input feature with the new fields; values_of(k) -> list of values for feature k (or None)."""
        src_fields = d.feats[0].fields() if d.feats else QgsFields()
        fields, pos = self._out_fields(src_fields, new_fields)
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                          QgsWkbTypes.multiType(d.feats[0].geometry().wkbType())
                                          if d.kind == "polygon" else d.feats[0].geometry().wkbType(), d.crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        for k, f in enumerate(d.feats):
            out = QgsFeature(fields)
            g = QgsGeometry(f.geometry())
            if d.kind == "polygon":
                g.convertToMultiType()
            out.setGeometry(g)
            attrs = list(f.attributes()) + [None] * (len(fields) - len(f.attributes()))
            vals = values_of(k)
            for p, v in zip(pos, vals or [None] * len(pos)):
                attrs[p] = v
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
        return dest

    def _hex_sink(self, parameters, context, crs, centres, width, new_fields, values_of):
        fields = QgsFields()
        for name, typ in new_fields:
            fields.append(QgsField(name, typ))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.Polygon, crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        for k, (cx, cy) in enumerate(centres):
            ring = wts.hex_ring(cx, cy, width)
            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]]))
            f.setAttributes(values_of(k))
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        return dest

    def _report(self, parameters, context, title, answer, sections, notes, caveats):
        path = self.parameterAsFileOutput(parameters, self.REPORT, context)
        if not path:
            return None
        lang = text_lang()
        if notes:
            sections.append({"heading": say({"en": "About the data", "ar": "عن البيانات"}), "paragraphs": notes})
        sections.append({"heading": say({"en": "What this does not mean", "ar": "ما لا تعنيه هذه النتيجة"}),
                         "warnings": [say(_CAVEATS[c]) for c in caveats]})
        write_report(path, title, sections, lang, answer)
        return path

    def _seed(self, parameters, context):
        return rng(self.parameterAsInt(parameters, self.SEED, context))

    def _perms(self, parameters, context):
        return self.parameterAsInt(parameters, self.PERMUTATIONS, context)


def _quad_labels():
    return {"x": say({"en": "Value (standardised)", "ar": "القيمة (معيارية)"}),
            "y": say({"en": "Average of the neighbours (standardised)", "ar": "متوسط الجيران (معياري)"}),
            "HH": "HH", "LL": "LL", "LH": "LH", "HL": "HL"}


def _moran_sentence(r, p):
    vals = {"i": _f(r["I"]), "p": fmt_p(p)}
    if p <= 0.05 and r["I"] > r["EI"]:
        return say({"en": "The values are clustered: similar values sit next to each other (I = {i}, {p}).",
                    "ar": "القيم متجمعة: القيم المتشابهة تقع بجوار بعضها (I = {i}، {p})."}, **vals)
    if p <= 0.05:
        return say({"en": "The values are dispersed: neighbours tend to differ, like a chessboard (I = {i}, {p}).",
                    "ar": "القيم متباعدة: الجيران يميلون إلى الاختلاف كرقعة الشطرنج (I = {i}، {p})."}, **vals)
    return say({"en": "No clear spatial pattern: the values could be placed at random (I = {i}, {p}).",
                "ar": "لا نمط مكاني واضح: يمكن أن تكون القيم موزعة عشوائيًا (I = {i}، {p})."}, **vals)


def _lisa_cats():
    return [(ac.QUAD_CODES[q], ac.QUAD_COLOURS[q], say(_QUAD_WORDS[q])) for q in (ac.HH, ac.LL, ac.HL, ac.LH, ac.NS)]


def _bin_cats():
    return [(b, ac.BIN_COLOURS[b], say(_BIN_WORDS[b])) for b in (3, 2, 1, 0, -1, -2, -3)]


# =========================================================================================================
class GlobalMoran(_SpatialStatsBase):
    ID = "ssmoran"
    NAME = "Is there a spatial pattern? (Global Moran's I)"
    HELP = ("Answers one question: do similar values sit next to each other (clustered), do neighbours differ "
            "(dispersed), or could the values be placed at random? Works on points or polygons with a numeric "
            "field. Neighbours: shared borders, nearest features or a distance (automatic by default). Gives "
            "Moran's I, its z-score and two p-values (formula and 999 random reshuffles), a Moran scatterplot "
            "and a plain-language answer.")

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points or polygons"))
        self._add_field(self.FIELD, tr("Value field"))
        self._add_weights()
        self._add_common(999, fdr_option=False)
        self.addOutput(QgsProcessingOutputNumber("MORAN_I", tr("Moran's I")))
        self.addOutput(QgsProcessingOutputNumber("Z_SCORE", tr("z-score")))
        self.addOutput(QgsProcessingOutputNumber("P_VALUE", tr("p-value")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        field = self.parameterAsStrings(parameters, self.FIELD, context)[0]
        d = self._load(parameters, context, feedback, [field])
        keep = self._keep_valid(d, feedback, [field])
        y = d.values[field][keep]
        self._duplicates(d, feedback, d.xy_k)
        feedback.setProgress(20)
        w, desc, _, card = self._weights(parameters, context, feedback, d, d.xy_k,
                                         [d.work_geoms[k] for k in keep])
        feedback.setProgress(50)
        perms = self._perms(parameters, context)
        r = ac.moran(y, wts.row_standardise(w), perms, self._seed(parameters, context))
        p = _best_p(r)
        answer = _moran_sentence(r, p)
        feedback.pushInfo(answer)
        lisa_q = ac.quadrant(r["z_values"], r["lag"])
        sections = [{
            "heading": say({"en": "Numbers", "ar": "الأرقام"}),
            "paragraphs": [say({"en": "Neighbours: {w}; weights row-standardised (each feature's neighbours share "
                                      "a weight of 1).",
                                "ar": "الجيران: {w}؛ الأوزان موحدة بالصف (جيران كل عنصر يتقاسمون وزنًا قدره 1)."},
                               w=desc)],
            "table": {"columns": [say({"en": "Statistic", "ar": "المقياس"}), say({"en": "Value", "ar": "القيمة"})],
                      "rows": [["Moran's I", r["I"]], [say({"en": "Expected if random", "ar": "المتوقع لو عشوائي"}),
                                                       r["EI"]],
                               ["z", r["z"]], [say({"en": "p (formula, randomisation)", "ar": "p (بالمعادلة)"}),
                                               r["p_norm"]],
                               [say({"en": "p ({n} permutations)", "ar": "p ({n} تبديلة)"}, n=perms),
                                r["p_sim"] if r["p_sim"] is not None else float("nan")],
                               [say({"en": "Features", "ar": "العناصر"}), int(r["n"])],
                               [say({"en": "Neighbours per feature (average)", "ar": "متوسط عدد الجيران"}),
                                float(card.mean())]]},
            "images": [ac.scatter_png(r["z_values"], r["lag"], r["I"], lisa_q, _quad_labels(),
                                      say({"en": "Moran scatterplot (slope = I)", "ar": "مخطط موران (الميل = I)"}))]
                      + ([ac.hist_png(r["sims"], r["I"], say({"en": "Moran's I of random reshuffles",
                                                                "ar": "قيم I لإعادات الترتيب العشوائية"}),
                                      "I")] if r["sims"] is not None else []),
        }]
        self._report(parameters, context, say({"en": "Global Moran's I - {f}", "ar": "مؤشر موران العام - {f}"}, f=field),
                     answer, sections, d.notes, ["cause", "weights", "edge", "maup"])
        feedback.setProgress(100)
        return {self.ANSWER: answer, "MORAN_I": r["I"], "Z_SCORE": r["z"], "P_VALUE": p,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}


# =========================================================================================================
class LocalMoran(_SpatialStatsBase):
    ID = "sslisa"
    NAME = "Where are the clusters and outliers? (Local Moran, LISA)"
    HELP = ("Finds, feature by feature, high values among high neighbours (hot clusters), low among low (cold "
            "clusters) and the outliers: high among low, low among high. Significance by conditional "
            "permutation (each feature keeps its value, its neighbours are reshuffled), with the false "
            "discovery rate correction on by default. The output layer gets lisa_i, lisa_z, lisa_p, lisa_type "
            "(HH, LL, HL, LH, NS) and a readable label, coloured with the standard five colours.")

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points or polygons"))
        self._add_field(self.FIELD, tr("Value field"))
        self._add_weights()
        self._add_common(999, fdr_option=True)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Clusters and outliers")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        field = self.parameterAsStrings(parameters, self.FIELD, context)[0]
        use_fdr = self.parameterAsBool(parameters, self.FDR, context)
        d = self._load(parameters, context, feedback, [field])
        keep = self._keep_valid(d, feedback, [field])
        y = d.values[field][keep]
        self._duplicates(d, feedback, d.xy_k)
        w, desc, _, _ = self._weights(parameters, context, feedback, d, d.xy_k, [d.work_geoms[k] for k in keep])
        feedback.setProgress(40)
        perms = max(self._perms(parameters, context), 99)
        g = self._seed(parameters, context)
        lm = ac.local_moran(y, w, perms, g, 0.05, use_fdr)
        glob = ac.moran(y, wts.row_standardise(w))
        feedback.setProgress(80)
        pos = {k: i for i, k in enumerate(keep)}

        def values_of(k):
            i = pos.get(k)
            if i is None:
                return [None] * 5
            q = int(lm["cluster"][i])
            return [float(lm["Is"][i]), float(lm["z_sim"][i]), float(lm["p"][i]), ac.QUAD_CODES[q],
                    say(_QUAD_WORDS[q])]

        dest = self._write_features(parameters, context, d, [
            ("lisa_i", QMetaType.Type.Double), ("lisa_z", QMetaType.Type.Double), ("lisa_p", QMetaType.Type.Double),
            ("lisa_type", QMetaType.Type.QString), ("lisa_label", QMetaType.Type.QString)], values_of)
        _style(context, dest, "lisa_type", _lisa_cats())
        counts = {q: int((lm["cluster"] == q).sum()) for q in (ac.HH, ac.LL, ac.HL, ac.LH, ac.NS)}
        answer = self._answer(counts, d.n, use_fdr)
        feedback.pushInfo(answer)
        rows = [[say(_QUAD_WORDS[q]), counts[q]] for q in counts]
        colours = [ac.QUAD_COLOURS[int(q)] for q in lm["cluster"]]
        legend = [(ac.QUAD_COLOURS[q], f"{say(_QUAD_WORDS[q])} ({counts[q]})") for q in counts]
        sections = [{
            "heading": say({"en": "Clusters and outliers", "ar": "التجمعات والقيم الشاذة"}),
            "paragraphs": [say({"en": "Neighbours: {w}. {n} conditional permutations; significance 5%{f}. Global "
                                      "Moran's I for comparison: {i}.",
                                "ar": "الجيران: {w}. {n} تبديلة شرطية؛ مستوى الدلالة 5%{f}. مؤشر موران العام للمقارنة: {i}."},
                               w=desc, n=perms, i=_f(glob["I"]),
                               f=say({"en": " after the false discovery rate correction",
                                      "ar": " بعد تصحيح معدل الاكتشاف الكاذب"}) if use_fdr else "")],
            "table": {"columns": [say({"en": "Type", "ar": "النوع"}), say({"en": "Features", "ar": "العناصر"})],
                      "rows": rows},
            "images": [ac.map_png(self._rings(d, keep), colours, legend,
                                  say({"en": "Cluster map (LISA)", "ar": "خريطة التجمعات (LISA)"}),
                                  points=d.xy_k if d.kind == "point" else None),
                       ac.scatter_png(lm["z"], lm["lag"], glob["I"], lm["cluster"], _quad_labels(),
                                      say({"en": "Moran scatterplot", "ar": "مخطط موران"}))],
        }]
        self._report(parameters, context, say({"en": "Clusters and outliers - {f}", "ar": "التجمعات والقيم الشاذة - {f}"},
                                               f=field), answer, sections, d.notes,
                     ["cause", "multiple", "weights", "edge", "maup"])
        return {self.ANSWER: answer, self.OUTPUT: dest,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}

    @staticmethod
    def _answer(c, n, use_fdr):
        if c[ac.HH] + c[ac.LL] + c[ac.HL] + c[ac.LH] == 0:
            return say({"en": "No feature stands out: no significant cluster or outlier among {n} features{f}.",
                        "ar": "لا عنصر بارز: لا تجمعات ولا قيم شاذة دالة بين {n} عنصرًا{f}."}, n=n,
                       f=say({"en": " (after the FDR correction)", "ar": " (بعد تصحيح FDR)"}) if use_fdr else "")
        return say({"en": "{hh} features are in hot clusters (high among high) and {ll} in cold clusters (low "
                          "among low); {o} are outliers (high among low: {hl}, low among high: {lh}) out of {n}.",
                    "ar": "{hh} عنصرًا في تجمعات ساخنة (مرتفع وسط مرتفع) و{ll} في تجمعات باردة (منخفض وسط منخفض)؛ "
                          "و{o} قيم شاذة (مرتفع وسط منخفض: {hl}، منخفض وسط مرتفع: {lh}) من أصل {n}."},
                   hh=c[ac.HH], ll=c[ac.LL], o=c[ac.HL] + c[ac.LH], hl=c[ac.HL], lh=c[ac.LH], n=n)


# =========================================================================================================
def _gi_auto_distance(xy, y):
    """The first significant peak of incremental Moran's I; without one, the average distance to the
    k nearest neighbours (k = 5% of the features, 3..30), never less than one neighbour each."""
    start, step = ac.default_bands(xy, 10)
    ds = ac.cap_bands(xy, start + step * np.arange(10))
    rows = ac.incremental(xy, y, ds)
    pk = ac.peaks([r[2] for r in rows])
    if pk:
        return rows[pk[0]][0], "peak", rows
    k = int(np.clip(0.05 * len(xy), 3, 30))
    return max(wts.min_threshold(xy), float(wts.nn_distances(xy, k).mean())), "knn", rows


class HotSpots(_SpatialStatsBase):
    ID = "sshotspots"
    NAME = "Hot spots and cold spots (Getis-Ord Gi*)"
    HELP = ("Finds statistically significant hot spots (high values, or many incidents, surrounded by high "
            "values) and cold spots, with 99/95/90% confidence and the false discovery rate correction. Give a "
            "value field, or leave it empty for incident points (accidents, complaints, crimes): they are then "
            "counted in a hexagon grid (cell size from the spacing of the points) and the counts are analysed. "
            "The neighbourhood distance is chosen automatically at the peak of incremental spatial "
            "autocorrelation unless you give one.")

    CELL = "CELL_SIZE"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points or polygons"))
        self._add_field(self.FIELD, tr("Value field (empty = count the points in hexagons)"), optional=True)
        self.addParameter(QgsProcessingParameterEnum(
            self.WEIGHTS, tr("Neighbours"),
            [tr("Automatic (distance at the peak of clustering)"), tr("Distance band"), tr("K nearest neighbours"),
             tr("Shared border or corner (queen, polygons)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.DISTANCE, tr("Distance band in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.K, tr("Number of nearest neighbours"), Qgis.ProcessingNumberParameterType.Integer, 8, minValue=1,
            maxValue=200))
        self.addParameter(QgsProcessingParameterNumber(
            self.CELL, tr("Hexagon size in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double, 0.0,
            minValue=0.0))
        self._add_common(199, fdr_option=True)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Hot spots")))
        self.addOutput(QgsProcessingOutputNumber("DISTANCE_USED", tr("Distance used (m)")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        fields = self.parameterAsStrings(parameters, self.FIELD, context)
        field = fields[0] if fields else None
        use_fdr = self.parameterAsBool(parameters, self.FDR, context)
        mode = ["auto", "band", "knn", "queen"][self.parameterAsEnum(parameters, self.WEIGHTS, context)]
        dist = self.parameterAsDouble(parameters, self.DISTANCE, context)
        k = self.parameterAsInt(parameters, self.K, context)
        d = self._load(parameters, context, feedback, [field] if field else [], points_only=False)
        incidents = field is None
        lang = text_lang()
        if incidents:
            if d.kind != "point":
                raise QgsProcessingException(tr("Polygons need a value field; only points can be counted."))
            self._duplicates(d, feedback, d.xy)
            cell = self.parameterAsDouble(parameters, self.CELL, context)
            if cell <= 0:
                cell = wts.auto_cell(d.xy)
                self._note(d, feedback, {
                    "en": "The points were counted in hexagons of {c} (chosen from the spacing of the points), "
                          "including the empty hexagons inside the area the points cover.",
                    "ar": "عُدّت النقاط في خلايا سداسية بعرض {c} (اختير من تباعد النقاط)، مع الخلايا الفارغة داخل "
                          "المنطقة التي تغطيها النقاط."}, c=fmt_dist(cell))
            grid = wts.hex_cells(d.xy, cell)
            xy, y = grid["centres"], grid["counts"]
            if len(y) < MIN_HARD:
                raise QgsProcessingException(tr("Only {n} hexagons: use a smaller hexagon size.").format(n=len(y)))
            if len(y) < MIN_SOFT:
                self._note(d, feedback, {"en": "Only {n} hexagons: the result is shaky; try a smaller cell size.",
                                         "ar": "عدد الخلايا {n} فقط: النتيجة مهتزة؛ جرّب خلية أصغر."}, n=len(y))
            if np.ptp(y) == 0:
                raise QgsProcessingException(tr("Every hexagon holds the same number of points: there is no "
                                                "pattern to find."))
            geoms = None
            if mode == "queen":
                mode = "band"
                dist = dist or cell * 1.01
        else:
            keep = self._keep_valid(d, feedback, [field])
            y = d.values[field][keep]
            xy = d.xy_k
            self._duplicates(d, feedback, xy)
            geoms = [d.work_geoms[i] for i in keep]
            if mode == "queen" and d.kind != "polygon":
                mode = "knn"
        feedback.setProgress(25)
        why = None
        rows = []
        if mode == "auto" or (mode == "band" and dist <= 0):
            dist, why, rows = _gi_auto_distance(xy, y)
            mode = "band"
        if mode == "band":
            if wts.pair_count(xy, [dist])[0] > MAX_PAIRS:
                raise QgsProcessingException(tr("The distance band {d} makes almost every feature a neighbour of "
                                                "every other; choose a smaller distance.").format(d=fmt_dist(dist)))
            w = wts.distance_band(xy, dist)
            desc = say({"en": "features within {d}", "ar": "العناصر في حدود {d}"}, d=fmt_dist(dist))
        elif mode == "knn":
            w = wts.knn(xy, k)
            desc = say({"en": "the {k} nearest features", "ar": "أقرب {k} عناصر"}, k=k)
            dist = float("nan")
        else:
            w = wts.contiguity(geoms, True, feedback)
            desc = say({"en": "polygons that share a border or a corner", "ar": "المضلعات التي تتشارك حدًا أو ركنًا"})
            dist = float("nan")
        if why == "peak":
            self._note(d, feedback, {"en": "Neighbourhood distance {d}: where clustering peaks (incremental "
                                           "spatial autocorrelation).",
                                     "ar": "مسافة الجوار {d}: حيث يبلغ التجمع ذروته (الارتباط الذاتي المكاني المتدرج)."},
                       d=fmt_dist(dist))
        elif why == "knn":
            self._note(d, feedback, {"en": "No distance showed a clear peak of clustering; the neighbourhood is the "
                                           "average distance to the nearest neighbours: {d}.",
                                     "ar": "لم تظهر ذروة واضحة للتجمع عند أي مسافة؛ فالجوار هو متوسط المسافة إلى أقرب "
                                           "الجيران: {d}."}, d=fmt_dist(dist))
        w, isl = wts.attach_islands(w, xy)
        if isl.size:
            self._note(d, feedback, {
                "en": "{n} features had no neighbour (islands); each was given its nearest feature as neighbour.",
                "ar": "{n} عنصرًا بلا جار (جُزر)؛ أُعطي كل منها أقرب عنصر إليه جارًا."}, n=int(isl.size))
        feedback.setProgress(55)
        z, p = ac.gi_star(y, w)
        bins = ac.confidence_bins(z, p, use_fdr)
        glob = ac.moran(y, wts.row_standardise(w), self._perms(parameters, context), self._seed(parameters, context))
        new = [("gi_z", QMetaType.Type.Double), ("gi_p", QMetaType.Type.Double), ("gi_bin", QMetaType.Type.Int),
               ("gi_label", QMetaType.Type.QString)]
        if incidents:
            dest = self._hex_sink(parameters, context, d.work, xy, grid["width"],
                                  [("count", QMetaType.Type.Double)] + new,
                                  lambda i: [float(y[i]), float(z[i]), float(p[i]), int(bins[i]),
                                             say(_BIN_WORDS[int(bins[i])])])
        else:
            pos = {kk: i for i, kk in enumerate(keep)}

            def values_of(kk):
                i = pos.get(kk)
                if i is None:
                    return None
                return [float(z[i]), float(p[i]), int(bins[i]), say(_BIN_WORDS[int(bins[i])])]

            dest = self._write_features(parameters, context, d, new, values_of)
        _style(context, dest, "gi_bin", _bin_cats())
        hot = int((bins > 0).sum())
        cold = int((bins < 0).sum())
        unit = say({"en": "hexagons", "ar": "خلية"}) if incidents else say({"en": "features", "ar": "عنصرًا"})
        if hot + cold == 0:
            answer = say({"en": "No significant hot or cold spots among {n} {u}: high and low values are not "
                                "grouped more than chance would do.",
                          "ar": "لا بؤر ساخنة أو باردة دالة بين {n} {u}: القيم المرتفعة والمنخفضة ليست متجمعة أكثر مما "
                                "تفعله الصدفة."}, n=len(y), u=unit)
        else:
            strong = int((bins == 3).sum())
            answer = say({"en": "{h} hot spots ({s} with 99% confidence) and {c} cold spots out of {n} {u} "
                                "(neighbourhood: {w}).",
                          "ar": "{h} بؤرة ساخنة ({s} منها بثقة 99%) و{c} بؤرة باردة من أصل {n} {u} (الجوار: {w})."},
                         h=hot, s=strong, c=cold, n=len(y), u=unit, w=desc)
        feedback.pushInfo(answer)
        rows_t = [[say(_BIN_WORDS[b]), int((bins == b).sum())] for b in (3, 2, 1, 0, -1, -2, -3)]
        colours = [ac.BIN_COLOURS[int(b)] for b in bins]
        legend = [(ac.BIN_COLOURS[b], say(_BIN_WORDS[b])) for b in (3, 2, 1, 0, -1, -2, -3)]
        if incidents:
            shapes = [[wts.hex_ring(cx, cy, grid["width"])] for cx, cy in xy]
            img = ac.map_png(shapes, colours, legend, say({"en": "Hot and cold spots", "ar": "البؤر الساخنة والباردة"}))
        else:
            img = ac.map_png(self._rings(d, keep), colours, legend,
                             say({"en": "Hot and cold spots", "ar": "البؤر الساخنة والباردة"}),
                             points=xy if d.kind == "point" else None)
        images = [img]
        if rows:
            images.append(ac.line_png([r[0] for r in rows], [[r[2] for r in rows]], [""],
                                      say({"en": "Clustering by distance", "ar": "التجمع حسب المسافة"}),
                                      say({"en": "Distance (m)", "ar": "المسافة (م)"}),
                                      say({"en": "z-score of Moran's I", "ar": "z لمؤشر موران"}),
                                      marks=[dist] if why == "peak" else (), hline=1.96))
        p_glob = _best_p(glob)
        sections = [{
            "heading": say({"en": "Hot and cold spots", "ar": "البؤر الساخنة والباردة"}),
            "paragraphs": [say({"en": "Gi* compares the sum of each place and its neighbours ({w}) with what the "
                                      "whole area would give. Overall clustering: {m}.",
                                "ar": "يقارن Gi* مجموع كل مكان وجيرانه ({w}) بما تعطيه المنطقة كلها. التجمع العام: {m}."},
                               w=desc, m=_moran_sentence(glob, p_glob))],
            "table": {"columns": [say({"en": "Class", "ar": "الفئة"}), unit], "rows": rows_t},
            "images": images,
        }]
        cav = ["cause", "multiple", "edge", "maup"] + (["counts"] if incidents else [])
        self._report(parameters, context, say({"en": "Hot spot analysis", "ar": "تحليل البؤر الساخنة"}), answer,
                     sections, d.notes, cav)
        return {self.ANSWER: answer, self.OUTPUT: dest, "DISTANCE_USED": dist,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}


# =========================================================================================================
class IncrementalMoran(_SpatialStatsBase):
    ID = "ssincremental"
    NAME = "At what distance is clustering strongest? (Incremental spatial autocorrelation)"
    HELP = ("Runs Moran's I over a series of increasing distance bands and charts its z-score. Peaks mark the "
            "distances where the clustering is most pronounced - the scale of the process - and the first peak "
            "is a good neighbourhood distance for hot spot analysis. Without a value field, points are counted "
            "in hexagons first.")

    BANDS, START, STEP, TABLE = "BANDS", "START", "INCREMENT", "TABLE"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points or polygons"))
        self._add_field(self.FIELD, tr("Value field (empty = count the points in hexagons)"), optional=True)
        self.addParameter(QgsProcessingParameterNumber(
            self.BANDS, tr("Number of distance bands"), Qgis.ProcessingNumberParameterType.Integer, 10, minValue=2,
            maxValue=50))
        self.addParameter(QgsProcessingParameterNumber(
            self.START, tr("First distance in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.STEP, tr("Distance increment in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=0.0))
        self._add_common(0, fdr_option=False)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.TABLE, tr("Results by distance"), Qgis.ProcessingSourceType.Vector, optional=True,
            createByDefault=False))
        self.addOutput(QgsProcessingOutputNumber("FIRST_PEAK", tr("First peak distance (m)")))
        self.addOutput(QgsProcessingOutputNumber("MAX_PEAK", tr("Strongest peak distance (m)")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        fields = self.parameterAsStrings(parameters, self.FIELD, context)
        field = fields[0] if fields else None
        d = self._load(parameters, context, feedback, [field] if field else [])
        if field is None:
            if d.kind != "point":
                raise QgsProcessingException(tr("Polygons need a value field; only points can be counted."))
            self._duplicates(d, feedback, d.xy)
            cell = wts.auto_cell(d.xy)
            grid = wts.hex_cells(d.xy, cell)
            xy, y = grid["centres"], grid["counts"]
            self._note(d, feedback, {"en": "The points were counted in hexagons of {c}.",
                                     "ar": "عُدّت النقاط في خلايا سداسية بعرض {c}."}, c=fmt_dist(cell))
            if np.ptp(y) == 0:
                raise QgsProcessingException(tr("Every hexagon holds the same number of points: there is no "
                                                "pattern to find."))
        else:
            keep = self._keep_valid(d, feedback, [field])
            xy, y = d.xy_k, d.values[field][keep]
            self._duplicates(d, feedback, xy)
        count = self.parameterAsInt(parameters, self.BANDS, context)
        start = self.parameterAsDouble(parameters, self.START, context)
        step = self.parameterAsDouble(parameters, self.STEP, context)
        a_start, a_step = ac.default_bands(xy, count)
        if start <= 0:
            start = a_start
        elif start < a_start:
            self._note(d, feedback, {"en": "At {d} some features have no neighbour; their results count as zero "
                                           "lag at the first distances.",
                                     "ar": "عند {d} بعض العناصر بلا جار؛ تُحسب نتائجها صفرًا في المسافات الأولى."},
                       d=fmt_dist(start))
        step = step if step > 0 else a_step
        ds = start + step * np.arange(count)
        capped = ac.cap_bands(xy, ds)
        if len(capped) < len(ds):
            self._note(d, feedback, {"en": "The bands beyond {d} were skipped: almost every feature would be a "
                                           "neighbour of every other.",
                                     "ar": "تُركت المسافات بعد {d}: كاد كل عنصر يصبح جارًا لكل عنصر آخر."},
                       d=fmt_dist(capped[-1]))
        rows = ac.incremental(xy, y, capped, feedback)
        zs = [r[2] for r in rows]
        pk = ac.peaks(zs)
        first = rows[pk[0]][0] if pk else float("nan")
        best = rows[max(pk, key=lambda i: zs[i])][0] if pk else float("nan")
        if pk:
            answer = say({"en": "Clustering is strongest at about {d} (first peak, z = {z}); use this distance as "
                                "the neighbourhood for hot spot analysis.{m}",
                          "ar": "التجمع أقوى ما يكون عند نحو {d} (أول ذروة، z = {z})؛ استخدم هذه المسافة جوارًا لتحليل "
                                "البؤر الساخنة.{m}"},
                         d=fmt_dist(first), z=_f(zs[pk[0]], 2),
                         m=say({"en": " The strongest peak is at {b}.", "ar": " وأقوى ذروة عند {b}."}, b=fmt_dist(best))
                         if best != first else "")
        elif any(z > 1.96 for z in zs):
            answer = say({"en": "The values are clustered at every distance tested, with no peak: the clustering "
                                "keeps growing with distance (a broad trend across the area).",
                          "ar": "القيم متجمعة عند كل المسافات المختبرة بلا ذروة: التجمع يزداد مع المسافة (اتجاه عام عبر "
                                "المنطقة)."})
        else:
            answer = say({"en": "No distance shows significant clustering.",
                          "ar": "لا توجد مسافة يظهر عندها تجمع دال."})
        feedback.pushInfo(answer)
        if parameters.get(self.TABLE) is not None:
            tf = QgsFields()
            for name in ("distance_m", "moran_i", "z_score", "p_value", "neighbours"):
                tf.append(QgsField(name, QMetaType.Type.Double))
            sink, tid = self.parameterAsSink(parameters, self.TABLE, context, tf, Qgis.WkbType.NoGeometry)
            if sink is not None:
                for r in rows:
                    f = QgsFeature(tf)
                    f.setAttributes([float(v) for v in r])
                    sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        else:
            tid = None
        sections = [{
            "heading": say({"en": "Moran's I by distance", "ar": "مؤشر موران حسب المسافة"}),
            "table": {"columns": [say({"en": "Distance", "ar": "المسافة"}), "I", "z", "p",
                                  say({"en": "Neighbours (avg)", "ar": "متوسط الجيران"})],
                      "rows": [[fmt_dist(r[0]), r[1], r[2], r[3], r[4]] for r in rows]},
            "images": [ac.line_png([r[0] for r in rows], [zs], [""],
                                   say({"en": "Clustering by distance", "ar": "التجمع حسب المسافة"}),
                                   say({"en": "Distance (m)", "ar": "المسافة (م)"}),
                                   say({"en": "z-score of Moran's I", "ar": "z لمؤشر موران"}),
                                   marks=[rows[i][0] for i in pk], hline=1.96)],
        }]
        self._report(parameters, context, say({"en": "Incremental spatial autocorrelation",
                                               "ar": "الارتباط الذاتي المكاني المتدرج"}),
                     answer, sections, d.notes, ["cause", "edge", "maup"])
        return {self.ANSWER: answer, "FIRST_PEAK": first, "MAX_PEAK": best, self.TABLE: tid,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}


# =========================================================================================================
class BivariateMoran(_SpatialStatsBase):
    ID = "ssbivariate"
    NAME = "Does X here go with Y around? (Bivariate local Moran)"
    HELP = ("Relates one variable at each place to another variable in the neighbouring places: for example "
            "income here against crime around. Gives the global bivariate Moran's I with a permutation p-value "
            "and, per feature, the type: high X among high Y (HH), low among low (LL), high among low (HL), low "
            "among high (LH), with conditional permutation p-values and the false discovery rate correction.")

    FIELD_Y = "FIELD_Y"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points or polygons"))
        self._add_field(self.FIELD, tr("Variable X (at the place)"))
        self._add_field(self.FIELD_Y, tr("Variable Y (in the neighbours)"))
        self._add_weights()
        self._add_common(999, fdr_option=True)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Bivariate clusters")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        fx = self.parameterAsStrings(parameters, self.FIELD, context)[0]
        fy = self.parameterAsStrings(parameters, self.FIELD_Y, context)[0]
        use_fdr = self.parameterAsBool(parameters, self.FDR, context)
        d = self._load(parameters, context, feedback, [fx, fy] if fx != fy else [fx])
        keep = self._keep_valid(d, feedback, [fx, fy] if fx != fy else [fx])
        x, y = d.values[fx][keep], d.values[fy][keep]
        self._duplicates(d, feedback, d.xy_k)
        w, desc, _, _ = self._weights(parameters, context, feedback, d, d.xy_k, [d.work_geoms[k] for k in keep])
        perms = max(self._perms(parameters, context), 99)
        g = self._seed(parameters, context)
        wr = wts.row_standardise(w)
        ib = ac.moran_bivariate(x, y, wr)
        zx = (x - x.mean()) / x.std()
        zy = (y - y.mean()) / y.std()
        sims = np.empty(perms)
        for s in range(perms):
            sims[s] = zx @ (wr @ g.permutation(zy)) / zx.size
        from ..spatial_stats.common import perm_p

        p = float(perm_p(ib, sims))
        r_plain = float(np.corrcoef(x, y)[0, 1])
        lm = ac.local_bivariate(x, y, w, perms, g, 0.05, use_fdr)
        pos = {k: i for i, k in enumerate(keep)}

        def values_of(k):
            i = pos.get(k)
            if i is None:
                return None
            q = int(lm["cluster"][i])
            return [float(lm["Is"][i]), float(lm["z_sim"][i]), float(lm["p"][i]), ac.QUAD_CODES[q],
                    say(_QUAD_WORDS[q])]

        dest = self._write_features(parameters, context, d, [
            ("bv_i", QMetaType.Type.Double), ("bv_z", QMetaType.Type.Double), ("bv_p", QMetaType.Type.Double),
            ("bv_type", QMetaType.Type.QString), ("bv_label", QMetaType.Type.QString)], values_of)
        _style(context, dest, "bv_type", _lisa_cats())
        counts = {q: int((lm["cluster"] == q).sum()) for q in (ac.HH, ac.LL, ac.HL, ac.LH, ac.NS)}
        vals = {"x": fx, "y": fy, "i": _f(ib), "p": fmt_p(p), "hh": counts[ac.HH], "ll": counts[ac.LL],
                "o": counts[ac.HL] + counts[ac.LH]}
        if p <= 0.05 and ib > 0:
            answer = say({"en": "High {x} tends to sit next to high {y} (and low next to low): bivariate I = {i}, "
                                "{p}. {hh} places are HH and {ll} LL; {o} go against the trend.",
                          "ar": "ارتفاع {x} يميل إلى مجاورة ارتفاع {y} (والمنخفض بجوار المنخفض): I الثنائي = {i}، {p}. "
                                "{hh} مكانًا HH و{ll} LL؛ و{o} عكس الاتجاه."}, **vals)
        elif p <= 0.05:
            answer = say({"en": "High {x} tends to sit next to low {y}: bivariate I = {i}, {p}.",
                          "ar": "ارتفاع {x} يميل إلى مجاورة انخفاض {y}: I الثنائي = {i}، {p}."}, **vals)
        else:
            answer = say({"en": "{x} here is not clearly related to {y} around it (bivariate I = {i}, {p}).",
                          "ar": "{x} هنا لا يرتبط بوضوح بـ{y} حوله (I الثنائي = {i}، {p})."}, **vals)
        feedback.pushInfo(answer)
        labels = _quad_labels()
        labels["x"] = say({"en": "{x} (standardised)", "ar": "{x} (معياري)"}, x=fx)
        labels["y"] = say({"en": "{y} of the neighbours (standardised)", "ar": "{y} لدى الجيران (معياري)"}, y=fy)
        colours = [ac.QUAD_COLOURS[int(q)] for q in lm["cluster"]]
        legend = [(ac.QUAD_COLOURS[q], f"{say(_QUAD_WORDS[q])} ({counts[q]})") for q in counts]
        sections = [{
            "heading": say({"en": "Numbers", "ar": "الأرقام"}),
            "paragraphs": [say({"en": "Neighbours: {w}. The plain correlation of {x} and {y} at the same place is "
                                      "{r}; the bivariate I looks at the neighbours instead.",
                                "ar": "الجيران: {w}. الارتباط العادي بين {x} و{y} في المكان نفسه {r}؛ أما I الثنائي فينظر "
                                      "إلى الجيران."}, w=desc, x=fx, y=fy, r=_f(r_plain))],
            "table": {"columns": [say({"en": "Statistic", "ar": "المقياس"}), say({"en": "Value", "ar": "القيمة"})],
                      "rows": [[say({"en": "Bivariate Moran's I", "ar": "مؤشر موران الثنائي"}), ib],
                               [say({"en": "p ({n} permutations)", "ar": "p ({n} تبديلة)"}, n=perms), p]]
                      + [[say(_QUAD_WORDS[q]), counts[q]] for q in counts]},
            "images": [ac.map_png(self._rings(d, keep), colours, legend,
                                  say({"en": "Bivariate cluster map", "ar": "خريطة التجمعات الثنائية"}),
                                  points=d.xy_k if d.kind == "point" else None),
                       ac.scatter_png(zx, lm["lag"], ib, lm["cluster"], labels,
                                      say({"en": "Bivariate Moran scatterplot", "ar": "مخطط موران الثنائي"}))],
        }]
        self._report(parameters, context, say({"en": "Bivariate local Moran - {x} and {y}",
                                               "ar": "موران المحلي الثنائي - {x} و{y}"}, x=fx, y=fy),
                     answer, sections, d.notes, ["cause", "multiple", "weights", "edge", "maup"])
        return {self.ANSWER: answer, self.OUTPUT: dest,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}


# =========================================================================================================
class Colocation(_SpatialStatsBase):
    ID = "sscolocation"
    NAME = "Is B found next to A? (Co-location quotient)"
    HELP = ("For points of two categories (for example pharmacies and clinics, or a crime type and bars): are "
            "the nearest neighbours of A more often B than chance would give? The co-location quotient is 1 "
            "when B is as common next to A as anywhere, above 1 when B gathers around A and below 1 when B "
            "avoids A. Global significance by random relabelling of the points, local (for each A point) exact "
            "from the hypergeometric distribution.")

    CATEGORY, CAT_A, CAT_B = "CATEGORY", "CATEGORY_A", "CATEGORY_B"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points"), points_only=True)
        self.addParameter(QgsProcessingParameterField(self.CATEGORY, tr("Category field"),
                                                      parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterString(self.CAT_A, tr("Category A (the focal points)")))
        self.addParameter(QgsProcessingParameterString(self.CAT_B, tr("Category B (the neighbours looked for)")))
        self.addParameter(QgsProcessingParameterNumber(
            self.K, tr("Number of nearest neighbours"), Qgis.ProcessingNumberParameterType.Integer, 8, minValue=1,
            maxValue=200))
        self._add_common(999, fdr_option=True)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Co-location of the A points"),
                                                            Qgis.ProcessingSourceType.VectorPoint))
        self.addOutput(QgsProcessingOutputNumber("CLQ", tr("Co-location quotient")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        cat = self.parameterAsStrings(parameters, self.CATEGORY, context)[0]
        a = (self.parameterAsString(parameters, self.CAT_A, context) or "").strip()
        b = (self.parameterAsString(parameters, self.CAT_B, context) or "").strip()
        k = self.parameterAsInt(parameters, self.K, context)
        use_fdr = self.parameterAsBool(parameters, self.FDR, context)
        d = self._load(parameters, context, feedback, [], points_only=True)
        labels = np.array([str(f[cat]).strip() if f[cat] is not None else "" for f in d.feats])
        found = sorted(set(labels.tolist()))
        for c in (a, b):
            if c not in found:
                shown = ", ".join(found[:15]) + (" ..." if len(found) > 15 else "")
                raise QgsProcessingException(tr("Category \"{c}\" is not in the field {f}. Its values: {v}")
                                             .format(c=c, f=cat, v=shown))
        is_a, is_b = labels == a, labels == b
        self._duplicates(d, feedback, d.xy)
        for c, m in ((a, is_a), (b, is_b)):
            if m.sum() < 10:
                self._note(d, feedback, {"en": "Only {n} points of \"{c}\": the result is shaky.",
                                         "ar": "عدد نقاط \"{c}\" {n} فقط: النتيجة مهتزة."}, n=int(m.sum()), c=c)
        perms = max(self._perms(parameters, context), 99)
        r = ac.colocation(d.xy, is_a, is_b, k, perms, self._seed(parameters, context))
        from ..spatial_stats.common import fdr

        ia = np.flatnonzero(is_a)
        pl = r["p_local"][ia]
        sig = fdr(pl, 0.05) if use_fdr else pl <= 0.05
        local = r["local"][ia]
        kinds = np.where(sig & (local > 1), "colocated", np.where(sig & (local < 1), "isolated", "not_significant"))
        words = {"colocated": {"en": "B gathers around it", "ar": "B يتجمع حوله"},
                 "isolated": {"en": "B avoids it", "ar": "B يتجنبه"},
                 "not_significant": {"en": "Not significant", "ar": "غير دال إحصائيًا"}}
        fields = QgsFields(d.feats[0].fields())
        fields, pos = self._out_fields(fields, [("clq", QMetaType.Type.Double), ("clq_b_near", QMetaType.Type.Int),
                                                ("clq_p", QMetaType.Type.Double), ("clq_type", QMetaType.Type.QString),
                                                ("clq_label", QMetaType.Type.QString)])
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, d.feats[0].geometry().wkbType(),
                                          d.crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        for j, i in enumerate(ia):
            f = d.feats[i]
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            attrs = list(f.attributes()) + [None] * (len(fields) - len(f.attributes()))
            for p_, v in zip(pos, [float(local[j]), int(r["count_b"][i]), float(pl[j]), str(kinds[j]),
                                   say(words[str(kinds[j])])]):
                attrs[p_] = v
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
        cols = {"colocated": "#C0392B", "isolated": "#2C7BB6", "not_significant": "#BDBDBD"}
        _style(context, dest, "clq_type", [(key, cols[key], say(words[key])) for key in cols])
        vals = {"a": a, "b": b, "q": _f(r["clq"], 2), "p": fmt_p(r["p"])}
        if r["p"] <= 0.05 and r["clq"] > 1:
            answer = say({"en": "\"{b}\" is found next to \"{a}\" {q} times as often as chance would give (CLQ = {q}, "
                                "{p}).",
                          "ar": "\"{b}\" يوجد بجوار \"{a}\" بمعدل {q} مرة مما تعطيه الصدفة (CLQ = {q}، {p})."}, **vals)
        elif r["p"] <= 0.05:
            answer = say({"en": "\"{b}\" avoids \"{a}\": it is found next to it less often than chance would give "
                                "(CLQ = {q}, {p}).",
                          "ar": "\"{b}\" يتجنب \"{a}\": يوجد بجواره أقل مما تعطيه الصدفة (CLQ = {q}، {p})."}, **vals)
        else:
            answer = say({"en": "No evidence that \"{b}\" gathers around or avoids \"{a}\" (CLQ = {q}, {p}).",
                          "ar": "لا دليل على أن \"{b}\" يتجمع حول \"{a}\" أو يتجنبه (CLQ = {q}، {p})."}, **vals)
        feedback.pushInfo(answer)
        n_col, n_iso = int((kinds == "colocated").sum()), int((kinds == "isolated").sum())
        colours = np.full(len(d.xy), "#E6E6E6", dtype=object)
        colours[is_b] = "#7F8C8D"
        colours[ia] = [cols[str(t)] for t in kinds]
        legend = [(cols["colocated"], f"{a}: {say(words['colocated'])} ({n_col})"),
                  (cols["isolated"], f"{a}: {say(words['isolated'])} ({n_iso})"),
                  (cols["not_significant"], f"{a}: {say(words['not_significant'])}"),
                  ("#7F8C8D", b), ("#E6E6E6", say({"en": "Other points", "ar": "نقاط أخرى"}))]
        sections = [{
            "heading": say({"en": "Numbers", "ar": "الأرقام"}),
            "paragraphs": [say({"en": "Each point's {k} nearest neighbours. Among all other points {e} are \"{b}\"; "
                                      "around \"{a}\" the share is {s}.",
                                "ar": "أقرب {k} جيران لكل نقطة. من بين كل النقاط الأخرى نسبة \"{b}\" {e}؛ وحول \"{a}\" "
                                      "النسبة {s}."},
                               k=r["k"], e=f"{r['expected']:.1%}", s=f"{r['expected'] * r['clq']:.1%}", a=a, b=b)],
            "table": {"columns": [say({"en": "Statistic", "ar": "المقياس"}), say({"en": "Value", "ar": "القيمة"})],
                      "rows": [["CLQ", r["clq"]], [say({"en": "p ({n} permutations)", "ar": "p ({n} تبديلة)"}, n=perms),
                                                  r["p"]],
                               [say({"en": "Points of A", "ar": "نقاط A"}), r["n_a"]],
                               [say({"en": "Points of B", "ar": "نقاط B"}), r["n_b"]],
                               [say({"en": "A points where B gathers", "ar": "نقاط A التي يتجمع حولها B"}), n_col],
                               [say({"en": "A points that B avoids", "ar": "نقاط A التي يتجنبها B"}), n_iso]]},
            "images": [ac.hist_png(r["sims"], r["clq"], say({"en": "CLQ of random relabellings",
                                                              "ar": "CLQ لإعادات التسمية العشوائية"}), "CLQ"),
                       ac.map_png(None, list(colours), legend, say({"en": "Co-location", "ar": "التجاور"}),
                                  points=d.xy)],
        }]
        self._report(parameters, context, say({"en": "Co-location: {a} and {b}", "ar": "التجاور: {a} و{b}"}, a=a, b=b),
                     answer, sections, d.notes, ["cause", "multiple", "edge"])
        return {self.ANSWER: answer, self.OUTPUT: dest, "CLQ": r["clq"],
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}


# =========================================================================================================
def _times(values):
    """Field values (QDateTime, QDate, datetime, text, numbers as years) -> numpy datetime64[s], NaT if not."""
    import pandas as pd
    from qgis.PyQt.QtCore import QDate, QDateTime

    out = []
    for v in values:
        if isinstance(v, QDateTime):
            out.append(np.datetime64(v.toSecsSinceEpoch() + v.offsetFromUtc(), "s") if v.isValid() else None)
        elif isinstance(v, QDate):
            out.append(np.datetime64(v.toString("yyyy-MM-dd")) if v.isValid() else None)
        elif v is None or str(v) in ("NULL", ""):
            out.append(None)
        else:
            out.append(str(v))
    s = pd.to_datetime(pd.Series([o if not isinstance(o, np.datetime64) else pd.Timestamp(o) for o in out],
                                 dtype=object), errors="coerce", format="mixed")
    return s.values.astype("datetime64[s]")


class EmergingHotSpots(_SpatialStatsBase):
    ID = "ssemerging"
    NAME = "Emerging hot spots (space-time)"
    HELP = ("For events with a date: counts them in hexagons and time steps, finds hot and cold spots in space "
            "and time (Gi*: the neighbouring hexagons in this and the previous step(s)), tests the trend of each "
            "place (Mann-Kendall) and names the pattern: new, consecutive, intensifying, persistent, "
            "diminishing, sporadic, oscillating or historical hot (or cold) spot, or no pattern - as defined for "
            "Emerging Hot Spot Analysis. At least 10 time steps are recommended for the trend.")

    TIME, STEP, CELL, WINDOW = "TIME", "STEP", "CELL_SIZE", "TIME_WINDOW"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Event points"), points_only=True)
        self.addParameter(QgsProcessingParameterField(self.TIME, tr("Date or time field"),
                                                      parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterEnum(
            self.STEP, tr("Time step"),
            [tr("Automatic"), tr("Hour"), tr("Day"), tr("Week"), tr("Month"), tr("Quarter"), tr("Year")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.CELL, tr("Hexagon size in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double, 0.0,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.DISTANCE, tr("Neighbourhood distance in metres (0 = the touching hexagons)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.WINDOW, tr("Previous time steps counted as neighbours"), Qgis.ProcessingNumberParameterType.Integer,
            1, minValue=0, maxValue=12))
        self._add_common(0, fdr_option=True)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Emerging hot spots"),
                                                            Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        tfield = self.parameterAsStrings(parameters, self.TIME, context)[0]
        unit = stc.STEP_UNITS[self.parameterAsEnum(parameters, self.STEP, context)]
        cell = self.parameterAsDouble(parameters, self.CELL, context)
        dist = self.parameterAsDouble(parameters, self.DISTANCE, context)
        window = self.parameterAsInt(parameters, self.WINDOW, context)
        use_fdr = self.parameterAsBool(parameters, self.FDR, context)
        d = self._load(parameters, context, feedback, [], points_only=True)
        times = _times([f[tfield] for f in d.feats])
        ok = ~np.isnat(times)
        if (~ok).sum():
            self._note(d, feedback, {"en": "{n} events without a readable date were left out.",
                                     "ar": "استُبعد {n} حدثًا بلا تاريخ مقروء."}, n=int((~ok).sum()))
        xy, times = d.xy[ok], times[ok]
        if len(xy) < MIN_HARD:
            raise QgsProcessingException(tr("Only {n} events have a date: at least {m} are needed.")
                                         .format(n=len(xy), m=MIN_HARD))
        if times.min() == times.max():
            raise QgsProcessingException(tr("All events have the same date: there is no time to analyse."))
        self._duplicates(d, feedback, xy)
        if unit == "auto":
            unit = stc.auto_unit(times)
        steps, starts = stc.to_steps(times, unit)
        t = len(starts)
        if t < 3:
            raise QgsProcessingException(tr("Only {n} time steps: choose a shorter time step.").format(n=t))
        if t < 10:
            self._note(d, feedback, {"en": "Only {n} time steps: the trend test needs at least 10 to be reliable; "
                                           "choose a shorter time step if you can.",
                                     "ar": "عدد الخطوات الزمنية {n} فقط: اختبار الاتجاه يحتاج 10 على الأقل ليكون موثوقًا؛ "
                                           "اختر خطوة زمنية أقصر إن أمكن."}, n=t)
        if cell <= 0:
            cell = wts.auto_cell(xy)
        grid, counts = stc.cube(xy, steps, cell)
        cells = grid["centres"]
        if len(cells) < MIN_HARD:
            raise QgsProcessingException(tr("Only {n} hexagons: use a smaller hexagon size.").format(n=len(cells)))
        wdist = dist if dist > 0 else cell * 1.01
        w = wts.distance_band(cells, wdist)
        feedback.setProgress(50)
        z, p = stc.space_time_gi(counts, w, window)
        sig = stc.significant(z, p, use_fdr)
        codes, sides, tz, tp = stc.classify(sig, z)
        _, cnt_tz, cnt_tp = ac.mann_kendall(counts.sum(0)[None, :])
        cnt_trend, cnt_trend_p = cnt_tz[0], cnt_tp[0]

        def full(code, side):
            if code == "no_pattern":
                return "no_pattern"
            return f"{code}_{'hot' if side > 0 else 'cold'}"

        def label(code, side):
            if code == "no_pattern":
                return say({"en": "No pattern detected", "ar": "لا نمط"})
            return say(_EHSA_WORDS[code], s=say(_SIDE_WORDS[int(side)]))

        dest = self._hex_sink(parameters, context, d.work, cells, cell, [
            ("events", QMetaType.Type.Int), ("pattern", QMetaType.Type.QString),
            ("pattern_label", QMetaType.Type.QString), ("hot_steps", QMetaType.Type.Int),
            ("cold_steps", QMetaType.Type.Int), ("trend_z", QMetaType.Type.Double),
            ("trend_p", QMetaType.Type.Double), ("final_z", QMetaType.Type.Double)],
            lambda i: [int(counts[i].sum()), full(codes[i], sides[i]), label(codes[i], sides[i]),
                       int((sig[i] > 0).sum()), int((sig[i] < 0).sum()), float(tz[i]), float(tp[i]),
                       float(z[i, -1])])
        cats = [("no_pattern", stc.NO_PATTERN_COLOUR, label("no_pattern", 0))]
        for code in stc.PATTERN_CODES[1:]:
            cats.append((f"{code}_hot", stc.HOT_COLOURS[code], label(code, 1)))
            cats.append((f"{code}_cold", stc.COLD_COLOURS[code], label(code, -1)))
        _style(context, dest, "pattern", cats)
        tally = {}
        for c, s in zip(codes, sides):
            if c != "no_pattern":
                tally[(c, int(s))] = tally.get((c, int(s)), 0) + 1
        order = sorted(tally.items(), key=lambda kv: -kv[1])
        unit_word = say({"en": {"h": "hour", "D": "day", "W": "week", "M": "month", "Q": "quarter", "Y": "year"}[unit],
                         "ar": {"h": "ساعة", "D": "يوم", "W": "أسبوع", "M": "شهر", "Q": "ربع سنة", "Y": "سنة"}[unit]})
        if not order:
            answer = say({"en": "No emerging pattern: none of the {n} places with events was a significant hot or "
                                "cold spot over the {t} time steps (one step = {u}).",
                          "ar": "لا نمط ناشئ: لم يكن أي من الأماكن الـ{n} ذات الأحداث بؤرة ساخنة أو باردة دالة خلال {t} "
                                "خطوة زمنية (الخطوة = {u})."}, n=len(cells), t=t, u=unit_word)
        else:
            parts = ", ".join(f"{n_} {label(c, s)}" for (c, s), n_ in order[:4])
            answer = say({"en": "Over {t} time steps (one step = {u}) of the {n} places with events: {parts}.",
                          "ar": "خلال {t} خطوة زمنية (الخطوة = {u}) ومن بين {n} مكانًا فيه أحداث: {parts}."},
                         t=t, u=unit_word, n=len(cells), parts=parts)
            new_hot = tally.get(("new", 1), 0) + tally.get(("consecutive", 1), 0) + tally.get(("intensifying", 1), 0)
            if new_hot:
                answer += " " + say({"en": "{k} places are new, consecutive or intensifying hot spots - the ones to "
                                           "watch.",
                                     "ar": "{k} مكانًا بؤر ساخنة جديدة أو متتالية أو تشتد - هي ما يجب مراقبته."}, k=new_hot)
        feedback.pushInfo(answer)
        per_step = counts.sum(0)
        date_labels = [str(s)[:10] if unit != "h" else str(s)[:13].replace("T", " ") for s in starts]
        rows_t = [[label(c, s), n_] for (c, s), n_ in order]
        rows_t.append([label("no_pattern", 0), int(sum(1 for c in codes if c == "no_pattern"))])
        colours = [stc.NO_PATTERN_COLOUR if c == "no_pattern" else
                   (stc.HOT_COLOURS if s > 0 else stc.COLD_COLOURS)[c] for c, s in zip(codes, sides)]
        legend = [((stc.HOT_COLOURS if s > 0 else stc.COLD_COLOURS)[c], f"{label(c, s)} ({n_})") for (c, s), n_ in order]
        legend.append((stc.NO_PATTERN_COLOUR, label("no_pattern", 0)))
        shapes = [[wts.hex_ring(cx, cy, cell)] for cx, cy in cells]
        sections = [{
            "heading": say({"en": "Patterns", "ar": "الأنماط"}),
            "paragraphs": [say({"en": "Hexagons of {c}; {t} time steps of one {u} from {a} to {b}. Neighbours: within "
                                      "{d} in the same and the previous {k} step(s). Hot or cold at 90% confidence{f}; "
                                      "trend: Mann-Kendall at 95%. Events per step trend: z = {tz} ({tp}).",
                                "ar": "خلايا سداسية بعرض {c}؛ {t} خطوة زمنية كل منها {u} من {a} إلى {b}. الجيران: في حدود "
                                      "{d} في الخطوة نفسها و{k} خطوة سابقة. بؤرة ساخنة أو باردة بثقة 90%{f}؛ الاتجاه: "
                                      "مان-كندال بثقة 95%. اتجاه عدد الأحداث لكل خطوة: z = {tz} ({tp})."},
                               c=fmt_dist(cell), t=t, u=unit_word, a=date_labels[0], b=date_labels[-1],
                               d=fmt_dist(wdist), k=window, tz=_f(float(cnt_trend), 2), tp=fmt_p(float(cnt_trend_p)),
                               f=say({"en": " (FDR within each step)", "ar": " (FDR داخل كل خطوة)"}) if use_fdr else "")],
            "table": {"columns": [say({"en": "Pattern", "ar": "النمط"}), say({"en": "Places", "ar": "الأماكن"})],
                      "rows": rows_t},
            "images": [ac.map_png(shapes, colours, legend, say({"en": "Emerging hot spots", "ar": "البؤر الناشئة"})),
                       ac.bars_png(date_labels, per_step, "#12A594",
                                   say({"en": "Events per time step", "ar": "الأحداث لكل خطوة زمنية"}),
                                   say({"en": "Events", "ar": "الأحداث"}), dates=True)],
        }]
        self._report(parameters, context, say({"en": "Emerging hot spots", "ar": "البؤر الساخنة الناشئة"}), answer,
                     sections, d.notes + [say({"en": "The last time step may be incomplete (data up to {e}); a short "
                                                     "last step can look like a cooling.",
                                               "ar": "قد تكون الخطوة الزمنية الأخيرة ناقصة (البيانات حتى {e})؛ والخطوة "
                                                     "الأخيرة القصيرة قد تبدو كأنها تبريد."},
                                              e=str(times.max())[:16].replace("T", " "))],
                     ["cause", "multiple", "edge", "maup", "counts"])
        return {self.ANSWER: answer, self.OUTPUT: dest,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}


# =========================================================================================================
class ScaleCheck(_SpatialStatsBase):
    ID = "ssscale"
    NAME = "Does the answer depend on the cell size? (Scale check, MAUP)"
    HELP = ("Counts the points (or the polygons' counts, at their centres) in hexagon grids of several sizes - "
            "by default 0.5 to 4 times a base size - and at each size computes global Moran's I and the share "
            "of hexagons that are hot spots. Then it says whether the conclusion holds at every size (robust) "
            "or only at some (it depends on the cell size you choose): the modifiable areal unit problem, "
            "checked instead of ignored.")

    CELL, FACTORS_PARAM = "CELL_SIZE", "FACTORS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_input(tr("Points or polygons"))
        self._add_field(self.FIELD, tr("Count or weight field (empty = one per point)"), optional=True)
        self.addParameter(QgsProcessingParameterNumber(
            self.CELL, tr("Base hexagon size in metres (0 = automatic)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterString(
            self.FACTORS_PARAM, tr("Sizes to try, as multiples of the base"), "0.5, 0.75, 1, 1.5, 2, 3, 4"))
        self._add_common(199, fdr_option=True)
        self.addOutput(QgsProcessingOutputString("VERDICT", tr("Verdict (robust, depends, none)")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        fields = self.parameterAsStrings(parameters, self.FIELD, context)
        field = fields[0] if fields else None
        d = self._load(parameters, context, feedback, [field] if field else [])
        if d.kind == "polygon" and field is None:
            raise QgsProcessingException(tr("Polygons need a count field to be re-aggregated."))
        if field:
            keep = self._keep_valid(d, feedback, [field])
            xy, wv = d.xy[keep], d.values[field][keep]
            if (wv < 0).any():
                raise QgsProcessingException(tr("The count field has negative values."))
        else:
            xy, wv = d.xy, None
        if d.kind == "polygon":
            self._note(d, feedback, {"en": "Each polygon's count was placed at its centre and counted in the "
                                           "hexagon holding that centre.",
                                     "ar": "وُضع عدد كل مضلع في مركزه وعُدّ في الخلية السداسية التي تحوي ذلك المركز."})
        else:
            self._duplicates(d, feedback, xy)
        base = self.parameterAsDouble(parameters, self.CELL, context)
        if base <= 0:
            base = wts.auto_cell(xy)
        factors = sc.parse_factors(self.parameterAsString(parameters, self.FACTORS_PARAM, context))
        perms = self._perms(parameters, context)
        use_fdr = self.parameterAsBool(parameters, self.FDR, context)
        rows = []
        for j, fct in enumerate(factors):
            if feedback.isCanceled():
                break
            r = sc.one_size(xy, wv, base * fct, perms, self._seed(parameters, context), use_fdr)
            rows.append(r)
            feedback.setProgress(10 + 85 * (j + 1) / len(factors))
        skipped = [r for r in rows if not r["ok"]]
        if skipped:
            self._note(d, feedback, {"en": "Sizes with fewer than 30 hexagons were skipped: {s}.",
                                     "ar": "تُركت الأحجام التي تعطي أقل من 30 خلية: {s}."},
                       s=", ".join(fmt_dist(r["width"]) for r in skipped))
        verdict, found = sc.verdict(rows)
        if verdict == "robust":
            answer = say({"en": "The clustering is found at every cell size tested ({a} to {b}) → the conclusion is "
                                "robust to the scale.",
                          "ar": "التجمع موجود عند كل أحجام الخلايا المختبرة ({a} إلى {b}) ← الاستنتاج ثابت مع تغيّر المقياس."},
                         a=fmt_dist(min(r["width"] for r in found)), b=fmt_dist(max(r["width"] for r in found)))
        elif verdict == "depends":
            answer = say({"en": "The clustering is found only at {s} → the conclusion depends on the cell size you "
                                "choose; say so when you show the map.",
                          "ar": "التجمع يظهر فقط عند {s} ← الاستنتاج يتوقف على حجم الخلية المختار؛ اذكر ذلك عند عرض "
                                "الخريطة."},
                         s=", ".join(fmt_dist(r["width"]) for r in found))
        else:
            answer = say({"en": "No significant clustering at any cell size tested.",
                          "ar": "لا تجمع دال عند أي حجم خلية مختبر."})
        feedback.pushInfo(answer)
        ok_rows = [r for r in rows if r["ok"]]
        images = []
        if ok_rows:
            images.append(ac.line_png([r["width"] for r in ok_rows], [[r["I"] for r in ok_rows]],
                                      ["Moran's I"], say({"en": "Clustering by cell size", "ar": "التجمع حسب حجم الخلية"}),
                                      say({"en": "Hexagon size (m)", "ar": "حجم الخلية (م)"}), "Moran's I",
                                      marks=[base], hline=0.0, logx=True,
                                      y2=([100 * r["hot"] for r in ok_rows],
                                          say({"en": "Hot spot hexagons (%)", "ar": "خلايا البؤر الساخنة (%)"}))))
        sections = [{
            "heading": say({"en": "Results by cell size", "ar": "النتائج حسب حجم الخلية"}),
            "paragraphs": [say({"en": "Base size {b}; neighbours: the six touching hexagons for Moran's I, two cell "
                                      "widths for the hot spots (95% confidence{f}).",
                                "ar": "الحجم الأساسي {b}؛ الجيران: الخلايا الست الملامسة لمؤشر موران، ومسافة خليتين للبؤر "
                                      "الساخنة (ثقة 95%{f})."}, b=fmt_dist(base),
                               f=say({"en": ", FDR", "ar": "، FDR"}) if use_fdr else "")],
            "table": {"columns": [say({"en": "Hexagon size", "ar": "حجم الخلية"}), say({"en": "Hexagons", "ar": "الخلايا"}),
                                  "Moran's I", "z", "p", say({"en": "Hot spots %", "ar": "البؤر الساخنة %"}),
                                  say({"en": "Cold spots %", "ar": "البؤر الباردة %"})],
                      "rows": [[fmt_dist(r["width"]), int(r["cells"]), r["I"], r["z"], r["p"],
                                100 * r["hot"] if r["ok"] else float("nan"),
                                100 * r["cold"] if r["ok"] else float("nan")] for r in rows]},
            "images": images,
        }]
        self._report(parameters, context, say({"en": "Scale check (MAUP)", "ar": "فحص المقياس (MAUP)"}), answer,
                     sections, d.notes, ["cause", "edge", "counts"])
        return {self.ANSWER: answer, "VERDICT": verdict,
                self.REPORT: self.parameterAsFileOutput(parameters, self.REPORT, context) or None}
