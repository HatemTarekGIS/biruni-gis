"""Private match by files: two parties find the features they have in common (the same parcel number, the same
place), and neither sees the other's list.

Each key is turned into a number of a large group and raised to a secret power that never leaves its owner
(Diffie-Hellman, RFC 3526 group 14). Raised by both powers, in either order, a key gives the same number: the
common keys meet, the others are numbers nobody can turn back.

    Start  (A)  -> file 1: A's keys, raised to a
    Answer (B)  -> file 2: file 1 raised to b (in A's order), and B's own keys raised to b (shuffled)
    Finish (A)  -> A raises B's keys to a and finds the common ones: its features are selected
                   (and, if A chooses, file 3: which of B's keys matched, so B selects its own)
    Finish (B)  -> B selects its features from file 3

A learns which of its features B also has, and how many keys B has; B learns how many keys A has (and, with
file 3, which of its own A has). Both are taken to follow the steps: a party that lies about its keys can only
make the answer wrong, not learn the other's keys. The secret power of A is kept in a session file on A's
computer only until A finishes; B's is never kept.
"""
import base64
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import time

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest,
                       QgsProject)

from ..arabic_text import normalise

FORMAT = 1
FILE_KINDS = {1: "biruni-psi-1", 2: "biruni-psi-2", 3: "biruni-psi-3"}
SUFFIX = {1: ".biruni-psi-1", 2: ".biruni-psi-2", 3: ".biruni-psi-3"}
DH_GROUP = "rfc3526-2048"
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
SIZE = (P.bit_length() + 7) // 8          # 256 bytes
EXP_BITS = 256                            # a short secret power: the discrete logarithm stays ~128-bit hard
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


class PsiError(Exception):
    pass


# ---- keys -------------------------------------------------------------------------------------------
def value_key(v):
    """One field's value as a key: case, spaces, Arabic letter forms and digits made one; 12, 12.0 and "12" the same."""
    if v is None:
        return ""
    try:
        from qgis.PyQt.QtCore import QVariant
        if isinstance(v, QVariant) and v.isNull():
            return ""
    except ImportError:
        pass
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            return ""
        return str(int(v)) if float(v).is_integer() else repr(float(v))
    t = str(v).translate(_DIGITS).strip()
    if _NUMBER.match(t):          # "12.0" and "12" are one; "012" and "12" stay apart
        if "." not in t:
            return t
        f = float(t)
        return str(int(f)) if f.is_integer() else repr(f)
    return normalise(str(v))


def field_keys(layer, fields, fids=None):
    """{key: [fids]} from the values of ``fields`` (one key from all of them)."""
    idx = [layer.fields().indexFromName(n) for n in fields]
    if not fields or min(idx) < 0:
        raise PsiError("fields")
    req = QgsFeatureRequest().setFlags(QgsFeatureRequest.Flag.NoGeometry).setSubsetOfAttributes(idx)
    if fids is not None:
        req.setFilterFids(list(fids))
    out = {}
    for f in layer.getFeatures(req):
        parts = [value_key(f.attribute(i)) for i in idx]
        if not any(parts):
            continue
        out.setdefault("v\x1f" + "\x1f".join(parts), []).append(f.id())
    return out


def _centres(layer, crs, fids=None):
    tr = None
    if layer.crs() != crs:
        tr = QgsCoordinateTransform(layer.crs(), crs, QgsProject.instance())
    req = QgsFeatureRequest().setNoAttributes()
    if fids is not None:
        req.setFilterFids(list(fids))
    for f in layer.getFeatures(req):
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty():
            continue
        c = g.centroid()
        if c is None or c.isNull():
            continue
        if tr is not None:
            c.transform(tr)
        p = c.asPoint()
        yield f.id(), p.x(), p.y(), int(g.type())


def shape_spec(layer, cell=1.0):
    """How a shape becomes a key: the square of a grid its centre falls in (``cell`` metres)."""
    crs = layer.crs()
    unit = cell / 111320.0 if crs.isGeographic() else cell
    return {"mode": "shape", "crs": crs.authid() or crs.toWkt(), "cell": unit, "tolerance": unit / 2.0}


def shape_keys(layer, spec, spread, fids=None):
    """{key: [fids]} of the shapes. With ``spread`` a shape gives the squares within the tolerance of its centre
    (up to four), so a centre drawn a little apart on the other side still meets one of them; the other side gives
    its own square only."""
    crs = QgsCoordinateReferenceSystem(spec["crs"])
    cell, tol = float(spec["cell"]), float(spec["tolerance"])
    out = {}
    for fid, x, y, kind in _centres(layer, crs, fids):
        if spread:
            xs = {math.floor((x - tol) / cell), math.floor((x + tol) / cell)}
            ys = {math.floor((y - tol) / cell), math.floor((y + tol) / cell)}
            cells = [(i, j) for i in xs for j in ys]
        else:
            cells = [(math.floor(x / cell), math.floor(y / cell))]
        for i, j in cells:
            out.setdefault(f"s\x1f{kind}\x1f{i}\x1f{j}", []).append(fid)
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


