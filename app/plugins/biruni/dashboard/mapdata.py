"""Map widgets with several styled layers (SPEC.md, Changes: "map v2").

A map's definition may list its layers in ``options.layers``::

    [{"source": "s1", "name": "Parcels", "where": "<expression>",
      "style": {"kind": "single|categories|graduated|layer", "field": "status", "palette": ["#..", ...],
                "classes": 5, "color": "#..", "outline": "#..", "opacity": 0.75, "width": 2, "size": 6},
      "label": "parcel_no", "popup": ["status", "area"]}, ...]

and gets ``{"layers": [{"id", "name", "kind": "fill|line|point", "geojson", "legend", "style", "label", "popup",
"total"}], "bbox", "total", "basemap"}``. Every feature carries ``_c`` (its colour, worked out here so the phone
draws exactly what the PC decided - for "layer", the colour QGIS itself gives it with the layer's own
renderer), ``_l`` (its label text), ``_k`` ({field: group key} for the fields other widgets group by - the
page's cross-filter) and the popup fields. The card's zone and filters cut every layer; a hidden field never
appears as a label, a popup field, a key or a colour (a renderer or a style that reads one falls back to a
single colour, which says nothing about it).

A map without ``options.layers`` keeps the first shape (``{"geojson", "bbox", "total"}``) - old dashboards and
their pages work as before.
"""
import json
import math

from qgis.core import (Qgis, QgsCoordinateTransform, QgsCsException, QgsExpressionContextUtils, QgsFeatureRequest,
                       QgsGeometry, QgsRectangle, QgsRenderContext)

from .data import MAX_MAP, NO_GEOM, POINT, WGS84, DataError, clean, key_of

PALETTE = ("#2F80ED", "#F2994A", "#27AE60", "#EB5757", "#9B51E0", "#F2C94C", "#2D9CDB", "#BB6BD9", "#219653",
           "#E2711D", "#56CCF2", "#828282")
RAMP = ("#FFF3C4", "#F7B267", "#E2711D", "#A4161A")
OTHER = "#9AA5B1"
MAX_CATS = 12
MAX_POPUP = 12
MAX_LABEL = 80
KINDS = ("single", "categories", "graduated", "layer")


def layers_of(d, w):
    """The map's layer entries (each with a source of this dashboard), or None for a first-shape map."""
    ls = (w.get("options") or {}).get("layers")
    if not isinstance(ls, list) or not ls:
        return None
    sources = d.get("sources") or {}
    return [x for x in ls if isinstance(x, dict) and x.get("source") in sources]


def fields_needed(d, sid):
    """Fields the map layers of a dashboard read from source ``sid`` (style, label, popup)."""
    need = set()
    for w in d.get("widgets") or []:
        if w.get("type") != "map":
            continue
        for L in layers_of(d, w) or ():
            if L.get("source") != sid:
                continue
            st = L.get("style") or {}
            if st.get("field"):
                need.add(st["field"])
            if L.get("label"):
                need.add(L["label"])
            need |= {f for f in (L.get("popup") or []) if isinstance(f, str)}
    return need


def xf_fields(d, sid):
    """The fields other widgets of source ``sid`` group by (bars, pies, leaderboards, progress, filters): the
    keys a feature carries for the page's cross-filter."""
    out = []
    for w in d.get("widgets") or []:
        g = w.get("group") or {}
        if w.get("source") == sid and g.get("field") and not g.get("time") and g["field"] not in out:
            out.append(g["field"])
    for f in d.get("filters") or []:
        if f.get("source") == sid and f.get("field") and f["field"] not in out:
            out.append(f["field"])
    return out


def chart_xf(w, hide):
    """The field a chart's items are keyed by, for the cross-filter (None for time or zone groups)."""
    g = w.get("group") or {}
    f = g.get("field")
    if not f or g.get("time") or g.get("zones") or f in hide:
        return None
    return f


def _hex(c):
    return c if isinstance(c, str) and c.startswith("#") and len(c) in (4, 7, 9) else None


def _palette(st):
    p = [c for c in (st.get("palette") or []) if _hex(c)]
    return p or list(PALETTE)


