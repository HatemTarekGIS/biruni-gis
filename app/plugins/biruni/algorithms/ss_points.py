"""Spatial statistics of points and rates: nearest neighbour, Ripley's K, kernel density, centre and
spread, DBSCAN clusters, and Empirical Bayes rate smoothing. Each tool checks the data first, answers in
one sentence (ANSWER), and can write an HTML report (answer, table, charts, what the result does not mean)
in Arabic or English. The maths lives in spatial_stats/points.py, distribution.py and rates.py."""
import math
import time

import numpy as np
from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingOutputNumber, QgsProcessingOutputString, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterString, QgsProcessingUtils)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..spatial_stats import distribution as dist_mod
from ..spatial_stats import points as pts_mod
from ..spatial_stats import rates as rates_mod
from ..spatial_stats.common import GROUP, GROUP_ID, _mpl_lays_out_arabic, chart_text, figure, png, rng, say, text_lang
from ..spatial_stats.report import write_report
from .base import BiruniAlgorithm, polygonal

ACCENT = "#12A594"
GREY = "#9AA5AE"
SERIES = ["#12A594", "#C98A12", "#3A6EA5", "#B5485D", "#6B8E23", "#7A5BA6", "#D2691E", "#2F4F4F"]


def _s(lang, en, ar, **kw):
    return say({"en": en, "ar": ar}, lang, **kw)


def _chart(lang, en, ar, **kw):
    """Chart text in the explanation language (Arabic shaped for matplotlib)."""
    text = _s(lang, en, ar, **kw)
    if lang == "ar" and not _mpl_lays_out_arabic():  # an old matplotlib draws reversed text without mirroring
        text = _mirror_lone_brackets(text)
    return chart_text(text)


def _mirror_lone_brackets(text):
    """chart_text reverses Arabic for matplotlib but keeps the bracket glyphs, so a bracket that is not inside
    a Latin run such as L(d) ends up facing the wrong way: swap those beforehand."""
    keep = set(".,%-+:/()")
    out, run = [], []

    def flush():
        if run and not any(c.isalnum() for c in run):
            out.extend({"(": ")", ")": "("}.get(c, c) for c in run)
        else:
            out.extend(run)
        run.clear()

    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in keep):
            run.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def _p(p):
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def _num(v, d=0):
    return f"{v:,.{d}f}"


def _metric_crs(crs, extent, context):
    """The layer's own CRS when it is projected in metres, else the UTM zone of the data. Returns
    (crs, changed: bool)."""
    if crs.isValid() and not crs.isGeographic() and crs.mapUnits() == Qgis.DistanceUnit.Meters:
        return crs, False
    if not crs.isValid():
        raise QgsProcessingException(tr("The layer has no coordinate system: set it first (Layer Properties)."))
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    c = extent.center()
    try:
        c = QgsCoordinateTransform(crs, wgs, context.transformContext()).transform(c)
    except Exception:
        raise QgsProcessingException(tr("The layer's coordinates cannot be converted to degrees."))
    zone = min(int((c.x() + 180) // 6) + 1, 60)
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if c.y() >= 0 else 32700) + zone}"), True


class PointData:
    """Points read from a layer, in metres: xy, weights, case values, and (when kept) the source features."""

    def __init__(self):
        self.xy = None
        self.w = None
        self.cases = None
        self.features = []      # (QgsFeature, part index) per point when kept
        self.src_crs = None
        self.work_crs = None
        self.to_src = None
        self.notes = []         # warnings as {"en": ..., "ar": ...}
        self.skipped = 0
        self.bad_weight = 0


class _StatsTool(BiruniAlgorithm):
    GROUP = GROUP
    GROUP_ID = GROUP_ID
    @staticmethod
    def _area_choices():
        return [tr("Convex hull of the points"), tr("A polygon layer (below)"), tr("Rectangle around the points")]

    # ---- parameters --------------------------------------------------------------------------------
    def _add_points(self, label=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            "INPUT", label or tr("Points"), [Qgis.ProcessingSourceType.VectorPoint]))

    def _add_area(self):
        self.addParameter(QgsProcessingParameterEnum(
            "AREA", tr("Study area"), self._area_choices(), defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSource(
            "AREA_LAYER", tr("Study area polygons (when the study area is a polygon layer)"),
            [Qgis.ProcessingSourceType.VectorPolygon], optional=True))

    def _add_common(self):
        p = QgsProcessingParameterNumber("SEED", tr("Random seed (the same seed gives the same result)"),
                                         Qgis.ProcessingNumberParameterType.Integer, 12345, minValue=0)
        p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterFileDestination(
            "REPORT", tr("Report"), tr("Web page (*.html)"), optional=True, createByDefault=True))
        self.addOutput(QgsProcessingOutputString("ANSWER", tr("Answer")))

    # ---- reading ----------------------------------------------------------------------------------
    def _read_points(self, parameters, context, feedback, weight=None, case=None, keep=False, min_points=3):
        src = self.parameterAsSource(parameters, "INPUT", context)
        if src is None:
            raise QgsProcessingException(tr("Choose a point layer."))
        d = PointData()
        d.src_crs = src.sourceCrs()
        d.work_crs, changed = _metric_crs(d.src_crs, src.sourceExtent(), context)
        to_work = QgsCoordinateTransform(d.src_crs, d.work_crs, context.transformContext())
        d.to_src = QgsCoordinateTransform(d.work_crs, d.src_crs, context.transformContext())
        if changed:
            d.notes.append({"en": f"The layer is not in metres ({d.src_crs.authid() or d.src_crs.description()}): "
                                  f"distances and areas were measured in {d.work_crs.description()} "
                                  f"({d.work_crs.authid()}).",
                            "ar": f"الطبقة ليست بالأمتار ({d.src_crs.authid() or d.src_crs.description()})، "
                                  f"فقيست المسافات والمساحات في {d.work_crs.description()} ({d.work_crs.authid()})."})
        xs, ws, cs = [], [], []
        total = max(src.featureCount(), 1)
        for k, f in enumerate(src.getFeatures()):
            if feedback.isCanceled():
                break
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                d.skipped += 1
                continue
            wv = 1.0
            if weight:
                try:
                    wv = float(f[weight])
                except (TypeError, ValueError):
                    wv = float("nan")
                if not math.isfinite(wv) or wv < 0:
                    d.bad_weight += 1
                    continue
            cv = None
            if case:
                cv = f[case]
                cv = None if cv is None or (hasattr(cv, "isNull") and cv.isNull()) else cv
            g = QgsGeometry(g)
            try:
                g.transform(to_work)
            except Exception:
                d.skipped += 1
                continue
            parts = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
            for i, p in enumerate(parts):
                xs.append((p.x(), p.y()))
                ws.append(wv)
                cs.append(cv)
                if keep:
                    d.features.append((f, i))
            if k % 2000 == 0:
                feedback.setProgress(10.0 * k / total)
        d.xy = np.array(xs, dtype=float).reshape(-1, 2)
        d.w = np.array(ws, dtype=float)
        d.cases = cs
        if d.skipped:
            d.notes.append({"en": f"{d.skipped} features without a usable geometry were left out.",
                            "ar": f"استُبعدت {d.skipped} معالم بلا شكل هندسي صالح."})
        if d.bad_weight:
            d.notes.append({"en": f"{d.bad_weight} points with an empty, negative or non-numeric weight were left out.",
                            "ar": f"استُبعدت {d.bad_weight} نقاط وزنها فارغ أو سالب أو غير رقمي."})
        if len(d.xy) < min_points:
            raise QgsProcessingException(
                tr("Too few points: {n} found, at least {m} are needed for this test.").format(n=len(d.xy), m=min_points))
        dup = pts_mod.duplicates(d.xy)
        if dup:
            d.notes.append({"en": f"{dup} points repeat a location already taken by another point (duplicates). "
                                  "If they are the same event entered twice, remove them; if they are separate "
                                  "events at one address, they count as very tight clustering.",
                            "ar": f"{dup} نقطة تكرر موقع نقطة أخرى (نقاط مكررة). إن كانت الحدث نفسه أُدخل مرتين "
                                  "فاحذفها، وإن كانت أحداثاً مختلفة في عنوان واحد فهي تُحسب تجمّعاً شديداً."})
        return d

    def _region(self, parameters, context, data, feedback):
        """The study area as a Region (metres), its geometry, its label, and the points inside it."""
        choice = self.parameterAsEnum(parameters, "AREA", context)
        lang = text_lang()
        xy = data.xy
        if choice == 1:
            src = self.parameterAsSource(parameters, "AREA_LAYER", context)
            if src is None:
                raise QgsProcessingException(tr("Choose the polygon layer of the study area."))
            xf = QgsCoordinateTransform(src.sourceCrs(), data.work_crs, context.transformContext())
            geoms = []
            for f in src.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                g = QgsGeometry(g)
                g.transform(xf)
                geoms.append(g)
            if not geoms:
                raise QgsProcessingException(tr("The study area layer has no polygons."))
            geom = polygonal(QgsGeometry.unaryUnion(geoms))
            label = _s(lang, "the polygons of {name}", "مضلعات {name}", name=src.sourceName() if hasattr(src, "sourceName") else "")
        elif choice == 2:
            x0, y0 = xy.min(axis=0)
            x1, y1 = xy.max(axis=0)
            geom = QgsGeometry.fromWkt(f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))")
            label = _s(lang, "the rectangle around the points", "المستطيل المحيط بالنقاط")
        else:
            geom = QgsGeometry.fromMultiPointXY([QgsPointXY(x, y) for x, y in xy]).convexHull()
            label = _s(lang, "the convex hull of the points", "الغلاف المحدّب للنقاط")
        if geom is None or geom.isEmpty() or geom.area() <= 0:
            raise QgsProcessingException(tr("The study area has no area (are all the points on one line?)."))
        geom = polygonal(geom)
        if geom.constGet().nCoordinates() > 4000:
            bb = geom.boundingBox()
            geom = polygonal(geom.simplify(max(bb.width(), bb.height()) / 4000.0)) or geom
        polys = [[np.array([(p.x(), p.y()) for p in ring]) for ring in poly] for poly in geom.asMultiPolygon()]
        region = pts_mod.Region(polys)
        inside = np.ones(len(xy), dtype=bool)
        if choice == 1:
            inside = region.contains(xy)
            out = int((~inside).sum())
            if out:
                data.notes.append({"en": f"{out} points lie outside the study area and were left out.",
                                   "ar": f"{out} نقطة تقع خارج منطقة الدراسة فاستُبعدت."})
        return region, geom, label, inside

    def _report(self, parameters, context, title, answer, sections, data_notes, limits):
        lang = text_lang()
        path = self.parameterAsFileOutput(parameters, "REPORT", context)
        if not path:
            return None
        secs = list(sections)
        if data_notes:
            secs.insert(0, {"heading": _s(lang, "Data checks", "فحص البيانات"),
                            "warnings": [say(n, lang) for n in data_notes]})
        secs.append({"heading": _s(lang, "What this does not mean", "ما لا تعنيه هذه النتيجة"), "warnings": limits})
        write_report(path, title, secs, lang, answer)
        return path

    @staticmethod
    def _push(feedback, notes):
        lang = text_lang()
        for n in notes:
            feedback.pushWarning(say(n, lang))