# ---- the group --------------------------------------------------------------------------------------
def to_group(session, key):
    """A key as a number of the group: a hash stretched to the group's size, squared (so it is in the subgroup
    of squares, where nobody can tell the numbers apart)."""
    seed = b"biruni-psi\n" + session.encode("ascii") + b"\n" + key.encode("utf-8")
    raw = b"".join(hashlib.sha256(seed + i.to_bytes(4, "big")).digest() for i in range((SIZE + 8 + 31) // 32))
    x = int.from_bytes(raw[:SIZE + 8], "big") % P
    return pow(x, 2, P) or 4


def new_secret():
    return secrets.randbits(EXP_BITS) | (1 << (EXP_BITS - 1))


def _enc(x):
    return base64.b64encode(x.to_bytes(SIZE, "big")).decode("ascii")


def _dec(s):
    try:
        b = base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        raise PsiError("format") from None
    x = int.from_bytes(b, "big")
    if len(b) != SIZE or not 1 < x < P - 1:
        raise PsiError("format")
    return x


def _raise_all(xs, e, feedback=None, start=0.0, span=100.0):
    out = []
    n = max(1, len(xs))
    for i, x in enumerate(xs):
        out.append(pow(x, e, P))
        if feedback is not None and i % 50 == 0:
            if feedback(start + span * i / n) is False:
                raise PsiError("cancelled")
    return out


def _digest(items):
    h = hashlib.sha256()
    for s in items:
        h.update(s.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


# ---- files ------------------------------------------------------------------------------------------
def _body_check(doc):
    body = {k: v for k, v in doc.items() if k != "check"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def write_file(doc, path):
    doc = dict(doc)
    doc["check"] = _body_check(doc)
    with gzip.open(path, "wb") as f:
        f.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return path


def read_file(path, step):
    """A file of the given step (1, 2 or 3); PsiError("format") when it is not one, or was altered."""
    try:
        with open(path, "rb") as f:
            head = f.read(2)
        opener = gzip.open if head == b"\x1f\x8b" else open
        with opener(path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError, EOFError):
        raise PsiError("format") from None
    if not isinstance(doc, dict) or doc.get("kind") != FILE_KINDS[step] or doc.get("group", DH_GROUP) != DH_GROUP:
        raise PsiError("format")
    if doc.get("check") != _body_check(doc):
        raise PsiError("altered")
    return doc


def sessions_dir(folder=None):
    d = folder or os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "psi")
    os.makedirs(d, exist_ok=True)
    return d


def _session_path(session, role, folder=None):
    return os.path.join(sessions_dir(folder), f"{session}-{role}.json")


def _save_session(state, folder=None):
    p = _session_path(state["session"], state["role"], folder)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return p


def load_session(session, role, folder=None):
    p = _session_path(session, role, folder)
    if not os.path.isfile(p):
        raise PsiError("session")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---- the steps --------------------------------------------------------------------------------------
class Step:
    """What a step did: the file it wrote, the features it found, how long it took."""

    def __init__(self, **kw):
        self.path = ""
        self.session = ""
        self.mine = 0            # keys of this side
        self.theirs = 0          # keys of the other side
        self.fids = []           # features of this side in common (the finishing steps)
        self.common = 0          # common keys
        self.seconds = 0.0
        self.__dict__.update(kw)

    @property
    def rate(self):
        return (self.mine + self.theirs) / self.seconds if self.seconds else 0.0


def start(layer, spec, path, fids=None, label="", folder=None, feedback=None):
    """A's first step. ``spec``: {"mode": "fields", "fields": [...]} or shape_spec(...)."""
    t0 = time.time()
    session = secrets.token_hex(12)
    if spec.get("mode") == "fields":
        keys = field_keys(layer, spec["fields"], fids)
        desc = {"mode": "fields", "fields": list(spec["fields"])}
    elif spec.get("mode") == "shape":
        keys = shape_keys(layer, spec, True, fids)
        desc = {k: spec[k] for k in ("mode", "crs", "cell", "tolerance")}
    else:
        raise PsiError("format")
    if not keys:
        raise PsiError("empty")
    a = new_secret()
    order = list(keys)
    secrets.SystemRandom().shuffle(order)
    blinded = _raise_all([to_group(session, k) for k in order], a, feedback)
    out = [_enc(x) for x in blinded]
    doc = {"format": FORMAT, "kind": FILE_KINDS[1], "group": DH_GROUP, "session": session, "made": _now(),
           "label": label, "key": desc, "count": len(out), "items": out}
    write_file(doc, path)
    _save_session({"session": session, "role": "start", "made": doc["made"], "layer": layer.name(),
                   "layer_id": layer.id(), "secret": format(a, "x"), "key": desc,
                   "fids": [keys[k] for k in order], "sent": _digest(out)}, folder)
    return Step(path=path, session=session, mine=len(out), seconds=time.time() - t0)


def describe(path):
    """What file 1 asks for (to show before answering): its key and how many keys."""
    doc = read_file(path, 1)
    return {"session": doc["session"], "key": doc["key"], "count": doc["count"], "label": doc.get("label", ""),
            "made": doc.get("made", "")}


def answer(path1, layer, path, fields=None, fids=None, folder=None, feedback=None):
    """B's step: file 1 and B's layer -> file 2. In "fields" mode ``fields`` are B's own fields for A's (as many,
    in the same order). B keeps no secret; with ``folder`` it keeps which of its features each of its keys is,
    for file 3."""
    t0 = time.time()
    doc = read_file(path1, 1)
    session, desc = doc["session"], doc["key"]
    if desc["mode"] == "fields":
        fields = list(fields or [])
        if len(fields) != len(desc["fields"]):
            raise PsiError("fields")
        keys = field_keys(layer, fields, fids)
    elif desc["mode"] == "shape":
        keys = shape_keys(layer, desc, False, fids)
    else:
        raise PsiError("format")
    if not keys:
        raise PsiError("empty")
    theirs = [_dec(s) for s in doc["items"]]
    b = new_secret()
    n = len(theirs) + len(keys)
    span = 100.0 * len(theirs) / max(1, n)
    again = _raise_all(theirs, b, feedback, 0.0, span)
    order = list(keys)
    secrets.SystemRandom().shuffle(order)
    mine = _raise_all([to_group(session, k) for k in order], b, feedback, span, 100.0 - span)
    mine_enc = [_enc(x) for x in mine]
    out = {"format": FORMAT, "kind": FILE_KINDS[2], "group": DH_GROUP, "session": session, "made": _now(),
           "of": _digest(doc["items"]), "again": [_enc(x) for x in again], "items": mine_enc}
    write_file(out, path)
    _save_session({"session": session, "role": "answer", "made": out["made"], "layer": layer.name(),
                   "layer_id": layer.id(), "key": desc, "fids": [keys[k] for k in order],
                   "sent": _digest(mine_enc)}, folder)
    del b
    return Step(path=path, session=session, mine=len(keys), theirs=len(theirs), seconds=time.time() - t0)


def finish(path2, folder=None, path3=None, feedback=None):
    """A's last step: file 2 -> A's features in common. With ``path3`` also file 3, for B to find its own."""
    t0 = time.time()
    doc = read_file(path2, 2)
    st = load_session(doc["session"], "start", folder)
    if not st.get("secret"):
        raise PsiError("finished")
    if doc.get("of") != st["sent"] or len(doc["again"]) != len(st["fids"]):
        raise PsiError("not ours")
    a = int(st["secret"], 16)
    mine = {_dec(s): i for i, s in enumerate(doc["again"])}       # H(x)^ab -> A's key
    theirs = [_dec(s) for s in doc["items"]]
    raised = _raise_all(theirs, a, feedback)
    hits = [(j, mine[x]) for j, x in enumerate(raised) if x in mine]
    fids = sorted({fid for _j, i in hits for fid in st["fids"][i]})
    res = Step(session=doc["session"], mine=len(st["fids"]), theirs=len(theirs), fids=fids, common=len(hits),
               seconds=time.time() - t0)
    if path3:
        out = {"format": FORMAT, "kind": FILE_KINDS[3], "group": DH_GROUP, "session": doc["session"], "made": _now(),
               "of": _digest(doc["items"]), "matched": sorted(j for j, _i in hits)}
        write_file(out, path3)
        res.path = path3
    st.pop("secret")                    # done: the power is forgotten, the answer kept
    st.update(finished=_now(), common=fids)
    _save_session(st, folder)
    return res


def finish_answer(path3, folder=None):
    """B's last step: file 3 -> B's features in common."""
    doc = read_file(path3, 3)
    st = load_session(doc["session"], "answer", folder)
    if doc.get("of") != st["sent"]:
        raise PsiError("not ours")
    n = len(st["fids"])
    matched = doc.get("matched") or []
    if any(not isinstance(j, int) or not 0 <= j < n for j in matched):
        raise PsiError("format")
    fids = sorted({fid for j in matched for fid in st["fids"][j]})
    st.update(finished=_now(), common=fids)
    _save_session(st, folder)
    return Step(session=doc["session"], mine=n, fids=fids, common=len(matched))
