"""What is in an area, proved: nothing at all, or exactly these features and no others.

A layer's reach (core.py) is a tree of squares whose every fingerprint holds the box its features reach. A proof
for an area keeps, from the top down, only what the area needs: a square whose box does not touch the area is
kept closed (its fingerprint and its box), one whose box touches it is opened. In an opened smallest square every
feature is there: closed (its fingerprints and its box) when its box is away from the area, with its shape when
the box touches the area, and with its values too when the shape is in it.

Whoever checks the proof builds the tree again from what it holds, up to one fingerprint: it must be the reach of
the certificate the data was delivered with (its code says so). A feature taken out, moved, or given another value
gives another fingerprint; a closed square or feature whose box touches the area is refused. So the proof holds
everything there is in the area, and shows nothing of the rest but boxes and fingerprints - the values of the
neighbours stay hidden.

"In the area" is more than touching it: a feature that only touches its edge, or comes into it less than the
tolerance (a shared boundary drawn a few centimetres apart), is not in it.
"""
import datetime as dt

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsRectangle, QgsWkbTypes

from . import core

KIND = "biruni-area-proof"


class ProofError(Exception):
    pass


def _inner(area, tolerance):
    if area is None or area.isNull() or area.isEmpty() or area.type() != QgsWkbTypes.PolygonGeometry:
        raise core.FingerprintError("area")
    inner = area.buffer(-tolerance, 8) if tolerance > 0 else QgsGeometry(area)
    if inner.isNull() or inner.isEmpty():
        raise core.FingerprintError("small")
    eng = QgsGeometry.createGeometryEngine(inner.constGet())
    eng.prepareGeometry()
    return inner, eng


def _hits(eng, b, step):
    if b is None:
        return False
    r = QgsRectangle((b[0] - 1) * step, (b[1] - 1) * step, (b[2] + 1) * step, (b[3] + 1) * step)
    g = QgsGeometry.fromRect(r)          # kept: the engine reads it after constGet returns
    return eng.intersects(g.constGet())


def _in(eng, g):
    return g is not None and not g.isNull() and not g.isEmpty() and eng.intersects(g.constGet())


def prove(layer, area, tolerance, cert=None, index=0, feedback=None):
    """The proof of what ``layer`` has in ``area`` (a polygon in the layer's coordinates). With ``cert`` (the
    certificate the layer was delivered with, its layer ``index``) the layer must still be what it says; without
    it the proof stands on the layer as it is now, with a code of its own."""
    if cert is not None:
        entry = cert["layers"][index]
        if not entry.get("reach"):
            raise core.FingerprintError("old certificate")
        t = core.tree_for(layer, entry, feedback)
        if t.root != entry["root"] or t.reach != entry["reach"]:
            raise core.FingerprintError("changed")
        layers = [core.code_part(e) for e in cert["layers"]]
        made, name = cert.get("made"), entry["name"]
    else:
        t = core.build(layer, feedback=feedback, commit=True, sums=True)     # as a certificate is made
        name = layer.name()
        layers, made, index = [core.code_part(core.certificate([(name, t)])["layers"][0])], None, 0
    step = t.step
    _inner_g, eng = _inner(area, tolerance)
    lower = {f.name().lower(): i for i, f in enumerate(layer.fields())}
    idx = [(n, lower[n]) for n in t.fields]

    opened = {}          # leaf key -> fids whose box touches the area
    def find(k):
        h, b = t.reach_nodes[k]
        if not _hits(eng, b, step):
            return
        if k[0] == 0:
            opened[k[1:]] = [i[3] for i in t.items[k[1:]] if _hits(eng, i[2], step)]
            return
        for c in t.kids[k]:
            find(c)

    for k in t.top():
        find(k)
    want = [fid for fids in opened.values() for fid in fids]
    feats = {f.id(): f for f in layer.getFeatures(QgsFeatureRequest().setFilterFids(want))} if want else {}
    inside = 0

    def item(i, near):
        nonlocal inside
        sh, vh, b, fid = i
        if fid not in near:
            return {"s": sh, "v": vh, "b": b}
        f = feats[fid]
        g = f.geometry()
        out = {"g": bytes(g.asWkb()).hex(), "b": b}
        if _in(eng, g):
            inside += 1
            out["vals"] = [[n, core._value(f.attribute(j))] for n, j in idx]
        else:
            out["v"] = vh
        return out

    def node(k):
        h, b = t.reach_nodes[k]
        if k[0] == 0:
            if k[1:] not in opened:
                return {"k": list(k), "h": h, "b": b}
            near = set(opened[k[1:]])
            return {"k": list(k), "items": [item(i, near) for i in t.items[k[1:]]]}
        if not _hits(eng, b, step):
            return {"k": list(k), "h": h, "b": b}
        return {"k": list(k), "kids": [node(c) for c in sorted(t.kids[k])]}

    tree = [node(k) for k in t.top()]
    return {"format": core.FORMAT, "kind": KIND,
            "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "layer": name, "settings": t.settings(), "features": t.count,
            "certificate": {"code": core.code(core.layer_codes(layers)), "made": made, "layers": layers,
                            "index": index},
            "area": {"wkt": area.asWkt(), "tolerance": tolerance},
            "inside": inside, "tree": tree}


