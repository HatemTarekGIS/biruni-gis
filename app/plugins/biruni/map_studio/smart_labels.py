"""Smarter labels: bilingual labels, priorities by importance, and a report of the labels that could not
be placed (and why to care)."""
from qgis.core import (Qgis, QgsLabelingEngineSettings, QgsMapRendererSequentialJob, QgsMapSettings,
                       QgsPalLayerSettings, QgsProperty, QgsTextBufferSettings, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QSizeF
from qgis.PyQt.QtGui import QColor, QFont

from .. import arabic_text
from ..i18n import tr

# importance words (English / Arabic) → rank 0..10
RANKS = [
    (10, ("capital", "national capital", "عاصمة", "العاصمة", "motorway", "freeway", "expressway", "طريق سريع",
          "اوتوستراد")),
    (8, ("city", "مدينة", "primary", "trunk", "highway", "رئيسي", "شرياني", "دائري")),
    (6, ("town", "بلدة", "محافظة", "secondary", "ثانوي", "تجميعي")),
    (4, ("village", "قرية", "مركز", "tertiary", "محلي")),
    (2, ("hamlet", "suburb", "neighbourhood", "neighborhood", "حي", "هجرة", "عزبة", "نجع", "residential",
         "service", "فرعي", "سكني", "خدمة")),
]


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _format(size=9.0, colour="#222222", font=None):
    fmt = QgsTextFormat()
    f = QFont(font or "Segoe UI")
    fmt.setFont(f)
    fmt.setSize(size)
    fmt.setColor(QColor(colour))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(0.8)
    buf.setColor(QColor("#ffffff"))
    fmt.setBuffer(buf)
    return fmt


def bilingual_expression(field_ar, field_en):
    """Arabic on the first line, English on the second; a missing one leaves a single line."""
    a, e = _quote(field_ar), _quote(field_en)
    return (f"trim(coalesce({a}, '') || CASE WHEN coalesce({a}, '') <> '' AND coalesce({e}, '') <> '' "
            f"THEN '\\n' ELSE '' END || coalesce({e}, ''))")


def bilingual(layer, field_ar, field_en, size=9.0, colour="#222222"):
    """Two-line labels (Arabic over English), centred; curved placements get the Arabic shaping fix."""
    for f in (field_ar, field_en):
        if layer.fields().indexOf(f) < 0:
            raise ValueError(tr("The layer has no field {f}.").format(f=f))
    old = layer.labeling()
    s = QgsPalLayerSettings(old.settings()) if old is not None and hasattr(old, "settings") else QgsPalLayerSettings()
    s.fieldName = bilingual_expression(field_ar, field_en)
    s.isExpression = True
    s.multilineAlign = Qgis.LabelMultiLineAlignment.Center
    if not s.format().font().family() or old is None:
        s.setFormat(_format(size, colour))
    if old is None and layer.geometryType() == Qgis.GeometryType.Line:
        s.placement = Qgis.LabelPlacement.Curved
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    changes = []
    try:
        from .. import script_labels

        changes = script_labels.fix_layer(layer) or []
    except Exception:  # noqa: BLE001 - shaping is a bonus
        pass
    layer.triggerRepaint()
    return changes


def rank_of(value, mapping=None):
    """Importance 0..10 of a value: from a mapping {value: rank}, a number, or importance words."""
    if value is None:
        return 0
    if mapping:
        if value in mapping:
            return mapping[value]
        if str(value) in mapping:
            return mapping[str(value)]
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    key = arabic_text.normalise(str(value))
    for rank, words in RANKS:
        for w in words:
            w = arabic_text.normalise(w)
            if w and (key == w or key.startswith(w + " ") or key.endswith(" " + w) or f" {w} " in f" {key} "):
                return rank
    return 1


def priorities(layer, field, mapping=None, base_size=None, step=0.35):
    """Label priority, size and z-order by importance (capitals before cities before villages; motorways before
    local streets). Numbers in the field are scaled to 0..10. Returns {value: rank}."""
    idx = layer.fields().indexOf(field)
    if idx < 0:
        raise ValueError(tr("The layer has no field {f}.").format(f=field))
    values = [v for v in layer.uniqueValues(idx) if v is not None and str(v) != "NULL"]
    ranks = {v: rank_of(v, mapping) for v in values}
    nums = [r for r in ranks.values() if isinstance(r, float)]
    if nums and len(nums) == len(ranks):
        lo, hi = min(nums), max(nums)
        span = (hi - lo) or 1.0
        ranks = {v: round(10.0 * (r - lo) / span) for v, r in ranks.items()}
    ranks = {v: int(max(0, min(10, round(r)))) for v, r in ranks.items()}
    cases = " ".join(
        f"WHEN {_quote(field)} = " + (repr(v) if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'")
        + f" THEN {r}" for v, r in ranks.items())
    rank_expr = f"CASE {cases} ELSE 1 END" if cases else "1"
    old = layer.labeling()
    s = QgsPalLayerSettings(old.settings()) if old is not None and hasattr(old, "settings") else QgsPalLayerSettings()
    if old is None:
        s.fieldName = field
        s.setFormat(_format())
    size = base_size or s.format().size() or 8.0
    props = s.dataDefinedProperties()
    props.setProperty(QgsPalLayerSettings.Property.Priority, QgsProperty.fromExpression(rank_expr))
    props.setProperty(QgsPalLayerSettings.Property.Size,
                      QgsProperty.fromExpression(f"{size} + ({rank_expr}) * {step}"))
    props.setProperty(QgsPalLayerSettings.Property.ZIndex, QgsProperty.fromExpression(rank_expr))
    s.setDataDefinedProperties(props)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)
    layer.triggerRepaint()
    return ranks


def _settings_from(target, layers=None, size=None):
    """QgsMapSettings from map settings, a layout map item, or a layout (its first map)."""
    from qgis.core import QgsLayout, QgsLayoutItemMap

    if isinstance(target, QgsMapSettings):
        ms = QgsMapSettings(target)
    else:
        item = target
        if isinstance(target, QgsLayout):
            maps = [i for i in target.items() if isinstance(i, QgsLayoutItemMap)]
            if not maps:
                raise ValueError(tr("The layout has no map."))
            item = maps[0]
        if not isinstance(item, QgsLayoutItemMap):
            raise ValueError(tr("Give map settings, a layout or a layout map."))
        ms = item.mapSettings(item.extent(), QSizeF(item.rect().width(), item.rect().height()), 150, True)
        if layers is None:
            layers = item.layersToRender() or item.layers()
    if layers is not None:
        ms.setLayers(layers)
    if size is not None:
        ms.setOutputSize(size)
    eng = QgsLabelingEngineSettings(ms.labelingEngineSettings())
    eng.setFlag(Qgis.LabelingFlag.CollectUnplacedLabels, True)
    eng.setFlag(Qgis.LabelingFlag.DrawUnplacedLabels, False)
    ms.setLabelingEngineSettings(eng)
    return ms


def hidden_labels_report(target, layers=None):
    """Which labels the engine could not place (crowding, collisions, too long for the feature).

    target: QgsMapSettings, a QgsLayoutItemMap or a QgsLayout. Returns a dict:
    {"placed": n, "hidden": n, "layers": {layer name: {"placed", "hidden", "examples": [text…]}}, "tips": [...]}"""
    ms = _settings_from(target, layers)
    job = QgsMapRendererSequentialJob(ms)
    job.start()
    job.waitForFinished()
    res = job.takeLabelingResults()
    report = {"placed": 0, "hidden": 0, "layers": {}, "tips": []}
    if res is None:
        return report
    from qgis.core import QgsProject

    for pos in res.allLabels():
        lyr = QgsProject.instance().mapLayer(pos.layerID)
        name = lyr.name() if isinstance(lyr, QgsVectorLayer) else pos.layerID
        entry = report["layers"].setdefault(name, {"placed": 0, "hidden": 0, "examples": []})
        if pos.isUnplaced:
            entry["hidden"] += 1
            report["hidden"] += 1
            if len(entry["examples"]) < 10:
                entry["examples"].append(pos.labelText)
        else:
            entry["placed"] += 1
            report["placed"] += 1
    total = report["placed"] + report["hidden"]
    if total and report["hidden"]:
        share = report["hidden"] / total
        report["tips"].append(tr("{p:.0%} of the labels are hidden for lack of room.").format(p=share))
        if share > 0.3:
            report["tips"].append(tr("Show fewer labels at this scale (scale-dependent labels) or label only the "
                                     "important features (Priorities)."))
        report["tips"].append(tr("Make the labels a little smaller, allow them to wrap on two lines, or group "
                                 "nearby equal names into one label (Group labels)."))
        report["tips"].append(tr("A bigger page or a larger scale gives labels more room."))
    return report


def report_text(report):
    lines = [tr("Labels placed: {p} · hidden: {h}").format(p=report["placed"], h=report["hidden"])]
    for name, e in report["layers"].items():
        if e["hidden"]:
            ex = "، ".join(str(x) for x in e["examples"][:5])
            lines.append(tr("{layer}: {h} hidden (e.g. {ex})").format(layer=name, h=e["hidden"], ex=ex))
    lines.extend(report["tips"])
    return "\n".join(lines)
