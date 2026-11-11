"""Cryptographic building blocks of locked files. Nothing here is imported at start-up: the bundled
'cryptography' library is loaded on first use.

- Keys from passwords: Argon2id (256 MiB, 4 passes, 4 lanes) with a random 16-byte salt per user;
  scrypt (N=2^17, r=8) where Argon2id is missing. The parameters are stored with each user, so they can
  be raised later without breaking older files.
- Data: AES-256-GCM, a random 96-bit nonce per message, and associated data that binds each ciphertext to
  its file family and its role (a layer's data cannot be swapped for another's).
- Signatures: Ed25519.
- Entries only the administrator may read (the activity log): X25519 + HKDF-SHA256 + AES-256-GCM to the
  administrator's box key."""
import base64
import hashlib
import json
import os
import zlib

ARGON2 = {"alg": "argon2id", "m": 262144, "t": 4, "p": 4}
SCRYPT = {"alg": "scrypt", "n": 1 << 17, "r": 8, "p": 1}


class CryptoError(Exception):
    """Decryption failed: a wrong key or password, or altered bytes."""


def _vendor():
    from ..vendor import ensure_vendor

    ensure_vendor()


def b64(data):
    return base64.b64encode(bytes(data)).decode("ascii")


def unb64(text):
    return base64.b64decode(text or "")


def canonical(obj):
    """The exact bytes that are signed: sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def random_key():
    return os.urandom(32)


# ---- keys from passwords ------------------------------------------------------------------------------
def new_kdf():
    """Fresh key-derivation parameters (with a new salt) for one password."""
    _vendor()
    try:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id  # noqa: F401

        params = dict(ARGON2)
    except ImportError:
        params = dict(SCRYPT)
    params["salt"] = b64(os.urandom(16))
    return params


def derive(password, params):
    """The 32-byte key of a password with the stored parameters (slow on purpose: ~0.3 s, 256 MiB)."""
    _vendor()
    pw = str(password).encode("utf-8")
    salt = unb64(params["salt"])
    if params.get("alg") == "argon2id":
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

        return Argon2id(salt=salt, length=32, iterations=int(params["t"]), lanes=int(params["p"]),
                        memory_cost=int(params["m"])).derive(pw)
    if params.get("alg") == "scrypt":
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        return Scrypt(salt=salt, length=32, n=int(params["n"]), r=int(params["r"]), p=int(params["p"])).derive(pw)
    raise CryptoError("unknown key derivation")


def hkdf(secret, salt, info, length=32):
    _vendor()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


# ---- authenticated encryption ------------------------------------------------------------------------
def seal(key, data, aad=b"", compress=False):
    """nonce + AES-256-GCM(data); compress=True deflates first (a flag byte says which)."""
    _vendor()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    body = (b"\x01" + zlib.compress(bytes(data), 1)) if compress else (b"\x00" + bytes(data))
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, body, aad)


def unseal(key, blob, aad=b"", max_size=None):
    """The data sealed by seal(). ``max_size`` caps the size once decompressed: a few compressed megabytes can
    expand to gigabytes (a "zip bomb") - a server opening messages from others must set it."""
    _vendor()
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = bytes(blob)
    if len(blob) < 29:
        raise CryptoError("too short")
    try:
        body = AESGCM(key).decrypt(blob[:12], blob[12:], aad)
    except InvalidTag as e:
        raise CryptoError("wrong key or altered data") from e
    if body[:1] != b"\x01":
        return body[1:]
    if max_size is None:
        return zlib.decompress(body[1:])
    d = zlib.decompressobj()
    out = d.decompress(body[1:], max_size + 1)
    if len(out) > max_size or d.unconsumed_tail:
        raise CryptoError("too large")
    return out


def seal_json(key, obj, aad=b""):
    return b64(seal(key, canonical(obj), aad, compress=True))


def unseal_json(key, text, aad=b""):
    return json.loads(unseal(key, unb64(text), aad).decode("utf-8"))


# ---- signatures -----------------------------------------------------------------------------------------
def public_key(seed):
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)


def sign(seed, msg):
    _vendor()
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PrivateKey.from_private_bytes(seed).sign(msg)


def verify(pub, sig, msg):
    _vendor()
    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except Exception:  # noqa: BLE001 - any failure is "not valid"
        return False


def fingerprint(pub):
    h = hashlib.sha256(pub).hexdigest()[:16].upper()
    return " ".join(h[i:i + 4] for i in range(0, 16, 4))


# ---- sealed boxes to the administrator ------------------------------------------------------------------
def box_keypair():
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import x25519

    priv = x25519.X25519PrivateKey.generate()
    raw = priv.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    return raw, priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)


def box_seal(pub, data, aad=b""):
    """Encrypts for the holder of the X25519 key 'pub' only: (ephemeral public key, ciphertext)."""
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import x25519

    eph = x25519.X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    shared = eph.exchange(x25519.X25519PublicKey.from_public_bytes(pub))
    key = hkdf(shared, eph_pub + pub, b"biruni-locked-box")
    return eph_pub, seal(key, data, aad, compress=True)


def box_public(priv):
    """The X25519 public key of a private one (32 bytes each)."""
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import x25519

    return x25519.X25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(ser.Encoding.Raw,
                                                                                      ser.PublicFormat.Raw)


def box_open(priv, eph_pub, blob, aad=b""):
    _vendor()
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import x25519

    me = x25519.X25519PrivateKey.from_private_bytes(priv)
    pub = me.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    shared = me.exchange(x25519.X25519PublicKey.from_public_bytes(eph_pub))
    return unseal(hkdf(shared, eph_pub + pub, b"biruni-locked-box"), blob, aad)


# ---- the recovery key -------------------------------------------------------------------------------------
_B32 = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1: easy to copy by hand


def new_recovery_code():
    """160 random bits written as 8 groups of 4 characters, e.g. 7KQM-2XHD-…"""
    n = int.from_bytes(os.urandom(20), "big")
    chars = []
    for _ in range(32):
        n, r = divmod(n, 32)
        chars.append(_B32[r])
    s = "".join(chars)
    return "-".join(s[i:i + 4] for i in range(0, 32, 4))


def normalize_recovery_code(text):
    return "".join(ch for ch in str(text or "").upper() if ch.isascii() and ch.isalnum())


def recovery_key(code, family):
    """The 32-byte key of a recovery code. The code has 160 random bits, so a fast derivation is enough."""
    s = normalize_recovery_code(code)
    if len(s) != 32 or any(ch not in _B32 for ch in s):
        raise CryptoError("not a recovery key")
    return hkdf(s.encode("ascii"), family.encode("ascii"), b"biruni-locked-recovery")
