"""The totals of an area, proved without the data: how many features, how much area (or length), the sum of the
numeric fields, against the code of the delivery certificate.

The certificate's sum tree (core.build(sums=True)) gives every square, beside its fingerprint and the box its
features reach, its sums: the count, the amount (area or length, from the coordinates as fingerprinted), the totals
of the numeric fields. A proof keeps closed every square whose box is all inside the area (it adds its sums, and
shows nothing of its features) and every square whose box is all outside it; only the squares on the edge are opened,
to the features: the ones outside show their sums sealed, the ones inside their numbers, and only the ones whose own
box crosses the edge show their shape. Nothing is left out: the sums of all the squares make the root in the code.

A feature crossing the edge is counted apart, its amount not added: the area holds the features all inside it.
"""
import datetime as dt

from qgis.core import QgsCoordinateReferenceSystem, QgsFeatureRequest, QgsGeometry, QgsRectangle, QgsUnitTypes, QgsWkbTypes

from . import core

KIND = "biruni-totals"


class TotalsError(Exception):
    pass


class _Area:
    """The area as the proof and its check both see it: in, out or on the edge, within a tolerance."""

    def __init__(self, area, tol, step):
        if area is None or area.isNull() or area.isEmpty() or area.type() != QgsWkbTypes.PolygonGeometry:
            raise core.FingerprintError("area")
        self.step = step
        self.inner = area.buffer(-tol, 8) if tol > 0 else QgsGeometry(area)
        if self.inner.isNull() or self.inner.isEmpty():
            raise core.FingerprintError("small")
        self.outer = area.buffer(tol, 8) if tol > 0 else QgsGeometry(area)
        self.ei = QgsGeometry.createGeometryEngine(self.inner.constGet())
        self.ei.prepareGeometry()
        self.eo = QgsGeometry.createGeometryEngine(self.outer.constGet())
        self.eo.prepareGeometry()

    def box(self, b):
        """"in": all of the box is in the area, "out": none of it, "edge": it crosses the edge."""
        if b is None:
            return "out"
        s = self.step
        rg = QgsGeometry.fromRect(QgsRectangle((b[0] - 1) * s, (b[1] - 1) * s, (b[2] + 1) * s, (b[3] + 1) * s))
        if self.eo.contains(rg.constGet()):
            return "in"
        if not self.ei.intersects(rg.constGet()):
            return "out"
        return "edge"

    def shape(self, g):
        if g is None or g.isNull() or g.isEmpty():
            return "out"
        if self.eo.contains(g.constGet()):
            return "in"
        if not self.ei.intersects(g.constGet()):
            return "out"
        return "edge"


def _what(layer):
    kind = layer.geometryType()
    if kind == QgsWkbTypes.PolygonGeometry:
        return "area"
    if kind == QgsWkbTypes.LineGeometry:
        return "length"
    return ""


def _unit(crs):
    c = QgsCoordinateReferenceSystem(crs)
    if c.isGeographic():
        return "m"
    return QgsUnitTypes.toAbbreviatedString(c.mapUnits()) or "m"


def prove_totals(layer, area, tolerance, cert=None, index=0, feedback=None):
    """The proof of the totals of ``layer`` in ``area`` (a polygon in the layer's coordinates). With ``cert`` the
    layer must still be what that certificate says (its layer ``index``)."""
    if cert is not None:
        entry = cert["layers"][index]
        if not entry.get("sums"):
            raise core.FingerprintError("old certificate")
        t = core.tree_for(layer, entry, feedback)
        if t.root != entry["root"] or t.sums != entry["sums"]:
            raise core.FingerprintError("changed")
        layers = [core.code_part(e) for e in cert["layers"]]
        made, name = cert.get("made"), entry["name"]
    else:
        t = core.build(layer, feedback=feedback, commit=True, sums=True)
        name, index, made = layer.name(), 0, None
        layers = [core.code_part({"name": name, "root": t.root, "reach": t.reach, "commit": t.commit,
                                  "sums": t.sums})]
    ar = _Area(area, tolerance, t.step)
    edge = {}           # leaf key -> fids whose own box crosses the edge

    def find(k):
        if ar.box(t.sum_nodes[k][1]) != "edge":
            return
        if k[0] == 0:
            edge[k[1:]] = {i[4] for i in t.sitems[k[1:]] if ar.box(i[2]) == "edge"}
            return
        for c in t.kids[k]:
            find(c)

    for k in t.top():
        find(k)
    want = [fid for fids in edge.values() for fid in fids]
    feats = {f.id(): f for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(want))} if want else {}

    def item(i, shown):
        sh, vh, b, sv, fid = i
        out = {"s": sh, "v": vh, "b": b, "h": core.sums_hash(sv)}
        if ar.box(b) != "out":
            out["n"] = sv
        if fid in shown:
            g = feats[fid].geometry()
            out["g"] = bytes(g.asWkb()).hex()
        return out

    def node(k):
        h, b, s = t.sum_nodes[k]
        if ar.box(b) != "edge":
            return {"k": list(k), "h": h, "b": b, "s": s}
        if k[0] == 0:
            return {"k": list(k), "s": s, "items": [item(i, edge[k[1:]]) for i in t.sitems[k[1:]]]}
        return {"k": list(k), "kids": [node(c) for c in sorted(t.kids[k])]}

    return {"format": core.FORMAT, "kind": KIND,
            "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "layer": name, "settings": t.settings(), "features": t.count,
            "sum_fields": t.sum_fields, "measure": _what(layer), "unit": _unit(t.crs),
            "certificate": {"code": core.code(core.layer_codes(layers)), "made": made, "layers": layers,
                            "index": index},
            "area": {"wkt": area.asWkt(), "tolerance": tolerance},
            "tree": [node(k) for k in t.top()]}


