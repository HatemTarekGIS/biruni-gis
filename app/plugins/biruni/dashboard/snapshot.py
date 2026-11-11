"""The dashboard in a link («اللوحة جوه رسالة»): a whole snapshot of a dashboard as one card sees it, packed into
the link itself, after "#" (never sent to any server).

    canonical JSON  ->  raw deflate  ->  base64url   +   ed25519 signature of the deflated bytes

The link opens the same public page as the live links (…/d/), with no transport at all: the page inflates
the snapshot, checks the signature with the PC's public key carried inside, and shows the dashboard as it was
- with the PC off. Any change to the link (one byte) breaks the signature and the page says so in red.
The key fingerprint is shown so a reader can compare it with the PC's (and the page remembers the keys of the
PCs it has seen live).

Size: a snapshot is cut to the card first (the PC's own session computes it, so the zone, hidden fields and
widgets of the card hold exactly as live). When it is too big for a comfortable link it is made smaller step
by step: coordinates rounded, long tables and feeds shortened, map geometries simplified, point layers
dropped, and at last the maps themselves; every step taken is reported.
"""
import base64
import copy
import hashlib
import json
import time
import zlib

from ..secure import crypto
from . import cards
from .host import PAGE_URL

WARN = 60 * 1024          # bytes of link above which a warning is shown (and shrinking starts)
HARD = 1500 * 1024        # never longer than this (browsers keep ~2 MB addresses; chat apps much less)
QR_MAX = 2900             # characters a QR code still holds (version 40, level L, byte mode)
STEPS = ("round6", "rows", "round5", "simplify", "points", "maps")


class SnapshotError(Exception):
    def __init__(self, why):
        super().__init__(why)
        self.why = why


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def fingerprint(pub_hex):
    """A short code of a public key (XXXX-XXXX), the same the page shows."""
    alpha = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    h = hashlib.sha256(bytes.fromhex(pub_hex)).digest()
    n = int.from_bytes(h[:5], "big")
    s = "".join(alpha[(n >> (35 - 5 * i)) & 31] for i in range(8))
    return s[:4] + "-" + s[4:]


# ---- what the card sees, from the PC's own session -----------------------------------------------------------------
def collect(service, card_text, lang="en"):
    """(dash message, {widget: data}) as a live viewer with this card would get them now."""
    box = []
    s = service.connect(box.append, "lan")
    try:
        s.receive({"t": "hello", "card": card_text, "lang": lang})
    finally:
        s.close()
    err = next((m for m in box if m.get("t") == "error"), None)
    if err is not None:
        raise SnapshotError(str(err.get("why") or "error"))
    dash = next((m for m in box if m.get("t") == "dash"), None)
    if dash is None:
        raise SnapshotError("no dashboard")
    widgets = {}
    for m in box:
        if m.get("t") == "data":
            widgets.update(m.get("widgets") or {})
    return dash, widgets


# ---- packing -----------------------------------------------------------------------------------------------------
def payload_of(dash, widgets, ts=None):
    return {"v": 1, "ts": round(float(ts if ts is not None else time.time()), 3), "pc": dash.get("pc", ""),
            "pub": dash.get("pub", ""), "dash": {k: dash.get(k) for k in ("def", "card", "pc", "pub")},
            "data": widgets}


def pack(payload, seed=None):
    """The fragment value: <b64u(deflated JSON)>.<b64u(signature)>"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    c = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
    z = c.compress(raw) + c.flush()
    sig = crypto.sign(seed if seed is not None else cards.seed(), z)
    return b64u(z) + "." + b64u(sig)


def unpack(value, pub_hex=None):
    """(payload, verified) of a fragment value - the check the page does, for tests and for "Check a link"."""
    a, b = value.split(".", 1)
    z, sig = unb64u(a), unb64u(b)
    payload = json.loads(zlib.decompress(z, -15).decode("utf-8"))
    pub = bytes.fromhex(pub_hex or payload.get("pub", ""))
    return payload, crypto.verify(pub, sig, z)


def link(value, lang="en", base=PAGE_URL):
    return f"{base}#s={value}&l={lang}"


# ---- making it smaller ----------------------------------------------------------------------------------------------
def _round_coords(obj, nd):
    if isinstance(obj, list):
        if obj and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in obj) and 2 <= len(obj) <= 4:
            return [round(v, nd) for v in obj]
        return [_round_coords(v, nd) for v in obj]
    if isinstance(obj, dict):
        return {k: (_round_coords(v, nd) if k in ("coordinates", "geometry", "features", "geojson", "layers",
                                                   "geometries") or isinstance(v, (list, dict)) else v)
                for k, v in obj.items()}
    return obj


def _rdp(pts, tol):
    """Douglas-Peucker on a list of [x, y] (keeps first and last)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i][:2], pts[j][:2]
        dx, dy = x2 - x1, y2 - y1
        n = (dx * dx + dy * dy) ** 0.5 or 1e-12
        best, at = -1.0, -1
        for k in range(i + 1, j):
            x, y = pts[k][:2]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / n
            if d > best:
                best, at = d, k
        if best > tol and at > 0:
            keep[at] = True
            stack += [(i, at), (at, j)]
    return [p for p, k in zip(pts, keep) if k]


