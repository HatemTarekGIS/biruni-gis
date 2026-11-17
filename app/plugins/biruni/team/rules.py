"""The room's merge constitution and escrowed numbers (idea I06 «الحد عنصر واحد», first piece).

Merge constitution
------------------
A per-layer, per-field policy the room's admin signs. It decides what happens when two people changed the same
field of the same feature at the same time (the three-way merge in merge.py). The host applies it while deciding
each change (hub.decide_op), so the result is the same whether the change came over the network, by carrier
pigeon or through a shared folder - every other computer then gets the host's decision from the stream, so all
end with identical attributes. A member who takes over as host has the same signed constitution (it travels
with the room's settings) and decides the same way.

Rules for one field (both sides changed it to different values; "base" = the value both started from):
  keep       the current value stays and the author is told: today's behaviour, the default
  last       the change decided last wins (the host's order), nobody is asked
  authority  the higher rank wins (rank 0 = the room's founder; lower is higher); equal ranks: the last
  max / min  the larger / smaller number (not numbers: like keep)
  sum        both changes add up: base + (mine - base) + (theirs - base) (counters, quantities)
  ask        never joined by itself: whenever the feature changed meanwhile and this field was changed, the
             author's value waits for the author's decision (even when the other side did not touch the field)

Escrowed numbers
----------------
The admin grants each member a range of numbers for a numbering field (parcel_no 1000-1099 to Mona). A new
feature drawn on that member's computer takes the next free number of its own ranges, offline too, so two
members never give the same number. The host flags a number outside its author's ranges and a number already
used in the layer (the change is kept; the flag goes to the author and to the stream note).

Both are signed by an admin (a member of rank 0) with the device key (ed25519): a forged or altered constitution
or grant is ignored, and the room behaves as if it had none. Limits: a computer that has not yet received the
constitution (a member who never joined over the network since it was set) still numbers with its old grants;
an admin who is not the host cannot change the rules (the room's settings flow from the host).
"""
import json
import time

from ..secure import crypto

RULES = ("keep", "last", "authority", "max", "min", "sum", "ask")
RULE_LABELS = {"keep": "Keep the current value and ask the author", "last": "The last change wins",
               "authority": "The higher rank wins", "max": "The larger number", "min": "The smaller number",
               "sum": "Add both changes", "ask": "Never join by itself: ask"}
CKEY, GKEY, HKEY = "constitution", "number_grants", "host_grants"


# ---- signing ------------------------------------------------------------------------------------------------
def _sign(seed_hex, doc):
    return crypto.sign(bytes.fromhex(seed_hex), crypto.canonical(doc)).hex()


def _verify(signed, lookup):
    """The doc of a signed {"doc", "sig"} when an admin (rank 0, still in the room) signed it, else None."""
    try:
        doc, sig = signed["doc"], signed["sig"]
        who = lookup(doc.get("by"))
        if not who or who.get("state") == "out" or not who.get("pub") or int(who.get("rank", 99)) != 0:
            return None
        if crypto.verify(bytes.fromhex(who["pub"]), bytes.fromhex(sig), crypto.canonical(doc)):
            return doc
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


def make_constitution(seed_hex, admin_id, layers, version):
    """layers: {layer_key: {field: rule}} -> a signed constitution."""
    clean = {str(k): {str(f): r for f, r in (v or {}).items() if r in RULES and r != "keep"}
             for k, v in (layers or {}).items()}
    doc = {"kind": "constitution", "v": int(version), "by": admin_id, "ts": round(time.time(), 3),
           "layers": {k: v for k, v in clean.items() if v}}
    return {"doc": doc, "sig": _sign(seed_hex, doc)}


def constitution(store, lookup):
    """The verified constitution doc of the room ({} when there is none or it does not verify)."""
    c = store.get(CKEY)
    return (_verify(c, lookup) or {}) if c else {}


def layer_rules(store, lookup, key):
    return dict((constitution(store, lookup).get("layers") or {}).get(key) or {})


def make_grant(seed_hex, admin_id, layer, field, member_id, lo, hi):
    lo, hi = int(lo), int(hi)
    if hi < lo:
        raise ValueError("range")
    doc = {"kind": "numbers", "layer": str(layer), "field": str(field), "member": str(member_id), "lo": lo,
           "hi": hi, "by": admin_id, "ts": round(time.time(), 3)}
    return {"doc": doc, "sig": _sign(seed_hex, doc)}


def grants(store, lookup, layer=None, field=None, member=None):
    """The verified grant docs (filtered)."""
    out = []
    for g in store.get(GKEY) or []:
        d = _verify(g, lookup)
        if d is None or d.get("kind") != "numbers":
            continue
        if (layer is None or d["layer"] == layer) and (field is None or d["field"] == field) and \
                (member is None or d["member"] == member):
            out.append(d)
    return out


def numbering_fields(store, lookup, layer):
    return sorted({d["field"] for d in grants(store, lookup, layer=layer)})


def in_ranges(value, docs):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return False
    return any(d["lo"] <= v <= d["hi"] for d in docs)


def next_number(docs, used):
    """The smallest number of the ranges not in ``used`` (a set of ints), or None when they are all taken."""
    for d in sorted(docs, key=lambda d: (d["lo"], d["hi"])):
        for v in range(d["lo"], d["hi"] + 1):
            if v not in used:
                return v
    return None


def left(docs, used):
    return sum(d["hi"] - d["lo"] + 1 for d in docs) - sum(1 for u in used if in_ranges(u, docs))


def make_host_grant(seed_hex, admin_id, member_id, allow):
    """Owner-signed grant deciding whether ``member_id`` may become the room's host."""
    doc = {"kind": "host", "member": str(member_id), "allow": bool(allow), "by": str(admin_id),
           "ts": round(time.time(), 3)}
    return {"doc": doc, "sig": _sign(seed_hex, doc)}


def may_host(store, lookup, member_id):
    """True when ``member_id`` has rank 0, or the newest owner-signed host grant allows it."""
    who = lookup(member_id)
    if who is not None:
        if who.get("state") == "out":
            return False
        if int(who.get("rank", 99)) == 0:
            return True
    valid = []
    for g in store.get(HKEY) or []:
        d = _verify(g, lookup)
        if d is not None and d.get("kind") == "host" and d.get("member") == member_id:
            valid.append(d)
    if not valid:
        return False
    newest = max(enumerate(valid), key=lambda p: (float(p[1].get("ts") or 0), p[0]))[1]
    return bool(newest.get("allow"))


# ---- the field decision -------------------------------------------------------------------------------------
def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v) if "." in str(v) else int(v)
    except (TypeError, ValueError):
        return None


def decide_field(rule, base, mine, theirs, mine_rank=99, theirs_rank=99):
    """(value, conflict?) for a field both sides changed to different values."""
    if rule == "last":
        return mine, False
    if rule == "authority":
        if mine_rank < theirs_rank or mine_rank == theirs_rank:
            return mine, False
        return theirs, False
    if rule in ("max", "min", "sum"):
        m, t = _num(mine), _num(theirs)
        if m is not None and t is not None:
            if rule == "max":
                return (mine if m >= t else theirs), False
            if rule == "min":
                return (mine if m <= t else theirs), False
            b = _num(base)
            if b is not None:
                s = b + (m - b) + (t - b)
                return (int(s) if isinstance(s, float) and s.is_integer() and isinstance(m, int) else s), False
    return theirs, True   # keep, ask, or numbers that are not numbers


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
