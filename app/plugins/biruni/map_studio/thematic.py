"""Thematic maps that fit the data («عايز توضّح إيه؟»).

analyse() tells what a field holds (categories, counts, rates, values around zero, plain measurements),
recommend() ranks the map types that suit it - with warnings for the classic mistakes (raw counts painted on
areas, too many categories) - and apply() styles the layer, or builds a companion layer (dots, hexagons,
flow lines) or a small-multiples layout. Every change is undoable with undo(layer)."""
import math
import random
import statistics

from qgis.core import (Qgis, QgsCategorizedSymbolRenderer, QgsClassificationEqualInterval, QgsClassificationJenks,
                       QgsClassificationQuantile, QgsClassificationStandardDeviation, QgsColorRampTransformer,
                       QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDataDefinedSizeLegend, QgsExpression,
                       QgsFeature, QgsFeatureRequest, QgsField, QgsFillSymbol, QgsGeometry, QgsGraduatedSymbolRenderer,
                       QgsHeatmapRenderer, QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutPoint,
                       QgsLayoutSize, QgsLineSymbol, QgsMarkerSymbol, QgsPointXY, QgsPrintLayout, QgsProject,
                       QgsProperty, QgsReadWriteContext, QgsRendererCategory, QgsRendererRange, QgsRuleBasedRenderer,
                       QgsSingleSymbolRenderer, QgsSizeScaleTransformer, QgsSymbol, QgsSymbolLayer, QgsUnitTypes,
                       QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtXml import QDomDocument

from ..i18n import format_number, tr
from . import themes

BACKUP_KEY = "biruni/thematic-backup"
MAX_CATEGORIES = 12
EQUAL_AREA = QgsCoordinateReferenceSystem("EPSG:6933")

COUNT_WORDS = ("count", "pop", "population", "number", "num", "total", "qty", "quantity", "cases", "units", "عدد",
               "سكان", "تعداد", "إجمالي", "اجمالي", "كمية")
RATE_WORDS = ("rate", "ratio", "pct", "percent", "share", "density", "per", "avg", "mean", "index", "نسبة", "معدل",
              "كثافة", "متوسط", "مؤشر", "%")
TIME_WORDS = ("year", "date", "month", "سنة", "عام", "تاريخ", "شهر")

DIVERGING = ["#2166AC", "#67A9CF", "#D1E5F0", "#F7F7F7", "#FDDBC7", "#EF8A62", "#B2182B"]
# Joshua Stevens' 3×3 bivariate palette (first variable → pink, second → blue)
BIVARIATE = {(1, 1): "#E8E8E8", (2, 1): "#E4ACAC", (3, 1): "#C85A5A",
             (1, 2): "#B0D5DF", (2, 2): "#AD9EA5", (3, 2): "#985356",
             (1, 3): "#64ACBE", (2, 3): "#627F8C", (3, 3): "#574249"}


def type_labels():
    return {"categorized": tr("Categories in different colours"),
            "choropleth": tr("Shaded areas by class (choropleth)"),
            "unclassed": tr("Smooth colour scale (no classes)"),
            "diverging": tr("Two colours around zero (diverging)"),
            "proportional": tr("Proportional circles"),
            "dot_density": tr("Dot density"),
            "bivariate": tr("Two variables together (bivariate)"),
            "hexbin": tr("Hexagon bins"),
            "heatmap": tr("Heat map"),
            "flow": tr("Flow lines"),
            "value_alpha": tr("Value by transparency"),
            "small_multiples": tr("Small multiples (one map per value)")}


def kind_labels():
    return {"qualitative": tr("Categories (names, types)"), "count": tr("Counts (how many)"),
            "rate": tr("Rates and ratios"), "diverging": tr("Values above and below zero"),
            "continuous": tr("Measured values"), "empty": tr("No values")}


# ---- analysis -----------------------------------------------------------------------------------------------
def _values(layer, field):
    idx = layer.fields().indexOf(field)
    if idx < 0:
        raise ValueError(tr("The field {f} is not in the layer.").format(f=field))
    out = []
    for f in layer.getFeatures(QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry)
                               .setSubsetOfAttributes([idx])):
        v = f.attribute(idx)
        if v is None or (hasattr(v, "isNull") and v.isNull()):
            continue
        out.append(v)
    return out


def _numeric(layer, field):
    return layer.fields().field(field).isNumeric()


def _skewness(xs):
    if len(xs) < 3:
        return 0.0
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) / len(xs)


def _has_word(name, words):
    n = name.lower()
    return any(w in n for w in words)


