"""Who can open what the phones seal (no-loss P3): the signed recipients list.

Without it a phone seals every photo for the office computer alone (envelope v1). With it the phone seals v2
(envelope.py) for every computer on the list whose scope allows that kind of item:

  office     this computer's handover key (always first, always everything)
  backup     another office computer (its own key, added in "Backup PC…")
  emergency  the emergency key, split 3-of-5 among named holders (emergency.py)
  sup        a supervisor: positions only unless the office says otherwise

The list: {"v": 1, "seq", "room", "made", "r": [{"k": kid hex, "p": public key, "role", "scope": [kinds], "n": name}]}.
It travels as {"l": the canonical JSON text, "ip": signer public key, "is": signature} in the hello and in the guide
(guide.rl). The signature is ECDSA P-256 / SHA-256 by this PC's identity key (bridge.identity, the key behind the
link's &i= pin) over "BRRL1|" + hex SHA-256 of l. The guide itself is not signed (rguide), so the list carries its
own signature; the QR code does not change.

The phone (server.py PAGE, RL) refuses a list when: the link has no pin (older QR codes: anyone who knows the bridge
key could otherwise add themselves as a recipient; such phones keep sealing v1), the signer is not the pinned identity,
seq is lower than the one it keeps for the same office key, the room is not its room, the office entry is not the key
it seals for, or there are more than 12 entries. Once a phone keeps a list, an unsigned hello or guide cannot change
the office key (pk) it seals for: only a signed list naming the new key can.
seq is floored to the minutes since 1970 (next_seq), so it keeps rising across restores and new PCs.
"""
import base64
import hashlib
import json
import os
import time

FILE = "recipients.json"
ROLES = ("office", "backup", "emergency", "sup")
KINDS = ("photo", "state", "pos")
DEFAULT_SCOPE = {"office": list(KINDS), "backup": list(KINDS), "emergency": list(KINDS), "sup": ["pos"]}
MAX = 12
SIGN_PREFIX = "BRRL1|"
PACK_MAGIC = b"BOP1"


class RecipientsError(ValueError):
    pass


