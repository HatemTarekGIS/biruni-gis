"""Envelope v2: one sealed item that several computers can open (no-loss P3).

v1 (handover.seal) is sealed for one computer only: if that computer's key is lost, every photo waiting for it is
lost too. v2 seals the item once under a random content key (CK) and wraps CK for each recipient: the office, a
backup PC, the emergency key held in 3-of-5 shares, a supervisor.

Layout:  0x02 | n (1 byte, 1..12) | eph (65, uncompressed P-256) | n x [kid (8) | wrap (48)] | iv (12) | ct

  kid      first 8 bytes of SHA-256("biruni-drive-kid-1:" + recipient public key, base64url) (= drive.key_id)
  wrap_i   AES-256-GCM(KEK_i, nonce = 12 zero bytes, CK) (32 + 16 tag). KEK_i = HKDF-SHA256(ECDH(eph, R_i),
           salt = eph || R_i (65 + 65 bytes), info = "biruni-seal-2"). Each KEK is used for one message only (eph is
           new every time), so the fixed nonce is safe.
  ct       AES-256-GCM(CK, iv, plaintext, aad = every byte before iv). Changing the header (dropping a recipient,
           swapping a wrap) breaks the tag.
  plaintext  the same as v1: the length of the meta JSON (4 bytes, big-endian) | meta | data.

The phone page has the same code in JS (server.py PAGE, ENV2); tests/test_envelope.py checks both make the very
same bytes from the same inputs.
"""
import base64
import hashlib
import json
import os

VERSION = 2
MAX_RECIPIENTS = 12
EPH = 65
KID = 8
WRAP = 48
ENTRY = KID + WRAP
IV = 12
INFO = b"biruni-seal-2"
ZERO_IV = bytes(12)


class EnvelopeError(ValueError):
    """Not a v2 envelope this key opens (the message says why, in English, for the log)."""


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(s):
    s = str(s)
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def kid(pub):
    """The 8-byte id of a recipient's public key (base64url of the 65-byte point)."""
    return hashlib.sha256(("biruni-drive-kid-1:" + str(pub)).encode("utf-8")).digest()[:KID]


def _crypto():
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return hashes, serialization, ec, AESGCM, HKDF


def _point(pub_raw):
    _h, _s, ec, _a, _k = _crypto()
    if len(pub_raw) != EPH or pub_raw[0] != 4:
        raise EnvelopeError("bad public key")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_raw)
    except ValueError as e:
        raise EnvelopeError("bad public key") from e


def _kek(shared, eph_raw, r_raw):
    hashes, _s, _e, _a, HKDF = _crypto()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_raw + r_raw, info=INFO).derive(shared)


def pack_plain(meta, data):
    m = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    return len(m).to_bytes(4, "big") + m + bytes(data)


def unpack_plain(plain):
    n = int.from_bytes(plain[:4], "big")
    if n <= 0 or 4 + n > len(plain):
        raise EnvelopeError("damaged")
    meta = json.loads(plain[4:4 + n].decode("utf-8"))
    if not isinstance(meta, dict):
        raise EnvelopeError("damaged")
    return meta, plain[4 + n:]


def seal_bytes(pubs, plain, _eph=None, _ck=None, _iv=None):
    """Seal ``plain`` for every public key in ``pubs`` (base64url, 65-byte points). _eph (an EC private key), _ck and
    _iv are for the test vectors only."""
    _h, serialization, ec, AESGCM, _k = _crypto()
    pubs = list(pubs)
    if not 1 <= len(pubs) <= MAX_RECIPIENTS:
        raise EnvelopeError("1 to 12 recipients")
    kids = [kid(p) for p in pubs]
    if len(set(kids)) != len(kids):
        raise EnvelopeError("a recipient is listed twice")
    eph = _eph or ec.generate_private_key(ec.SECP256R1())
    eph_raw = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ck = bytes(_ck) if _ck is not None else os.urandom(32)
    iv = bytes(_iv) if _iv is not None else os.urandom(IV)
    if len(ck) != 32 or len(iv) != IV:
        raise EnvelopeError("bad test input")
    head = bytearray([VERSION, len(pubs)]) + eph_raw
    for p, k in zip(pubs, kids):
        r_raw = _unb64u(p)
        shared = eph.exchange(ec.ECDH(), _point(r_raw))
        head += k + AESGCM(_kek(shared, eph_raw, r_raw)).encrypt(ZERO_IV, ck, None)
    head = bytes(head)
    return head + iv + AESGCM(ck).encrypt(iv, bytes(plain), head)


def seal(pubs, meta, data):
    """(meta, data) sealed for several computers: the v2 twin of handover.seal."""
    return seal_bytes(pubs, pack_plain(meta, data))


def parse(blob):
    """(n, eph_raw, [(kid, wrap)], header bytes, iv, ct) - the shape only, nothing is trusted yet."""
    blob = bytes(blob)
    if len(blob) < 2 or blob[0] != VERSION:
        raise EnvelopeError("not a v2 envelope")
    n = blob[1]
    if not 1 <= n <= MAX_RECIPIENTS:
        raise EnvelopeError("bad recipient count")
    hl = 2 + EPH + n * ENTRY
    if len(blob) < hl + IV + 16:
        raise EnvelopeError("damaged")
    eph_raw = blob[2:2 + EPH]
    entries = []
    for i in range(n):
        o = 2 + EPH + i * ENTRY
        entries.append((blob[o:o + KID], blob[o + KID:o + ENTRY]))
    return n, eph_raw, entries, blob[:hl], blob[hl:hl + IV], blob[hl + IV:]


def kids(blob):
    """The recipient ids of a v2 envelope (hex, like drive.key_id); [] for anything else."""
    try:
        return [k.hex() for k, _w in parse(blob)[2]]
    except EnvelopeError:
        return []


def open_bytes(blob, private_key, pub=None):
    """The plaintext of a v2 envelope, for one recipient's EC private key. EnvelopeError when it is not for it or the
    envelope was changed."""
    from cryptography.exceptions import InvalidTag

    _h, serialization, ec, AESGCM, _k = _crypto()
    _n, eph_raw, entries, head, iv, ct = parse(blob)
    r_raw = private_key.public_key().public_bytes(serialization.Encoding.X962,
                                                  serialization.PublicFormat.UncompressedPoint)
    my = kid(pub or _b64u(r_raw))
    mine = [w for k, w in entries if k == my]
    if not mine:
        raise EnvelopeError("sealed for another computer")
    try:
        shared = private_key.exchange(ec.ECDH(), _point(eph_raw))
        kek = _kek(shared, eph_raw, r_raw)
    except ValueError as e:
        raise EnvelopeError("damaged") from e
    for w in mine:
        try:
            ck = AESGCM(kek).decrypt(ZERO_IV, w, None)
        except InvalidTag:
            continue
        try:
            return AESGCM(ck).decrypt(iv, ct, head)
        except InvalidTag as e:
            raise EnvelopeError("the envelope was changed") from e
    raise EnvelopeError("sealed for another computer")


def open(blob, private_key, pub=None):  # noqa: A001 - the module's verb, like handover.HandoverKey.open
    """A v2 envelope -> (meta, data)."""
    return unpack_plain(open_bytes(blob, private_key, pub))
