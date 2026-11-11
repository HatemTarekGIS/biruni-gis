"""A short written reading of a map, built from the data with plain rules (no AI): where the highest and lowest
values are, the average, where the values concentrate, how many places stand out, and which category leads.

caption(layer, field, name_field=None, lang="ar") → text;  add_caption(layout, text, x, y, w) → label item."""
import math
import statistics

from qgis.core import QgsFeatureRequest, QgsLayoutPoint, QgsLayoutSize

_T = {
    "ar": {
        "max": "أعلى قيمة لـ«{f}» في {n} ({v})، وأقلها في {m} ({w}).",
        "mean": "المتوسط {a} على {k} عنصر.",
        "conc": "القيم الأعلى متركّزة في {q} المنطقة.",
        "spread": "القيم موزّعة بشكل متقارب على المنطقة.",
        "out": "{c} من العناصر أعلى من المتوسط بفرق كبير (أكثر من انحراف معياري).",
        "none_out": "مفيش عناصر شاذة بشكل واضح عن المتوسط.",
        "cat": "أكثر فئة في «{f}» هي «{c}» ({p}٪ من العناصر).",
        "q": {"N": "شمال", "S": "جنوب", "E": "شرق", "W": "غرب", "NE": "شمال شرق", "NW": "شمال غرب",
              "SE": "جنوب شرق", "SW": "جنوب غرب"},
        "item": "العنصر {i}",
    },
    "en": {
        "max": "The highest «{f}» is in {n} ({v}) and the lowest in {m} ({w}).",
        "mean": "The average is {a} over {k} features.",
        "conc": "Higher values concentrate in the {q} of the area.",
        "spread": "Values are spread fairly evenly over the area.",
        "out": "{c} features stand well above the average (more than one standard deviation).",
        "none_out": "No feature stands clearly apart from the average.",
        "cat": "The most common «{f}» is «{c}» ({p}% of the features).",
        "q": {"N": "north", "S": "south", "E": "east", "W": "west", "NE": "north-east", "NW": "north-west",
              "SE": "south-east", "SW": "south-west"},
        "item": "feature {i}",
    },
}


def _fmt(v):
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _guess_name_field(layer):
    names = [f.name() for f in layer.fields()]
    for cand in ("name_ar", "name", "NAME", "Name", "اسم", "الاسم", "district", "city", "label"):
        if cand in names:
            return cand
    for f in layer.fields():
        if not f.isNumeric() and not f.isDateOrTime():
            return f.name()
    return None


def _direction(dx, dy, threshold):
    ns = "N" if dy > threshold else "S" if dy < -threshold else ""
    ew = "E" if dx > threshold else "W" if dx < -threshold else ""
    return ns + ew


def caption(layer, field, name_field=None, lang="ar"):
    t = _T["ar" if lang == "ar" else "en"]
    name_field = name_field or _guess_name_field(layer)
    rows = []
    for f in layer.getFeatures(QgsFeatureRequest()):
        v = f[field]
        g = f.geometry()
        c = g.centroid().asPoint() if g and not g.isEmpty() else None
        label = str(f[name_field]) if name_field and f[name_field] not in (None, "") else t["item"].format(i=f.id())
        rows.append((v, c, label))
    if not rows:
        return ""
    numeric = []
    for v, c, label in rows:
        try:
            numeric.append((float(v), c, label))
        except (TypeError, ValueError):
            pass
    if len(numeric) < max(2, len(rows) // 2):          # mostly text → categorical reading
        cats = {}
        for v, _c, _l in rows:
            if v not in (None, ""):
                cats[str(v)] = cats.get(str(v), 0) + 1
        if not cats:
            return ""
        top, n = max(cats.items(), key=lambda kv: kv[1])
        return t["cat"].format(f=field, c=top, p=round(100 * n / sum(cats.values())))
    vals = [r[0] for r in numeric]
    hi = max(numeric, key=lambda r: r[0])
    lo = min(numeric, key=lambda r: r[0])
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    parts = [t["max"].format(f=field, n=hi[2], v=_fmt(hi[0]), m=lo[2], w=_fmt(lo[0])),
             t["mean"].format(a=_fmt(mean), k=len(vals))]
    pts = [(v, c) for v, c, _l in numeric if c is not None]
    if pts and sum(v for v, _c in pts if v > 0) > 0:
        cx = statistics.fmean(c.x() for _v, c in pts)
        cy = statistics.fmean(c.y() for _v, c in pts)
        w = [max(v, 0) for v, _c in pts]
        wx = sum(wi * c.x() for wi, (_v, c) in zip(w, pts)) / sum(w)
        wy = sum(wi * c.y() for wi, (_v, c) in zip(w, pts)) / sum(w)
        span = max(max(c.x() for _v, c in pts) - min(c.x() for _v, c in pts),
                   max(c.y() for _v, c in pts) - min(c.y() for _v, c in pts)) or 1
        d = _direction(wx - cx, wy - cy, 0.05 * span)
        parts.append(t["conc"].format(q=t["q"][d]) if d else t["spread"])
    outliers = sum(1 for v in vals if sd > 0 and v > mean + sd)
    parts.append(t["out"].format(c=outliers) if outliers else t["none_out"])
    return " ".join(parts)


def add_caption(layout, text, x, y, width, size=8.0, lang="ar"):
    from qgis.core import QgsLayoutItemLabel
    from qgis.PyQt.QtCore import Qt

    from .auto_layout import _fmt as text_format

    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    lab.setTextFormat(text_format(size))
    lab.setHAlign(Qt.AlignmentFlag.AlignRight if lang in ("ar", "fa", "ur", "he") else Qt.AlignmentFlag.AlignLeft)
    layout.addLayoutItem(lab)
    lines = max(1, math.ceil(len(text) * size * 0.18 / max(width, 1)))
    lab.attemptMove(QgsLayoutPoint(x, y))
    lab.attemptResize(QgsLayoutSize(width, lines * size * 0.45 + 2))
    lab.setId("biruni-caption")
    return lab
