"""The quality passport: the spatial rules of a delivery and how the data kept them, sealed in its certificate.

When a certificate is made, every layer with spatial rules (georules) is checked against all of them; the rules and
how many features break each go into the certificate's layer as its passport, and the passport's fingerprint into
the code. Whoever receives the data reads, under the code they were given, what was checked at delivery and what it
found ("no overlap: 0 broken of 3,600"), and can check it again on the data itself: a passport cannot be made
better after the code was sent.
"""
import hashlib

from . import core


def _rules(layer):
    from ..georules import core as gr
    try:
        doc = gr.load(layer) or {}
    except Exception:  # noqa: BLE001 - a layer whose rules cannot be read has none here
        doc = {}
    return [r for r in doc.get("rules") or [] if r.get("on", True)]


def results(layer, rules, project=None):
    """[{kind, params, level, broken, of}]: every rule with the number of features breaking it now."""
    from ..georules import core as gr
    ck = gr.Checker(layer, rules, project)
    found = ck.check()
    out = []
    for r in rules:
        fids = {v.fid for v in found if v.rule is r}
        out.append({"kind": r["kind"], "params": r.get("params", {}), "level": r.get("level", "error"),
                    "broken": None if any(m is r for m in ck.missing) else len(fids), "of": layer.featureCount()})
    return out


def seal_of(passport):
    return hashlib.sha256(b"passport\n" + core._j(passport)).hexdigest()


def attach(cert, layers, project=None):
    """The certificate with a passport in each of its layers (when any layer has rules), its code made again.
    ``layers``: the map layers, in the order of the certificate's."""
    rules = [_rules(l) for l in layers]
    if not any(rules):
        return cert
    for e, lyr, rs in zip(cert["layers"], layers, rules):
        p = {"rules": results(lyr, rs, project) if rs else []}
        e["passport"] = p
        e["quality"] = seal_of(p)
    cert["code"] = core.code(core.layer_codes(cert["layers"]))
    return cert


def verify(cert):
    """None when the certificate's code and passports are what they say, else why: "code" or "passport"."""
    for e in cert.get("layers", []):
        if "passport" in e or "quality" in e:
            if seal_of(e.get("passport")) != e.get("quality"):
                return "passport"
    if core.code(core.layer_codes(cert.get("layers", []))) != cert.get("code"):
        return "code"
    return None


def again(layer, entry, project=None):
    """The passport's rules checked on the layer now: [(result at delivery, broken now)]."""
    p = entry.get("passport") or {}
    rules = [{"kind": r["kind"], "params": r.get("params", {}), "level": r.get("level", "error")}
             for r in p.get("rules", [])]
    if not rules:
        return []
    now = results(layer, rules, project)
    return [(r, n["broken"]) for r, n in zip(p["rules"], now)]
