"""Spatial statistics: what explains a value? Global OLS with spatial diagnostics (biruni:ssols) and
geographically weighted regression (biruni:ssgwr). The numbers live in spatial_stats/regression.py and
spatial_stats/gwr.py; this file reads the layer, checks the data, writes the output layer, styles it and
writes the report."""
import math
import re

from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGraduatedSymbolRenderer,
                       QgsLinePatternFillSymbolLayer, QgsMapLayerStyle, QgsProcessingException,
                       QgsProcessingLayerPostProcessorInterface, QgsProcessingOutputNumber,
                       QgsProcessingOutputString, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber, QgsProperty,
                       QgsRendererRange, QgsSimpleMarkerSymbolLayer, QgsSymbol, QgsSymbolLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor

from ..i18n import tr
from ..spatial_stats.common import GROUP, GROUP_ID, say, text_lang
from .base import BiruniAlgorithm

# blue (below) -> pale -> red (above): ColorBrewer RdBu, 7 classes
DIVERGING = ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"]
RESIDUAL_BREAKS = [-1e300, -2.5, -1.5, -0.5, 0.5, 1.5, 2.5, 1e300]
RESIDUAL_NAMES = ["< -2.5", "-2.5 … -1.5", "-1.5 … -0.5", "-0.5 … 0.5", "0.5 … 1.5", "1.5 … 2.5", "> 2.5"]


# ------------------------------------------------------------------ styling ---

def _base_symbol(layer):
    sym = QgsSymbol.defaultSymbol(layer.geometryType())
    if layer.geometryType() == Qgis.GeometryType.Polygon:
        sym.symbolLayer(0).setStrokeColor(QColor("#8a949c"))
        sym.symbolLayer(0).setStrokeWidth(0.1)
    elif layer.geometryType() == Qgis.GeometryType.Point:
        sym.setSize(2.4)
    return sym


def graduated(layer, field, breaks, labels, colours, not_significant=None):
    """Graduated renderer; not_significant: an expression - where true, polygons get a grey hatch and points
    a grey cross on top of the colour."""
    ranges = []
    for i, colour in enumerate(colours):
        sym = _base_symbol(layer)
        if not_significant:
            over = None
            if layer.geometryType() == Qgis.GeometryType.Polygon:
                over = QgsLinePatternFillSymbolLayer()
                over.setLineAngle(45)
                over.setDistance(1.6)
                over.setLineWidth(0.25)
                over.setColor(QColor("#59636b"))
            elif layer.geometryType() == Qgis.GeometryType.Point:
                over = QgsSimpleMarkerSymbolLayer(Qgis.MarkerShape.Cross2, 2.6)
                over.setColor(QColor("#59636b"))
                over.setStrokeColor(QColor("#59636b"))
            if over is not None:
                over.setLocked(True)
                over.setDataDefinedProperty(QgsSymbolLayer.Property.LayerEnabled,
                                            QgsProperty.fromExpression(not_significant))
                sym.appendSymbolLayer(over)
        sym.setColor(QColor(colour))
        ranges.append(QgsRendererRange(breaks[i], breaks[i + 1], sym, labels[i]))
    return QgsGraduatedSymbolRenderer(field, ranges)


def symmetric_classes(values, n=7):
    """Class breaks symmetric about zero (sign matters for a coefficient), outer classes open."""
    import numpy as np
    from ..spatial_stats import regression as R

    v = np.asarray([x for x in values if x is not None and math.isfinite(x)], dtype=float)
    m = float(np.max(np.abs(v))) if v.size else 1.0
    m = m or 1.0
    edges = list(np.linspace(-m, m, n + 1))
    labels = [f"{R.fmt(edges[i])} … {R.fmt(edges[i + 1])}" for i in range(n)]
    edges[0], edges[-1] = -1e300, 1e300
    return edges, labels


def style_ols(layer):
    layer.setRenderer(graduated(layer, "ols_stdres", RESIDUAL_BREAKS, RESIDUAL_NAMES, DIVERGING))
    layer.triggerRepaint()


