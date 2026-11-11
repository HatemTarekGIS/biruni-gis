"""The spatial statistics advisor: the user picks a QUESTION in plain words, not a tool name; the advisor
looks at the data before anything runs (geometry, number of features, coordinate system, empty and constant
values, zero-heavy counts, duplicates, skewness, too few features for the method), says what it found as a
short checklist and opens the matching Processing tool with the parameters filled in.

Only tools that are registered at runtime are offered (questions() keeps the full catalogue; tools_for()
filters it). Nothing is computed at import time."""
import math

from .common import say

# Each question: id, the words (ar/en), tools in the order they are suggested, and what it needs:
#   geometry: "point" | "polygon" | "any"
#   field:    None | "numeric" | "numeric_optional"            (the main value)
#   field2:   None | "numeric" | "numeric_multi" | "date" | "category"
#   min_stop / min_warn: features below which the question cannot / can hardly be answered
QUESTIONS = [
    {"id": "clustered", "tools": ["biruni:ssnearest", "biruni:ssripley"], "geometry": "point", "field": None,
     "field2": None, "min_stop": 5, "min_warn": 30,
     "ar": "هل نقاطي متجمّعة أم متباعدة؟", "en": "Are my points clustered or spread out?"},
    {"id": "hotspots", "tools": ["biruni:sshotspots"], "geometry": "any", "field": "numeric_optional",
     "field2": None, "min_stop": 10, "min_warn": 30,
     "ar": "أين البؤر الساخنة؟", "en": "Where are the hot spots?"},
    {"id": "similar", "tools": ["biruni:ssmoran", "biruni:sslisa"], "geometry": "any", "field": "numeric",
     "field2": None, "min_stop": 8, "min_warn": 30,
     "ar": "هل القيم المتشابهة متجاورة؟", "en": "Are similar values next to each other?"},
    {"id": "distance", "tools": ["biruni:ssincremental", "biruni:ssripley"], "geometry": "any",
     "field": "numeric_optional", "field2": None, "min_stop": 10, "min_warn": 30,
     "ar": "على أي مسافة يحدث التجمّع؟", "en": "At what distance does clustering happen?"},
    {"id": "scale", "tools": ["biruni:ssscale"], "geometry": "any", "field": "numeric_optional", "field2": None,
     "min_stop": 10, "min_warn": 30,
     "ar": "هل تتغير إجابتي بتغيّر وحدات الخريطة أو مقياسها؟",
     "en": "Does my answer depend on the map's units/scale?"},
    {"id": "time", "tools": ["biruni:ssemerging"], "geometry": "point", "field": None, "field2": "date",
     "min_stop": 20, "min_warn": 100,
     "ar": "كيف تغيّرت البؤر الساخنة مع الزمن؟", "en": "How did hot spots change over time?"},
    {"id": "colocation", "tools": ["biruni:sscolocation"], "geometry": "point", "field": None,
     "field2": "category", "min_stop": 10, "min_warn": 30,
     "ar": "هل توجد «أ» غالبًا بالقرب من «ب»؟", "en": "Are A's often found near B's?"},
    {"id": "centre", "tools": ["biruni:sscentre"], "geometry": "any", "field": "numeric_optional", "field2": None,
     "min_stop": 3, "min_warn": 10,
     "ar": "أين المركز وفي أي اتجاه تنتشر؟", "en": "Where is the centre and which way does it spread?"},
    {"id": "dense", "tools": ["biruni:ssdbscan", "biruni:sskde"], "geometry": "point", "field": None,
     "field2": None, "min_stop": 5, "min_warn": 30,
     "ar": "أين المجموعات الكثيفة؟", "en": "Where are the dense groups?"},
    {"id": "rates", "tools": ["biruni:ssrates"], "geometry": "polygon", "field": "numeric", "field2": "numeric",
     "min_stop": 5, "min_warn": 20,
     "ar": "هل هذه المعدلات موثوقة في المناطق الصغيرة؟", "en": "Are these rates reliable in small areas?"},
    {"id": "explain", "tools": ["biruni:ssols", "biruni:ssgwr"], "geometry": "any", "field": "numeric",
     "field2": "numeric_multi", "min_stop": 10, "min_warn": 30,
     "ar": "ما الذي يفسّر هذه القيمة، وهل يتغير ذلك من مكان لآخر؟",
     "en": "What explains this value, and does it change from place to place?"},
    {"id": "bivariate", "tools": ["biruni:ssbivariate"], "geometry": "any", "field": "numeric",
     "field2": "numeric", "min_stop": 8, "min_warn": 30,
     "ar": "ما العلاقة بين متغيرين في المكان؟", "en": "What is the relationship between two variables in space?"},
]