def analyse(layer, field):
    """{kind, n, distinct, min, max, mean, median, std, skew, integers, geometry}."""
    vals = _values(layer, field)
    info = {"field": field, "n": len(vals), "distinct": len(set(map(str, vals))),
            "geometry": QgsWkbTypes.geometryDisplayString(layer.geometryType()) if layer.isSpatial() else "none"}
    if not vals:
        info["kind"] = "empty"
        return info
    if not _numeric(layer, field):
        info["kind"] = "qualitative"
        info["categories"] = _category_counts(vals)
        return info
    xs = [float(v) for v in vals]
    info.update(min=min(xs), max=max(xs), mean=statistics.fmean(xs), median=statistics.median(xs),
                std=statistics.pstdev(xs), skew=_skewness(xs), integers=all(float(x).is_integer() for x in xs))
    alias = layer.attributeDisplayName(layer.fields().indexOf(field))
    name = f"{field} {alias}"
    if info["min"] < 0 < info["max"]:
        kind = "diverging"
    elif _has_word(name, RATE_WORDS):
        kind = "rate"
    elif _has_word(name, COUNT_WORDS) or (info["integers"] and info["min"] >= 0 and info["distinct"] > 12):
        kind = "count"
    elif info["integers"] and info["distinct"] <= 7:
        kind = "qualitative"
        info["categories"] = _category_counts(vals)
    elif 0 <= info["min"] and info["max"] <= 1:
        kind = "rate"
    else:
        kind = "continuous"
    info["kind"] = kind
    return info