class Result:
    def __init__(self):
        self.ok = False
        self.why = ""
        self.code = ""
        self.count = 0           # features all inside the area
        self.amount = 0.0        # their area (or length), in the layer's unit
        self.fields = {}         # field -> total of the features inside
        self.crossing = 0        # features crossing the edge, not counted
        self.crossing_amount = 0.0
        self.opened = 0          # smallest squares opened
        self.closed = 0          # squares kept closed
        self.shown = 0           # features whose shape is in the proof
        self.measure = ""
        self.unit = ""
        self.area = None
        self.text = ""


def _ints(v, w):
    if not isinstance(v, list) or len(v) != w or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
        raise TotalsError("format")
    return v


def _box(b):
    if b is None:
        return None
    if not isinstance(b, list) or len(b) != 4 or not all(isinstance(x, int) for x in b):
        raise TotalsError("format")
    return b


def verify_totals(pr):
    """Check a totals proof from what it holds alone."""
    r = Result()
    try:
        if pr.get("kind") != KIND:
            raise TotalsError("format")
        cert = pr["certificate"]
        layers = cert["layers"]
        r.code = core.code(core.layer_codes(layers))
        if r.code != cert["code"]:
            raise TotalsError("code")
        root = layers[cert["index"]].get("sums")
        if not root:
            raise TotalsError("format")
        st = pr["settings"]
        step, crs = float(st["step"]), st["crs"]
        geographic = QgsCoordinateReferenceSystem(crs).isGeographic()
        names = list(pr["sum_fields"])
        w = 2 + len(names)
        area = QgsGeometry.fromWkt(pr["area"]["wkt"])
        try:
            ar = _Area(area, float(pr["area"]["tolerance"]), step)
        except core.FingerprintError as e:
            raise TotalsError(str(e)) from e
        r.area = area
        inside, crossing = [0] * w, [0] * w

        def add(acc, s):
            for i, x in enumerate(s):
                acc[i] += x

        def walk(n, level):
            k = tuple(n["k"])
            if len(k) != 3 or k[0] != level:
                raise TotalsError("format")
            if "h" in n:
                b, s = _box(n["b"]), _ints(n["s"], w)
                c = ar.box(b)
                if c == "edge":
                    raise TotalsError("open")
                if c == "in":
                    add(inside, s)
                r.closed += 1
                return k, n["h"], b, s
            if "items" in n:
                if level != 0:
                    raise TotalsError("format")
                r.opened += 1
                items = []
                for it in n["items"]:
                    sh, vh, b, hs = it["s"], it["v"], _box(it["b"]), it["h"]
                    c = ar.box(b)
                    if c == "out":
                        items.append((sh, vh, b, hs))
                        continue
                    if "n" not in it:
                        raise TotalsError("hidden")
                    sv = _ints(it["n"], w)
                    if sv[0] != 1 or core.sums_hash(sv) != hs:
                        raise TotalsError("amount")
                    if c == "edge":
                        if "g" not in it:
                            raise TotalsError("hidden")
                        g = QgsGeometry()
                        g.fromWkb(bytes.fromhex(it["g"]))
                        sk = core.shape_key(g, step)
                        if core.shape_hash(sk) != sh:
                            raise TotalsError("shape")
                        if core.reach_box(g, sk, step) != b:
                            raise TotalsError("box")
                        m = core.measure(g, sk, step, crs, geographic)
                        if abs(m - sv[1]) > 1 + abs(m) * 1e-9:
                            raise TotalsError("amount")
                        r.shown += 1
                        c = ar.shape(g)
                        if c == "edge":
                            add(crossing, sv)
                    if c == "in":
                        add(inside, sv)
                    items.append((sh, vh, b, hs))
                return (k,) + core.sum_leaf_open(items, _ints(n["s"], w))
            if "kids" in n:
                if level == 0:
                    raise TotalsError("format")
                entries = []
                for c in n["kids"]:
                    kk, h, b, s = walk(c, level - 1)
                    if (kk[1] >> 1, kk[2] >> 1) != (k[1], k[2]):
                        raise TotalsError("format")
                    entries.append((kk, h, b, s))
                return (k,) + core.sum_up(entries, w)
            raise TotalsError("format")

        top = [walk(n, core.TOP) for n in pr["tree"]]
        h, _all = core.sum_root(top, names, w)
        if h != root:
            raise TotalsError("sums")
        r.count = inside[0]
        r.amount = inside[1] / core.MEASURE_SCALE
        r.fields = {n: inside[2 + i] / core.VALUE_SCALE for i, n in enumerate(names)}
        r.crossing = crossing[0]
        r.crossing_amount = crossing[1] / core.MEASURE_SCALE
        r.measure, r.unit = pr.get("measure", ""), pr.get("unit", "")
        r.ok = True
    except TotalsError as e:
        r.why = str(e)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        r.why = "format"
    return r