# what the field choosers are called for each question (content shown in the dialog)
FIELD_WORDS = {
    "hotspots": ({"ar": "القيمة (اتركها فارغة لعدّ النقاط)", "en": "Value (leave empty to count points)"}, None),
    "similar": ({"ar": "القيمة", "en": "Value"}, None),
    "distance": ({"ar": "القيمة (اختياري)", "en": "Value (optional)"}, None),
    "scale": ({"ar": "القيمة (اختياري)", "en": "Value (optional)"}, None),
    "time": (None, {"ar": "حقل التاريخ", "en": "Date field"}),
    "colocation": (None, {"ar": "حقل الفئة (أ، ب…)", "en": "Category field (A, B …)"}),
    "centre": ({"ar": "الوزن (اختياري)", "en": "Weight (optional)"}, None),
    "rates": ({"ar": "عدد الحالات", "en": "Number of cases"}, {"ar": "عدد السكان المعرّضين", "en": "Population at risk"}),
    "explain": ({"ar": "القيمة المراد تفسيرها", "en": "Value to explain"},
                {"ar": "المتغيرات المفسِّرة", "en": "Explanatory variables"}),
    "bivariate": ({"ar": "المتغير الأول", "en": "First variable"}, {"ar": "المتغير الثاني", "en": "Second variable"}),
}

OK, WARN, STOP = "ok", "warn", "stop"
MAX_READ = 200_000  # features read for the checks (enough to judge; huge layers are sampled)


def questions():
    """The questions, each a dict: id, ar, en, tools, geometry, field, field2, min_stop, min_warn."""
    return [dict(q) for q in QUESTIONS]


def question(qid):
    for q in QUESTIONS:
        if q["id"] == qid:
            return dict(q)
    return None


def field_words(qid):
    """(words for the first field chooser, words for the second) - None where the question has none."""
    return FIELD_WORDS.get(qid, (None, None))


def question_text(q, lang=None):
    return say({"ar": q["ar"], "en": q["en"]}, lang)


def registered(alg_id):
    from qgis.core import QgsApplication

    return QgsApplication.processingRegistry().algorithmById(alg_id) is not None


def tools_for(qid):
    """Tool ids of a question that are registered now (in the suggested order)."""
    q = question(qid)
    return [t for t in (q["tools"] if q else []) if registered(t)]


def available_questions():
    """Questions with at least one registered tool."""
    return [q for q in questions() if tools_for(q["id"])]


# ------------------------------------------------------------------- checks ---

def _geometry_kind(layer):
    from qgis.core import Qgis

    gt = layer.geometryType()
    return {Qgis.GeometryType.Point: "point", Qgis.GeometryType.Line: "line",
            Qgis.GeometryType.Polygon: "polygon"}.get(gt, "none")


def _values(layer, name, limit=MAX_READ):
    """Field values (None for empty) of up to `limit` features, without geometry."""
    from qgis.core import QgsFeatureRequest

    idx = layer.fields().indexOf(name)
    if idx < 0:
        return None
    req = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry).setSubsetOfAttributes([idx]).setLimit(limit)
    out = []
    for f in layer.getFeatures(req):
        v = f.attributes()[idx]
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            out.append(None)
        else:
            out.append(v)
    return out


def _numbers(vals):
    out = []
    for v in vals:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x):
            out.append(x)
    return out