def next_seq(old):
    """The list's next number: never lower than the minutes since 1970, so a PC restored from an older backup (or a
    new PC) still publishes a higher number than any list the phones already keep."""
    return max(int(old or 0) + 1, int(time.time() // 60))


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def kid_hex(pub):
    return hashlib.sha256(("biruni-drive-kid-1:" + str(pub)).encode("utf-8")).hexdigest()[:16]


def check_pub(pub):
    """A recipient's public key: base64url of an uncompressed P-256 point. Returns it cleaned, or RecipientsError."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives.asymmetric import ec

    pub = str(pub or "").strip()
    try:
        raw = _unb64u(pub)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except (ValueError, TypeError) as e:
        raise RecipientsError("not a public key") from e
    return _b64u(raw)


# ---- the office's own copy: who besides this computer (a file beside the handover key) ---------------------------
def load(folder):
    """{"seq": int, "r": [{"p", "role", "scope", "n", "at"}], "emergency": {...}} - the extra recipients (the office
    itself is added when the list is built). seq 0: no list yet (phones seal v1)."""
    try:
        with open(os.path.join(folder, FILE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    seq = d.get("seq") if isinstance(d.get("seq"), int) and d.get("seq") >= 0 else 0
    rows = []
    for e in d.get("r") or []:
        if isinstance(e, dict) and e.get("role") in ROLES[1:] and isinstance(e.get("p"), str):
            scope = [k for k in (e.get("scope") or DEFAULT_SCOPE[e["role"]]) if k in KINDS]
            rows.append({"p": e["p"], "role": e["role"], "scope": scope or DEFAULT_SCOPE[e["role"]],
                         "n": str(e.get("n") or "")[:60], "at": str(e.get("at") or "")})
    out = {"seq": seq, "r": rows}
    if isinstance(d.get("emergency"), dict):
        out["emergency"] = d["emergency"]
    return out


def save(folder, doc):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return doc


def enable(folder):
    """Start a list (seq 1) with the office alone: phones then seal v2, ready for more recipients."""
    d = load(folder)
    if d["seq"] == 0:
        d["seq"] = next_seq(0)
        save(folder, d)
    return d


def add(folder, pub, role, name="", scope=None, office_pub=None):
    """Add a recipient (backup / emergency / sup). One emergency key at a time: a new one replaces the old one."""
    if role not in ROLES[1:]:
        raise RecipientsError("unknown role")
    pub = check_pub(pub)
    if office_pub and pub == office_pub:
        raise RecipientsError("this is this computer's own key")
    d = load(folder)
    rows = [e for e in d["r"] if e["p"] != pub and not (role == "emergency" and e["role"] == "emergency")]
    if len(rows) + 1 >= MAX:
        raise RecipientsError("at most 12 recipients")
    sc = [k for k in (scope or DEFAULT_SCOPE[role]) if k in KINDS] or DEFAULT_SCOPE[role]
    rows.append({"p": pub, "role": role, "scope": sc, "n": str(name or "")[:60], "at": time.strftime("%Y-%m-%d")})
    d["r"] = rows
    d["seq"] = next_seq(d["seq"])
    return save(folder, d)


def remove(folder, pub):
    d = load(folder)
    rows = [e for e in d["r"] if e["p"] != pub]
    if len(rows) != len(d["r"]):
        d["r"] = rows
        d["seq"] = next_seq(d["seq"])
        if not any(e["role"] == "emergency" for e in rows):
            d.pop("emergency", None)
        save(folder, d)
    return d


def merge(folder, doc):
    """Take a list from a key backup: the newer list wins (a list already here that is newer stays), and the number
    always moves on, so a restore never rolls the phones back."""
    cur = load(folder)
    if not isinstance(doc, dict) or not isinstance(doc.get("seq"), int):
        return cur
    top = max(cur["seq"], doc["seq"])
    if doc["seq"] > cur["seq"]:
        save(folder, doc)
    d = load(folder)
    if d["seq"] or doc["seq"]:
        d["seq"] = next_seq(top)
        save(folder, d)
    return load(folder)


# ---- the list the phones get ---------------------------------------------------------------------------------------
def build(folder, office_pub, room):
    d = load(folder)
    r = [{"k": kid_hex(office_pub), "p": office_pub, "role": "office", "scope": list(KINDS), "n": ""}]
    for e in d["r"]:
        r.append({"k": kid_hex(e["p"]), "p": e["p"], "role": e["role"], "scope": list(e["scope"]), "n": e["n"]})
    return {"v": 1, "seq": d["seq"], "room": room, "made": int(time.time()), "r": r[:MAX]}


def canonical(lst):
    return json.dumps(lst, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign_text(l_text):
    return SIGN_PREFIX + hashlib.sha256(l_text.encode("utf-8")).hexdigest()


def sign_with(sk_pkcs8_b64u, l_text):
    """(public key b64u, raw r|s signature b64u) - the same shape WebCrypto makes."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    sk = serialization.load_der_private_key(_unb64u(sk_pkcs8_b64u), None)
    r, s = decode_dss_signature(sk.sign(sign_text(l_text).encode("utf-8"), ec.ECDSA(hashes.SHA256())))
    pub = sk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64u(pub), _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def signed(folder, office_pub, key):
    """The list for the phones of one bridge key, signed by that key's identity; None while there is no list."""
    from . import bridge

    if load(folder)["seq"] < 1:
        return None
    l_text = canonical(build(folder, office_pub, key[:12]))
    ip, sig = sign_with(bridge.identity(key)[0], l_text)
    return {"l": l_text, "ip": ip, "is": sig}


def pin_of(pub):
    return _b64u(hashlib.sha256(_unb64u(pub)).digest())[:22]


def verify(rl, office_pub, room, pin=None, first_signer=None, last_seq=0):
    """The phone's checks, in Python (the tests and the key backup use it). Returns the list; RecipientsError says
    why it was refused."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if not isinstance(rl, dict) or not all(isinstance(rl.get(x), str) for x in ("l", "ip", "is")):
        raise RecipientsError("not a list")
    if pin and pin_of(rl["ip"]) != pin:
        raise RecipientsError("wrong signer")
    if not pin and first_signer and rl["ip"] != first_signer:
        raise RecipientsError("wrong signer")
    try:
        raw, sig = _unb64u(rl["ip"]), _unb64u(rl["is"])
        if len(raw) != 65 or len(sig) != 64:
            raise ValueError
        pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pk.verify(der, sign_text(rl["l"]).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError) as e:
        raise RecipientsError("bad signature") from e
    try:
        lst = json.loads(rl["l"])
    except ValueError as e:
        raise RecipientsError("not a list") from e
    if not isinstance(lst, dict) or lst.get("v") != 1 or not isinstance(lst.get("seq"), int) \
            or not isinstance(lst.get("r"), list):
        raise RecipientsError("not a list")
    if lst.get("room") != room:
        raise RecipientsError("another room")
    if lst["seq"] < last_seq:
        raise RecipientsError("rollback")
    r = lst["r"]
    if not 1 <= len(r) <= MAX:
        raise RecipientsError("at most 12 recipients")
    offices = [e for e in r if isinstance(e, dict) and e.get("role") == "office"]
    if len(offices) != 1 or offices[0].get("p") != office_pub:
        raise RecipientsError("the office key is missing")
    seen = set()
    for e in r:
        if not isinstance(e, dict) or e.get("role") not in ROLES:
            raise RecipientsError("not a list")
        check_pub(e.get("p"))
        if kid_hex(e["p"]) in seen:
            raise RecipientsError("a recipient is listed twice")
        seen.add(kid_hex(e["p"]))
    return lst


def for_kind(lst, kind):
    """The public keys a phone seals one kind of item for (office first)."""
    return [e["p"] for e in lst["r"] if kind in (e.get("scope") or DEFAULT_SCOPE.get(e.get("role"), []))]


# ---- a backup PC: what it needs from the office to read the office's Drive folder and positions --------------------
def office_pack(backup_pub, keys, surveyor_keys=None, office_pub=""):
    """The office's bridge keys (and surveyor codes), sealed (envelope v2) for one backup PC only. keys: {settings
    name: bridge key}. The file opens only with that PC's own handover key.
    Anyone can seal a file for a public key, so a pack proves nothing about who made it: the backup PC's user compares
    the office id it shows with the one on the office's screen before keeping it. With the pack the backup PC holds
    the office's bridge keys (it could make phone certificates for the office): give packs only to the office's own
    computers."""
    from . import envelope

    backup_pub = check_pub(backup_pub)
    meta = {"biruni": "office-pack", "v": 1, "made": time.strftime("%Y-%m-%d"),
            "office": kid_hex(office_pub) if office_pub else "",
            "keys": {str(n): str(k) for n, k in (keys or {}).items() if k},
            "surveyors": surveyor_keys or {"qr_mode": "shared", "surveyors": {}}}
    return PACK_MAGIC + envelope.seal([backup_pub], meta, b"")


def open_office_pack(handover_key, blob):
    """A HandoverKey and a pack -> {"keys", "surveyors"}; RecipientsError when it is not for this computer."""
    blob = bytes(blob)
    if blob[:4] != PACK_MAGIC:
        raise RecipientsError("not an office pack")
    try:
        meta, _data = handover_key.open(blob[4:])
    except ValueError as e:
        raise RecipientsError("this pack is for another computer") from e
    if meta.get("biruni") != "office-pack" or not isinstance(meta.get("keys"), dict):
        raise RecipientsError("not an office pack")
    return meta


def import_files(paths, store, handover_key, pack=None):
    """A backup PC takes in sealed files: .bfx bundles (bundle.py) and .bfp files from the office's Drive folder
    (checked with the office pack's codes, like the office's own intake). Returns {"imported", "skipped",
    "refused": [(path, reason)]}."""
    from . import bundle, drive
    from .server import BlockedDeviceError

    out = {"imported": 0, "skipped": 0, "refused": []}
    checks = []
    for k in ((pack or {}).get("keys") or {}).values():
        if isinstance(k, str) and len(k) == 43:
            checks.append((drive.room_tag(k), drive.auth_keys(k, (pack or {}).get("surveyors"))))
    for p in paths:
        try:
            with open(p, "rb") as f:
                blob = f.read()
        except OSError as e:
            out["refused"].append((p, str(e)))
            continue
        if blob[:4] == b"BFX1":
            try:
                r = bundle.import_bundle(blob, store, handover_key)
            except ValueError as e:
                out["refused"].append((p, str(e)))
                continue
            out["imported"] += r["imported"]
            out["skipped"] += r["skipped"]
            out["refused"] += [(p, why) for _i, why in r["refused"]]
            continue
        if blob[:4] != b"BFP1":
            out["refused"].append((p, "not a sealed file"))
            continue
        if not checks:
            out["refused"].append((p, "open the office pack first"))
            continue
        last = "not from this office's code"
        for room, kas in checks:
            try:
                h, body, sk = drive.verify(blob, kas, handover_key.kid, room)
                meta, data = handover_key.open(body)
                if str(meta.get("id") or "") != h["id"]:
                    raise drive.BfpError("photo id does not match its header")
                meta = dict(meta, sk=sk, route="drive")
                meta.pop("preview", None)
                _rec, new = store.save(meta, data, ssha=h["sha"])
            except BlockedDeviceError as b:
                last = b.reason
                continue
            except (ValueError, TypeError, KeyError) as e:
                last = str(e) or type(e).__name__
                continue
            out["imported" if new else "skipped"] += 1
            break
        else:
            out["refused"].append((p, last))
    return out
