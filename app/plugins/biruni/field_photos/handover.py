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

FILE = "handover-p256.key"


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


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