def _skew(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    m = sum(xs) / n
    s2 = sum((x - m) ** 2 for x in xs) / n
    if s2 <= 0:
        return 0.0
    return sum((x - m) ** 3 for x in xs) / n / s2 ** 1.5


def _numeric_checks(name, vals, n, lang, role="value"):
    out = []
    if vals is None:
        return [(STOP, say({"ar": "الحقل «{f}» غير موجود في الطبقة.", "en": "The field “{f}” is not in the layer."},
                           lang, f=name))]
    empty = sum(1 for v in vals if v is None)
    nums = _numbers(vals)
    if len(nums) < len(vals) - empty:
        out.append((STOP, say({"ar": "«{f}» ليس حقلًا رقميًا.", "en": "“{f}” is not a numeric field."}, lang, f=name)))
        return out
    if empty:
        out.append((WARN, say({"ar": "«{f}»: {e} من {n} معلم بدون قيمة؛ ستُستبعد من التحليل.",
                               "en": "“{f}”: {e} of {n} features have no value; they will be left out."},
                              lang, f=name, e=empty, n=len(vals))))
    if not nums:
        out.append((STOP, say({"ar": "«{f}» فارغ تمامًا.", "en": "“{f}” is empty."}, lang, f=name)))
        return out
    lo, hi = min(nums), max(nums)
    if lo == hi:
        out.append((STOP, say({"ar": "«{f}» له القيمة نفسها ({v}) في كل المعالم: لا يوجد ما يُحلَّل.",
                               "en": "“{f}” has the same value ({v}) everywhere: there is nothing to analyse."},
                              lang, f=name, v=f"{lo:g}")))
        return out
    integer = all(float(x).is_integer() for x in nums)
    zeros = sum(1 for x in nums if x == 0) / len(nums)
    if integer and lo >= 0 and zeros > 0.5:
        out.append((WARN, say({"ar": "«{f}» عدّ أغلبه أصفار ({z}%): الاختبارات التي تفترض توزيعًا طبيعيًا تضعف؛ "
                                     "فكّر في تجميع المناطق أو حساب معدلات.",
                               "en": "“{f}” is a count that is mostly zeros ({z}%): tests that assume a normal "
                                     "distribution lose power; consider larger areas or rates."},
                              lang, f=name, z=round(100 * zeros))))
    sk = _skew(nums)
    if sk > 1 and lo >= 0:
        out.append((WARN, say({"ar": "«{f}» ملتوٍ بشدة (الالتواء {s}): جرّب التحويل اللوغاريتمي {t} قبل التحليل.",
                               "en": "“{f}” is strongly skewed (skewness {s}): try a log transform {t} first."},
                              lang, f=name, s=f"{sk:.1f}", t="log(x + 1)" if lo == 0 else "log(x)")))
    if role == "count" and lo < 0:
        out.append((STOP, say({"ar": "«{f}» فيه قيم سالبة ولا يمكن أن يكون عددًا.",
                               "en": "“{f}” has negative values; it cannot be a count."}, lang, f=name)))
    if not [m for m in out if m[0] != OK]:
        out.append((OK, say({"ar": "«{f}»: قيم كاملة ومتنوعة (من {a} إلى {b}).",
                             "en": "“{f}”: complete, varied values ({a} to {b})."}, lang, f=name, a=f"{lo:g}",
                            b=f"{hi:g}")))
    return out


def _date_checks(layer, name, lang):
    from qgis.PyQt.QtCore import QDate, QDateTime

    vals = _values(layer, name)
    if vals is None:
        return [(STOP, say({"ar": "الحقل «{f}» غير موجود.", "en": "The field “{f}” is not in the layer."}, lang, f=name))]
    days = set()
    bad = 0
    for v in vals:
        if v is None:
            continue
        if isinstance(v, QDateTime):
            d = v.date()
        elif isinstance(v, QDate):
            d = v
        else:
            d = QDate.fromString(str(v)[:10], "yyyy-MM-dd")
        if d is None or not d.isValid():
            bad += 1
        else:
            days.add(d.toJulianDay())
    out = []
    if not days:
        return [(STOP, say({"ar": "«{f}» لا يحتوي تواريخ مقروءة (المطلوب تاريخ أو نص مثل 2024-05-31).",
                            "en": "“{f}” holds no readable dates (a date, or text like 2024-05-31, is needed)."},
                           lang, f=name))]
    if bad:
        out.append((WARN, say({"ar": "{b} قيمة في «{f}» ليست تاريخًا مقروءًا.",
                               "en": "{b} values in “{f}” are not readable dates."}, lang, b=bad, f=name)))
    span = max(days) - min(days)
    if span < 10:
        out.append((WARN, say({"ar": "التواريخ تغطي {d} يوم فقط: قد لا تكفي لعشر فترات زمنية.",
                               "en": "The dates cover only {d} days: maybe too short for ten time steps."},
                              lang, d=span + 1)))
    else:
        out.append((OK, say({"ar": "التواريخ تغطي {d} يوم ({u} يوم مختلف).",
                             "en": "The dates cover {d} days ({u} different days)."}, lang, d=span + 1, u=len(days))))
    return out


def _category_checks(layer, name, lang):
    vals = _values(layer, name)
    if vals is None:
        return [(STOP, say({"ar": "الحقل «{f}» غير موجود.", "en": "The field “{f}” is not in the layer."}, lang, f=name))]
    counts = {}
    for v in vals:
        if v is not None and str(v).strip():
            counts[str(v)] = counts.get(str(v), 0) + 1
    if len(counts) < 2:
        return [(STOP, say({"ar": "«{f}» فيه أقل من فئتين؛ المطلوب فئتان على الأقل (أ و ب).",
                            "en": "“{f}” has fewer than two categories; two (A and B) are needed."}, lang, f=name))]
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    out = [(OK, say({"ar": "{k} فئة في «{f}»؛ الأكثر: {t}.", "en": "{k} categories in “{f}”; the largest: {t}."},
                    lang, k=len(counts), f=name, t=", ".join(f"{a} ({b})" for a, b in top[:3])))]
    small = [a for a, b in top if b < 10]
    if small:
        out.append((WARN, say({"ar": "{s} فئة فيها أقل من 10 نقاط؛ نتائجها ضعيفة.",
                               "en": "{s} categories have fewer than 10 points; their results will be weak."},
                              lang, s=len(small))))
    return out


def top_categories(layer, name, k=2):
    vals = _values(layer, name) or []
    counts = {}
    for v in vals:
        if v is not None and str(v).strip():
            counts[str(v)] = counts.get(str(v), 0) + 1
    return [a for a, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:k]]


