"""Photos handed from phone to phone, sealed for this computer.

Without internet two phones can link directly (two QR codes, see the phone page) and one hands its waiting
photos to the other. Each photo is sealed for this computer: ECDH on P-256 with this computer's key, then
AES-256-GCM with SHA-256 of the shared secret. The phone that carries it cannot open it; whichever phone reaches
the computer first delivers it, and the computer keeps one copy (the photo's own id).

A sealed photo: 1 (version) | the sender's one-time public key (65 bytes, uncompressed) | nonce (12) | ciphertext.
Inside: the length of the photo's data (4 bytes, big-endian) | the data (JSON) | the picture.
"""
import base64
import hashlib
import json
import os
import time

FILE = "handover-p256.key"


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def seal(pub, meta, data):
    """Seal (meta, data) for a computer's public key (same ECDH P-256 + AES-256-GCM envelope as the phone page)."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw_pub = base64.urlsafe_b64decode(str(pub) + "==="[: (len(str(pub)) + 3) % 4])
    pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw_pub)
    eph = ec.generate_private_key(ec.SECP256R1())
    shared = eph.exchange(ec.ECDH(), pk)
    eph_raw = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    iv = os.urandom(12)
    m_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    plain = len(m_bytes).to_bytes(4, "big") + m_bytes + bytes(data)
    ct = AESGCM(hashlib.sha256(shared).digest()).encrypt(iv, plain, None)
    return b"\x01" + eph_raw + iv + ct


class HandoverKey:
    """This computer's key for sealed photos: made once, kept in ``folder`` (protected for this Windows user)."""

    def __init__(self, folder):
        from ..vendor import ensure_vendor

        ensure_vendor()
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from .tunnel import protect, unprotect

        path = os.path.join(folder, FILE)
        key = None
        try:
            with open(path, encoding="ascii") as f:
                pem = unprotect(f.read().strip())
            key = serialization.load_pem_private_key(pem.encode("ascii"), None) if pem else None
        except (OSError, ValueError, TypeError):
            key = None
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            if os.path.isfile(path):
                # never write over a key that could not be read: photos waiting in Google Drive are sealed for it,
                # and a backup or another Windows account may still open it
                try:
                    os.replace(path, path + ".unreadable-" + time.strftime("%Y%m%d-%H%M%S"))
                except OSError:
                    pass
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode("ascii")
            os.makedirs(folder, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="ascii") as f:
                f.write(protect(pem))
            os.replace(tmp, path)
        self._key = key
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public = _b64u(raw)

    def open(self, blob):
        """A sealed photo -> (its data, the picture). ValueError when it is not sealed for this computer."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = bytes(blob)
        if len(blob) < 1 + 65 + 12 + 16 or blob[0] != 1:
            raise ValueError("not a sealed photo")
        try:
            eph = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), blob[1:66])
            shared = self._key.exchange(ec.ECDH(), eph)
            plain = AESGCM(hashlib.sha256(shared).digest()).decrypt(blob[66:78], blob[78:], None)
        except (InvalidTag, ValueError) as e:
            raise ValueError("sealed for another computer") from e
        n = int.from_bytes(plain[:4], "big")
        if n <= 0 or 4 + n > len(plain):
            raise ValueError("damaged")
        meta = json.loads(plain[4:4 + n].decode("utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("damaged")
        return meta, plain[4 + n:]


# ---- a backup of this computer's key, locked with a password ------------------------------------------------------
BACKUP_KIND = "biruni-handover-key"


def _aad(pub):
    return ("biruni-handover-key-1|" + pub).encode("ascii")


def backup(folder, path, password):
    """Write this computer's key to ``path``, sealed with a key derived from ``password`` (Argon2id or scrypt,
    then AES-256-GCM; the public key is bound in as associated data). Returns the public key."""
    from ..secure import crypto
    from .tunnel import unprotect

    hk = HandoverKey(folder)          # makes the key when there is none yet: the backup is of the key in use
    with open(os.path.join(folder, FILE), encoding="ascii") as f:
        pem = unprotect(f.read().strip())
    if not pem:
        raise ValueError("this computer's key could not be read")
    kdf = crypto.new_kdf()
    ct = crypto.seal(crypto.derive(password, kdf), pem.encode("ascii"), _aad(hk.public))
    doc = {"biruni": BACKUP_KIND, "v": 1, "pub": hk.public, "made": time.strftime("%Y-%m-%d"),
           "kdf": kdf, "ct": crypto.b64(ct)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    return hk.public


def backup_info(path):
    """{"pub", "made"} of a backup file, without its password; ValueError when it is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("not a key backup") from e
    if not isinstance(doc, dict) or doc.get("biruni") != BACKUP_KIND or doc.get("v") != 1             or not isinstance(doc.get("pub"), str) or not isinstance(doc.get("kdf"), dict)             or not isinstance(doc.get("ct"), str):
        raise ValueError("not a key backup")
    return {"pub": doc["pub"], "made": str(doc.get("made") or ""), "_doc": doc}


def restore(folder, path, password):
    """Make the key in a backup this computer's key. A different key already here is kept beside it
    (handover-p256.key.<time>.old). ValueError for a wrong password or a changed file; nothing is touched then."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from ..secure import crypto
    from .tunnel import protect

    info = backup_info(path)
    doc = info["_doc"]
    try:
        pem = crypto.unseal(crypto.derive(password, doc["kdf"]), crypto.unb64(doc["ct"]), _aad(doc["pub"]))
        key = serialization.load_pem_private_key(pem, None)
    except (crypto.CryptoError, ValueError, TypeError, KeyError) as e:
        raise ValueError("wrong password or a changed file") from e
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("not a key backup")
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    if _b64u(raw) != doc["pub"]:
        raise ValueError("wrong password or a changed file")
    target = os.path.join(folder, FILE)
    os.makedirs(folder, exist_ok=True)
    if os.path.isfile(target):
        current = None
        try:
            current = HandoverKey(folder).public
        except Exception:  # noqa: BLE001 - an unreadable key was already moved aside by HandoverKey
            current = None
        if current == doc["pub"]:
            return doc["pub"]
        if os.path.isfile(target):
            os.replace(target, target + "." + time.strftime("%Y%m%d-%H%M%S") + ".old")
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(protect(pem.decode("ascii")))
    os.replace(tmp, target)
    return doc["pub"]