def style_gwr(layer, keys):
    """One named style per local coefficient (diverging, hatched where not significant), plus local R²,
    condition number and residuals; the first explanatory coefficient is shown."""
    sm = layer.styleManager()
    old = sm.currentStyle()
    names = []
    for key in keys:
        field = f"b_{key}"
        if layer.fields().indexOf(field) < 0:
            continue
        edges, labels = symmetric_classes([f[field] for f in layer.getFeatures()])
        layer.setRenderer(graduated(layer, field, edges, labels, DIVERGING, f'"sig_{key}" = 0'))
        names.append(tr("Coefficient: {name}").format(name=key))
        _add_style(layer, names[-1])
    ramp = ["#f7fcf5", "#c7e9c0", "#74c476", "#31a354", "#006d2c"]
    layer.setRenderer(graduated(layer, "gwr_r2", [-1e300, 0.2, 0.4, 0.6, 0.8, 1e300],
                                ["< 0.2", "0.2 … 0.4", "0.4 … 0.6", "0.6 … 0.8", "> 0.8"], ramp))
    _add_style(layer, tr("Local R²"))
    layer.setRenderer(graduated(layer, "gwr_cond", [-1e300, 10, 20, 30, 1e300],
                                ["< 10", "10 … 20", "20 … 30", tr("> 30 (unreliable)")],
                                ["#f1eef6", "#bdc9e1", "#74a9cf", "#b2182b"]))
    _add_style(layer, tr("Condition number"))
    layer.setRenderer(graduated(layer, "gwr_stdres", RESIDUAL_BREAKS, RESIDUAL_NAMES, DIVERGING))
    _add_style(layer, tr("Standardised residual"))
    if names:
        sm.setCurrentStyle(names[1] if len(names) > 1 else names[0])
        if old not in names:
            sm.removeStyle(old)
    layer.triggerRepaint()
    return names


def _add_style(layer, name):
    st = QgsMapLayerStyle()
    st.readFromLayer(layer)
    layer.styleManager().addStyle(name, st)


class _Styler(QgsProcessingLayerPostProcessorInterface):
    instances = []  # Python references must outlive the run

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        _Styler.instances.append(self)

    def postProcessLayer(self, layer, context, feedback):  # noqa: N802
        if layer is not None:
            try:
                self.fn(layer)
            except Exception as e:  # noqa: BLE001 - a style must never fail the analysis
                feedback.reportError(str(e))


# --------------------------------------------------------------- shared ---

def field_key(name, taken):
    """A short field-safe key for a variable name (b_<key>, t_<key> …)."""
    key = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_")[:18] or "x"
    base, i = key, 2
    while key in taken:
        key = f"{base[:15]}_{i}"
        i += 1
    taken.add(key)
    return key