def _duplicates(layer, limit=MAX_READ):
    from qgis.core import QgsFeatureRequest

    seen, dup, empty, multi = set(), 0, 0, 0
    req = QgsFeatureRequest().setNoAttributes().setLimit(limit)
    for f in layer.getFeatures(req):
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            empty += 1
            continue
        if g.isMultipart():
            multi += 1
        p = g.centroid().asPoint()
        key = (round(p.x(), 7), round(p.y(), 7))
        if key in seen:
            dup += 1
        else:
            seen.add(key)
    return dup, empty, multi


def inspect(layer, field=None, question_id=None, field2=None):
    """Look at the data before a tool runs. Returns [(level, message)], level 'ok' | 'warn' | 'stop'.
    field: the main value field; field2: the second field (a name, or a list of names for explanatory
    variables)."""
    from qgis.core import QgsVectorLayer

    lang = None
    q = question(question_id) if question_id else None
    out = []
    if layer is None or not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return [(STOP, say({"ar": "اختر طبقة متجهة (نقاط أو مضلعات).", "en": "Choose a vector layer (points or "
                                                                          "polygons)."}, lang))]
    n = layer.featureCount()
    kind = _geometry_kind(layer)
    need = q["geometry"] if q else "any"
    words = {"point": {"ar": "نقاط", "en": "points"}, "polygon": {"ar": "مضلعات", "en": "polygons"},
             "line": {"ar": "خطوط", "en": "lines"}, "none": {"ar": "بلا هندسة", "en": "no geometry"}}
    kind_w = say(words.get(kind, words["none"]), lang)
    if kind == "none":
        return [(STOP, say({"ar": "الطبقة بلا هندسة: التحليل المكاني يحتاج مواقع.",
                            "en": "The layer has no geometry: spatial analysis needs locations."}, lang))]
    if need == "point" and kind != "point":
        out.append((STOP, say({"ar": "هذا السؤال عن نقاط، والطبقة {k}. حوّلها إلى نقاط (المراكز) أو اختر سؤالًا "
                                     "عن القيم.",
                               "en": "This question is about points, and the layer has {k}. Turn them into points "
                                     "(centroids) or pick a question about values."}, lang, k=kind_w)))
    elif need == "polygon" and kind != "polygon":
        out.append((STOP, say({"ar": "هذا السؤال عن مناطق (مضلعات)، والطبقة {k}. اجمع النقاط في مناطق أولًا.",
                               "en": "This question is about areas (polygons), and the layer has {k}. Count the "
                                     "points into areas first."}, lang, k=kind_w)))
    elif kind == "line":
        out.append((WARN, say({"ar": "الطبقة خطوط: ستُستخدم نقطة منتصف كل خط.",
                               "en": "The layer has lines: the centre of each line will be used."}, lang)))
    # number of features
    stop_n = q["min_stop"] if q else 3
    warn_n = q["min_warn"] if q else 30
    if n == 0:
        return out + [(STOP, say({"ar": "الطبقة فارغة.", "en": "The layer is empty."}, lang))]
    if n < stop_n:
        out.append((STOP, say({"ar": "لديك {n} {k} فقط: هذا أقل من الحد الأدنى ({m}) للإجابة عن هذا السؤال.",
                               "en": "You have only {n} {k}: fewer than the minimum ({m}) to answer this question."},
                              lang, n=n, k=kind_w, m=stop_n)))
    elif n < warn_n:
        out.append((WARN, _few_words(question_id, n, kind_w, warn_n, lang)))
    else:
        out.append((OK, say({"ar": "{n} {k}: عدد كافٍ.", "en": "{n} {k}: enough to work with."}, lang, n=n,
                            k=kind_w)))
    # coordinate system
    crs = layer.crs()
    if not crs.isValid():
        out.append((WARN, say({"ar": "الطبقة بلا نظام إحداثيات: ستُقاس المسافات بوحدات الطبقة كما هي.",
                               "en": "The layer has no coordinate system: distances will be in its raw units."},
                              lang)))
    elif crs.isGeographic():
        out.append((OK, say({"ar": "الإحداثيات بالدرجات ({c}): الأداة ستقيس المسافات بالأمتار في نطاق UTM المحلي.",
                             "en": "Coordinates are in degrees ({c}): the tool will measure distances in metres in "
                                   "the local UTM zone."}, lang, c=crs.authid())))
    else:
        out.append((OK, say({"ar": "نظام الإحداثيات {c} مُسقط: المسافات صحيحة.",
                             "en": "Coordinate system {c} is projected: distances are fine."}, lang, c=crs.authid())))
    # geometry quality
    dup, empty, multi = _duplicates(layer)
    if empty:
        out.append((WARN, say({"ar": "{e} معلم بلا هندسة؛ سيُستبعد.", "en": "{e} features have no geometry; they "
                                                                         "will be left out."}, lang, e=empty)))
    if dup:
        point_q = q is not None and q["geometry"] == "point"
        out.append((WARN, say({"ar": "{d} معلم في موقع مكرر{x}.", "en": "{d} features sit on the same spot as "
                                                                       "another{x}."}, lang, d=dup,
                              x=say({"ar": "؛ أدوات أنماط النقاط تراها مسافة صفر - قد تكون أحداثًا متكررة "
                                           "(اجمعها في حقل عدد) أو تكرارًا بالخطأ",
                                     "en": "; point-pattern tools see a zero distance - repeated events (collect "
                                           "them into a count) or copies by mistake"}, lang) if point_q else "")))
    # fields
    if q is not None:
        f1, f2 = q["field"], q["field2"]
        if f1 == "numeric" and not field:
            out.append((STOP, say({"ar": "اختر الحقل الرقمي ({w}).", "en": "Choose the numeric field ({w})."}, lang,
                                  w=say(field_words(question_id)[0] or {"ar": "القيمة", "en": "value"}, lang))))
        if field and f1:
            out += _numeric_checks(field, _values(layer, field), n, lang,
                                   "count" if question_id == "rates" else "value")
        elif f1 == "numeric_optional" and question_id == "hotspots" and kind == "polygon":
            out.append((STOP, say({"ar": "لمضلعات اختر القيمة التي تبحث عن بؤرها (مثل عدد الحوادث).",
                                   "en": "For polygons choose the value whose hot spots you want (e.g. number of "
                                         "accidents)."}, lang)))
        if f2 == "numeric":
            if not field2:
                out.append((STOP, say({"ar": "اختر الحقل الثاني.", "en": "Choose the second field."}, lang)))
            else:
                out += _numeric_checks(field2, _values(layer, field2), n, lang,
                                       "count" if question_id == "rates" else "value")
                if question_id == "rates":
                    pops = _numbers(_values(layer, field2) or [])
                    if pops and min(pops) <= 0:
                        out.append((WARN, say({"ar": "بعض المناطق عدد سكانها صفر: لا يُحسب لها معدل.",
                                               "en": "Some areas have zero population: no rate can be computed "
                                                     "there."}, lang)))
                    if pops and min(pops) > 0 and max(pops) / min(pops) > 20:
                        out.append((WARN, say({"ar": "عدد السكان يتفاوت أكثر من 20 مرة بين المناطق: معدلات المناطق "
                                                     "الصغيرة غير مستقرة - التمهيد (Empirical Bayes) مفيد هنا.",
                                               "en": "Population varies more than 20-fold between areas: rates of "
                                                     "small areas are unstable - smoothing (Empirical Bayes) helps "
                                                     "here."}, lang)))
        elif f2 == "numeric_multi":
            names = [x for x in (field2 or []) if x and x != field]
            if not names:
                out.append((STOP, say({"ar": "اختر متغيرًا مفسِّرًا واحدًا على الأقل.",
                                       "en": "Choose at least one explanatory variable."}, lang)))
            for nm in names:
                out += [m for m in _numeric_checks(nm, _values(layer, nm), n, lang) if m[0] != OK]
            if names and n < 10 * len(names):
                out.append((WARN, say({"ar": "{n} معلم لـ{k} متغير: يُنصح بعشرة معالم على الأقل لكل متغير.",
                                       "en": "{n} features for {k} variables: about ten features per variable are "
                                             "advised."}, lang, n=n, k=len(names))))
            if names and n < 50:
                out.append((WARN, say({"ar": "أقل من 50 معلمًا: الانحدار الموزون جغرافيًا (GWR) سيكون ضعيفًا؛ "
                                             "اكتفِ بـ OLS.",
                                       "en": "Fewer than 50 features: geographically weighted regression (GWR) will "
                                             "be weak; stay with OLS."}, lang)))
        elif f2 == "date":
            out += _date_checks(layer, field2, lang) if field2 else [
                (STOP, say({"ar": "اختر حقل التاريخ.", "en": "Choose the date field."}, lang))]
        elif f2 == "category":
            out += _category_checks(layer, field2, lang) if field2 else [
                (STOP, say({"ar": "اختر حقل الفئة.", "en": "Choose the category field."}, lang))]
    out.append(_verdict(out, lang))
    return out