def _simplify_geom(g, tol):
    t = (g or {}).get("type")
    c = (g or {}).get("coordinates")
    if t == "LineString":
        return {**g, "coordinates": _rdp(c, tol)}
    if t == "MultiLineString":
        return {**g, "coordinates": [_rdp(x, tol) for x in c]}
    if t == "Polygon":
        return {**g, "coordinates": [r if len(r) <= 5 else (_rdp(r, tol) if len(_rdp(r, tol)) >= 4 else r) for r in c]}
    if t == "MultiPolygon":
        return {**g, "coordinates": [[r if len(r) <= 5 else (_rdp(r, tol) if len(_rdp(r, tol)) >= 4 else r)
                                      for r in p] for p in c]}
    return g


def _collections(v):
    """Every GeoJSON FeatureCollection in a widget's data (map layers, legacy map, choropleth)."""
    out = []
    if isinstance(v.get("geojson"), dict):
        out.append(v["geojson"])
    for lyr in v.get("layers") or []:
        if isinstance(lyr, dict) and isinstance(lyr.get("geojson"), dict):
            out.append(lyr["geojson"])
    return out


def shrink(widgets, step):
    w = copy.deepcopy(widgets)
    for wid, v in list(w.items()):
        if not isinstance(v, dict):
            continue
        if step in ("round6", "round5"):
            for fc in _collections(v):
                fc["features"] = _round_coords(fc.get("features") or [], 6 if step == "round6" else 5)
            if isinstance(v.get("bbox"), list):
                v["bbox"] = [round(x, 6) for x in v["bbox"]]
        elif step == "rows":
            if isinstance(v.get("rows"), list) and len(v["rows"]) > 40:
                v["rows"] = v["rows"][:40]
            if isinstance(v.get("items"), list) and len(v["items"]) > 30 and "rows" not in v:
                v["items"] = v["items"][:30]
        elif step == "simplify":
            for fc in _collections(v):
                for f in fc.get("features") or []:
                    if isinstance(f, dict) and isinstance(f.get("geometry"), dict):
                        f["geometry"] = _simplify_geom(f["geometry"], 0.00002)
        elif step == "points":
            if isinstance(v.get("layers"), list):
                v["layers"] = [lyr for lyr in v["layers"] if (lyr or {}).get("kind") != "point"]
        elif step == "maps":
            if _collections(v):
                w[wid] = {"hidden": True, "snapshot": "too big"}
    return w


def build(service, card_text, lang="en", budget=WARN, base=PAGE_URL, seed=None, ts=None):
    """The snapshot link of a card. -> {"link", "size", "steps", "warn", "qr", "fingerprint", "verified"}"""
    dash, widgets = collect(service, card_text, lang)
    steps = []
    value = pack(payload_of(dash, widgets, ts), seed)
    full = link(value, lang, base)
    cur = widgets
    for step in STEPS:
        if len(full) <= budget:
            break
        cur = shrink(cur, step)
        steps.append(step)
        value = pack(payload_of(dash, cur, ts), seed)
        full = link(value, lang, base)
    if len(full) > HARD:
        raise SnapshotError("too big")
    return {"link": full, "size": len(full), "steps": steps, "warn": len(full) > budget, "qr": len(full) <= QR_MAX,
            "fingerprint": fingerprint(dash.get("pub", "") or cards.public_hex()), "value": value}
