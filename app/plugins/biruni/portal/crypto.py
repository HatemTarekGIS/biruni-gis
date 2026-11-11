"""The cryptography of a signed portal (SPEC.md §3). Everything here has a twin in the viewer page (page.py), so the
formats are kept deliberately plain: no compression flag, no canonical JSON to re-create in JavaScript - what is
signed is the exact UTF-8 text that is stored.

- signatures: Ed25519 over the stored text of the index and of each item manifest;
- an item shared with groups: AES-256-GCM with its own content key (CEK), aad = "item:<id>";
- the CEK wrapped for each group: AES-256-GCM with the group key, aad = "cek:<item>:<group>:<version>";
- a group key sent to a member: X25519 (ephemeral) -> HKDF-SHA256 (salt = eph || member, info "biruni-portal-group")
  -> AES-256-GCM, aad = "group:<portal>:<group>:<version>";
- a member's key file for the browser: PBKDF2-SHA256 (600 000 rounds) -> AES-256-GCM, aad "biruni-portal-key"
  (PBKDF2, not Argon2id, because every browser has it in WebCrypto).
"""
import base64
import hashlib
import json
import os

PBKDF2_ROUNDS = 600_000
KEY_KIND = "biruni-portal-key"
CARD_PREFIX = "BPM1"


class PortalCryptoError(Exception):
    """A wrong key or passphrase, or altered bytes."""


def _vendor():
    from ..vendor import ensure_vendor

    ensure_vendor()


def b64u(data):
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def unb64u(text):
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sha256_hex(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def text_of(obj):
    """The stored (and signed) text of a JSON object: sorted keys, no spaces, UTF-8, integers only by design."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---- signatures -------------------------------------------------------------------------------------------------------
def ed_public(seed):
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(ser.Encoding.Raw,
                                                                                         ser.PublicFormat.Raw)


def sign_text(seed, text):
    _vendor()
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return b64u(ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(text.encode("utf-8")))


def verify_text(pub, sig_b64u, text):
    _vendor()
    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        pub = bytes.fromhex(pub) if isinstance(pub, str) else bytes(pub)
        ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(unb64u(sig_b64u), str(text).encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "not signed by this key"
        return False


def key_print(pub):
    """A short fingerprint to read aloud: 8 characters of SHA-256 in Crockford base32, e.g. 7KQM-2XHD."""
    pub = bytes.fromhex(pub) if isinstance(pub, str) else bytes(pub)
    h = hashlib.sha256(pub).digest()
    alpha = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    n = int.from_bytes(h[:5], "big")
    s = "".join(alpha[(n >> (35 - 5 * i)) & 31] for i in range(8))
    return s[:4] + "-" + s[4:]


# ---- AES-GCM ------------------------------------------------------------------------------------------------------------
def seal(key, data, aad):
    _vendor()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(bytes(key)).encrypt(nonce, bytes(data), aad.encode("utf-8"))


def unseal(key, blob, aad):
    _vendor()
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = bytes(blob)
    if len(blob) < 28:
        raise PortalCryptoError("too short")
    try:
        return AESGCM(bytes(key)).decrypt(blob[:12], blob[12:], aad.encode("utf-8"))
    except InvalidTag as e:
        raise PortalCryptoError("wrong key or altered data") from e


def new_key():
    return os.urandom(32)


# ---- X25519 boxes --------------------------------------------------------------------------------------------------------
def box_public(priv):
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import x25519

    return x25519.X25519PrivateKey.from_private_bytes(bytes(priv)).public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)


def _hkdf(secret, salt, info):
    _vendor()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(secret)


def box_to(member_pub, data, aad):
    """Encrypt ``data`` for the holder of the X25519 key ``member_pub`` -> {"e": eph, "c": ciphertext} (base64url)."""
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import x25519

    member_pub = bytes.fromhex(member_pub) if isinstance(member_pub, str) else bytes(member_pub)
    eph = x25519.X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    shared = eph.exchange(x25519.X25519PublicKey.from_public_bytes(member_pub))
    key = _hkdf(shared, eph_pub + member_pub, b"biruni-portal-group")
    return {"e": b64u(eph_pub), "c": b64u(seal(key, data, aad))}


def box_open(priv, env, aad):
    _vendor()
    from cryptography.hazmat.primitives.asymmetric import x25519

    me = x25519.X25519PrivateKey.from_private_bytes(bytes(priv))
    pub = box_public(priv)
    eph_pub = unb64u(env["e"])
    shared = me.exchange(x25519.X25519PublicKey.from_public_bytes(eph_pub))
    return unseal(_hkdf(shared, eph_pub + pub, b"biruni-portal-group"), unb64u(env["c"]), aad)


# ---- member keys (for a phone or a browser) ------------------------------------------------------------------------------
def new_member_key(name):
    """A fresh member key: (private 32 bytes, public card text)."""
    priv = os.urandom(32)
    return priv, member_card(name, box_public(priv))


def member_card(name, box_pub):
    """The public card of a member, to hand to a portal owner (a file or a QR code): BPM1.<b64u(json)>."""
    body = text_of({"v": 1, "name": str(name or "")[:80], "box": bytes(box_pub).hex()})
    return f"{CARD_PREFIX}.{b64u(body.encode('utf-8'))}"


def parse_card(text):
    """-> {"name", "box"} of a member card; ValueError when it is not one."""
    text = str(text or "").strip()
    if not text.startswith(CARD_PREFIX + "."):
        raise ValueError("not a member card")
    try:
        d = json.loads(unb64u(text[len(CARD_PREFIX) + 1:]).decode("utf-8"))
        box = bytes.fromhex(d["box"])
        assert len(box) == 32
    except Exception as e:  # noqa: BLE001
        raise ValueError("not a member card") from e
    return {"name": str(d.get("name") or "")[:80], "box": box.hex()}


def _pbkdf2(passphrase, salt, rounds):
    _vendor()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=rounds).derive(
        str(passphrase).encode("utf-8"))


def key_file(name, priv, passphrase=None, rounds=PBKDF2_ROUNDS):
    """The member key file opened in the viewer page (JSON text). With a passphrase the private key is sealed."""
    d = {"kind": KEY_KIND, "v": 1, "name": str(name or "")[:80], "box": box_public(priv).hex()}
    if passphrase:
        salt = os.urandom(16)
        d["kdf"] = {"alg": "pbkdf2-sha256", "it": int(rounds), "salt": b64u(salt)}
        d["ct"] = b64u(seal(_pbkdf2(passphrase, salt, rounds), priv, KEY_KIND))
    else:
        d["priv"] = bytes(priv).hex()
    return json.dumps(d, ensure_ascii=False, indent=1)


def read_key_file(text, passphrase=None):
    """-> (name, private key) of a member key file; PortalCryptoError on a wrong passphrase."""
    d = json.loads(text)
    if d.get("kind") != KEY_KIND:
        raise ValueError("not a portal key file")
    if "priv" in d:
        priv = bytes.fromhex(d["priv"])
    else:
        if not passphrase:
            raise PortalCryptoError("passphrase needed")
        k = d["kdf"]
        priv = unseal(_pbkdf2(passphrase, unb64u(k["salt"]), int(k["it"])), unb64u(d["ct"]), KEY_KIND)
    if box_public(priv).hex() != d.get("box"):
        raise PortalCryptoError("the key file does not match its public key")
    return d.get("name") or "", priv
