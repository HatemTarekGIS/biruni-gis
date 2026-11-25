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
        # which computer a sealed item is for: the .bfp header's kid and the v2 envelope's recipient id
        self.kid = hashlib.sha256(("biruni-drive-kid-1:" + self.public).encode("utf-8")).hexdigest()[:16]

    def open(self, blob):
        """A sealed photo -> (its data, the picture). ValueError when it is not sealed for this computer."""
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = bytes(blob)
        if blob[:1] == b"\x02":   # v2: sealed for several computers (envelope.py)
            from . import envelope

            return envelope.open(blob, self._key, self.public)
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
    _keep_copy(folder, path)
    return hk.public


def backup_info(path):
    """{"pub", "made"} of a backup file, without its password; ValueError when it is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("not a key backup") from e
    if not isinstance(doc, dict) or doc.get("biruni") != BACKUP_KIND or doc.get("v") not in (1, 2) \
            or not isinstance(doc.get("pub"), str) or not isinstance(doc.get("kdf"), dict) \
            or not isinstance(doc.get("ct"), str):
        raise ValueError("not a key backup")
    return {"pub": doc["pub"], "made": str(doc.get("made") or ""), "v": doc["v"], "_doc": doc}


# ---- key backup v2 (no-loss P3): everything that makes this computer "the office" ----------------------------------
# v1 held the handover key only. The bridge keys (the QR codes) and the identity keys (the &i= pins, which now also sign
# the recipients list) stayed in this Windows account (DPAPI): a new PC restored from v1 opened Drive photos but every
# phone had to scan a new code. v2 holds them all, so a restored PC keeps the same QR codes and pins.
BRIDGE_KEYS = ("biruni/field-photos/bridge-key", "biruni/gps/bridge-key")


def _aad2(pub):
    return ("biruni-handover-key-2|" + pub).encode("ascii")


def _settings(settings):
    if settings is not None:
        return settings
    from qgis.core import QgsSettings

    return QgsSettings()


def backup_v2(folder, path, password, settings=None):
    """Write the v2 backup: the handover key, the bridge keys, the identity keys and the recipients list, sealed with
    a key derived from ``password`` (it must pass secure.passwords for an administrator). Returns the public key."""
    from ..secure import crypto, passwords
    from . import bridge, policy, recipients
    from .tunnel import unprotect

    why = passwords.problem(password, passwords.MIN_ADMIN)
    if why:
        raise ValueError(why.replace("{n}", str(passwords.MIN_ADMIN)))
    st = _settings(settings)
    hk = HandoverKey(folder)
    with open(os.path.join(folder, FILE), encoding="ascii") as f:
        pem = unprotect(f.read().strip())
    if not pem:
        raise ValueError("this computer's key could not be read")
    keys = {}
    for name in BRIDGE_KEYS:
        k = unprotect(st.value(name, "") or "")
        if len(k) == 43:
            keys[name] = k
    ids = {}
    try:
        stored = json.loads(st.value(bridge.ID_STORE, "") or "{}")
    except (TypeError, ValueError):
        stored = {}
    for h, v in (stored if isinstance(stored, dict) else {}).items():
        sk = unprotect(v or "")
        if sk:
            ids[h] = sk
    for k in keys.values():   # an identity made in this run only (no settings yet) is saved with its key
        h = bridge.identity_slot(k)
        if h not in ids:
            ids[h] = bridge.identity(k, settings=st)[0]
    body = {"handover": pem, "bridge": keys, "ids": ids, "rl": recipients.load(folder), "pl": policy.load(folder)}
    try:   # the Google Drive archive's ZIP password (archive_drive.py): without it an encrypted archive never opens
        with open(os.path.join(folder, "drive_archive_pw.dat"), encoding="ascii") as f:
            dpw = unprotect(f.read().strip())
        if dpw:
            body["darchive_pw"] = dpw
    except OSError:
        pass
    from ..backup import offsite as backup_offsite, secret as backup_secret   # P9: the office's restic and rclone passwords

    bpw = backup_secret.load_password(folder)
    if bpw:
        body["backup_pw"] = bpw
    rpw = backup_offsite.load_password(folder)
    if rpw:
        body["rclone_pw"] = rpw
    from . import mailbox   # P6 (D2): the organization's cloud mailbox secret

    mb_sec = mailbox.load_secret(st)
    if mb_sec:
        body["mb_secret"] = mb_sec
    kdf = crypto.new_kdf()
    ct = crypto.seal(crypto.derive(password, kdf), json.dumps(body).encode("utf-8"), _aad2(hk.public))
    doc = {"biruni": BACKUP_KIND, "v": 2, "pub": hk.public, "made": time.strftime("%Y-%m-%d"),
           "kdf": kdf, "ct": crypto.b64(ct)}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    _keep_copy(folder, path)
    return hk.public


def _keep_copy(folder, path):
    """Every key backup is also kept in <cert>\\keybackup\\ (the backup program carries that folder to its disk).
    A copy never overwrites an older one; failures are ignored (the backup itself is already written)."""
    try:
        dest = os.path.join(folder, "keybackup")
        if os.path.normcase(os.path.abspath(os.path.dirname(path))) == os.path.normcase(os.path.abspath(dest)):
            return
        os.makedirs(dest, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(path))
        name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{ext or '.biruni-key'}"
        n = 1
        while os.path.exists(os.path.join(dest, name)):
            n += 1
            name = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}-{n}{ext or '.biruni-key'}"
        with open(path, "rb") as a, open(os.path.join(dest, name) + ".tmp", "wb") as b:
            b.write(a.read())
        os.replace(os.path.join(dest, name) + ".tmp", os.path.join(dest, name))
    except OSError:
        pass


def _restore_v2_rest(body, folder, settings):
    """The bridge keys, identities and recipients list of a v2 backup (the handover key is already in place).
    A different bridge key already here is kept under <name>-before-restore."""
    from . import bridge, policy, recipients
    from .tunnel import protect, unprotect

    st = _settings(settings)
    for name, k in (body.get("bridge") or {}).items():
        if name not in BRIDGE_KEYS or not isinstance(k, str) or len(k) != 43:
            continue
        cur = unprotect(st.value(name, "") or "")
        if cur and cur != k:
            st.setValue(name + "-before-restore", protect(cur))
        st.setValue(name, protect(k))
    try:
        stored = json.loads(st.value(bridge.ID_STORE, "") or "{}")
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    if any(stored.get(h) and unprotect(stored[h]) != sk for h, sk in (body.get("ids") or {}).items()):
        st.setValue(bridge.ID_STORE + "-before-restore", json.dumps(stored))   # what this PC had, kept aside
    for h, sk in (body.get("ids") or {}).items():
        if isinstance(h, str) and isinstance(sk, str) and sk:
            stored.pop(h, None)
            stored[h] = protect(sk)       # newest last: kept when ID_KEEP trims
    while len(stored) > bridge.ID_KEEP:
        stored.pop(next(iter(stored)))
    st.setValue(bridge.ID_STORE, json.dumps(stored))
    if isinstance(body.get("rl"), dict):
        recipients.merge(folder, body["rl"])
    if isinstance(body.get("pl"), dict):
        policy.merge(folder, body["pl"])
    dpw = body.get("darchive_pw")
    if isinstance(dpw, str) and dpw:
        path = os.path.join(folder, "drive_archive_pw.dat")
        cur = ""
        try:
            with open(path, encoding="ascii") as f:
                cur = unprotect(f.read().strip())
        except OSError:
            cur = ""
        if cur != dpw:
            if cur:   # a different password already here (archives made with it): kept aside, never lost
                os.replace(path, path + "." + time.strftime("%Y%m%d-%H%M%S") + ".old")
            with open(path + ".tmp", "w", encoding="ascii") as f:
                f.write(protect(dpw))
            os.replace(path + ".tmp", path)
    bpw = body.get("backup_pw")
    if isinstance(bpw, str) and bpw:
        from ..backup import secret as backup_secret

        backup_secret.install_password(folder, bpw)   # a different one already here is kept as .old
    rpw = body.get("rclone_pw")
    if isinstance(rpw, str) and rpw:
        from ..backup import offsite as backup_offsite

        backup_offsite.install_password(folder, rpw)  # a different one already here is kept as .old
    mb_sec = body.get("mb_secret")
    if isinstance(mb_sec, str) and len(mb_sec) == 43:
        from . import mailbox

        cur_on = mailbox.enabled(st) if st.value(mailbox.ON_KEY, None) is not None else True
        mailbox.save_config(mb_sec, mailbox.load_days(st), settings=st, on=cur_on)
    if settings is None:
        bridge.forget_cached_identities()


def restore(folder, path, password, settings=None):
    """Make the key in a backup this computer's key (v2: also its QR codes, pins and recipients list). A different key
    already here is kept beside it (handover-p256.key.<time>.old). ValueError for a wrong password or a changed file;
    nothing is touched then."""
    from ..vendor import ensure_vendor

    ensure_vendor()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from ..secure import crypto
    from .tunnel import protect

    info = backup_info(path)
    doc = info["_doc"]
    body = None
    try:
        if doc["v"] == 2:
            body = json.loads(crypto.unseal(crypto.derive(password, doc["kdf"]), crypto.unb64(doc["ct"]),
                                            _aad2(doc["pub"])).decode("utf-8"))
            if not isinstance(body, dict) or not isinstance(body.get("handover"), str):
                raise ValueError("not a key backup")
            pem = body["handover"].encode("ascii")
        else:
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
            if body is not None:
                _restore_v2_rest(body, folder, settings)
            return doc["pub"]
        if os.path.isfile(target):
            os.replace(target, target + "." + time.strftime("%Y%m%d-%H%M%S") + ".old")
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(protect(pem.decode("ascii")))
    os.replace(tmp, target)
    if body is not None:
        _restore_v2_rest(body, folder, settings)
    return doc["pub"]