def _category_counts(vals):
    counts = {}
    for v in vals:
        counts[str(v)] = counts.get(str(v), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def recommend(layer, field, second_field=None):
    """[{type, label, score, reason, warnings}] best first."""
    info = analyse(layer, field)
    kind = info["kind"]
    geom = layer.geometryType()
    poly, point, line = (geom == Qgis.GeometryType.Polygon, geom == Qgis.GeometryType.Point,
                         geom == Qgis.GeometryType.Line)
    labels = type_labels()
    out = []

    def add(t, score, reason, warnings=()):
        out.append({"type": t, "label": labels[t], "score": score, "reason": reason, "warnings": list(warnings)})

    if kind == "empty":
        return out
    if kind == "qualitative":
        warn = []
        if info["distinct"] > MAX_CATEGORIES:
            warn.append(tr("{n} categories are too many to tell apart: the rare ones will be grouped as «Other».")
                        .format(n=info["distinct"]))
        add("categorized", 100, tr("The field holds names or types: one clear colour per category."), warn)
        if point:
            add("heatmap", 40, tr("Shows where the points crowd together, whatever their type."))
        if _has_word(field, TIME_WORDS) or info["distinct"] <= 12:
            add("small_multiples", 35, tr("One small map per value makes each group easy to compare."))
        out.sort(key=lambda r: -r["score"])
        return out

    count_warn = [tr("These are raw counts: big areas look important just because they are big. Divide by the "
                     "area (per km²) or by another field (per person, per household) before shading areas.")]
    if kind == "count":
        add("proportional", 95, tr("Counts are best shown as sizes: a circle twice as big means twice as many."))
        if poly:
            add("dot_density", 80, tr("One dot per so many: shows both amount and where it is."))
            add("choropleth", 40, tr("Only after dividing by area or population."), count_warn)
        if point:
            add("hexbin", 75, tr("Adds up the values in equal hexagons: no point hides another."))
            add("heatmap", 70, tr("Shows where the values pile up."))
        if line:
            add("flow", 85, tr("Line width shows how much moves along each line."))
    elif kind in ("rate", "continuous"):
        if poly:
            add("choropleth", 95, tr("Rates and densities are best shown as shaded areas."))
            add("unclassed", 70, tr("A smooth scale keeps every small difference."))
        else:
            add("proportional", 75 if kind == "continuous" else 60,
                tr("Sizes show the values on points and lines."))
            add("choropleth", 70, tr("Colours by class on the features themselves."))
        if point:
            add("hexbin", 50, tr("Adds up the values in equal hexagons: no point hides another."))
        if line:
            add("flow", 60, tr("Line width shows the value of each line."))
    elif kind == "diverging":
        add("diverging", 100, tr("Values go above and below zero: two colours meet at zero so gains and losses "
                                 "are told apart."))
        add("proportional", 45, tr("Circle sizes for the amount; colour for the sign."))
    if second_field:
        add("bivariate", 65, tr("Shows two variables on one map with a 3 × 3 colour grid."))
        add("value_alpha", 55, tr("The second field fades features that matter less (e.g. few people)."))
    if any(_has_word(f.name(), TIME_WORDS) for f in layer.fields()):
        add("small_multiples", 50, tr("A time field was found: one map per year or month shows the change."))
    out.sort(key=lambda r: -r["score"])
    return out


# ---- class breaks -------------------------------------------------------------------------------------------
def method_for(skew):
    """(method id, reason) from the shape of the distribution."""
    if abs(skew) < 0.5:
        return "equal", tr("The values are spread evenly, so classes of equal width are easiest to read.")
    if abs(skew) < 1.5:
        return "jenks", tr("The values gather in groups, so natural breaks follow the gaps in the data.")
    return "quantile", tr("Most values are small with a few very large ones, so each class gets the same number "
                          "of features.")


def _method(mid):
    return {"equal": QgsClassificationEqualInterval, "jenks": QgsClassificationJenks,
            "quantile": QgsClassificationQuantile, "stddev": QgsClassificationStandardDeviation}[mid]()


def nice_breaks(breaks, lo, hi):
    """Round inner class breaks to round numbers (1, 2, 2.5, 5 × 10^k) keeping them strictly increasing; the
    outer bounds become the round numbers just outside lo/hi. Returns (bounds list of n+1, unit)."""
    k = max(len(breaks) - 1, 1)
    span = (hi - lo) or abs(hi) or 1.0
    base = 10 ** math.floor(math.log10(span / k))
    for unit in (base, base / 2, base / 4, base / 5, base / 10, base / 20, base / 100):
        inner = [round(b / unit) * unit for b in breaks[1:-1]]
        lower = math.floor(lo / unit) * unit
        upper = math.ceil(hi / unit) * unit
        seq = [round(x, 10) for x in [lower] + inner + [upper]]  # no 0.013000000000000001
        if all(a < b for a, b in zip(seq, seq[1:])):
            return seq, unit
    return list(breaks), 0.0


def _decimals(unit):
    if unit <= 0 or unit >= 1:
        return 0
    return min(6, math.ceil(-math.log10(unit) - 1e-9))


def class_label(lower, upper, count, decimals):
    return "{a}–{b} ({n})".format(a=format_number(lower, decimals), b=format_number(upper, decimals),
                                  n=format_number(count, 0))


# ---- undo ---------------------------------------------------------------------------------------------------
def _renderer_xml(layer):
    doc = QDomDocument()
    el = layer.renderer().save(doc, QgsReadWriteContext())
    doc.appendChild(el)
    return doc.toString()


def _backup(layer):
    """Keep the style the layer had before the first thematic change (later changes keep the original)."""
    if layer.customProperty(BACKUP_KEY):
        return
    import json

    layer.setCustomProperty(BACKUP_KEY, json.dumps({"renderer": _renderer_xml(layer), "opacity": layer.opacity(),
                                                    "companions": [], "virtual": []}))


def _note(layer, key, value):
    import json

    data = json.loads(layer.customProperty(BACKUP_KEY) or "{}")
    data.setdefault(key, []).append(value)
    layer.setCustomProperty(BACKUP_KEY, json.dumps(data))


def has_backup(layer):
    return bool(layer.customProperty(BACKUP_KEY))


def undo(layer, project=None):
    """Put the original style back and remove generated layers and fields. True when something was undone."""
    import json

    raw = layer.customProperty(BACKUP_KEY)
    if not raw:
        return False
    project = project or QgsProject.instance()
    data = json.loads(raw)
    doc = QDomDocument()
    doc.setContent(data["renderer"])
    renderer = QgsFeatureRendererLoad(doc.documentElement())
    if renderer is not None:
        layer.setRenderer(renderer)
    layer.setOpacity(data.get("opacity", 1.0))
    for lid in data.get("companions", []):
        if project.mapLayer(lid) is not None:
            project.removeMapLayer(lid)
    for name in data.get("virtual", []):
        idx = layer.fields().indexOf(name)
        if idx >= 0 and layer.fields().fieldOrigin(idx) == QgsFields_OriginExpression():
            layer.removeExpressionField(idx)
    layer.removeCustomProperty(BACKUP_KEY)
    layer.triggerRepaint()
    return True


def QgsFeatureRendererLoad(element):  # noqa: N802 - thin wrapper, keeps the import local
    from qgis.core import QgsFeatureRenderer

    return QgsFeatureRenderer.load(element, QgsReadWriteContext())


def QgsFields_OriginExpression():  # noqa: N802
    from qgis.core import QgsFields

    return QgsFields.FieldOrigin.OriginExpression


# ---- normalisation ------------------------------------------------------------------------------------------
def normalise(layer, field, by="area", out_name=None):
    """Add a virtual field: the value per km² (by='area', true ellipsoid-free equal-area size) or per another
    field (by=<field name>). Returns the new field name."""
    _backup(layer)
    if by == "area":
        crs = layer.crs().authid() or "EPSG:4326"
        expr = (f'"{field}" / nullif(area(transform($geometry, \'{crs}\', \'EPSG:6933\')) / 1000000.0, 0)')
        name = out_name or f"{field}_per_km2"[:60]
    else:
        expr = f'"{field}" / nullif("{by}", 0)'
        name = out_name or f"{field}_per_{by}"[:60]
    if layer.fields().indexOf(name) < 0:
        layer.addExpressionField(expr, QgsField(name, QMetaType.Type.Double))
        _note(layer, "virtual", name)
    return name


# ---- renderers ----------------------------------------------------------------------------------------------
def _base_symbol(layer, color):
    geom = layer.geometryType()
    if geom == Qgis.GeometryType.Polygon:
        return QgsFillSymbol.createSimple({"color": QColor(color).name(), "outline_color": "#FFFFFF",
                                           "outline_width": "0.2"})
    if geom == Qgis.GeometryType.Line:
        return QgsLineSymbol.createSimple({"color": QColor(color).name(), "width": "0.8"})
    return QgsMarkerSymbol.createSimple({"name": "circle", "color": QColor(color).name(), "outline_color": "#FFFFFF",
                                         "outline_width": "0.3", "size": "2.6"})


def _count_in(layer, field, lower, upper, last):
    expr = f'"{field}" >= {lower!r} AND "{field}" {"<=" if last else "<"} {upper!r}'
    return sum(1 for _ in layer.getFeatures(QgsFeatureRequest(QgsExpression(expr))
                                            .setFlags(QgsFeatureRequest.Flag.NoGeometry).setNoAttributes()))


def _categorized(layer, field, options):
    info = analyse(layer, field)
    counts = info.get("categories") or _category_counts(_values(layer, field))
    keys = list(counts)
    other = keys[MAX_CATEGORIES - 1:] if len(keys) > MAX_CATEGORIES else []
    shown = keys[:MAX_CATEGORIES - 1] if other else keys
    colors = themes.safe_qualitative(len(shown))
    cats = []
    numeric = _numeric(layer, field)
    for key, color in zip(shown, colors):
        value = layer.fields().field(field).convertCompatible(key) if numeric else key
        cats.append(QgsRendererCategory(value, _base_symbol(layer, color), f"{key} ({counts[key]})"))
    if other:
        n = sum(counts[k] for k in other)
        cats.append(QgsRendererCategory(None, _base_symbol(layer, "#BDBDBD"),
                                        tr("Other ({n})").format(n=n)))
    r = QgsCategorizedSymbolRenderer(field, cats)
    layer.setRenderer(r)
    return tr("{n} categories{o}.").format(n=len(shown),
                                          o=tr(", {k} rare ones grouped as «Other»").format(k=len(other))
                                          if other else "")


def _graduated(layer, field, options, colors=None, bounds=None):
    info = analyse(layer, field)
    n = int(options.get("classes", 5))
    method_id = options.get("method") or method_for(info.get("skew", 0))[0]
    r = QgsGraduatedSymbolRenderer(field)
    r.setClassificationMethod(_method(method_id))
    r.setSourceSymbol(_base_symbol(layer, "#888888"))
    r.updateClasses(layer, n)
    raw = [r.ranges()[0].lowerValue()] + [rg.upperValue() for rg in r.ranges()]
    if bounds is None:
        bounds, unit = nice_breaks(raw, info["min"], info["max"])
    else:
        unit = options.get("unit", 0)
    dec = _decimals(unit) if unit else 2
    n = len(bounds) - 1
    colors = colors or themes.ramp_colors(options.get("palette", "ylgnbu"), n)
    ranges = []
    for i in range(n):
        lo, hi = bounds[i], bounds[i + 1]
        cnt = _count_in(layer, field, lo, hi, i == n - 1)
        ranges.append(QgsRendererRange(lo, hi, _base_symbol(layer, colors[i]), class_label(lo, hi, cnt, dec)))
    g = QgsGraduatedSymbolRenderer(field, ranges)
    g.setClassificationMethod(_method(method_id))
    layer.setRenderer(g)
    return tr("{n} classes ({m}) with round breaks.").format(n=n, m=method_id)


def _unclassed(layer, field, options):
    info = analyse(layer, field)
    sym = _base_symbol(layer, "#888888")
    prop_key = {Qgis.GeometryType.Line: QgsSymbolLayer.Property.StrokeColor}.get(layer.geometryType(),
                                                                                 QgsSymbolLayer.Property.FillColor)
    prop = QgsProperty.fromField(field)
    prop.setTransformer(QgsColorRampTransformer(info["min"], info["max"],
                                                themes.ramp(options.get("palette", "viridis")), QColor("#DDDDDD")))
    sym.symbolLayer(0).setDataDefinedProperty(prop_key, prop)
    layer.setRenderer(QgsSingleSymbolRenderer(sym))
    return tr("Colour follows the value from {a} to {b}.").format(a=format_number(info["min"]),
                                                                  b=format_number(info["max"]))


def _diverging(layer, field, options):
    info = analyse(layer, field)
    mid = float(options.get("midpoint", 0.0))
    reach = max(abs(info["max"] - mid), abs(info["min"] - mid)) or 1.0
    per_side = int(options.get("classes", 6)) // 2 or 1
    step_raw = reach / per_side
    base = 10 ** math.floor(math.log10(step_raw))
    step = next(base * m for m in (1, 2, 2.5, 5, 10) if base * m >= step_raw)
    bounds = [mid + step * i for i in range(-per_side, per_side + 1)]
    pal = DIVERGING if per_side == 3 else _diverging_colors(per_side)
    return _graduated(layer, field, dict(options, unit=step), colors=pal[:2 * per_side], bounds=bounds) \
        and tr("Two colours meet at {m}; each class is {s} wide.").format(m=format_number(mid, _decimals(step)),
                                                                          s=format_number(step, _decimals(step)))


def _diverging_colors(per_side):
    blue = themes.ramp(["#2166AC", "#D1E5F0"])
    red = themes.ramp(["#FDDBC7", "#B2182B"])
    left = [blue.color(i / max(per_side - 1, 1)).name() for i in range(per_side)]
    right = [red.color(i / max(per_side - 1, 1)).name() for i in range(per_side)]
    return left + right


def _proportional(layer, field, options):
    info = analyse(layer, field)
    max_size = float(options.get("max_size", 14.0))
    min_size = float(options.get("min_size", 1.0))
    color = options.get("color", "#D55E00")
    marker = QgsMarkerSymbol.createSimple({"name": "circle", "color": QColor(color).name(),
                                           "outline_color": "#FFFFFF", "outline_width": "0.3"})
    marker.setOpacity(0.85)
    t = QgsSizeScaleTransformer(QgsSizeScaleTransformer.ScaleType.Area, 0.0, max(info["max"], 1e-12),
                                min_size, max_size, 0.0, 0.5)
    prop = QgsProperty.fromField(field)
    prop.setTransformer(t)
    marker.setDataDefinedSize(prop)
    if layer.geometryType() == Qgis.GeometryType.Point:
        sym = marker
    else:
        from qgis.core import QgsCentroidFillSymbolLayer, QgsSimpleFillSymbolLayer

        sym = QgsFillSymbol()
        sym.changeSymbolLayer(0, QgsSimpleFillSymbolLayer(QColor("#F2F2F2"), strokeColor=QColor("#BDBDBD"),
                                                          strokeWidth=0.2))
        centroid = QgsCentroidFillSymbolLayer()
        centroid.setSubSymbol(marker)
        centroid.setPointOnSurface(True)
        sym.appendSymbolLayer(centroid)
    r = QgsSingleSymbolRenderer(sym)
    if layer.geometryType() == Qgis.GeometryType.Point:
        legend = QgsDataDefinedSizeLegend()
        legend.setLegendType(QgsDataDefinedSizeLegend.LegendType.LegendCollapsed)
        r.setDataDefinedSizeLegend(legend)
    r.setOrderBy(QgsFeatureRequest.OrderBy([QgsFeatureRequest.OrderByClause(field, False)]))
    r.setOrderByEnabled(True)  # big circles first, small ones drawn on top
    layer.setRenderer(r)
    return tr("Circle area grows with the value (largest {v}); big circles are drawn under small ones.") \
        .format(v=format_number(info["max"], 0))


def _bivariate(layer, field, options):
    second = options.get("second_field")
    if not second:
        raise ValueError(tr("Choose the second field for a bivariate map."))
    qa = _tertiles(layer, field)
    qb = _tertiles(layer, second)

    def klass(f, q):
        return f'CASE WHEN "{f}" <= {q[0]!r} THEN 1 WHEN "{f}" <= {q[1]!r} THEN 2 ELSE 3 END'

    expr = f"concat({klass(field, qa)}, '-', {klass(second, qb)})"
    words = {1: tr("low"), 2: tr("medium"), 3: tr("high")}
    cats = []
    for (a, b), color in sorted(BIVARIATE.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        label = f"{field}: {words[a]} · {second}: {words[b]}"
        cats.append(QgsRendererCategory(f"{a}-{b}", _base_symbol(layer, color), label))
    layer.setRenderer(QgsCategorizedSymbolRenderer(expr, cats))
    return tr("3 × 3 colours: {a} goes from grey to pink, {b} from grey to blue.").format(a=field, b=second)


def _tertiles(layer, field):
    xs = sorted(float(v) for v in _values(layer, field))
    if not xs:
        return (0.0, 0.0)
    return (xs[int(len(xs) / 3)], xs[int(2 * len(xs) / 3)])


def _value_alpha(layer, field, options):
    second = options.get("second_field")
    if not second:
        raise ValueError(tr("Choose the field that sets the transparency."))
    msg = _graduated(layer, field, options)
    vals = [float(v) for v in _values(layer, second)]
    lo, hi = (min(vals), max(vals)) if vals else (0, 1)
    alpha = f"scale_linear(\"{second}\", {lo!r}, {hi!r}, 40, 255)"
    prop_key = {Qgis.GeometryType.Line: QgsSymbolLayer.Property.StrokeColor}.get(layer.geometryType(),
                                                                                 QgsSymbolLayer.Property.FillColor)
    r = layer.renderer()
    for i, rg in enumerate(r.ranges()):
        sym = rg.symbol().clone()
        sym.symbolLayer(0).setDataDefinedProperty(
            prop_key, QgsProperty.fromExpression(f"set_color_part(@symbol_color, 'alpha', {alpha})"))
        r.updateRangeSymbol(i, sym)
    return msg + " " + tr("Features with a low {f} fade out.").format(f=second)


def _heatmap(layer, field, options):
    r = QgsHeatmapRenderer()
    r.setRadius(float(options.get("radius", 12)))
    r.setRadiusUnit(Qgis.RenderUnit.Millimeters)
    if field and _numeric(layer, field):
        r.setWeightExpression(f'"{field}"')
    r.setColorRamp(themes.ramp(options.get("palette", "magma")))
    layer.setRenderer(r)
    return tr("Heat map, radius {r} mm.").format(r=format_number(r.radius(), 0))


# ---- generated layers -------------------------------------------------------------------------------------------
def _add_companion(layer, new_layer, project):
    project = project or QgsProject.instance()
    project.addMapLayer(new_layer)
    _note(layer, "companions", new_layer.id())
    return new_layer


def dot_density(layer, field, options, project=None):
    """A points layer with one dot per `per_dot` of the value inside each polygon (random but repeatable)."""
    if layer.geometryType() != Qgis.GeometryType.Polygon:
        raise ValueError(tr("Dot density needs a polygon layer."))
    info = analyse(layer, field)
    total = sum(max(float(v), 0) for v in _values(layer, field))
    per_dot = float(options.get("per_dot") or 0)
    if per_dot <= 0:  # about 5000 dots in all, rounded to a round number
        raw = max(total / 5000.0, 1e-9)
        base = 10 ** math.floor(math.log10(raw))
        per_dot = next(base * m for m in (1, 2, 2.5, 5, 10) if base * m >= raw)
    out = QgsVectorLayer(f"Point?crs={layer.crs().authid()}", tr("{l}: 1 dot = {n} {f}").format(
        l=layer.name(), n=format_number(per_dot, _decimals(per_dot)), f=field), "memory")
    feats = []
    for f in layer.getFeatures():
        v = f[field]
        if v is None or f.geometry().isEmpty():
            continue
        n = int(round(max(float(v), 0) / per_dot))
        if n <= 0:
            continue
        for p in f.geometry().randomPointsInPolygon(n, seed=int(f.id()) + 1):
            nf = QgsFeature()
            nf.setGeometry(QgsGeometry.fromPointXY(p))
            feats.append(nf)
    out.dataProvider().addFeatures(feats)
    out.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
        {"name": "circle", "color": options.get("color", "#0072B2"), "outline_style": "no", "size": "0.8"})))
    _backup(layer)
    layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
        {"color": "#F7F7F7", "outline_color": "#9E9E9E", "outline_width": "0.2"})))
    _add_companion(layer, out, project)
    return out, tr("{n} dots, 1 dot = {v}.").format(n=format_number(len(feats), 0),
                                                    v=format_number(per_dot, _decimals(per_dot)))