class _RegressionBase(BiruniAlgorithm):
    GROUP = GROUP
    GROUP_ID = GROUP_ID
    INPUT, DEPENDENT, EXPLANATORY, OUTPUT, REPORT = "INPUT", "DEPENDENT", "EXPLANATORY", "OUTPUT", "REPORT"

    def _common_inputs(self):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Layer"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterField(
            self.DEPENDENT, tr("Value to explain (dependent variable)"), parentLayerParameterName=self.INPUT,
            type=Qgis.ProcessingFieldParameterDataType.Numeric))
        self.addParameter(QgsProcessingParameterField(
            self.EXPLANATORY, tr("Explanatory variables"), parentLayerParameterName=self.INPUT,
            type=Qgis.ProcessingFieldParameterDataType.Numeric, allowMultiple=True))

    def _report_param(self):
        self.addParameter(QgsProcessingParameterFileDestination(
            self.REPORT, tr("Report (HTML)"), tr("HTML files") + " (*.html)", optional=True, createByDefault=False))

    def _read(self, parameters, context, feedback, min_extra):
        import numpy as np
        from ..spatial_stats import regression as R

        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        dep = self.parameterAsString(parameters, self.DEPENDENT, context)
        expl = [e for e in self.parameterAsStrings(parameters, self.EXPLANATORY, context) if e]
        if not dep:
            raise QgsProcessingException(tr("Choose the value to explain."))
        if dep in expl:
            expl.remove(dep)
            feedback.reportError(tr("“{name}” is the value to explain, so it was taken out of the explanatory "
                                    "variables.").format(name=dep))
        if not expl:
            raise QgsProcessingException(tr("Choose at least one explanatory variable."))
        data = R.read_features(source, dep, expl, context, feedback)
        ok = data["ok"]
        n_all, n = len(ok), int(ok.sum())
        if n_all - n:
            feedback.reportError(tr("{n} features were left out because a value or the geometry is missing.")
                                 .format(n=n_all - n))
        k = len(expl) + 1
        need = max(k + min_extra, 10)
        if n < need:
            raise QgsProcessingException(tr("Only {n} complete features for {k} unknowns: a regression needs many "
                                            "more features than variables (at least {m}).")
                                         .format(n=n, k=k, m=need))
        if n < max(30, 10 * len(expl)):
            feedback.reportError(tr("Only {n} features: the results will be uncertain (about 10 features per "
                                    "explanatory variable and at least 30 are advised).").format(n=n))
        y, X = data["y"][ok], data["X"][ok]
        if np.std(y) == 0:
            raise QgsProcessingException(tr("“{name}” has the same value everywhere: there is nothing to explain.")
                                         .format(name=dep))
        for j, name in enumerate(expl):
            if np.std(X[:, j]) == 0:
                raise QgsProcessingException(tr("“{name}” has the same value everywhere: it cannot explain "
                                                "anything. Remove it.").format(name=name))
        if np.linalg.matrix_rank(np.column_stack([np.ones(n), X])) < k:
            raise QgsProcessingException(tr("Some explanatory variables are exact combinations of others (for "
                                            "example percentages that add up to 100). Remove one of them."))
        if data["projected_to"]:
            feedback.pushInfo(tr("Distances are measured in {crs} (metres), because the layer is not in a "
                                 "metric coordinate system.").format(crs=data["projected_to"]))
        coords = data["coords"][ok]
        dup = n - len(np.unique(np.round(coords, 6), axis=0))
        if dup:
            feedback.pushInfo(tr("{n} features share a location with another feature.").format(n=dup))
        return source, dep, expl, data, y, X, coords

    def _sink(self, parameters, context, source, extra):
        names = {e[0] for e in extra}
        fields = QgsFields()
        kept = []
        for f in source.fields():
            if f.name() not in names:
                fields.append(f)
                kept.append(f.name())
        for name, typ in extra:
            fields.append(QgsField(name, typ))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, source.wkbType(),
                                          source.sourceCrs())
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))
        return sink, dest, fields, kept

    @staticmethod
    def _write(sink, fields, kept, data, rows):
        """rows: {feature index -> [extra values]}"""
        width = len(fields) - len(kept)
        for i, f in enumerate(data["features"]):
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            vals = rows.get(i) or [None] * width
            out.setAttributes([f[name] for name in kept] + [None if v is None or (isinstance(v, float) and not
                                                                                   math.isfinite(v)) else v
                                                            for v in vals])
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)


# ------------------------------------------------------------------- OLS ---