def _seed(alg, parameters, context):
    return rng(alg.parameterAsInt(parameters, "SEED", context))


def _scatter_region(ax, region, xy, colour=GREY, size=3):
    for paths in region.polygons:
        for k, pth in enumerate(paths):
            v = pth.vertices
            ax.plot(v[:, 0], v[:, 1], color="#5b6670", lw=0.8, ls="-" if k == 0 else ":")
    ax.scatter(xy[:, 0], xy[:, 1], s=size, color=colour, lw=0)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])


def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(alpha=0.25, lw=0.5)


# ======================================================================================================
class NearestNeighbour(_StatsTool):
    """Clark-Evans ratio with a Monte Carlo check in the actual study area."""

    ID = "ssnearest"
    NAME = "Average nearest neighbour (clustered, random or dispersed?)"
    HELP = ("Are the points clustered, randomly scattered or evenly spread? Compares the average distance from "
            "each point to its nearest neighbour with the distance expected if the same number of points were "
            "scattered at random over the study area (Clark-Evans ratio R: below 1 clustered, near 1 random, "
            "above 1 dispersed; z and p). Because the answer depends heavily on the study area, the area is "
            "your choice (convex hull, a polygon layer or the rectangle around the points), Donnelly's edge "
            "correction is available, and random patterns are simulated inside the same area as a check that "
            "does not rely on formulas. Layers in degrees are measured in their UTM zone.")
    @staticmethod
    def _corrections():
        return [tr("None (Clark-Evans)"), tr("Donnelly perimeter correction")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_points()
        self._add_area()
        self.addParameter(QgsProcessingParameterEnum(
            "CORRECTION", tr("Edge correction"), self._corrections(), defaultValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            "SIMULATIONS", tr("Random patterns simulated in the study area (0 = none)"),
            Qgis.ProcessingNumberParameterType.Integer, 199, minValue=0, maxValue=9999))
        self._add_common()
        for k, label in (("R", tr("Nearest neighbour ratio R")), ("Z", tr("z-score")), ("P", tr("p-value")),
                         ("P_SIMULATED", tr("p-value from the simulations"))):
            self.addOutput(QgsProcessingOutputNumber(k, label))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        lang = text_lang()
        data = self._read_points(parameters, context, feedback, min_points=10)
        region, geom, area_label, inside = self._region(parameters, context, data, feedback)
        xy = data.xy[inside]
        if len(xy) < 10:
            raise QgsProcessingException(tr("Too few points inside the study area: {n} (at least 10).").format(n=len(xy)))
        if len(xy) < 30:
            data.notes.append({"en": f"Only {len(xy)} points: the test has little power, a 'random' answer may only "
                                     "mean there are too few points to tell.",
                               "ar": f"{len(xy)} نقطة فقط: قوة الاختبار ضعيفة، وقد تعني نتيجة «عشوائي» أن النقاط أقل "
                                     "من أن تكشف نمطاً."})
        corr = "donnelly" if self.parameterAsEnum(parameters, "CORRECTION", context) == 1 else "none"
        ce = pts_mod.clark_evans(xy, region.area, region.perimeter, corr)
        sims = self.parameterAsInt(parameters, "SIMULATIONS", context)
        mc = None
        if sims > 0:
            feedback.setProgressText(tr("Simulating random patterns in the study area..."))
            mc = pts_mod.nn_monte_carlo(ce["observed"], len(xy), region, sims, _seed(self, parameters, context))
        feedback.setProgress(90)
        self._push(feedback, data.notes)

        p_use = mc["p"] if mc else ce["p"]
        exp_use = mc["mean"] if mc else ce["expected"]
        if p_use < 0.05 and ce["observed"] < exp_use:
            verdict = _s(lang, "clustered", "متجمّعة")
        elif p_use < 0.05:
            verdict = _s(lang, "dispersed (more evenly spread than random)", "متباعدة (أكثر انتظاماً من العشوائية)")
        else:
            verdict = _s(lang, "not distinguishable from random", "لا تختلف عن التوزيع العشوائي")
        obs, exp = pts_mod.fmt_distance(ce["observed"], lang), pts_mod.fmt_distance(exp_use, lang)
        answer = _s(lang, "The points are {v}: the average distance to the nearest neighbour is {o}, against {e} "
                          "expected if the same {n} points were scattered at random in {area} (R = {R:.2f}, "
                          "z = {z:.2f}, {p}).",
                    "النقاط {v}: متوسط المسافة إلى أقرب جار {o}، مقابل {e} متوقعة لو تبعثرت النقاط نفسها ({n}) "
                    "عشوائياً داخل {area} (R = {R:.2f}، z = {z:.2f}، {p}).",
                    v=verdict, o=obs, e=exp, n=_num(len(xy)), area=area_label, R=ce["R"], z=ce["z"], p=_p(ce["p"]))
        if mc:
            agree = (mc["p"] < 0.05) == (ce["p"] < 0.05)
            answer += " " + (_s(lang, "{k} random patterns simulated in the same area agree ({p}, R from the "
                                      "simulations = {R:.2f}).",
                                "{k} نمطاً عشوائياً محاكى في المنطقة نفسها يؤكد ذلك ({p}، R من المحاكاة = {R:.2f}).",
                                k=sims, p=_p(mc["p"]), R=mc["R"]) if agree else
                             _s(lang, "Caution: {k} random patterns simulated in the same area give {p} (R = {R:.2f}), "
                                      "so the formula's verdict is driven by edge effects; the answer above follows "
                                      "the simulations.",
                                "تنبيه: {k} نمطاً عشوائياً محاكى في المنطقة نفسها يعطي {p} (R = {R:.2f})، أي أن حكم "
                                "المعادلة تأثر بأثر الحواف؛ والإجابة أعلاه تتبع المحاكاة.",
                                k=sims, p=_p(mc["p"]), R=mc["R"]))

        rows = [[_s(lang, "Points", "عدد النقاط"), len(xy)],
                [_s(lang, "Study area", "منطقة الدراسة"), area_label],
                [_s(lang, "Area (km²)", "المساحة (كم²)"), region.area / 1e6],
                [_s(lang, "Observed mean nearest-neighbour distance (m)", "متوسط المسافة المرصودة لأقرب جار (م)"), ce["observed"]],
                [_s(lang, "Expected under randomness (m)", "المتوقع في التوزيع العشوائي (م)"), ce["expected"]],
                [_s(lang, "Edge correction", "تصحيح الحواف"), self._corrections()[1 if corr == "donnelly" else 0]],
                ["R", ce["R"]], ["z", ce["z"]], ["p", ce["p"]]]
        if mc:
            rows += [[_s(lang, "Simulations", "عدد المحاكاة"), sims],
                     [_s(lang, "Simulated mean distance (m)", "متوسط المسافة في المحاكاة (م)"), mc["mean"]],
                     [_s(lang, "R from the simulations", "R من المحاكاة"), mc["R"]],
                     [_s(lang, "p from the simulations", "p من المحاكاة"), mc["p"]]]
        imgs = []
        fig = figure(9.6, 3.6)
        ax = fig.add_subplot(1, 2, 1)
        ax.hist(ce["nn"], bins=40, color=ACCENT, alpha=0.85)
        ax.axvline(ce["observed"], color="#1d2329", lw=1.5)
        ax.axvline(ce["expected"], color="#C98A12", lw=1.5, ls="--")
        ax.set_title(_chart(lang, "Nearest-neighbour distances", "المسافات إلى أقرب جار"), fontsize=10)
        ax.set_xlabel(_chart(lang, "metres", "متر"))
        _style(ax)
        ax = fig.add_subplot(1, 2, 2)
        if mc:
            ax.hist(mc["sims"], bins=30, color=GREY)
            ax.axvline(ce["observed"], color=ACCENT, lw=2)
            ax.set_title(_chart(lang, "Simulated random patterns vs observed", "الأنماط العشوائية المحاكاة مقابل المرصود"),
                         fontsize=10)
            ax.set_xlabel(_chart(lang, "mean nearest-neighbour distance (m)", "متوسط المسافة لأقرب جار (م)"))
            _style(ax)
        else:
            _scatter_region(ax, region, xy)
        imgs.append(png(fig))
        limits = [
            _s(lang, "R depends on the study area: the same points in a bigger area look more clustered, in a "
                     "tighter one more dispersed. Choose the area where the points could really have been.",
               "تعتمد R على منطقة الدراسة: النقاط نفسها في منطقة أكبر تبدو أكثر تجمّعاً، وفي منطقة أضيق أكثر تباعداً. "
               "اختر المنطقة التي كان يمكن أن تقع فيها النقاط فعلاً."),
            _s(lang, "One number for all distances: points can be clustered at 200 m and dispersed at 2 km. "
                     "Ripley's K shows the answer distance by distance.",
               "رقم واحد لكل المسافات: قد تتجمع النقاط على مسافة 200 م وتتباعد على 2 كم؛ دالة ريبلي K تبيّن ذلك مسافةً "
               "بمسافة."),
            _s(lang, "'Clustered' does not say where the clusters are, nor why (the population or the roads may be "
                     "clustered too). Use kernel density or hot spot analysis to locate them.",
               "«متجمّعة» لا تقول أين التجمعات ولا لماذا (قد يكون السكان أو الطرق متجمّعين أيضاً). استخدم كثافة النواة "
               "أو تحليل البؤر الساخنة لتحديد أماكنها."),
            _s(lang, "Not finding a pattern is not proof of randomness.", "عدم ظهور نمط ليس دليلاً على العشوائية.")]
        sections = [{"heading": _s(lang, "The numbers", "الأرقام"), "table": {
            "columns": [_s(lang, "Measure", "المقياس"), _s(lang, "Value", "القيمة")], "rows": rows}, "images": imgs}]
        report = self._report(parameters, context, tr(self.NAME), answer, sections, data.notes, limits)
        return {"ANSWER": answer, "R": ce["R"], "Z": ce["z"], "P": ce["p"],
                "P_SIMULATED": mc["p"] if mc else float("nan"), "REPORT": report}


# ======================================================================================================
class RipleyK(_StatsTool):
    """Ripley's K / L(d) - d with CSR envelopes and the distances where the points are clustered."""

    ID = "ssripley"
    NAME = "Ripley's K - at which distances are points clustered?"
    HELP = ("Tells at which distances the points are clustered, random or dispersed: Ripley's K function, shown "
            "as L(d) - d (above zero = more neighbours within d than random), compared with an envelope of "
            "random patterns simulated inside the actual study area (99 by default). Where the curve leaves the "
            "envelope the pattern departs from randomness at that distance: 'clustered between 200 m and "
            "1.4 km, random beyond'. A global test over all distances guards against reading chance crossings. "
            "Edge correction: Ripley's isotropic (default) or border. Optionally the cross-K of two categories "
            "(are B points nearer to A points than chance?), tested by shuffling the labels. Large layers are "
            "randomly thinned for speed, which does not change the expected K.")
    @staticmethod
    def _corrections():
        return [tr("Ripley's isotropic"), tr("Border (reduced sample)"), tr("None")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_points()
        self._add_area()
        self.addParameter(QgsProcessingParameterEnum(
            "CORRECTION", tr("Edge correction"), self._corrections(), defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "MAX_DISTANCE", tr("Largest distance (m, 0 = a quarter of the study area's shorter side)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            "STEPS", tr("Number of distances"), Qgis.ProcessingNumberParameterType.Integer, 30, minValue=5,
            maxValue=200))
        self.addParameter(QgsProcessingParameterNumber(
            "SIMULATIONS", tr("Random patterns for the envelope"), Qgis.ProcessingNumberParameterType.Integer, 99,
            minValue=19, maxValue=999))
        self.addParameter(QgsProcessingParameterField(
            "CATEGORY_FIELD", tr("Category field (for the cross-K of two categories)"), parentLayerParameterName="INPUT",
            optional=True))
        self.addParameter(QgsProcessingParameterString("CATEGORY_A", tr("Category A (from)"), optional=True))
        self.addParameter(QgsProcessingParameterString("CATEGORY_B", tr("Category B (to)"), optional=True))
        p = QgsProcessingParameterNumber("MAX_POINTS", tr("Thin larger layers to this many points"),
                                         Qgis.ProcessingNumberParameterType.Integer, 1500, minValue=200)
        p.setFlags(p.flags() | Qgis.ProcessingParameterFlag.Advanced)
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterFeatureSink(
            "TABLE", tr("Curve table"), Qgis.ProcessingSourceType.Vector, optional=True, createByDefault=False))
        self._add_common()
        self.addOutput(QgsProcessingOutputNumber("P", tr("Global p-value")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        lang = text_lang()
        cat = self.parameterAsString(parameters, "CATEGORY_FIELD", context)
        a_val = self.parameterAsString(parameters, "CATEGORY_A", context).strip()
        b_val = self.parameterAsString(parameters, "CATEGORY_B", context).strip()
        cross = bool(cat and a_val and b_val)
        if cat and not cross:
            raise QgsProcessingException(tr("For the cross-K give both category A and category B."))
        data = self._read_points(parameters, context, feedback, case=cat or None, min_points=10)
        region, geom, area_label, inside = self._region(parameters, context, data, feedback)
        xy = data.xy[inside]
        corr = ("isotropic", "border", "none")[self.parameterAsEnum(parameters, "CORRECTION", context)]
        dists = pts_mod.default_distances(region, self.parameterAsInt(parameters, "STEPS", context),
                                          self.parameterAsDouble(parameters, "MAX_DISTANCE", context))
        sims = self.parameterAsInt(parameters, "SIMULATIONS", context)
        gen = _seed(self, parameters, context)
        maxp = self.parameterAsInt(parameters, "MAX_POINTS", context)
        feedback.setProgressText(tr("Simulating random patterns in the study area..."))
        if cross:
            labels = np.array([str(c) if c is not None else "" for c in data.cases], dtype=object)[inside]
            sel = (labels == a_val) | (labels == b_val)
            na, nb = int((labels == a_val).sum()), int((labels == b_val).sum())
            if na < 5 or nb < 5:
                raise QgsProcessingException(tr("Too few points in the categories: A has {a}, B has {b} (5 each at "
                                                "least).").format(a=na, b=nb))
            res = pts_mod.cross_k(xy[sel], labels[sel] == a_val, region, dists, corr, sims, gen, max_points=2 * maxp)
        else:
            if len(xy) < 10:
                raise QgsProcessingException(tr("Too few points inside the study area: {n} (at least 10).").format(n=len(xy)))
            res = pts_mod.ripley(xy, region, dists, corr, sims, gen, max_points=maxp)
        feedback.setProgress(90)
        if res["thinned"]:
            data.notes.append({"en": f"{len(xy):,} points were randomly thinned to {res['n']:,} for the simulations; "
                                     "random thinning leaves Ripley's K unchanged in expectation.",
                               "ar": f"خُففت النقاط ({len(xy):,}) عشوائياً إلى {res['n']:,} لأجل المحاكاة؛ التخفيف "
                                     "العشوائي لا يغيّر دالة ريبلي K في المتوسط."})
        self._push(feedback, data.notes)

        answer = self._sentence(res, lang, cross, a_val, b_val, dists[-1])
        if cross:
            answer += " " + _s(lang, "(global test over all distances: {p}, {k} random relabellings.)",
                               "(الاختبار الشامل لكل المسافات: {p}، {k} إعادة توزيع عشوائي للفئات.)", p=_p(res["p"]), k=sims)
        else:
            answer += " " + _s(lang, "(global test over all distances: {p}, {k} random patterns in {area}.)",
                               "(الاختبار الشامل لكل المسافات: {p}، {k} نمطاً عشوائياً داخل {area}.)",
                               p=_p(res["p"]), k=sims, area=area_label)
        if res["p"] >= 0.05 and any(s != 0 for s, _a, _b in res["runs"]):
            answer += " " + _s(lang, "The global test does not confirm the departures: they may be chance.",
                               "الاختبار الشامل لا يؤكد هذه الانحرافات: قد تكون مصادفة.")

        table_sink, table_id = self.parameterAsSink(parameters, "TABLE", context, self._table_fields(),
                                                    Qgis.WkbType.NoGeometry, QgsCoordinateReferenceSystem())
        if table_sink is not None:
            for k in range(len(dists)):
                f = QgsFeature(self._table_fields())
                f.setAttributes([float(dists[k]), float(res["L"][k]), float(res["low"][k]), float(res["high"][k]),
                                 int(res["state"][k])])
                table_sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        km = dists[-1] >= 3000
        sc = 1000.0 if km else 1.0
        unit = _chart(lang, "km", "كم") if km else _chart(lang, "m", "م")
        fig = figure(7.0, 4.2)
        ax = fig.add_subplot(1, 1, 1)
        ax.fill_between(dists / sc, res["low"] / sc, res["high"] / sc, color=GREY, alpha=0.35, lw=0,
                        label=_chart(lang, "envelope of random patterns", "نطاق الأنماط العشوائية"))
        ax.plot(dists / sc, res["mean"] / sc, color=GREY, lw=0.8, ls="--")
        ax.plot(dists / sc, res["L"] / sc, color=ACCENT, lw=2, label=_chart(lang, "observed", "المرصود"))
        ax.axhline(0, color="#5b6670", lw=0.6)
        for s, a, b in res["runs"]:
            if s:
                ax.axvspan(a / sc, b / sc, color="#C98A12" if s > 0 else "#3A6EA5", alpha=0.08, lw=0)
        ax.set_xlabel(_chart(lang, "distance d", "المسافة d") + f" ({unit})")
        ax.set_ylabel("L(d) - d" + f" ({unit})")
        ax.set_title(_chart(lang, "Above the envelope: clustered; below: dispersed",
                        "فوق النطاق: تجمّع؛ تحته: تباعد"), fontsize=10)
        ax.legend(frameon=False, fontsize=8)
        _style(ax)
        imgs = [png(fig)]
        fig = figure(4.2, 4.2)
        ax = fig.add_subplot(1, 1, 1)
        _scatter_region(ax, region, xy)
        imgs.append(png(fig))
        state_word = {1: _s(lang, "clustered", "تجمّع") if not cross else _s(lang, "attraction", "تجاذب"),
                      -1: _s(lang, "dispersed", "تباعد") if not cross else _s(lang, "repulsion", "تنافر"),
                      0: _s(lang, "random", "عشوائي") if not cross else _s(lang, "independent", "مستقل")}
        step = max(1, len(dists) // 15)
        rows = [[float(dists[k]), float(res["L"][k]), float(res["low"][k]), float(res["high"][k]),
                 state_word[int(res["state"][k])]] for k in range(0, len(dists), step)]
        limits = [
            _s(lang, "The envelope is pointwise: with many distances some crossings happen by chance. Trust "
                     "crossings when the global test is significant and they span several distances.",
               "النطاق نقطي: مع كثرة المسافات قد يخرج المنحنى منه مصادفة. ثق بالخروج حين يكون الاختبار الشامل دالاً "
               "ويمتد على عدة مسافات."),
            _s(lang, "Clustering against complete spatial randomness may just reflect where people live or the "
                     "street network: random points assume every place of the study area is equally possible.",
               "التجمّع مقارنة بالعشوائية الكاملة قد يعكس فقط أماكن السكن أو شبكة الطرق: النقاط العشوائية تفترض أن كل مكان "
               "في منطقة الدراسة محتمل بالقدر نفسه."),
            _s(lang, "Distances beyond a quarter of the study area are dominated by edge corrections; the curve "
                     "there is unreliable.",
               "المسافات الأكبر من ربع منطقة الدراسة يغلب عليها تصحيح الحواف؛ والمنحنى عندها غير موثوق."),
            _s(lang, "The scale where L(d) - d peaks is not the size of the clusters, only a hint of it.",
               "المسافة التي يبلغ عندها L(d) - d ذروته ليست حجم التجمعات، بل مؤشر عليه فقط.")]
        sections = [
            {"heading": _s(lang, "The curve", "المنحنى"), "images": imgs,
             "paragraphs": [_s(lang, "L(d) - d counts the neighbours within each distance d, scaled so that random "
                                     "points give 0. Grey: the range of {k} random patterns in the same study area "
                                     "({c} edge correction).",
                               "يعدّ L(d) - d الجيران ضمن كل مسافة d، مضبوطاً بحيث تعطي النقاط العشوائية صفراً. "
                               "الرمادي: مدى {k} نمطاً عشوائياً في منطقة الدراسة نفسها (تصحيح الحواف: {c}).",
                               k=sims, c=self._corrections()[("isotropic", "border", "none").index(corr)])]},
            {"heading": _s(lang, "Values", "القيم"), "table": {
                "columns": [_s(lang, "Distance (m)", "المسافة (م)"), "L(d) - d", _s(lang, "Envelope low", "حد النطاق الأدنى"),
                            _s(lang, "Envelope high", "حد النطاق الأعلى"), _s(lang, "Reading", "القراءة")], "rows": rows}}]
        report = self._report(parameters, context, tr(self.NAME), answer, sections, data.notes, limits)
        return {"ANSWER": answer, "P": res["p"], "REPORT": report, "TABLE": table_id}

    @staticmethod
    def _table_fields():
        fields = QgsFields()
        for n, t in (("distance", QMetaType.Type.Double), ("L_minus_d", QMetaType.Type.Double),
                     ("env_low", QMetaType.Type.Double), ("env_high", QMetaType.Type.Double),
                     ("state", QMetaType.Type.Int)):
            fields.append(QgsField(n, t))
        return fields

    @staticmethod
    def _sentence(res, lang, cross, a_val, b_val, dmax):
        f = lambda d: pts_mod.fmt_distance(d, lang)  # noqa: E731
        runs = res["runs"]
        if all(s == 0 for s, _a, _b in runs):
            if cross:
                return _s(lang, "'{b}' points are placed independently of '{a}' points at every distance up to {d}.",
                          "نقاط «{b}» مستقلة عن نقاط «{a}» على كل المسافات حتى {d}.", a=a_val, b=b_val, d=f(dmax))
            return _s(lang, "The points do not depart from randomness at any distance up to {d}.",
                      "لا تختلف النقاط عن العشوائية على أي مسافة حتى {d}.", d=f(dmax))
        words = {1: (_s(lang, "clustered", "متجمّعة"), _s(lang, "nearer than chance (attraction)", "أقرب من المصادفة (تجاذب)")),
                 -1: (_s(lang, "dispersed", "متباعدة"), _s(lang, "farther than chance (repulsion)", "أبعد من المصادفة (تنافر)")),
                 0: (_s(lang, "random", "عشوائية"), _s(lang, "independent", "مستقلة"))}
        parts = []
        for k, (s, a, b) in enumerate(runs):
            w = words[s][1 if cross else 0]
            if s == 0 and k == len(runs) - 1 and k > 0:
                parts.append(_s(lang, "{w} beyond", "{w} بعد ذلك", w=w))
            elif s == 0 and k == 0:
                parts.append(_s(lang, "{w} below {b}", "{w} دون {b}", w=w, b=f(runs[1][1])))
            elif a == b:
                parts.append(_s(lang, "{w} at {a}", "{w} عند {a}", w=w, a=f(a)))
            else:
                parts.append(_s(lang, "{w} between {a} and {b}", "{w} بين {a} و{b}", w=w, a=f(a), b=f(b)))
        body = "، ".join(parts) if lang == "ar" else ", ".join(parts)
        if cross:
            return _s(lang, "'{b}' points relative to '{a}' points: {t}.", "نقاط «{b}» بالنسبة إلى نقاط «{a}»: {t}.",
                      a=a_val, b=b_val, t=body)
        lead = _s(lang, "The points are ", "النقاط ")
        return lead + body + "."


# ======================================================================================================
class KernelDensity(_StatsTool):
    """Kernel density raster with an explained bandwidth and the share of points in the densest 10%."""

    ID = "sskde"
    NAME = "Kernel density (with the bandwidth explained)"
    HELP = ("Turns points into a smooth density surface (points per km², or the weight per km² with a weight "
            "field), as a raster in a metric coordinate system. The bandwidth - how far each point spreads - "
            "decides the picture, so it is chosen and explained: Silverman's rule for two dimensions (from the "
            "spread and the number of points), likelihood cross-validation (the distance that best predicts each "
            "point from the others), or your own distance. The report shows the map at half and double the "
            "bandwidth beside the chosen one, and says what share of the points falls in the densest 10% of the "
            "study area. The surface integrates to the number of points (or the total weight).")
    @staticmethod
    def _methods():
        return [tr("Silverman's rule (2-D)"), tr("Likelihood cross-validation"), tr("Fixed distance (below)")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_points()
        self.addParameter(QgsProcessingParameterField(
            "WEIGHT", tr("Weight field (optional, e.g. number of people)"), parentLayerParameterName="INPUT",
            type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            "METHOD", tr("Bandwidth"), self._methods(), defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "BANDWIDTH", tr("Fixed bandwidth (m)"), Qgis.ProcessingNumberParameterType.Double, 500.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            "CELL_SIZE", tr("Cell size (m, 0 = automatic)"), Qgis.ProcessingNumberParameterType.Double, 0.0,
            minValue=0.0))
        self._add_area()
        self.addParameter(QgsProcessingParameterRasterDestination("OUTPUT", tr("Density")))
        self._add_common()
        self.addOutput(QgsProcessingOutputNumber("BANDWIDTH_USED", tr("Bandwidth used (m)")))
        self.addOutput(QgsProcessingOutputNumber("TOP10_SHARE", tr("Share of points in the densest 10% of the area")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        lang = text_lang()
        wf = self.parameterAsString(parameters, "WEIGHT", context)
        data = self._read_points(parameters, context, feedback, weight=wf or None, min_points=5)
        region, geom, area_label, inside = self._region(parameters, context, data, feedback)
        xy, w = data.xy[inside], data.w[inside]
        if w.sum() <= 0:
            raise QgsProcessingException(tr("All the weights are zero."))
        method = self.parameterAsEnum(parameters, "METHOD", context)
        h_s, info = dist_mod.silverman_bandwidth(xy, w)
        lcv = None
        if method == 2:
            h = self.parameterAsDouble(parameters, "BANDWIDTH", context)
            if h <= 0:
                raise QgsProcessingException(tr("Give a bandwidth above zero."))
            why = _s(lang, "your own distance of {h}; Silverman's rule would give {s}.",
                     "المسافة التي اخترتها {h}؛ وكانت قاعدة سيلفرمان ستعطي {s}.",
                     h=pts_mod.fmt_distance(h, lang), s=pts_mod.fmt_distance(h_s, lang))
        elif method == 1:
            feedback.setProgressText(tr("Cross-validating the bandwidth..."))
            h, grid, scores = dist_mod.lcv_bandwidth(xy, w, h_s, _seed(self, parameters, context))
            lcv = (grid, scores)
            why = _s(lang, "likelihood cross-validation: {h} predicts each point best from all the others "
                           "(Silverman's rule would give {s}).",
                     "التحقق المتقاطع بالاحتمال: المسافة {h} هي الأفضل في توقع كل نقطة من باقي النقاط "
                     "(وكانت قاعدة سيلفرمان ستعطي {s}).", h=pts_mod.fmt_distance(h, lang), s=pts_mod.fmt_distance(h_s, lang))
            if h in (grid[0], grid[-1]):
                data.notes.append({"en": "Cross-validation chose the end of the range tried: the points may hold "
                                         "repeated or nearly repeated locations; check the bandwidth by eye.",
                                   "ar": "اختار التحقق المتقاطع طرف المدى المجرّب: قد تحوي النقاط مواقع مكررة أو شبه "
                                         "مكررة؛ راجع عرض النطاق بالنظر."})
        else:
            h = h_s
            why = _s(lang, "Silverman's rule: spread of the points {sg} × (effective number of points {n})^(-1/6) = {h}.",
                     "قاعدة سيلفرمان: انتشار النقاط {sg} × (العدد الفعلي للنقاط {n})^(-1/6) = {h}.",
                     sg=pts_mod.fmt_distance(info["sigma"], lang), n=_num(info["n"]), h=pts_mod.fmt_distance(h, lang))
        x0, y0, x1, y1 = region.bbox
        pad = 4.0 * h
        ext = (min(x0, xy[:, 0].min()) - pad, min(y0, xy[:, 1].min()) - pad,
               max(x1, xy[:, 0].max()) + pad, max(y1, xy[:, 1].max()) + pad)
        cell = self.parameterAsDouble(parameters, "CELL_SIZE", context) or dist_mod.grid_for(ext, h)
        if (ext[2] - ext[0]) * (ext[3] - ext[1]) / (cell * cell) > 25e6:
            raise QgsProcessingException(tr("The cell size is too small for this area (over 25 million cells)."))
        feedback.setProgressText(tr("Computing the density surface..."))
        dens, gx0, gy1, cols, rows = dist_mod.kde_grid(xy, w, h, ext, cell)
        total = float(dens.sum() * cell * cell)
        # share of the points in the densest 10% of the study area's cells
        cx = gx0 + (np.arange(cols) + 0.5) * cell
        cy = gy1 - (np.arange(rows) + 0.5) * cell
        step = max(1, int(math.sqrt(rows * cols / 400000)))
        sub = np.zeros((rows, cols), dtype=bool)
        gxx, gyy = np.meshgrid(cx[::step], cy[::step])
        sub[::step, ::step] = region.contains(np.column_stack([gxx.ravel(), gyy.ravel()])).reshape(gxx.shape)
        at_pts = dist_mod.sample_grid(dens, gx0, gy1, cell, xy)
        share, thr = dist_mod.top_share(dens, sub, at_pts, w, 0.10)
        out = self.parameterAsOutputLayer(parameters, "OUTPUT", context)
        self._write_raster(out, dens * 1e6, gx0, gy1, cell, data.work_crs)
        feedback.setProgress(80)
        self._push(feedback, data.notes)

        what = _s(lang, "the total weight", "مجموع الأوزان") if wf else _s(lang, "the points", "النقاط")
        if share == share:
            answer = _s(lang, "The densest 10% of the study area holds {s:.0%} of {what} (an even spread would put about "
                              "10% there). Bandwidth {h}, chosen by {why}",
                        "يضم أكثف 10% من منطقة الدراسة {s:.0%} من {what} (التوزيع المتساوي يضع هناك نحو 10%). عرض "
                        "النطاق {h}، والاختيار حسب {why}", s=share, what=what, h=pts_mod.fmt_distance(h, lang), why=why)
        else:
            answer = _s(lang, "Bandwidth {h}, chosen by {why}", "عرض النطاق {h}، والاختيار حسب {why}",
                        h=pts_mod.fmt_distance(h, lang), why=why)
        answer = answer.replace("%%", "%")

        imgs = [self._small_multiples(xy, w, h, region, lang)]
        if lcv:
            fig = figure(6.0, 3.4)
            ax = fig.add_subplot(1, 1, 1)
            ax.semilogx(lcv[0], lcv[1], color=ACCENT, lw=1.8)
            ax.axvline(h, color="#1d2329", lw=1)
            ax.axvline(h_s, color="#C98A12", lw=1, ls="--")
            ax.set_xlabel(_chart(lang, "bandwidth (m)", "عرض النطاق (م)"))
            ax.set_ylabel(_chart(lang, "cross-validated log-likelihood", "لوغاريتم الاحتمال بالتحقق المتقاطع"))
            ax.set_title(_chart(lang, "Chosen (black) and Silverman's rule (dashed)", "المختار (أسود) وقاعدة سيلفرمان (متقطع)"),
                         fontsize=10)
            _style(ax)
            imgs.append(png(fig))
        rows_t = [[_s(lang, "Points", "عدد النقاط"), len(xy)],
                  [_s(lang, "Total weight", "مجموع الأوزان"), float(w.sum())],
                  [_s(lang, "Bandwidth (m)", "عرض النطاق (م)"), h],
                  [_s(lang, "Method", "الطريقة"), self._methods()[method]],
                  [_s(lang, "Silverman's rule (m)", "قاعدة سيلفرمان (م)"), h_s],
                  [_s(lang, "Cell size (m)", "حجم الخلية (م)"), cell],
                  [_s(lang, "Raster size (columns × rows)", "حجم الشبكة (أعمدة × صفوف)"), f"{cols} × {rows}"],
                  [_s(lang, "Integral of the surface (should equal the total weight)", "تكامل السطح (يساوي مجموع الأوزان)"),
                   total],
                  [_s(lang, "Densest 10% starts at (per km²)", "يبدأ أكثف 10% عند (لكل كم²)"), thr * 1e6],
                  [_s(lang, "Share in the densest 10%", "النسبة في أكثف 10%"), f"{share:.1%}" if share == share else "-"],
                  [_s(lang, "Coordinate system of the raster", "نظام إحداثيات الشبكة"), data.work_crs.authid()]]
        limits = [
            _s(lang, "The bandwidth makes the map: half of it shows many small spots, double of it one blob. The "
                     "small multiples show how much your picture depends on it.",
               "عرض النطاق يصنع الخريطة: نصفه يُظهر بقعاً صغيرة كثيرة، وضعفه كتلة واحدة. الخرائط الصغيرة تبيّن مدى اعتماد "
               "الصورة عليه."),
            _s(lang, "Density is not risk: many accidents where traffic is heavy is expected. Divide by the "
                     "population or exposure (rate smoothing) before calling a place dangerous.",
               "الكثافة ليست خطورة: كثرة الحوادث حيث المرور كثيف أمر متوقع. اقسم على السكان أو التعرّض (تنعيم المعدلات) "
               "قبل أن تصف مكاناً بالخطر."),
            _s(lang, "Bright spots are not tested: use the hot spot analysis to know which are more than chance.",
               "البقع الساطعة غير مختبرة إحصائياً: استخدم تحليل البؤر الساخنة لتعرف أيها أكثر من المصادفة."),
            _s(lang, "Near the edge of the study area the density is underestimated (the kernel spills outside).",
               "قرب حافة منطقة الدراسة تُقدَّر الكثافة بأقل من حقيقتها (جزء من النواة يقع خارج المنطقة).")]
        sections = [{"heading": _s(lang, "The surface at three bandwidths", "السطح بثلاثة عروض للنطاق"), "images": imgs,
                     "paragraphs": [_s(lang, "Middle: the chosen bandwidth; left half of it, right double of it. "
                                             "Units: {u} per km².",
                                       "في الوسط: عرض النطاق المختار؛ يساراً نصفه، ويميناً ضعفه. الوحدة: {u} لكل كم².",
                                       u=wf or _s(lang, "points", "نقطة"))]},
                    {"heading": _s(lang, "The numbers", "الأرقام"), "table": {
                        "columns": [_s(lang, "Measure", "المقياس"), _s(lang, "Value", "القيمة")], "rows": rows_t}}]
        report = self._report(parameters, context, tr(self.NAME), answer, sections, data.notes, limits)
        return {"ANSWER": answer, "OUTPUT": out, "BANDWIDTH_USED": h, "TOP10_SHARE": share, "REPORT": report}

    @staticmethod
    def _write_raster(path, arr, x0, y1, cell, crs):
        from osgeo import gdal

        ext = path.lower().rsplit(".", 1)[-1]
        drv = {"tif": "GTiff", "tiff": "GTiff", "img": "HFA", "asc": "AAIGrid"}.get(ext, "GTiff")
        opts = ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"] if drv == "GTiff" else []
        if drv == "AAIGrid":
            mem = gdal.GetDriverByName("MEM").Create("", arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
            ds = mem
        else:
            ds = gdal.GetDriverByName(drv).Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32, opts)
        ds.SetGeoTransform((x0, cell, 0, y1, 0, -cell))
        ds.SetProjection(crs.toWkt())
        band = ds.GetRasterBand(1)
        band.WriteArray(arr.astype(np.float32))
        band.SetDescription("density per km2")
        if drv == "AAIGrid":
            gdal.GetDriverByName(drv).CreateCopy(path, ds)
        ds = None

    @staticmethod
    def _small_multiples(xy, w, h, region, lang):
        x0, y0, x1, y1 = region.bbox
        pad = 0.05 * max(x1 - x0, y1 - y0)
        ext = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        cell = max((ext[2] - ext[0]), (ext[3] - ext[1])) / 220.0
        fig = figure(10.5, 3.8)
        for k, (f, title) in enumerate(((0.5, _chart(lang, "half: {h}", "النصف: {h}", h=pts_mod.fmt_distance(h / 2, lang))),
                                        (1.0, _chart(lang, "chosen: {h}", "المختار: {h}", h=pts_mod.fmt_distance(h, lang))),
                                        (2.0, _chart(lang, "double: {h}", "الضعف: {h}", h=pts_mod.fmt_distance(2 * h, lang))))):
            d, gx0, gy1, cols, rows = dist_mod.kde_grid(xy, w, h * f, ext, cell)
            ax = fig.add_subplot(1, 3, k + 1)
            ax.imshow(d * 1e6, extent=(gx0, gx0 + cols * cell, gy1 - rows * cell, gy1), cmap="magma_r",
                      interpolation="bilinear")
            for paths in region.polygons:
                v = paths[0].vertices
                ax.plot(v[:, 0], v[:, 1], color="#5b6670", lw=0.6)
            if len(xy) <= 3000:
                ax.scatter(xy[:, 0], xy[:, 1], s=0.6, color="#1d2329", lw=0, alpha=0.5)
            ax.set_title(title, fontsize=10)
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_aspect("equal")
        return png(fig)


# ======================================================================================================
class CentreSpread(_StatsTool):
    """Mean and median centre, standard distance and deviational ellipse per group, and how they moved."""

    ID = "sscentre"
    NAME = "Centre and spread (mean centre, standard ellipse)"
    HELP = ("Where is the centre of the points, how far do they spread, and in which direction? Gives the mean "
            "centre (weighted if you choose a weight field), the median centre (the point with the least total "
            "distance to all points, less pulled by outliers), the standard distance circle and the standard "
            "deviational ellipse (1, 2 or 3 standard deviations; 1 holds about 63% of the points of a normal "
            "cloud, 2 about 98%) with its orientation in degrees from north. With a case field (category or year) "
            "everything is computed per group and the answer says how the centre moved between the first and "
            "last group, tested by shuffling the group labels.")
    @staticmethod
    def _levels():
        return [tr("1 standard deviation"), tr("2 standard deviations"), tr("3 standard deviations")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_points()
        self.addParameter(QgsProcessingParameterField(
            "WEIGHT", tr("Weight field (optional)"), parentLayerParameterName="INPUT",
            type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterField(
            "CASE_FIELD", tr("Group by (category or year, optional)"), parentLayerParameterName="INPUT", optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            "LEVEL", tr("Ellipse and circle size"), self._levels(), defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "PERMUTATIONS", tr("Label shuffles to test the movement"), Qgis.ProcessingNumberParameterType.Integer,
            499, minValue=0, maxValue=9999))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "CENTRES", tr("Centres"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "SHAPES", tr("Standard distance and ellipses"), Qgis.ProcessingSourceType.VectorPolygon))
        self._add_common()

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        lang = text_lang()
        wf = self.parameterAsString(parameters, "WEIGHT", context)
        case = self.parameterAsString(parameters, "CASE_FIELD", context)
        data = self._read_points(parameters, context, feedback, weight=wf or None, case=case or None, min_points=3)
        level = self.parameterAsEnum(parameters, "LEVEL", context) + 1
        groups = {}
        for k, c in enumerate(data.cases):
            groups.setdefault(c if case else "", []).append(k)
        keys = sorted(groups, key=_sort_key)
        small = [k for k in keys if len(groups[k]) < 3]
        if small:
            data.notes.append({"en": f"{len(small)} groups have fewer than 3 points and were left out: "
                                     + ", ".join(str(s) for s in small[:10]),
                               "ar": f"{len(small)} مجموعة فيها أقل من 3 نقاط فاستُبعدت: "
                                     + "، ".join(str(s) for s in small[:10])})
        keys = [k for k in keys if len(groups[k]) >= 3]
        if not keys:
            raise QgsProcessingException(tr("No group has 3 points or more."))
        if len(keys) > 500:
            raise QgsProcessingException(tr("Too many groups ({n}): choose a field with fewer values.").format(n=len(keys)))

        pf = QgsFields()
        for n, t in (("group", QMetaType.Type.QString), ("kind", QMetaType.Type.QString), ("points", QMetaType.Type.Int),
                     ("weight", QMetaType.Type.Double), ("x", QMetaType.Type.Double), ("y", QMetaType.Type.Double)):
            pf.append(QgsField(n, t))
        sf = QgsFields()
        for n, t in (("group", QMetaType.Type.QString), ("kind", QMetaType.Type.QString), ("sd_level", QMetaType.Type.Int),
                     ("points", QMetaType.Type.Int), ("radius_m", QMetaType.Type.Double), ("major_m", QMetaType.Type.Double),
                     ("minor_m", QMetaType.Type.Double), ("azimuth", QMetaType.Type.Double),
                     ("area_km2", QMetaType.Type.Double)):
            sf.append(QgsField(n, t))
        csink, cid = self.parameterAsSink(parameters, "CENTRES", context, pf, Qgis.WkbType.Point, data.src_crs)
        ssink, sid = self.parameterAsSink(parameters, "SHAPES", context, sf, Qgis.WkbType.Polygon, data.src_crs)
        res = {}
        for gi, key in enumerate(keys):
            idx = np.array(groups[key])
            xy, w = data.xy[idx], data.w[idx]
            if w.sum() <= 0:
                continue
            mc = dist_mod.mean_centre(xy, w)
            md = dist_mod.median_centre(xy, w)
            sd = dist_mod.standard_distance(xy, w)
            c, a, b, az = dist_mod.deviational_ellipse(xy, w)
            res[key] = {"n": len(idx), "w": float(w.sum()), "mean": mc, "median": md, "sd": sd, "a": a, "b": b, "az": az,
                        "xy": xy, "wts": w}
            label = "" if key is None else str(key)
            for kind, pt in (("mean centre", mc), ("median centre", md)):
                f = QgsFeature(pf)
                g = QgsGeometry.fromPointXY(QgsPointXY(*pt))
                g.transform(data.to_src)
                f.setGeometry(g)
                f.setAttributes([label, kind, len(idx), float(w.sum()), float(pt[0]), float(pt[1])])
                csink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            circle = dist_mod.ellipse_ring(mc, sd, sd, 0.0, level)
            ell = dist_mod.ellipse_ring(c, a, b, az, level)
            for kind, ring, vals in (("standard distance", circle, [level * sd, None, None, None]),
                                     ("ellipse", ell, [None, level * a, level * b, az])):
                f = QgsFeature(sf)
                g = QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in ring]])
                area = g.area() / 1e6
                g.transform(data.to_src)
                f.setGeometry(g)
                f.setAttributes([label, kind, level, len(idx)] + vals + [area])
                ssink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(10 + 70 * (gi + 1) / len(keys))
        self._push(feedback, data.notes)

        f = lambda d: pts_mod.fmt_distance(d, lang)  # noqa: E731
        first, last = keys[0], keys[-1]
        r0, r1 = res[first], res[last]
        move = None
        if len(keys) >= 2:
            dx, dy = r1["mean"] - r0["mean"]
            dist = math.hypot(dx, dy)
            perms = self.parameterAsInt(parameters, "PERMUTATIONS", context)
            p = float("nan")
            if perms:
                gen = _seed(self, parameters, context)
                both = np.vstack([r0["xy"], r1["xy"]])
                bw = np.concatenate([r0["wts"], r1["wts"]])
                n0 = len(r0["xy"])
                sims = np.empty(perms)
                for s in range(perms):
                    o = gen.permutation(len(both))
                    a_, b_ = o[:n0], o[n0:]
                    sims[s] = np.hypot(*(np.average(both[a_], axis=0, weights=bw[a_]) -
                                         np.average(both[b_], axis=0, weights=bw[b_])))
                p = (np.sum(sims >= dist) + 1.0) / (perms + 1.0)
            move = (dist, dx, dy, p)
            answer = _s(lang, "The mean centre moved {d} towards the {dir} between {a} and {b}",
                        "تحرّك المركز المتوسط {d} نحو {dir} بين {a} و{b}",
                        d=f(dist), dir=dist_mod.compass(dx, dy, lang), a=first, b=last)
            if p == p:
                answer += _s(lang, " ({p}: {sig}).", " ({p}: {sig}).", p=_p(p),
                             sig=_s(lang, "more than shuffling the two groups would give", "أكثر مما يعطيه خلط المجموعتين")
                             if p < 0.05 else _s(lang, "within what shuffling the two groups gives, so it may be chance",
                                                 "ضمن ما يعطيه خلط المجموعتين، فقد يكون مصادفة"))
            else:
                answer += "."
            answer += " " + _s(lang, "The spread (standard distance) went from {s0} to {s1}; the ellipse turned from "
                                     "{a0:.0f}° to {a1:.0f}°.",
                               "تغيّر الانتشار (المسافة المعيارية) من {s0} إلى {s1}، ودار القطع الناقص من {a0:.0f}° إلى "
                               "{a1:.0f}°.", s0=f(r0["sd"]), s1=f(r1["sd"]), a0=r0["az"], a1=r1["az"])
        else:
            elong = r0["a"] / max(r0["b"], 1e-9)
            answer = _s(lang, "Half of the spread lies within {sd} of the mean centre (standard distance; the circle "
                              "holds about 63% of points spread evenly around it). The points stretch {o} (ellipse "
                              "{a} × {b}, {az:.0f}° from north{el}).",
                        "يقع معظم الانتشار ضمن {sd} من المركز المتوسط (المسافة المعيارية؛ تضم الدائرة نحو 63% من نقاط "
                        "منتشرة بانتظام حوله). وتمتد النقاط {o} (قطع ناقص {a} × {b}، بزاوية {az:.0f}° من الشمال{el}).",
                        sd=f(r0["sd"]), o=_axis_words(r0["az"], lang), a=f(2 * r0["a"]), b=f(2 * r0["b"]), az=r0["az"],
                        el=_s(lang, ", clearly elongated", "، ممتدة بوضوح") if elong > 1.5 else
                        _s(lang, ", almost round", "، شبه دائرية"))
            answer = answer.replace("Half of the spread lies", "The points spread").replace("يقع معظم الانتشار", "تنتشر النقاط")

        # report
        imgs = []
        fig = figure(6.4, 5.4)
        ax = fig.add_subplot(1, 1, 1)
        for gi, key in enumerate(keys[:8] if len(keys) <= 8 else keys[:1]):
            r = res[key]
            col = SERIES[gi % len(SERIES)]
            sample = r["xy"] if len(r["xy"]) <= 4000 else r["xy"][:: len(r["xy"]) // 4000 + 1]
            ax.scatter(sample[:, 0], sample[:, 1], s=2, color=col, alpha=0.35, lw=0)
            e = dist_mod.ellipse_ring(r["mean"], r["a"], r["b"], r["az"], level)
            ax.plot(e[:, 0], e[:, 1], color=col, lw=1.6, label=chart_text(str(key)) if case else None)
            ax.plot(*r["mean"], marker="o", color=col, mec="white", ms=8)
            ax.plot(*r["median"], marker="s", color=col, mec="#1d2329", ms=5)
        if move:
            ax.annotate("", xy=res[last]["mean"], xytext=res[first]["mean"],
                        arrowprops={"arrowstyle": "->", "color": "#1d2329", "lw": 1.5})
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(_chart(lang, "Mean centre (circle), median centre (square), {k} SD ellipse",
                        "المركز المتوسط (دائرة)، المركز الوسيط (مربع)، القطع الناقص {k} انحراف معياري", k=level), fontsize=10)
        if case and len(keys) <= 8:
            ax.legend(frameon=False, fontsize=8)
        imgs.append(png(fig))
        rows = []
        for key in keys:
            r = res[key]
            rows.append([str(key) if case else "-", r["n"], float(r["mean"][0]), float(r["mean"][1]),
                         float(np.hypot(*(r["median"] - r["mean"]))), r["sd"], 2 * r["a"], 2 * r["b"], r["az"]])
        cols = [_s(lang, "Group", "المجموعة"), _s(lang, "Points", "النقاط"), "X", "Y",
                _s(lang, "Median to mean centre (m)", "بين المركز الوسيط والمتوسط (م)"),
                _s(lang, "Standard distance (m)", "المسافة المعيارية (م)"),
                _s(lang, "Ellipse long axis 1 SD (m)", "المحور الطويل 1 انحراف (م)"),
                _s(lang, "Ellipse short axis 1 SD (m)", "المحور القصير 1 انحراف (م)"),
                _s(lang, "Orientation (° from north)", "الاتجاه (° من الشمال)")]
        limits = [
            _s(lang, "The mean centre may fall where nothing is (between two towns, in a lake): it is a summary, "
                     "not a place.",
               "قد يقع المركز المتوسط حيث لا يوجد شيء (بين مدينتين، أو في بحيرة): إنه ملخّص لا مكان."),
            _s(lang, "A moved centre can mean more events in one area or fewer in another; compare the counts.",
               "تحرّك المركز قد يعني زيادة الأحداث في جهة أو نقصها في أخرى؛ قارن الأعداد."),
            _s(lang, "The 63% / 98% shares hold for a normal (bell-shaped) cloud; for several clusters the ellipse "
                     "describes the whole, not any cluster.",
               "نسب 63% و98% تصح لسحابة نقاط طبيعية (جرسية)؛ ومع عدة تجمعات يصف القطع الناقص المجموع لا أي تجمع."),
            _s(lang, "Orientation of an almost round ellipse is meaningless.", "اتجاه قطع ناقص شبه دائري لا معنى له.")]
        sections = [{"heading": _s(lang, "Centres and ellipses", "المراكز والقطوع الناقصة"), "images": imgs},
                    {"heading": _s(lang, "Per group", "لكل مجموعة"), "table": {"columns": cols, "rows": rows},
                     "paragraphs": [_s(lang, "Coordinates in {crs}. Axes are full lengths (twice the semi-axes).",
                                       "الإحداثيات في {crs}. المحاور بطولها الكامل (ضعف نصف المحور).",
                                       crs=data.work_crs.authid())]}]
        report = self._report(parameters, context, tr(self.NAME), answer, sections, data.notes, limits)
        return {"ANSWER": answer, "CENTRES": cid, "SHAPES": sid, "REPORT": report}


def _sort_key(v):
    if v is None:
        return (2, "")
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, str(v))


def _axis_words(az, lang):
    k = int(((az % 180) + 22.5) // 45) % 4
    en = ["north-south", "north-east to south-west", "east-west", "north-west to south-east"]
    ar = ["من الشمال إلى الجنوب", "من الشمال الشرقي إلى الجنوب الغربي", "من الشرق إلى الغرب",
          "من الشمال الغربي إلى الجنوب الشرقي"]
    return (ar if lang == "ar" else en)[k]


# ======================================================================================================
class DensityClusters(_StatsTool):
    """DBSCAN with eps from the k-distance elbow, cluster outlines with counts."""

    ID = "ssdbscan"
    NAME = "Density clusters (DBSCAN)"
    HELP = ("Finds groups of points that lie close together (DBSCAN): a point with at least the minimum number "
            "of points within the search distance (eps) starts or extends a cluster; points in no cluster are "
            "noise. Leave eps at 0 and it is suggested from the 'elbow' of the k-distance curve (the distance at "
            "which points stop being in dense places). Gives every point its cluster (0 = noise) and draws the "
            "outline of each cluster (convex or concave hull) with its count, area and density.")
    @staticmethod
    def _hulls():
        return [tr("Convex hull"), tr("Concave hull")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self._add_points()
        self.addParameter(QgsProcessingParameterNumber(
            "MIN_POINTS", tr("Minimum points in a cluster's core neighbourhood"),
            Qgis.ProcessingNumberParameterType.Integer, 5, minValue=2))
        self.addParameter(QgsProcessingParameterNumber(
            "EPS", tr("Search distance eps (m, 0 = suggest from the k-distance elbow)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterEnum(
            "HULL", tr("Cluster outline"), self._hulls(), defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "OUTPUT", tr("Points with cluster"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "CLUSTERS", tr("Cluster outlines"), Qgis.ProcessingSourceType.VectorPolygon))
        self._add_common()
        self.addOutput(QgsProcessingOutputNumber("EPS_USED", tr("eps used (m)")))
        self.addOutput(QgsProcessingOutputNumber("N_CLUSTERS", tr("Number of clusters")))
        self.addOutput(QgsProcessingOutputNumber("NOISE_SHARE", tr("Share of noise points")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        lang = text_lang()
        minp = self.parameterAsInt(parameters, "MIN_POINTS", context)
        data = self._read_points(parameters, context, feedback, keep=True, min_points=max(10, minp + 1))
        xy = data.xy
        eps = self.parameterAsDouble(parameters, "EPS", context)
        kd_eps, kd = pts_mod.suggest_eps(xy, minp)
        auto = eps <= 0
        if auto:
            eps = kd_eps
            if eps <= 0:
                raise QgsProcessingException(tr("All points share a few locations: no distance can be suggested; give eps."))
        labels, core = pts_mod.dbscan(xy, eps, minp)
        k = int(labels.max())
        noise = float((labels == 0).mean())
        feedback.setProgress(60)

        src = self.parameterAsSource(parameters, "INPUT", context)
        fields = QgsFields(src.fields())
        extra = QgsFields()
        extra.append(QgsField("cluster", QMetaType.Type.Int))
        extra.append(QgsField("core", QMetaType.Type.Int))
        fields = QgsProcessingUtils.combineFields(fields, extra)
        sink, sid = self.parameterAsSink(parameters, "OUTPUT", context, fields, Qgis.WkbType.Point, data.src_crs)
        for n, (feat, part) in enumerate(data.features):
            f = QgsFeature(fields)
            g = feat.geometry()
            if g.isMultipart():
                g = QgsGeometry.fromPointXY(g.asMultiPoint()[part])
            f.setGeometry(QgsGeometry(g))
            f.setAttributes(list(feat.attributes()) + [int(labels[n]), int(core[n])])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        cf = QgsFields()
        for n, t in (("cluster", QMetaType.Type.Int), ("points", QMetaType.Type.Int), ("area_m2", QMetaType.Type.Double),
                     ("per_km2", QMetaType.Type.Double), ("x", QMetaType.Type.Double), ("y", QMetaType.Type.Double)):
            cf.append(QgsField(n, t))
        csink, cid = self.parameterAsSink(parameters, "CLUSTERS", context, cf, Qgis.WkbType.MultiPolygon, data.src_crs)
        concave = self.parameterAsEnum(parameters, "HULL", context) == 1
        rows = []
        for c in range(1, k + 1):
            sel = xy[labels == c]
            mp = QgsGeometry.fromMultiPointXY([QgsPointXY(x, y) for x, y in sel])
            hull = mp.concaveHull(0.3) if concave else mp.convexHull()
            if hull is None or hull.isEmpty() or hull.area() <= 0:
                hull = mp.convexHull().buffer(max(eps * 0.1, 1e-6), 8)
            hull = polygonal(hull)
            area = hull.area()
            centre = sel.mean(axis=0)
            rows.append([c, len(sel), area, len(sel) / max(area, 1e-9) * 1e6, float(centre[0]), float(centre[1])])
            hull.transform(data.to_src)
            f = QgsFeature(cf)
            f.setGeometry(hull)
            f.setAttributes(rows[-1])
            csink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
        self._push(feedback, data.notes)

        fd = lambda d: pts_mod.fmt_distance(d, lang)  # noqa: E731
        if k:
            answer = _s(lang, "{k} density clusters (the largest has {big} points); {ns:.0%} of the points are noise, in no "
                              "cluster. eps = {e} ({how}), at least {m} points.",
                        "{k} تجمعات كثيفة (أكبرها {big} نقطة)؛ و{ns:.0%} من النقاط ضوضاء لا تنتمي لأي تجمع. eps = {e} "
                        "({how})، والحد الأدنى {m} نقاط.",
                        k=k, big=_num(rows[0][1]), ns=noise, e=fd(eps), m=minp,
                        how=_s(lang, "suggested from the k-distance elbow", "مقترحة من انعطاف منحنى المسافة k") if auto
                        else _s(lang, "your choice", "من اختيارك"))
        else:
            answer = _s(lang, "No cluster: no point has {m} points within {e}. Increase eps or lower the minimum.",
                        "لا تجمعات: لا توجد نقطة حولها {m} نقاط ضمن {e}. زد eps أو قلّل الحد الأدنى.", m=minp, e=fd(eps))
        imgs = []
        fig = figure(10.0, 4.0)
        ax = fig.add_subplot(1, 2, 1)
        ax.plot(np.arange(len(kd)), kd, color=ACCENT, lw=1.6)
        ax.axhline(eps, color="#C98A12", ls="--", lw=1)
        ax.set_xlabel(_chart(lang, "points sorted", "النقاط مرتبة"))
        ax.set_ylabel(_chart(lang, "distance to neighbour {m} (m)", "المسافة إلى الجار رقم {m} (م)", m=max(1, minp - 1)))
        ax.set_title(_chart(lang, "k-distance curve and eps (dashed)", "منحنى المسافة k وقيمة eps (متقطع)"), fontsize=10)
        _style(ax)
        ax = fig.add_subplot(1, 2, 2)
        ax.scatter(xy[labels == 0, 0], xy[labels == 0, 1], s=2, color="#c8ced3", lw=0)
        for c in range(1, min(k, 40) + 1):
            s = xy[labels == c]
            ax.scatter(s[:, 0], s[:, 1], s=3, color=SERIES[(c - 1) % len(SERIES)], lw=0)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(_chart(lang, "Clusters (grey: noise)", "التجمعات (الرمادي: ضوضاء)"), fontsize=10)
        imgs.append(png(fig))
        limits = [
            _s(lang, "eps sets the scale: a smaller eps splits clusters and calls more points noise, a larger one "
                     "merges them. Clusters are found, not tested: random points also form some clusters.",
               "قيمة eps تحدد المقياس: الأصغر تقسّم التجمعات وتعدّ نقاطاً أكثر ضوضاء، والأكبر تدمجها. التجمعات مكتشفة لا "
               "مختبرة إحصائياً: النقاط العشوائية أيضاً تكوّن بعض التجمعات."),
            _s(lang, "One eps for the whole map: dense city clusters and sparse rural ones cannot both be found well.",
               "قيمة eps واحدة للخريطة كلها: لا يمكن إيجاد تجمعات المدينة الكثيفة والريف المتفرق جيداً معاً."),
            _s(lang, "Noise points are not errors, only points away from dense places.",
               "نقاط الضوضاء ليست أخطاء، بل نقاط بعيدة عن الأماكن الكثيفة.")]
        sections = [{"heading": _s(lang, "Clusters", "التجمعات"), "images": imgs, "table": {
            "columns": [_s(lang, "Cluster", "التجمع"), _s(lang, "Points", "النقاط"), _s(lang, "Area (m²)", "المساحة (م²)"),
                        _s(lang, "Points per km²", "نقطة لكل كم²"), "X", "Y"], "rows": rows[:50]}}]
        report = self._report(parameters, context, tr(self.NAME), answer, sections, data.notes, limits)
        return {"ANSWER": answer, "OUTPUT": sid, "CLUSTERS": cid, "EPS_USED": eps, "N_CLUSTERS": k,
                "NOISE_SHARE": noise, "REPORT": report}


# ======================================================================================================
class RateSmoothing(_StatsTool):
    """Raw rates with exact intervals, global and local Empirical Bayes, reliability flags and a funnel plot."""

    ID = "ssrates"
    NAME = "Rates for small numbers (Empirical Bayes smoothing)"
    HELP = ("Maps of rates (cases per 100,000 people, accidents per 1,000 vehicles...) mislead when some areas are "
            "small: one case among 50 people is a rate of 2,000 per 100,000, so the highest and lowest rates are "
            "mostly the smallest areas. This tool computes the raw rate with its exact 95% interval, flags rates "
            "that rest on too few events or no population, and smooths the rates by Empirical Bayes - towards "
            "the overall rate (global) and towards the neighbours' rate (local, neighbours by shared borders, "
            "nearest areas or distance) - pulling small areas much more than large ones. The report has a funnel "
            "plot that shows which areas are really unusual for their size.")
    @staticmethod
    def _neighbours():
        return [tr("Shared border or corner (queen contiguity)"), tr("Nearest areas (k)"), tr("Within a distance")]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            "INPUT", tr("Areas (polygons)"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            "EVENTS", tr("Events (cases) field"), parentLayerParameterName="INPUT",
            type=Qgis.ProcessingFieldParameterDataType.Numeric))
        self.addParameter(QgsProcessingParameterField(
            "POPULATION", tr("Population (at risk) field"), parentLayerParameterName="INPUT",
            type=Qgis.ProcessingFieldParameterDataType.Numeric))
        self.addParameter(QgsProcessingParameterField(
            "NAME", tr("Name field (for the report, optional)"), parentLayerParameterName="INPUT", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "MULTIPLIER", tr("Rate per (e.g. 100000 people)"), Qgis.ProcessingNumberParameterType.Double, 100000.0,
            minValue=1.0))
        self.addParameter(QgsProcessingParameterEnum(
            "NEIGHBOURS", tr("Neighbours for the local smoothing"), self._neighbours(), defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "K", tr("Number of nearest areas"), Qgis.ProcessingNumberParameterType.Integer, 6, minValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            "DISTANCE", tr("Neighbour distance (m, between centroids)"), Qgis.ProcessingNumberParameterType.Double,
            5000.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            "MIN_EVENTS", tr("Fewer events than this make a raw rate unreliable"),
            Qgis.ProcessingNumberParameterType.Integer, 20, minValue=1))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "OUTPUT", tr("Smoothed rates"), Qgis.ProcessingSourceType.VectorPolygon))
        self._add_common()

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        lang = text_lang()
        src = self.parameterAsSource(parameters, "INPUT", context)
        if src is None:
            raise QgsProcessingException(tr("Choose a polygon layer."))
        ef = self.parameterAsString(parameters, "EVENTS", context)
        pf = self.parameterAsString(parameters, "POPULATION", context)
        nf = self.parameterAsString(parameters, "NAME", context)
        mult = self.parameterAsDouble(parameters, "MULTIPLIER", context)
        min_ev = self.parameterAsInt(parameters, "MIN_EVENTS", context)
        work, changed = _metric_crs(src.sourceCrs(), src.sourceExtent(), context)
        xf = QgsCoordinateTransform(src.sourceCrs(), work, context.transformContext())
        notes = []
        feats, geoms, centres, ev, pop, names = [], [], [], [], [], []
        bad = 0
        for f in src.getFeatures():
            g = f.geometry()
            try:
                e = float(f[ef])
                p = float(f[pf])
            except (TypeError, ValueError):
                e = p = float("nan")
            if not (math.isfinite(e) and math.isfinite(p)) or e < 0 or p < 0:
                bad += 1
                e, p = float("nan"), float("nan")
            feats.append(f)
            geoms.append(QgsGeometry(g) if g is not None and not g.isEmpty() else None)
            if g is not None and not g.isEmpty():
                gw = QgsGeometry(g)
                gw.transform(xf)
                c = gw.pointOnSurface().asPoint()
                centres.append((c.x(), c.y()))
            else:
                centres.append((np.nan, np.nan))
            ev.append(e)
            pop.append(p)
            names.append(str(f[nf]) if nf and f[nf] is not None else str(f.id()))
        ev, pop = np.array(ev), np.array(pop)
        valid = np.isfinite(ev) & np.isfinite(pop) & (pop > 0)
        if valid.sum() < 5:
            raise QgsProcessingException(tr("Too few areas with a population above zero: {n} (at least 5).").format(
                n=int(valid.sum())))
        if bad:
            notes.append({"en": f"{bad} areas have empty, negative or non-numeric events or population: left without a rate.",
                          "ar": f"{bad} منطقة أحداثها أو سكانها فارغة أو سالبة أو غير رقمية: تُركت بلا معدل."})
        zero = int((np.isfinite(pop) & (pop == 0)).sum())
        if zero:
            notes.append({"en": f"{zero} areas have zero population: a rate cannot be computed there.",
                          "ar": f"{zero} منطقة سكانها صفر: لا يمكن حساب معدل لها."})
        over = int((valid & (ev > pop)).sum())
        if over:
            notes.append({"en": f"{over} areas have more events than people: check the fields (are they swapped, or is "
                                "the population of another year?).",
                          "ar": f"{over} منطقة أحداثها أكثر من سكانها: راجع الحقلين (هل هما معكوسان، أو السكان لسنة أخرى؟)."})
        if valid.any() and np.any(np.abs(ev[valid] - np.round(ev[valid])) > 1e-9):
            notes.append({"en": "Some event counts are not whole numbers: the smoothing assumes counts of events.",
                          "ar": "بعض أعداد الأحداث ليست أعداداً صحيحة: التنعيم يفترض أعداداً للأحداث."})

        # neighbours
        mode = self.parameterAsEnum(parameters, "NEIGHBOURS", context)
        cen = np.array(centres)
        feedback.setProgressText(tr("Finding the neighbours..."))
        if mode == 0:
            nb = rates_mod.contiguity_neighbours(geoms, feedback)
        elif mode == 1:
            ok = np.isfinite(cen[:, 0])
            nb = [np.zeros(0, int)] * len(cen)
            idx_ok = np.nonzero(ok)[0]
            for i, row in zip(idx_ok, rates_mod.knn_neighbours(cen[ok], self.parameterAsInt(parameters, "K", context))):
                nb[i] = idx_ok[row]
        else:
            ok = np.isfinite(cen[:, 0])
            nb = [np.zeros(0, int)] * len(cen)
            idx_ok = np.nonzero(ok)[0]
            dist = self.parameterAsDouble(parameters, "DISTANCE", context)
            for i, row in zip(idx_ok, rates_mod.band_neighbours(cen[ok], dist)):
                nb[i] = idx_ok[row]
        islands = [i for i in range(len(nb)) if valid[i] and len(nb[i]) == 0]
        if islands:
            ok = np.isfinite(cen[:, 0])
            idx_ok = np.nonzero(ok)[0]
            near = rates_mod.knn_neighbours(cen[ok], 1)
            pos = {v: k for k, v in enumerate(idx_ok)}
            for i in islands:
                if i in pos:
                    nb[i] = idx_ok[near[pos[i]]]
            notes.append({"en": f"{len(islands)} areas have no neighbour by this rule; their nearest area was used.",
                          "ar": f"{len(islands)} منطقة بلا جار حسب هذه القاعدة؛ فاستُخدمت أقرب منطقة إليها."})

        e_v = np.where(valid, ev, 0.0)
        p_v = np.where(valid, pop, 0.0)
        raw = rates_mod.raw_rates(e_v, p_v)
        lo, hi = rates_mod.poisson_ci(e_v, p_v)
        geb, gw, b, a = rates_mod.global_eb(e_v, p_v)
        nb_valid = [np.array([j for j in n if valid[j]], dtype=int) for n in nb]
        leb, lw = rates_mod.local_eb(e_v, p_v, nb_valid)
        expected = b * p_v
        status = np.where(~valid, "no rate", np.where(e_v < min_ev, "unreliable", "ok"))
        feedback.setProgress(70)

        out_f = QgsFields()
        for n, t in (("raw_rate", QMetaType.Type.Double), ("rate_low95", QMetaType.Type.Double),
                     ("rate_high95", QMetaType.Type.Double), ("eb_global", QMetaType.Type.Double),
                     ("eb_local", QMetaType.Type.Double), ("own_weight", QMetaType.Type.Double),
                     ("expected", QMetaType.Type.Double), ("ratio_obs_exp", QMetaType.Type.Double),
                     ("neighbours", QMetaType.Type.Int), ("reliability", QMetaType.Type.QString)):
            out_f.append(QgsField(n, t))
        fields = QgsProcessingUtils.combineFields(src.fields(), out_f)
        sink, sid = self.parameterAsSink(parameters, "OUTPUT", context, fields, src.wkbType(), src.sourceCrs())

        def v(x):
            return None if not np.isfinite(x) else float(x)

        for i, f in enumerate(feats):
            o = QgsFeature(fields)
            o.setGeometry(f.geometry())
            ok = valid[i]
            o.setAttributes(list(f.attributes()) + [
                v(raw[i] * mult) if ok else None, v(lo[i] * mult) if ok else None, v(hi[i] * mult) if ok else None,
                v(geb[i] * mult) if ok else None, v(leb[i] * mult) if ok else None, v(gw[i]) if ok else None,
                v(expected[i]) if ok else None, v(e_v[i] / expected[i]) if ok and expected[i] > 0 else None,
                int(len(nb[i])), str(status[i])])
            sink.addFeature(o, QgsFeatureSink.Flag.FastInsert)
        for n in notes:
            feedback.pushWarning(say(n, lang))

        vi = np.nonzero(valid)[0]
        unreliable = int((status == "unreliable").sum())
        med_pop = float(np.median(pop[vi]))
        top = vi[np.argsort(-raw[vi])][:max(1, min(10, len(vi) // 5))]
        top_small = int((pop[top] < med_pop).sum())
        r_raw = (np.nanmin(raw[vi]) * mult, np.nanmax(raw[vi]) * mult)
        r_eb = (np.nanmin(leb[vi]) * mult, np.nanmax(leb[vi]) * mult)
        best = vi[np.nanargmax(leb[vi])]
        per = _num(mult)
        answer = _s(lang, "The overall rate is {b} per {per}. {u} of {n} areas have fewer than {m} events, so their raw "
                          "rates are unreliable; {ts} of the {tn} highest raw rates are in areas below the median "
                          "population ({mp}) - small numbers, not necessarily real risk. After Empirical Bayes "
                          "smoothing the range narrows from {r0}-{r1} to {e0}-{e1} per {per}; the highest smoothed "
                          "rate is in {best}.",
                    "المعدل العام {b} لكل {per}. {u} من {n} منطقة أحداثها أقل من {m}، فمعدلاتها الخام غير موثوقة؛ و{ts} "
                    "من أعلى {tn} معدلات خام في مناطق سكانها دون الوسيط ({mp}) - أعداد صغيرة لا خطورة حقيقية بالضرورة. بعد "
                    "تنعيم بايز التجريبي يضيق المدى من {r0}-{r1} إلى {e0}-{e1} لكل {per}؛ وأعلى معدل منعّم في {best}.",
                    b=_fmt_rate(b * mult), per=per, u=unreliable, n=len(vi), m=min_ev, ts=top_small, tn=len(top),
                    mp=_num(med_pop), r0=_fmt_rate(r_raw[0]), r1=_fmt_rate(r_raw[1]), e0=_fmt_rate(r_eb[0]),
                    e1=_fmt_rate(r_eb[1]), best=names[best])
        if a == 0:
            answer += " " + _s(lang, "The areas differ no more than chance would make them, so the global smoothing pulls "
                                     "every area fully to the overall rate.",
                               "المناطق لا تختلف أكثر مما تصنعه المصادفة، لذا يسحب التنعيم العام كل منطقة كلياً إلى المعدل العام.")

        imgs = [self._funnel(raw[vi] * mult, pop[vi], b, mult, lang), self._shrink(raw[vi] * mult, leb[vi] * mult, pop[vi], lang)]
        order = vi[np.argsort(-leb[vi])][:20]
        rel_word = {"ok": _s(lang, "reliable", "موثوق"), "unreliable": _s(lang, "unreliable", "غير موثوق"),
                    "no rate": _s(lang, "no rate", "بلا معدل")}
        rows = [[names[i], float(ev[i]), float(pop[i]), float(raw[i] * mult), float(lo[i] * mult), float(hi[i] * mult),
                 float(geb[i] * mult), float(leb[i] * mult), rel_word[str(status[i])]] for i in order]
        limits = [
            _s(lang, "Smoothed rates are estimates: smoothing can hide a real small cluster. Check the raw count of "
                     "an area before dismissing it.",
               "المعدلات المنعّمة تقديرات: قد يخفي التنعيم تجمعاً صغيراً حقيقياً. راجع العدد الخام للمنطقة قبل استبعادها."),
            _s(lang, "The local rate depends on the neighbours chosen; areas at the edge of the map have fewer.",
               "يعتمد المعدل المحلي على الجيران المختارين؛ ومناطق حافة الخريطة لها جيران أقل."),
            _s(lang, "The population must be the population at risk for the same period as the events; rates are "
                     "not age-standardised.",
               "يجب أن يكون السكان هم المعرّضين للخطر في فترة الأحداث نفسها؛ والمعدلات غير معيّرة حسب العمر."),
            _s(lang, "A high rate is not a cause: it says where, not why.", "المعدل المرتفع ليس سبباً: يقول أين لا لماذا.")]
        sections = [
            {"heading": _s(lang, "Why small areas show extreme rates", "لماذا تظهر المعدلات المتطرفة في المناطق الصغيرة"),
             "paragraphs": [_s(lang, "With few people, one event more or less moves the rate a lot: in the funnel plot "
                                     "the band of chance variation (95% and 99.8%) is wide for small populations and "
                                     "narrows for large ones. Areas outside the funnel are unusual for their size. "
                                     "Empirical Bayes keeps a share of each area's own rate (the own_weight field: near "
                                     "1 for large areas, near 0 for tiny ones) and fills the rest with the overall or "
                                     "neighbourhood rate.",
                               "مع قلة السكان يغيّر حدث واحد زائد أو ناقص المعدل كثيراً: في مخطط القمع يكون نطاق التغير "
                               "بالمصادفة (95% و99.8%) واسعاً للسكان القليلين ويضيق للكثيرين. المناطق خارج القمع غير "
                               "عادية بالنسبة لحجمها. يحتفظ بايز التجريبي بنصيب من معدل المنطقة نفسها (حقل own_weight: "
                               "قرب 1 للمناطق الكبيرة وقرب 0 للصغيرة جداً) ويكمل الباقي من المعدل العام أو معدل الجوار.")],
             "images": imgs},
            {"heading": _s(lang, "Highest smoothed rates (per {per})", "أعلى المعدلات المنعّمة (لكل {per})", per=per),
             "table": {"columns": [_s(lang, "Area", "المنطقة"), _s(lang, "Events", "الأحداث"), _s(lang, "Population", "السكان"),
                                   _s(lang, "Raw rate", "المعدل الخام"), _s(lang, "Low 95%", "الحد الأدنى 95%"),
                                   _s(lang, "High 95%", "الحد الأعلى 95%"), _s(lang, "EB global", "بايز عام"),
                                   _s(lang, "EB local", "بايز محلي"), _s(lang, "Reliability", "الموثوقية")], "rows": rows}}]
        report = self._report(parameters, context, tr(self.NAME), answer, sections, notes, limits)
        return {"ANSWER": answer, "OUTPUT": sid, "REPORT": report}

    @staticmethod
    def _funnel(raw, pop, b, mult, lang):
        fig = figure(6.6, 4.2)
        ax = fig.add_subplot(1, 1, 1)
        ps = np.logspace(math.log10(max(pop.min(), 1.0)), math.log10(pop.max()), 200)
        for level, ls in ((0.95, "--"), (0.998, ":")):
            lo, hi = rates_mod.funnel_limits(b, ps, level)
            ax.plot(ps, lo * mult, color="#5b6670", ls=ls, lw=1)
            ax.plot(ps, hi * mult, color="#5b6670", ls=ls, lw=1)
        ax.axhline(b * mult, color="#1d2329", lw=1)
        ax.scatter(pop, raw, s=12, color=ACCENT, alpha=0.75, lw=0)
        ax.set_xscale("log")
        ax.set_xlabel(_chart(lang, "population (log scale)", "السكان (مقياس لوغاريتمي)"))
        ax.set_ylabel(_chart(lang, "raw rate", "المعدل الخام"))
        ax.set_title(_chart(lang, "Funnel plot: outside the dashed lines = unusual for the size",
                        "مخطط القمع: خارج الخطوط المتقطعة = غير عادي بالنسبة للحجم"), fontsize=10)
        _style(ax)
        return png(fig)

    @staticmethod
    def _shrink(raw, eb, pop, lang):
        fig = figure(6.6, 4.2)
        ax = fig.add_subplot(1, 1, 1)
        size = 8 + 120 * (pop / pop.max()) ** 0.5
        ax.scatter(raw, eb, s=size, color="#3A6EA5", alpha=0.55, lw=0)
        m = max(np.nanmax(raw), np.nanmax(eb))
        ax.plot([0, m], [0, m], color="#5b6670", lw=0.8, ls="--")
        ax.set_xlabel(_chart(lang, "raw rate", "المعدل الخام"))
        ax.set_ylabel(_chart(lang, "local Empirical Bayes rate", "معدل بايز التجريبي المحلي"))
        ax.set_title(_chart(lang, "Small areas (small dots) are pulled the most", "المناطق الصغيرة (النقاط الصغيرة) تُسحب أكثر"),
                     fontsize=10)
        _style(ax)
        return png(fig)


def _fmt_rate(v):
    if not np.isfinite(v):
        return "-"
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.1f}"
    return f"{v:.3g}"