def hexbin(layer, field, options, project=None):
    """Equal hexagons over the points with their count (and the sum/mean of `field`), shaded by class."""
    if layer.geometryType() != Qgis.GeometryType.Point:
        raise ValueError(tr("Hexagon bins need a point layer."))
    import processing

    ext = layer.extent()
    size = float(options.get("size") or 0)
    if size <= 0:
        size = max(ext.width(), ext.height()) / 25.0 or 1.0
    grid = processing.run("native:creategrid", {
        "TYPE": 4, "EXTENT": ext.buffered(size), "HSPACING": size, "VSPACING": size * math.sqrt(3) / 2,
        "CRS": layer.crs(), "OUTPUT": "memory:"})["OUTPUT"]
    stat = options.get("stat", "count")
    params = {"POLYGONS": grid, "POINTS": layer, "FIELD": "n", "OUTPUT": "memory:"}
    if field and _numeric(layer, field) and stat == "sum":
        params["WEIGHT"] = field
    counted = processing.run("native:countpointsinpolygon", params)["OUTPUT"]
    out = QgsVectorLayer(f"Polygon?crs={layer.crs().authid()}&field=n:double", tr("{l}: hexagons").format(
        l=layer.name()), "memory")
    feats = []
    for f in counted.getFeatures():
        if (f["n"] or 0) > 0:
            nf = QgsFeature(out.fields())
            nf.setGeometry(f.geometry())
            nf.setAttributes([float(f["n"])])
            feats.append(nf)
    out.dataProvider().addFeatures(feats)
    out.updateExtents()
    _backup(layer)
    _add_companion(layer, out, project)
    _graduated(out, "n", dict(options, palette=options.get("palette", "magma")))
    return out, tr("{n} hexagons with data.").format(n=len(feats))