class SpatialOLS(_RegressionBase):
    ID = "ssols"
    NAME = "What explains this value? (OLS regression with spatial checks)"
    HELP = ("Ordinary least squares regression: how much each explanatory variable goes with the value to "
            "explain, how sure that is, and whether the model can be trusted. Reports coefficients with normal "
            "and robust (HC1) standard errors, R² and adjusted R², AICc, VIF for overlapping variables, "
            "Jarque–Bera (normal residuals) and Koenker–Bassett (changing spread, a hint that relationships vary "
            "in space), and Moran's I of the residuals: clustered residuals mean a variable is missing or the "
            "relationships change from place to place (try geographically weighted regression). Adds the "
            "predicted value, the residual and the standardised residual to every feature and maps the "
            "residuals. The answer is written in plain words, with an optional HTML report.")
    NEIGHBOURHOOD, NEIGHBOURS, DISTANCE, PERMUTATIONS = "NEIGHBOURHOOD", "NEIGHBOURS", "DISTANCE", "PERMUTATIONS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._common_inputs()
        self.addParameter(QgsProcessingParameterEnum(
            self.NEIGHBOURHOOD, tr("Neighbours for the residual check"),
            [tr("K nearest neighbours"), tr("Distance band"), tr("Polygons that touch (queen)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.NEIGHBOURS, tr("Number of neighbours (K)"), Qgis.ProcessingNumberParameterType.Integer, 8,
            minValue=1, maxValue=100))
        self.addParameter(QgsProcessingParameterNumber(
            self.DISTANCE, tr("Distance band in metres (0 = the smallest that gives everyone a neighbour)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.PERMUTATIONS, tr("Permutations"), Qgis.ProcessingNumberParameterType.Integer, 999, minValue=99,
            maxValue=99999))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Regression residuals")))
        self._report_param()
        self.addOutput(QgsProcessingOutputString("ANSWER", tr("Answer")))
        self.addOutput(QgsProcessingOutputNumber("R2", tr("R²")))
        self.addOutput(QgsProcessingOutputNumber("AICC", tr("AICc")))
        self.addOutput(QgsProcessingOutputNumber("MORAN_I", tr("Moran's I of the residuals")))
        self.addOutput(QgsProcessingOutputNumber("MORAN_P", tr("p-value of Moran's I")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import numpy as np

        from ..spatial_stats import regression as R
        from ..spatial_stats import weights as W

        source, dep, expl, data, y, X, coords = self._read(parameters, context, feedback, 3)
        lang = text_lang()
        res = R.ols(y, X, expl)
        feedback.setProgress(30)
        mode = self.parameterAsEnum(parameters, self.NEIGHBOURHOOD, context)
        k = self.parameterAsInt(parameters, self.NEIGHBOURS, context)
        perms = self.parameterAsInt(parameters, self.PERMUTATIONS, context)
        n = len(y)
        if mode == 2 and QgsWkbTypes.geometryType(source.wkbType()) != Qgis.GeometryType.Polygon:
            feedback.reportError(tr("“Polygons that touch” needs a polygon layer: K nearest neighbours are used."))
            mode = 0
        if mode == 1:
            dist = self.parameterAsDouble(parameters, self.DISTANCE, context) or R.default_band(coords)
            w, isl = R.band_weights(coords, dist)
            neigh = say({"ar": "كل المعالم في حدود {d} م", "en": "all features within {d} m"}, lang, d=R.fmt(dist))
            if isl:
                feedback.reportError(tr("{n} features had no neighbour inside the distance band; their nearest "
                                        "feature was used.").format(n=isl))
        elif mode == 2:
            geoms = [f.geometry() for f, good in zip(data["features"], data["ok"]) if good]
            wc, isl = W.attach_islands(W.contiguity(geoms, True, feedback), coords)
            w = W.row_standardise(wc)
            neigh = say({"ar": "المضلعات المتلامسة (queen)", "en": "polygons that touch (queen)"}, lang)
            if len(isl):
                feedback.reportError(tr("{n} polygons touch no other polygon; their nearest one was used.")
                                     .format(n=len(isl)))
        else:
            w = R.knn_weights(coords, min(k, n - 1))
            neigh = say({"ar": "أقرب {k} جيران", "en": "the {k} nearest neighbours"}, lang, k=min(k, n - 1))
        moran = R.morans_i(res["resid"], w, perms)
        feedback.setProgress(70)

        extra = [("ols_fit", QMetaType.Type.Double), ("ols_resid", QMetaType.Type.Double),
                 ("ols_stdres", QMetaType.Type.Double)]
        sink, dest, fields, kept = self._sink(parameters, context, source, extra)
        idx = np.flatnonzero(data["ok"])
        rows = {int(i): [float(res["fitted"][j]), float(res["resid"][j]), float(res["std_resid"][j])]
                for j, i in enumerate(idx)}
        self._write(sink, fields, kept, data, rows)
        if context.willLoadLayerOnCompletion(dest):
            context.layerToLoadOnCompletionDetails(dest).setPostProcessor(_Styler(style_ols))

        answer = R.answer_text(res, X, dep, moran, lang)
        warns = R.warnings_list(res, lang)
        feedback.pushInfo(answer)
        for wtxt in warns:
            feedback.reportError(wtxt)
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)
        if report:
            self._report(report, res, moran, neigh, perms, y, X, coords, dep, answer, warns, data, lang)
        feedback.setProgress(100)
        return {self.OUTPUT: dest, self.REPORT: report or None, "ANSWER": answer, "R2": res["r2"],
                "AICC": res["aicc"], "MORAN_I": moran["I"], "MORAN_P": moran["p"]}

    @staticmethod
    def _report(path, res, moran, neigh, perms, y, X, coords, dep, answer, warns, data, lang):
        from ..spatial_stats import regression as R
        from ..spatial_stats.report import write_report

        s = lambda ar, en, **v: say({"ar": ar, "en": en}, lang, **v)  # noqa: E731
        use_r = math.isfinite(res["kb_p"]) and res["kb_p"] < 0.05
        rows = []
        for j, name in enumerate(res["names"]):
            rows.append([name, float(res["beta"][j]), float(res["se"][j]), float(res["t"][j]), float(res["p"][j]),
                         float(res["se_robust"][j]), float(res["p_robust"][j]),
                         float(res["vif"][j - 1]) if j > 0 else "-"])
        fit_rows = [
            [s("عدد المعالم", "Features"), res["n"]],
            ["R²", res["r2"]], [s("R² المعدّل", "Adjusted R²"), res["adj_r2"]], ["AICc", res["aicc"]],
            [s("اختبار F (كل المعاملات معًا)", "F test (all slopes together)"), res["f"]],
            [s("قيمة p لاختبار F", "p of F"), res["f_p"]],
            ["Jarque–Bera", res["jb"]], [s("قيمة p لـ Jarque–Bera", "p of Jarque–Bera"), res["jb_p"]],
            ["Koenker–Bassett", res["kb"]], [s("قيمة p لـ Koenker–Bassett", "p of Koenker–Bassett"), res["kb_p"]],
            [s("Moran's I للبواقي", "Moran's I of the residuals"), moran["I"]],
            [s("قيمة p (تباديل)", "p (permutations)"), moran["p"]],
            [s("الجيران", "Neighbours"), neigh], [s("التباديل", "Permutations"), perms],
        ]
        if data["projected_to"]:
            fit_rows.append([s("المسافات مقاسة في", "Distances measured in"), data["projected_to"]])
        sections = [
            {"heading": s("المعاملات", "Coefficients"),
             "paragraphs": [s("الأخطاء المعيارية المتينة (HC1) هي المعتمدة لأن تباين البواقي غير ثابت.",
                              "The robust (HC1) standard errors are the ones to read, because the spread of "
                              "the residuals is not constant.")] if use_r else [],
             "table": {"columns": [s("المتغير", "Variable"), s("المعامل", "Coefficient"), s("الخطأ المعياري", "SE"),
                                   "t", "p", s("خطأ متين HC1", "Robust SE (HC1)"), s("p متين", "Robust p"), "VIF"],
                       "rows": rows}},
            {"heading": s("جودة النموذج والفحوص", "Model fit and checks"),
             "table": {"columns": [s("المقياس", "Measure"), s("القيمة", "Value")], "rows": fit_rows},
             "warnings": warns},
            {"heading": s("الرسوم", "Charts"),
             "images": [R.chart_observed_predicted(y, res["fitted"], dep, lang),
                        R.chart_residual_histogram(res["std_resid"], lang),
                        R.chart_residual_map(coords, res["std_resid"], lang)]},
            {"heading": s("ما لا تعنيه النتيجة", "What this does not mean"), "paragraphs": R.caveats(lang)},
        ]
        write_report(path, s("ما الذي يفسّر «{y}»؟ (انحدار OLS)", "What explains “{y}”? (OLS regression)", y=dep),
                     sections, lang, answer)


# ------------------------------------------------------------------- GWR ---

class SpatialGWR(_RegressionBase):
    ID = "ssgwr"
    NAME = "Does the relationship change from place to place? (geographically weighted regression)"
    HELP = ("Geographically weighted regression (GWR): fits a regression around every feature, giving more "
            "weight to nearby features, so each place gets its own coefficients. The bandwidth (the number of "
            "neighbours, or a distance) is chosen by golden-section search on AICc unless you give one. Outputs "
            "local coefficients, t-values, local R² and condition numbers (above 30: the variables overlap "
            "locally) for every feature, and compares the model with global OLS (AICc and an ANOVA-style F test). "
            "Each coefficient gets its own map style (layer styles): blue below zero, red above, hatched where "
            "not significant after the Silva–Fotheringham correction for the many local tests.")
    KERNEL, BW_MODE, BANDWIDTH = "KERNEL", "BW_MODE", "BANDWIDTH"

    def initAlgorithm(self, config=None):  # noqa: N802
        self._common_inputs()
        self.addParameter(QgsProcessingParameterEnum(
            self.KERNEL, tr("Neighbourhood (kernel)"),
            [tr("Nearest neighbours (adaptive bisquare)"), tr("Fixed distance (Gaussian)"),
             tr("Fixed distance (bisquare)")], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            self.BW_MODE, tr("Bandwidth"), [tr("Best by AICc (golden-section search)"), tr("Use my bandwidth")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.BANDWIDTH, tr("My bandwidth (neighbours, or metres for a fixed distance)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("Local regression")))
        self._report_param()
        self.addOutput(QgsProcessingOutputString("ANSWER", tr("Answer")))
        self.addOutput(QgsProcessingOutputNumber("BANDWIDTH_USED", tr("Bandwidth used")))
        self.addOutput(QgsProcessingOutputNumber("AICC", tr("AICc")))
        self.addOutput(QgsProcessingOutputNumber("AICC_OLS", tr("AICc of global OLS")))
        self.addOutput(QgsProcessingOutputNumber("R2", tr("R²")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import numpy as np

        from ..spatial_stats import gwr as G
        from ..spatial_stats import regression as R

        source, dep, expl, data, y, X, coords = self._read(parameters, context, feedback, 10)
        lang = text_lang()
        n = len(y)
        if n < 50:
            feedback.reportError(tr("GWR with fewer than 50 features is fragile: every local model sees only a "
                                    "handful of features."))
        if n > 8000:
            feedback.pushInfo(tr("{n} features: the bandwidth search may take a few minutes.").format(n=n))
        kernel = G.KERNEL_IDS[self.parameterAsEnum(parameters, self.KERNEL, context)]
        model = G.GWR(coords, y, X, expl, kernel)
        if self.parameterAsEnum(parameters, self.BW_MODE, context) == 1:
            bw = self.parameterAsDouble(parameters, self.BANDWIDTH, context)
            lo, hi = model.bounds()
            if bw <= 0:
                raise QgsProcessingException(tr("Enter the bandwidth, or let the tool find the best one."))
            if kernel == G.ADAPTIVE_BISQUARE:
                bw = float(min(max(round(bw), lo), n))
            elif bw < lo:
                feedback.reportError(tr("This distance leaves some features with too few neighbours; {d} m is used "
                                        "instead.").format(d=R.fmt(lo)))
                bw = lo
            model.aicc(bw)
        else:
            feedback.pushInfo(tr("Searching the best bandwidth (golden-section search on AICc)…"))
            bw = model.search()
            lo, hi = model.bounds()
            if bw >= hi * 0.98:
                feedback.pushInfo(tr("The best bandwidth is (nearly) the whole map: the relationships look the "
                                     "same everywhere."))
        if feedback.isCanceled():
            return {}
        feedback.setProgress(60)
        res = model.fit(bw)
        cmp = G.compare_with_ols(res, y, X, expl)
        feedback.setProgress(80)

        taken = {"intercept"}
        keys = ["intercept"] + [field_key(e, taken) for e in expl]
        extra = [("gwr_fit", QMetaType.Type.Double), ("gwr_resid", QMetaType.Type.Double),
                 ("gwr_stdres", QMetaType.Type.Double), ("gwr_r2", QMetaType.Type.Double),
                 ("gwr_cond", QMetaType.Type.Double)]
        for key in keys:
            extra += [(f"b_{key}", QMetaType.Type.Double), (f"se_{key}", QMetaType.Type.Double),
                      (f"t_{key}", QMetaType.Type.Double), (f"sig_{key}", QMetaType.Type.Int)]
        sink, dest, fields, kept = self._sink(parameters, context, source, extra)
        rows = {}
        for j, i in enumerate(np.flatnonzero(data["ok"])):
            vals = [float(res["fitted"][j]), float(res["resid"][j]), float(res["std_resid"][j]),
                    float(res["local_r2"][j]), float(res["cond"][j])]
            for c in range(len(keys)):
                vals += [float(res["beta"][j, c]), float(res["se"][j, c]), float(res["t"][j, c]),
                         int(res["significant"][j, c])]
            rows[int(i)] = vals
        self._write(sink, fields, kept, data, rows)
        if context.willLoadLayerOnCompletion(dest):
            context.layerToLoadOnCompletionDetails(dest).setPostProcessor(_Styler(lambda lyr: style_gwr(lyr, keys)))

        answer = G.answer_text(res, cmp, dep, lang)
        feedback.pushInfo(answer)
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)
        if report:
            self._report(report, res, cmp, model, coords, dep, answer, data, lang)
        feedback.setProgress(100)
        return {self.OUTPUT: dest, self.REPORT: report or None, "ANSWER": answer, "BANDWIDTH_USED": res["bandwidth"],
                "AICC": res["aicc"], "AICC_OLS": cmp["ols"]["aicc"], "R2": res["r2"]}

    @staticmethod
    def _report(path, res, cmp, model, coords, dep, answer, data, lang):
        import numpy as np
        from ..spatial_stats import regression as R
        from ..spatial_stats import gwr as G
        from ..spatial_stats.report import write_report

        s = lambda ar, en, **v: say({"ar": ar, "en": en}, lang, **v)  # noqa: E731
        g = cmp["ols"]
        fit_rows = [
            [s("عدد المعالم", "Features"), res["n"]],
            [s("الجوار", "Neighbourhood"), G.kernel_words(res["kernel"], res["bandwidth"], lang)],
            [s("عدد المعاملات الفعلي (ENP = tr S)", "Effective number of parameters (ENP = tr S)"), res["enp"]],
            ["AICc GWR", res["aicc"]], ["AICc OLS", g["aicc"]], [s("انخفاض AICc", "AICc drop"), cmp["aicc_drop"]],
            ["R² GWR", res["r2"]], [s("R² المعدّل GWR", "Adjusted R² GWR"), res["adj_r2"]], ["R² OLS", g["r2"]],
            [s("α المعدّلة (Silva–Fotheringham)", "Adjusted α (Silva–Fotheringham)"), res["alpha_adj"]],
            [s("قيمة t الحرجة", "Critical |t|"), res["t_crit"]],
        ]
        if data["projected_to"]:
            fit_rows.append([s("المسافات مقاسة في", "Distances measured in"), data["projected_to"]])
        anova = [[G.anova_label(key, lang), ss, df, ms, f if math.isfinite(f) else "-"]
                 for key, ss, df, ms, f in cmp["anova"]]
        coef_rows = []
        for j, name in enumerate(res["names"]):
            b = res["beta"][:, j]
            q = np.percentile(b, [0, 25, 50, 75, 100])
            coef_rows.append([name, float(g["beta"][j])] + [float(v) for v in q]
                             + [f"{100 * float(np.mean(res['significant'][:, j])):.0f}%"])
        sections = [
            {"heading": s("عرض النطاق وجودة النموذج", "Bandwidth and fit"),
             "table": {"columns": [s("المقياس", "Measure"), s("القيمة", "Value")], "rows": fit_rows},
             "images": [G.chart_aicc(model.history, res["bandwidth"], res["kernel"], lang)]},
            {"heading": s("مقارنة مع النموذج العام (ANOVA)", "Comparison with the global model (ANOVA)"),
             "paragraphs": [s("اختبار F تقريبي: هل تحسّن GWR أكبر من الصدفة؟ قيمة p = {p}.",
                              "Approximate F test: is the GWR improvement larger than chance? p = {p}.",
                              p=R.fmt(cmp["f_p"]))],
             "table": {"columns": [s("المصدر", "Source"), s("مجموع المربعات", "Sum of squares"), "df",
                                   s("متوسط المربعات", "Mean square"), "F"], "rows": anova}},
            {"heading": s("المعاملات المحلية", "Local coefficients"),
             "table": {"columns": [s("المتغير", "Variable"), "OLS", s("الأدنى", "Min"), "Q1", s("الوسيط", "Median"),
                                   "Q3", s("الأعلى", "Max"), s("معنوي", "Significant")], "rows": coef_rows},
             "images": [G.chart_coefficients(coords, res, lang)],
             "warnings": [s("{b} معلم رقم الشرط عندها أكبر من 30.", "{b} features have a condition number "
                            "above 30.", b=int(np.sum(res["cond"] > G.COND_LIMIT)))]
             if np.any(res["cond"] > G.COND_LIMIT) else []},
            {"heading": s("ما لا تعنيه النتيجة", "What this does not mean"), "paragraphs": G.caveats(lang)},
        ]
        write_report(path, s("هل تتغير العلاقة من مكان لآخر؟ (GWR) - «{y}»",
                             "Does the relationship change from place to place? (GWR) - “{y}”", y=dep),
                     sections, lang, answer)
