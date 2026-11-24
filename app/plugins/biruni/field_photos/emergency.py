"""The emergency key (no-loss P3): when the office computer and its key backup are both gone, 3 of 5 named people
together can still open what the phones sealed.

A new P-256 key is made in memory. Its private key (PKCS#8 DER) is split with Shamir 3-of-5 (team_vault.shamir)
into 5 share files, one per holder; then the private key is dropped. It is never written to disk whole: only the
public key goes on the recipients list (role "emergency"), so phones seal for it too.

A share file (BEK1, JSON):
  {"biruni": "emergency-share", "v": "BEK1", "pub": the emergency public key, "x": 1..5, "k": 3, "of": 5,
   "holder": name, "made": date, "share": base64url, "check": first 16 hex of SHA-256("BEK1|" pub "|" x "|" share)}

The check catches a damaged or mixed-up file early; it is not a secret. 2 shares say nothing about the key.
"""
import base64
import hashlib
import json
import os
import re
import time

KIND = "emergency-share"
VERSION = "BEK1"
K, N = 3, 5


class EmergencyError(ValueError):
    pass


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _check(pub, x, share_b64):
    return hashlib.sha256(f"{VERSION}|{pub}|{x}|{share_b64}".encode("ascii")).hexdigest()[:16]


def _safe(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", str(name or "")).strip().rstrip(".")
    return s[:40] or "holder"


def create(folder, holders):
    """Make the emergency key and write its 5 share files into ``folder``. holders: 5 names (one per share).
    Returns (public key b64u, [file paths])."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from ..team_vault import shamir

    holders = [str(h or "").strip()[:60] for h in (holders or [])]
    if len(holders) != N or not all(holders):
        raise EmergencyError("five holder names are needed")
    key = ec.generate_private_key(ec.SECP256R1())
    pub = _b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint))
    secret = bytearray(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    try:
        shares = shamir.split(bytes(secret), K, N)
    finally:
        for i in range(len(secret)):   # what Python lets us wipe; the key object goes out of scope below
            secret[i] = 0
        del key
    os.makedirs(folder, exist_ok=True)
    made = time.strftime("%Y-%m-%d")
    paths = []
    for (x, b), holder in zip(shares, holders):
        sb = _b64u(b)
        doc = {"biruni": KIND, "v": VERSION, "pub": pub, "x": x, "k": K, "of": N, "holder": holder, "made": made,
               "share": sb, "check": _check(pub, x, sb)}
        path = os.path.join(folder, f"biruni-emergency-{x}-{_safe(holder)}.bek")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        paths.append(path)
    return pub, paths


def read_share(path_or_doc):
    """A share file (path or parsed JSON) -> its doc; EmergencyError when it is not one or is damaged."""
    doc = path_or_doc
    if not isinstance(doc, dict):
        try:
            with open(path_or_doc, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            raise EmergencyError("not an emergency share") from e
    if not isinstance(doc, dict) or doc.get("biruni") != KIND or doc.get("v") != VERSION:
        raise EmergencyError("not an emergency share")
    x, pub, sb = doc.get("x"), doc.get("pub"), doc.get("share")
    if not isinstance(x, int) or not 1 <= x <= N or not isinstance(pub, str) or not isinstance(sb, str):
        raise EmergencyError("not an emergency share")
    if doc.get("check") != _check(pub, x, sb):
        raise EmergencyError("a damaged share")
    return doc


def recover(shares):
    """3 share files (paths or docs) of one key -> the emergency private key, in memory only."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from ..team_vault import shamir

    docs = [read_share(s) for s in shares]
    pubs = {d["pub"] for d in docs}
    if len(pubs) != 1:
        raise EmergencyError("shares of different keys")
    byx = {d["x"]: d for d in docs}
    if len(byx) < K:
        raise EmergencyError("three different shares are needed")
    pick = [(x, _unb64u(byx[x]["share"])) for x in sorted(byx)[:K]]
    secret = bytearray(shamir.combine(pick))
    try:
        key = serialization.load_der_private_key(bytes(secret), None)
    except (ValueError, TypeError) as e:
        raise EmergencyError("these shares do not make the key") from e
    finally:
        for i in range(len(secret)):
            secret[i] = 0
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise EmergencyError("these shares do not make the key")
    pub = _b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint))
    if pub != pubs.pop():
        raise EmergencyError("these shares do not make the key")
    return key


def open_blob(key, blob):
    """With the recovered key: a .bfp file, or a sealed item (v2 envelope) -> (meta, data)."""
    from cryptography.hazmat.primitives import serialization

    from . import envelope

    blob = bytes(blob)
    if blob[:4] == b"BFP1":
        from . import drive

        _h, _signed, _sig, blob = drive.parse(blob)
    pub = _b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint))
    if blob[:1] != b"\x02":
        raise EmergencyError("sealed before the emergency key existed (for the office only)")
    return envelope.open(blob, key, pub)


def open_files(shares, paths, out_folder):
    """Open sealed files (.bfp, or raw sealed items) with 3 shares and write their pictures into ``out_folder``.
    Returns {"opened": [(path, out path)], "refused": [(path, reason)]}. The key lives only during this call."""
    key = recover(shares)
    os.makedirs(out_folder, exist_ok=True)
    out = {"opened": [], "refused": []}
    for p in paths:
        try:
            with open(p, "rb") as f:
                meta, data = open_blob(key, f.read())
        except (OSError, ValueError) as e:
            out["refused"].append((p, str(e) or type(e).__name__))
            continue
        name = re.sub(r"[^\w.-]+", "_", str(meta.get("id") or os.path.splitext(os.path.basename(p))[0]))[:80]
        target = os.path.join(out_folder, name + (".jpg" if bytes(data[:2]) == b"\xff\xd8" else ".bin"))
        with open(target, "wb") as f:
            f.write(data)
        with open(os.path.join(out_folder, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        out["opened"].append((p, target))
    del key
    return out