def flow_lines(layer, field, options, project=None):
    """Line width by value. For a line layer it styles the layer; with an origin/destination table it first
    draws curved lines between the matching points."""
    od = options.get("od_layer")
    if od is None:
        if layer.geometryType() != Qgis.GeometryType.Line:
            raise ValueError(tr("Flow lines need a line layer, or a table of origins and destinations."))
        _width_by_value(layer, field, options)
        return layer, tr("Line width follows {f}.").format(f=field)
    id_field, frm, to = options["id_field"], options["from_field"], options["to_field"]
    pts = {str(f[id_field]): f.geometry().centroid().asPoint() for f in layer.getFeatures()}
    out = QgsVectorLayer(f"LineString?crs={layer.crs().authid()}&field=from:string&field=to:string"
                         f"&field=value:double", tr("{l}: flows").format(l=layer.name()), "memory")
    feats = []
    for f in od.getFeatures():
        a, b = pts.get(str(f[frm])), pts.get(str(f[to]))
        v = f[field]
        if a is None or b is None or v is None or a == b:
            continue
        nf = QgsFeature(out.fields())
        nf.setGeometry(QgsGeometry.fromPolylineXY(_arc(a, b)))
        nf.setAttributes([str(f[frm]), str(f[to]), float(v)])
        feats.append(nf)
    out.dataProvider().addFeatures(feats)
    _backup(layer)
    _add_companion(layer, out, project)
    _width_by_value(out, "value", options)
    return out, tr("{n} flows drawn.").format(n=len(feats))