def _few_words(qid, n, kind_w, warn_n, lang):
    if qid == "similar":
        return say({"ar": "لديك {n} {k}: قوة Moran's I ستكون ضعيفة (قد لا يظهر نمط موجود). فكّر في بيانات أكثر "
                          "تفصيلًا أو اقرأ النتيجة بحذر.",
                    "en": "You have {n} {k}: Moran's I will have little power (a real pattern may not show). "
                          "Consider more detailed data, or read the result with care."}, lang, n=n, k=kind_w)
    if qid == "explain":
        return say({"ar": "لديك {n} {k}: الانحدار سيكون غير مؤكد؛ استخدم متغيرات قليلة.",
                    "en": "You have {n} {k}: the regression will be uncertain; use few variables."},
                   lang, n=n, k=kind_w)
    return say({"ar": "لديك {n} {k} فقط (يُفضّل {m} على الأقل): النتيجة ستكون غير مؤكدة.",
                "en": "You have only {n} {k} ({m} or more are better): the result will be uncertain."},
               lang, n=n, k=kind_w, m=warn_n)


def _verdict(found, lang):
    stops = sum(1 for lv, _ in found if lv == STOP)
    warns = sum(1 for lv, _ in found if lv == WARN)
    if stops:
        return (STOP, say({"ar": "لا يمكن التشغيل بعد: أصلح ما عليه علامة ✖ أولًا.",
                           "en": "Not ready yet: fix the items marked ✖ first."}, lang))
    if warns:
        return (WARN, say({"ar": "يمكن التشغيل، لكن انتبه للتحذيرات ({w}) عند قراءة النتيجة.",
                           "en": "Ready to run, but keep the {w} warnings in mind when reading the result."},
                          lang, w=warns))
    return (OK, say({"ar": "البيانات جاهزة.", "en": "The data are ready."}, lang))