def _lerp(a, b, t):
    a, b = a.lstrip("#"), b.lstrip("#")
    if len(a) == 3:
        a = "".join(x * 2 for x in a)
    if len(b) == 3:
        b = "".join(x * 2 for x in b)
    ca = [int(a[i:i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def ramp(colors, n):
    """n colours along a ramp of 2 or more colours."""
    colors = [c for c in colors if _hex(c)] or list(RAMP)
    if len(colors) == 1:
        colors = [colors[0], colors[0]]
    if n <= 1:
        return [colors[-1]]
    out = []
    for i in range(n):
        t = i / (n - 1) * (len(colors) - 1)
        j = min(int(t), len(colors) - 2)
        out.append(_lerp(colors[j], colors[j + 1], t - j))
    return out


def breaks(values, n):
    """Quantile class breaks: n+1 edges (fewer when values repeat)."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return []
    edges = [v[0]]
    for i in range(1, n):
        q = v[min(len(v) - 1, int(round(i * len(v) / n)))]
        if q > edges[-1]:
            edges.append(q)
    if v[-1] > edges[-1] or len(edges) == 1:
        edges.append(v[-1])
    return edges


def _kind(lyr):
    gt = lyr.geometryType()
    if gt == POINT:
        return "point"
    if gt == Qgis.GeometryType.Line:
        return "line"
    return "fill"


def _num_text(v):
    if v is None:
        return "–"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:.4g}"


class Renderer:
    """Colours and legend of a layer's own QGIS renderer (categorized, graduated, rule-based, single...)."""

    def __init__(self, lyr, project):
        self.ren = None
        r = lyr.renderer()
        if r is None:
            return
        self.ren = r.clone()
        self.ctx = QgsRenderContext()
        self.ctx.expressionContext().appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(lyr))
        self.lyr = lyr

    def used(self):
        if self.ren is None:
            return set()
        try:
            return set(self.ren.usedAttributes(self.ctx))
        except Exception:  # noqa: BLE001
            return set()

    def colours(self, fids):
        """{fid: (fill hex, outline hex|None)} for the features drawn by the renderer."""
        out = {}
        if self.ren is None or not fids:
            return out
        try:
            self.ren.startRender(self.ctx, self.lyr.fields())
        except Exception:  # noqa: BLE001
            return out
        try:
            req = QgsFeatureRequest().setFilterFids(list(fids))
            used = self.used()
            if used and "*" not in used and not self.ren.filterNeedsGeometry():
                req.setFlags(NO_GEOM)
                req.setSubsetOfAttributes(sorted(used), self.lyr.fields())
            for f in self.lyr.getFeatures(req):
                self.ctx.expressionContext().setFeature(f)
                try:
                    syms = self.ren.originalSymbolsForFeature(f, self.ctx)
                except Exception:  # noqa: BLE001
                    syms = []
                if not syms:
                    continue
                s = syms[0]
                stroke = None
                try:
                    sl = s.symbolLayer(0)
                    if hasattr(sl, "strokeColor") and sl.strokeColor().alpha() > 0:
                        stroke = sl.strokeColor().name()
                except Exception:  # noqa: BLE001
                    pass
                out[f.id()] = (s.color().name(), stroke)
        finally:
            self.ren.stopRender(self.ctx)
        return out

    def legend(self):
        out = []
        if self.ren is None:
            return out
        try:
            items = self.ren.legendSymbolItems()
        except Exception:  # noqa: BLE001
            return out
        for it in items[:MAX_CATS * 2]:
            s = it.symbol()
            if s is None:
                continue
            out.append({"label": str(it.label() or "")[:MAX_LABEL], "color": s.color().name()})
        return out


class Geometries:
    """GeoJSON geometries in EPSG:4326, simplified once per layer, kept until an edit touches them."""

    def __init__(self, project):
        self.project = project
        self.cache = {}   # layer id -> {fid: (geometry dict, (xmin, ymin, xmax, ymax))}
        self.tol = {}

    def invalidate(self, layer_id=None, fids=None):
        if layer_id is None:
            self.cache.clear()
            self.tol.clear()
        elif fids is None:
            self.cache.pop(layer_id, None)
            self.tol.pop(layer_id, None)
        else:
            c = self.cache.get(layer_id)
            if c:
                for f in fids:
                    c.pop(f, None)

    def _tolerance(self, lyr, tr):
        t = self.tol.get(lyr.id())
        if t is None:
            e = lyr.extent()
            if tr is not None:
                try:
                    e = tr.transformBoundingBox(e)
                except QgsCsException:
                    e = QgsRectangle()
            t = math.hypot(e.width(), e.height()) / 6000.0 if not e.isEmpty() else 0.0
            self.tol[lyr.id()] = t
        return t

    def get(self, lyr, fids):
        c = self.cache.setdefault(lyr.id(), {})
        missing = [f for f in fids if f not in c]
        if missing:
            tr = None
            if lyr.crs().isValid() and lyr.crs() != WGS84:
                tr = QgsCoordinateTransform(lyr.crs(), WGS84, self.project)
            tol = self._tolerance(lyr, tr)
            point = lyr.geometryType() == POINT
            req = QgsFeatureRequest().setFilterFids(missing).setNoAttributes()
            for f in lyr.getFeatures(req):
                g = QgsGeometry(f.geometry())
                if g.isEmpty():
                    c[f.id()] = None
                    continue
                if tr is not None:
                    try:
                        g.transform(tr)
                    except QgsCsException:
                        c[f.id()] = None
                        continue
                if tol and not point:
                    s = g.simplify(tol)
                    if not s.isEmpty():
                        g = s
                try:
                    geo = json.loads(g.asJson(6))
                except ValueError:
                    c[f.id()] = None
                    continue
                b = g.boundingBox()
                c[f.id()] = (geo, (b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()))
            for f in missing:
                c.setdefault(f, None)
        return {f: c.get(f) for f in fids}


def compute(comp, w, card, eff, k, hide):
    """The data of a map with ``options.layers`` (see the module's docstring)."""
    d = comp.d
    entries = layers_of(d, w) or []
    if not entries:
        raise DataError("the map has no layers")
    geo = comp.geometries()
    out_layers, box, total = [], None, 0
    for n, L in enumerate(entries[:8]):
        sid = L["source"]
        lyr = comp.layer(sid)
        r = comp.rows(sid)
        idx = comp.selection(sid, card, eff, where=L.get("where") or None)
        lid = f"L{n}"
        name = str(L.get("name") or lyr.name())[:MAX_LABEL]
        kind = _kind(lyr)
        if k and len(idx) < k:
            out_layers.append({"id": lid, "name": name, "kind": kind, "hidden": True})
            continue
        total += len(idx)
        st = L.get("style") or {}
        skind = st.get("kind") if st.get("kind") in KINDS else "single"
        field = st.get("field") if st.get("field") in r.cols and st.get("field") not in hide else None
        label = L.get("label") if L.get("label") in r.cols and L.get("label") not in hide else None
        popup = [f for f in (L.get("popup") or []) if isinstance(f, str) and f in r.cols and f not in hide]
        popup = popup[:MAX_POPUP]
        xfs = [f for f in xf_fields(d, sid) if f in r.cols and f not in hide]
        chosen = sorted(idx, key=lambda i: r.fids[i])
        if len(chosen) > MAX_MAP:
            step = len(chosen) / MAX_MAP
            chosen = [chosen[int(j * step)] for j in range(MAX_MAP)]
        base = _hex(st.get("color")) or PALETTE[n % len(PALETTE)]
        colour = {}
        outline_of = {}
        legend = []
        if skind == "layer":
            ren = Renderer(lyr, comp.project)
            used = ren.used()
            if ren.ren is None or "*" in used or (used & hide):
                skind = "single"
            else:
                got = ren.colours([r.fids[i] for i in chosen])
                for fid, (fc, oc) in got.items():
                    colour[fid] = fc
                    if oc:
                        outline_of[fid] = oc
                legend = ren.legend()
        if skind == "categories" and field:
            keys = comp.keys(r, field)
            counts = {}
            for i in idx:
                counts[keys[i]] = counts.get(keys[i], 0) + 1
            order = sorted(counts, key=lambda x: (-counts[x], x))
            pal = _palette(st)
            fixed = st.get("colors") if isinstance(st.get("colors"), dict) else {}
            cats = {}
            for j, key in enumerate(order):
                if j < MAX_CATS:
                    cats[key] = _hex(fixed.get(key)) or pal[j % len(pal)]
            for i in chosen:
                colour[r.fids[i]] = cats.get(keys[i], OTHER)
            legend = [{"label": key or "—", "color": cats[key], "key": key, "count": counts[key]} for key in cats]
            if len(order) > MAX_CATS:
                legend.append({"label": "…", "color": OTHER})
        elif skind == "graduated" and field:
            nums = comp.numbers(r, field)
            try:
                nclass = max(2, min(9, int(st.get("classes") or 5)))
            except (TypeError, ValueError):
                nclass = 5
            edges = breaks([nums[i] for i in idx], nclass)
            cols = ramp(st.get("palette") or RAMP, max(1, len(edges) - 1))
            for i in chosen:
                v = nums[i]
                if v is None or not edges:
                    colour[r.fids[i]] = OTHER
                    continue
                j = 0
                while j < len(edges) - 2 and v > edges[j + 1]:
                    j += 1
                colour[r.fids[i]] = cols[min(j, len(cols) - 1)]
            legend = [{"label": f"{_num_text(edges[j])} – {_num_text(edges[j + 1])}", "color": cols[j]}
                      for j in range(len(edges) - 1)]
        elif skind != "layer":
            skind = "single"
        if skind == "single":
            legend = [{"label": name, "color": base}]
        fids = [r.fids[i] for i in chosen]
        geoms = geo.get(lyr, fids)
        lab = r.cols.get(label) if label else None
        kcols = {f: comp.keys(r, f) for f in xfs}
        feats = []
        for i in chosen:
            fid = r.fids[i]
            gg = geoms.get(fid)
            if gg is None:
                continue
            g, b = gg
            p = {"id": fid, "_c": colour.get(fid, base)}
            if fid in outline_of:
                p["_o"] = outline_of[fid]
            if lab is not None:
                v = clean(lab[i])
                if v is not None and v != "":
                    p["_l"] = str(v)[:MAX_LABEL]
            if kcols:
                p["_k"] = {f: kcols[f][i] for f in kcols}
            for f in popup:
                p[f] = clean(r.cols[f][i])
            feats.append({"type": "Feature", "id": fid, "geometry": g, "properties": p})
            box = list(b) if box is None else [min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]),
                                                max(box[3], b[3])]
        fl = lyr.fields()
        style = {"color": base, "opacity": _clamp(st.get("opacity"), 0.05, 1.0, 0.72 if kind == "fill" else 1.0),
                 "outline": _hex(st.get("outline")), "width": _clamp(st.get("width"), 0.5, 12, 2.0 if kind == "line"
                                                                     else 1.0),
                 "size": _clamp(st.get("size"), 2, 30, 6)}
        out_layers.append({
            "id": lid, "name": name, "kind": kind, "style": style, "legend": legend[:MAX_CATS + 1],
            "label": bool(label), "total": len(idx),
            "popup": [{"key": f, "label": fl.field(fl.indexOf(f)).displayName()} for f in popup],
            "keys": xfs,
            "geojson": {"type": "FeatureCollection", "features": feats}})
    if box is not None:
        box = [round(x, 6) for x in box]
    o = w.get("options") or {}
    return {"layers": out_layers, "bbox": box, "total": total, "basemap": o.get("basemap") == "project"}