def _arc(a, b, bend=0.2, steps=24):
    mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
    dx, dy = b.x() - a.x(), b.y() - a.y()
    cx, cy = mx - dy * bend, my + dx * bend  # control point to the left: A→B and B→A don't overlap
    out = []
    for i in range(steps + 1):
        t = i / steps
        out.append(QgsPointXY((1 - t) ** 2 * a.x() + 2 * (1 - t) * t * cx + t * t * b.x(),
                              (1 - t) ** 2 * a.y() + 2 * (1 - t) * t * cy + t * t * b.y()))
    return out


def _width_by_value(layer, field, options):
    vals = [float(v) for v in _values(layer, field)]
    hi = max(vals) if vals else 1.0
    sym = QgsLineSymbol.createSimple({"color": options.get("color", "#0072B2"), "capstyle": "round"})
    sym.setOpacity(0.8)
    prop = QgsProperty.fromField(field)
    prop.setTransformer(QgsSizeScaleTransformer(QgsSizeScaleTransformer.ScaleType.Linear, 0.0, hi or 1.0,
                                                0.2, float(options.get("max_width", 5.0)), 0.0, 1.0))
    sym.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.Property.StrokeWidth, prop)
    r = QgsSingleSymbolRenderer(sym)
    r.setOrderBy(QgsFeatureRequest.OrderBy([QgsFeatureRequest.OrderByClause(field, False)]))
    r.setOrderByEnabled(True)
    _backup(layer)
    layer.setRenderer(r)