class Result:
    def __init__(self):
        self.ok = False
        self.why = ""            # code / open / hidden / box / reach / format
        self.code = ""
        self.inside = []         # [(QgsGeometry, [[field, value]])]
        self.area = None
        self.opened = 0          # smallest squares opened
        self.closed = 0          # squares and features kept closed


def verify(proof):
    """Check a proof from what it holds alone. Result.ok: the area holds Result.inside and nothing else, in the
    data whose certificate has the code Result.code."""
    r = Result()
    try:
        if proof.get("kind") != KIND:
            raise ProofError("format")
        cert = proof["certificate"]
        layers = cert["layers"]
        r.code = core.code(core.layer_codes(layers))
        if r.code != cert["code"]:
            raise ProofError("code")
        reach = layers[cert["index"]]["reach"]
        step = float(proof["settings"]["step"])
        r.area = QgsGeometry.fromWkt(proof["area"]["wkt"])
        _inner_g, eng = _inner(r.area, float(proof["area"]["tolerance"]))

        def walk(n):
            if "h" in n:
                if _hits(eng, n["b"], step):
                    raise ProofError("open")
                r.closed += 1
                return n["h"], n["b"]
            if "items" in n:
                r.opened += 1
                items = []
                for it in n["items"]:
                    b = it["b"]
                    if "s" in it:
                        if _hits(eng, b, step):
                            raise ProofError("open")
                        r.closed += 1
                        items.append((it["s"], it["v"], b))
                        continue
                    g = QgsGeometry()
                    g.fromWkb(bytes.fromhex(it["g"]))
                    sk = core.shape_key(g, step)
                    if core.reach_box(g, sk, step) != b:
                        raise ProofError("box")
                    if "vals" in it:
                        vh = core.values_hash(it["vals"])
                        if _in(eng, g):
                            r.inside.append((g, it["vals"]))
                    else:
                        if _in(eng, g):
                            raise ProofError("hidden")
                        vh = it["v"]
                    items.append((core.shape_hash(sk), vh, b))
                return core.reach_leaf(items)
            return core.reach_up([(tuple(c["k"]),) + walk(c) for c in n["kids"]])

        top = [(tuple(c["k"]),) + walk(c) for c in proof["tree"]]
        if core.reach_up(top, top=True)[0] != reach:
            raise ProofError("reach")
        r.ok = True
    except ProofError as e:
        r.why = str(e)
    except core.FingerprintError as e:
        r.why = str(e)
    except (KeyError, TypeError, ValueError, IndexError):
        r.why = "format"
    return r