def ready(found):
    return not any(lv == STOP for lv, _ in found)


# --------------------------------------------------------------- parameters ---

def parameters(alg_id, layer, field=None, field2=None, question_id=None):
    """Values to prefill in a tool's dialog, found from its parameter definitions (so it works for every
    tool): the first input layer, the value field(s), the date or category field, the explanatory list and
    two categories."""
    from qgis.core import (QgsApplication, QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                           QgsProcessingParameterString, QgsProcessingParameterVectorLayer)

    alg = QgsApplication.processingRegistry().algorithmById(alg_id)
    if alg is None or layer is None:
        return {}
    q = question(question_id) if question_id else None
    kind2 = q["field2"] if q else None
    params = {}
    defs = alg.parameterDefinitions()
    src = None
    for d in defs:
        if isinstance(d, (QgsProcessingParameterFeatureSource, QgsProcessingParameterVectorLayer)):
            src = d.name()
            params[src] = layer.id()
            break
    if src is None:
        return params
    numeric_values = [v for v in [field, field2 if kind2 == "numeric" else None] if v]
    cats = top_categories(layer, field2, 2) if kind2 == "category" and field2 else []
    for d in defs:
        if isinstance(d, QgsProcessingParameterField) and d.parentLayerParameterName() == src:
            nm = d.name().upper()
            if d.allowMultiple():
                if kind2 == "numeric_multi" and field2:
                    params[d.name()] = [x for x in field2 if x != field]
            elif kind2 == "date" and field2 and ("TIME" in nm or "DATE" in nm
                                                 or d.dataType() == _field_type("DateTime")):
                params[d.name()] = field2
            elif kind2 == "category" and field2 and ("CAT" in nm or "CLASS" in nm or "TYPE" in nm):
                params[d.name()] = field2
            elif d.dataType() == _field_type("Numeric") and numeric_values:
                if d.flags() & _optional_flag() and q is not None and q["field"] is None:
                    continue  # an optional weight the question did not ask for
                params[d.name()] = numeric_values.pop(0)
        elif isinstance(d, QgsProcessingParameterString) and cats:
            nm = d.name().upper()
            if "CAT" in nm and nm.endswith("_A"):
                params[d.name()] = cats[0]
            elif "CAT" in nm and nm.endswith("_B") and len(cats) > 1:
                params[d.name()] = cats[1]
    return params


def _field_type(name):
    from qgis.core import Qgis

    return getattr(Qgis.ProcessingFieldParameterDataType, name)


def _optional_flag():
    from qgis.core import Qgis

    return Qgis.ProcessingParameterFlag.Optional


_open = []  # dialogs kept alive


def open_tool(plugin, alg_id, params):
    """Open the tool's Processing dialog with the parameters filled in (not modal, like the ribbon does)."""
    import processing

    dlg = processing.createAlgorithmDialog(alg_id, params)
    if dlg is None:
        return None
    keep = getattr(getattr(plugin, "runner", None), "_dialogs", None)
    keep = keep if isinstance(keep, list) else _open
    keep.append(dlg)
    dlg.finished.connect(lambda *_: keep.remove(dlg) if dlg in keep else None)
    dlg.show()
    dlg.raise_()
    return dlg