def small_multiples(layer, field, options, project=None):
    """A print layout with one small map per value of `field` (at most 12), each showing only its features,
    with a shared legend. Returns (layout, message)."""
    project = project or QgsProject.instance()
    values = list(_category_counts(_values(layer, field)))
    try:
        values.sort(key=float)
    except ValueError:
        values.sort()
    values = values[:12]
    if not values:
        raise ValueError(tr("The field has no values."))
    clone = layer.clone()
    clone.setName(tr("{l} by {f}").format(l=layer.name(), f=field))
    # keep the layer's own style, shown only for the value of each map (@map_id = the map item's id)
    inner = QgsRuleBasedRenderer.convertFromRenderer(layer.renderer(), layer)
    if inner is None:
        inner = QgsRuleBasedRenderer(QgsSymbol.defaultSymbol(layer.geometryType()))
    root = QgsRuleBasedRenderer.Rule(None)
    gate = QgsRuleBasedRenderer.Rule(None, 0, 0, f'to_string("{field}") = @map_id')
    for child in inner.rootRule().children():
        gate.appendChild(child.clone())
    root.appendChild(gate)
    clone.setRenderer(QgsRuleBasedRenderer(root))
    project.addMapLayer(clone, False)
    _backup(layer)
    _note(layer, "companions", clone.id())
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    name = options.get("title") or tr("{l} by {f}").format(l=layer.name(), f=field)
    layout.setName(_unique_layout_name(project, name))
    from qgis.core import QgsLayoutItemPage

    layout.pageCollection().page(0).setPageSize("A4", QgsLayoutItemPage.Orientation.Landscape)
    w, h = 297.0, 210.0
    n = len(values)
    cols = math.ceil(math.sqrt(n * w / h))
    rows = math.ceil(n / cols)
    margin, top, gap = 10.0, 22.0, 4.0
    cell_w = (w - 2 * margin - (cols - 1) * gap) / cols
    cell_h = (h - top - margin - (rows - 1) * gap) / rows
    title = QgsLayoutItemLabel(layout)
    title.setText(name)
    font = title.textFormat()
    font.setSize(16)
    title.setTextFormat(font)
    title.attemptMove(QgsLayoutPoint(margin, 6, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    title.attemptResize(QgsLayoutSize(w - 2 * margin, 12, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
    layout.addLayoutItem(title)
    extent = layer.extent()
    for i, value in enumerate(values):
        r, c = divmod(i, cols)
        x, y = margin + c * (cell_w + gap), top + r * (cell_h + gap)
        m = QgsLayoutItemMap(layout)
        m.setId(str(value))
        m.attemptMove(QgsLayoutPoint(x, y + 6, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
        m.attemptResize(QgsLayoutSize(cell_w, cell_h - 6, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
        m.setLayers([clone])
        m.setKeepLayerSet(True)
        m.setFrameEnabled(True)
        m.zoomToExtent(extent.buffered(max(extent.width(), extent.height()) * 0.03))  # setExtent resizes the item
        layout.addLayoutItem(m)
        cap = QgsLayoutItemLabel(layout)
        cap.setText(str(value))
        cap.attemptMove(QgsLayoutPoint(x, y, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
        cap.attemptResize(QgsLayoutSize(cell_w, 6, QgsUnitTypes.LayoutUnit.LayoutMillimeters))
        layout.addLayoutItem(cap)
    project.layoutManager().addLayout(layout)
    return layout, tr("{n} maps, one per {f}.").format(n=n, f=field)


def _unique_layout_name(project, name):
    names = {lay.name() for lay in project.layoutManager().layouts()}
    out, k = name, 2
    while out in names:
        out, k = f"{name} ({k})", k + 1
    return out


# ---- apply ----------------------------------------------------------------------------------------------------
STYLERS = {"categorized": _categorized, "choropleth": _graduated, "unclassed": _unclassed, "diverging": _diverging,
           "proportional": _proportional, "bivariate": _bivariate, "value_alpha": _value_alpha, "heatmap": _heatmap}
BUILDERS = {"dot_density": dot_density, "hexbin": hexbin, "flow": flow_lines, "small_multiples": small_multiples}


def apply(layer, field, map_type, options=None, project=None):
    """Style the layer (or build its companion layer / layout). Returns {message, layer, layout}."""
    options = dict(options or {})
    _backup(layer)
    if map_type in STYLERS:
        if map_type == "heatmap" and layer.geometryType() != Qgis.GeometryType.Point:
            raise ValueError(tr("A heat map needs a point layer."))
        msg = STYLERS[map_type](layer, field, options)
        layer.triggerRepaint()
        return {"message": msg, "layer": layer, "layout": None}
    if map_type in BUILDERS:
        obj, msg = BUILDERS[map_type](layer, field, options, project=project)
        layer.triggerRepaint()
        if map_type == "small_multiples":
            return {"message": msg, "layer": layer, "layout": obj}
        return {"message": msg, "layer": obj, "layout": None}
    raise ValueError(map_type)