def _clamp(v, lo, hi, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return max(lo, min(hi, x))


def explain(comp, w, key, card, eff, k, hide):
    """What is behind one feature of a map: "L<n>:<fid>"."""
    try:
        ln, fid = str(key).split(":", 1)
        n, fid = int(ln.lstrip("L")), int(fid)
    except (TypeError, ValueError):
        raise DataError("no such feature") from None
    entries = layers_of(comp.d, w) or []
    if not 0 <= n < len(entries):
        raise DataError("no such feature")
    L = entries[n]
    sid = L["source"]
    idx = comp.selection(sid, card, eff, where=L.get("where") or None)
    r = comp.rows(sid)
    if k and len(idx) < k:
        return {"t": "explain", "widget": w["id"], "key": key, "count": 0, "hidden": True, "features": [],
                "bbox": None, "more": False}
    i = r.index.get(fid)
    if i is None or i not in set(idx):
        raise DataError("no such feature")
    lyr = comp.layer(sid)
    label = L.get("label") if L.get("label") in r.cols and L.get("label") not in hide else None
    by, when = comp._history(lyr)(fid)
    g = comp.geometries().get(lyr, [fid]).get(fid)
    bbox = [round(x, 6) for x in g[1]] if g else None
    fl = lyr.fields()
    fields = []
    for f in (L.get("popup") or [])[:MAX_POPUP]:
        if isinstance(f, str) and f in r.cols and f not in hide:
            fields.append({"label": fl.field(fl.indexOf(f)).displayName(), "value": clean(r.cols[f][i])})
    return {"t": "explain", "widget": w["id"], "key": key, "count": 1, "bbox": bbox, "more": False,
            "features": [{"id": fid, "label": str(clean(r.cols[label][i])) if label else f"#{fid}", "by": by,
                          "when": when, "fields": fields}]}
