"""Verification seals for delivered files: the office signs the SHA-256 of a file
with its own Ed25519 key, and anyone who receives the file can check, from a QR
code or a link, that it is byte-for-byte the file that was sealed. No server:
the link carries the seal after the "#", and the page on GitHub Pages checks
the file inside the browser.

The signed message is a short binary record (see message()). The link carries
the same record with only the first 8 bytes of the hash, to keep the QR code
small; the full hash comes back from the file being checked."""
import base64
import datetime as dt
import hashlib
import html
import json
import os
import secrets
import struct
import time

PAGE_URL = "https://hatemtarekgis.github.io/biruni-gis/v/"
KEY = "biruni/seal/"
MAGIC = b"BIRUNI-SEAL"
VERSION = 1
NAME_BYTES, OFFICE_BYTES, HASH_IN_LINK = 40, 60, 8
SEAL_EXT = ".seal.json"
QR_EXT = ".seal.png"
CERT_EXT = ".seal.html"

UNCHANGED, CHANGED, BAD_SIGNATURE, BAD_SEAL = "unchanged", "changed", "bad_signature", "bad_seal"


# ---- signatures: the bundled 'cryptography' library, or plain Python -------------------------------------
def _lib():
    try:
        from ..vendor import ensure_vendor

        ensure_vendor()
        from cryptography.hazmat.primitives.asymmetric import ed25519

        return ed25519
    except Exception:  # missing or broken: the plain-Python fallback signs the same way
        return None


def public_key(seed):
    lib = _lib()
    if lib is None:
        from . import ed25519

        return ed25519.public_key(seed)
    from cryptography.hazmat.primitives import serialization as ser

    return lib.Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        ser.Encoding.Raw, ser.PublicFormat.Raw)


def sign(seed, msg):
    lib = _lib()
    if lib is None:
        from . import ed25519

        return ed25519.sign(seed, msg)
    return lib.Ed25519PrivateKey.from_private_bytes(seed).sign(msg)


def verify(pub, sig, msg):
    lib = _lib()
    if lib is None:
        from . import ed25519

        return ed25519.verify(pub, sig, msg)
    try:
        lib.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except Exception:
        return False


# ---- the office key and name ------------------------------------------------------------------------------
def _settings():
    from qgis.core import QgsSettings

    return QgsSettings()


def key_pair():
    """(private seed, public key) of this Windows user, made the first time. The seed is kept in the
    settings encrypted for this user (DPAPI), so a copied profile cannot seal in the office's name."""
    from ..field_photos.tunnel import protect, unprotect

    s = _settings()
    seed = b""
    stored = s.value(KEY + "key", "") or ""
    if stored:
        try:
            seed = base64.b64decode(unprotect(stored))
        except ValueError:
            seed = b""
    if len(seed) != 32:  # first time, or a profile from another user: a new key
        seed = secrets.token_bytes(32)
        s.setValue(KEY + "key", protect(base64.b64encode(seed).decode("ascii")))
    pub = public_key(seed)
    s.setValue(KEY + "public", b64(pub))
    return seed, pub


def office():
    return str(_settings().value(KEY + "office", "") or "").strip()


def set_office(name):
    _settings().setValue(KEY + "office", str(name or "").strip())


def fingerprint(pub):
    """A short, readable fingerprint of a public key, for offices to publish: 4 groups of 4 hex digits."""
    h = hashlib.sha256(pub).hexdigest()[:16].upper()
    return " ".join(h[i:i + 4] for i in range(0, 16, 4))


# ---- packing -----------------------------------------------------------------------------------------------
def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text):
    text = str(text).strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def short(text, limit):
    """text cut to at most limit UTF-8 bytes (on a character boundary, with an ellipsis)."""
    data = str(text or "").encode("utf-8")
    if len(data) <= limit:
        return str(text or "")
    data = data[:limit - 3]
    while data and (data[-1] & 0xC0) == 0x80:
        data = data[:-1]
    if data and data[-1] >= 0xC0:
        data = data[:-1]
    return data.decode("utf-8") + "…"


def _fields(t, size, digest, pub, name, office_name):
    n, o = name.encode("utf-8"), office_name.encode("utf-8")
    return (bytes([VERSION]) + struct.pack(">I", t) + _varint(size) + digest + pub + bytes([len(n)]) + n
            + bytes([len(o)]) + o)


def message(t, size, digest, pub, name, office_name):
    """The signed bytes: a fixed prefix, then version, time, size, full SHA-256, key, name and office."""
    return MAGIC + _fields(t, size, digest, pub, name, office_name)


def pack_link(seal):
    """The record that goes after the '#' of the link: the signed fields with only the first bytes of the
    hash, then the signature."""
    f = _fields(seal["t"], seal["size"], bytes.fromhex(seal["sha256"]), unb64(seal["key"]), seal["name"],
                seal["office"])
    cut = 1 + 4 + len(_varint(seal["size"]))
    return b64(f[:cut + HASH_IN_LINK] + f[cut + 32:] + unb64(seal["signature"]))


def unpack_link(text):
    """The seal in a link (or just the part after '#'); the hash is only its first bytes ('sha256_start').
    Raises ValueError when the record is damaged."""
    text = str(text).strip()
    if "#" in text:
        text = text.split("#", 1)[1]
    try:
        b = unb64(text)
    except Exception as e:
        raise ValueError("not a seal") from e
    try:
        if b[0] != VERSION:
            raise ValueError("unknown seal version")
        t = struct.unpack(">I", b[1:5])[0]
        i, size, shift = 5, 0, 0
        while True:
            size |= (b[i] & 0x7F) << shift
            shift += 7
            i += 1
            if not b[i - 1] & 0x80:
                break
        start = b[i:i + HASH_IN_LINK]
        i += HASH_IN_LINK
        pub = b[i:i + 32]
        i += 32
        n = b[i]
        name = b[i + 1:i + 1 + n].decode("utf-8")
        i += 1 + n
        o = b[i]
        office_name = b[i + 1:i + 1 + o].decode("utf-8")
        i += 1 + o
        sig = b[i:]
    except (IndexError, UnicodeDecodeError, struct.error) as e:
        raise ValueError("damaged seal") from e
    if len(sig) != 64 or len(pub) != 32 or len(start) != HASH_IN_LINK:
        raise ValueError("damaged seal")
    return {"name": name, "size": size, "sha256_start": start.hex(), "t": t, "office": office_name,
            "key": b64(pub), "signature": b64(sig)}


# ---- sealing -----------------------------------------------------------------------------------------------
def file_sha256(path, feedback=None):
    h = hashlib.sha256()
    total, done = max(os.path.getsize(path), 1), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            done += len(chunk)
            if feedback is not None:
                if feedback.isCanceled():
                    raise InterruptedError
                feedback.setProgress(100.0 * done / total)
    return h.hexdigest()


def is_seal_output(path):
    p = path.lower()
    return p.endswith(SEAL_EXT) or p.endswith(QR_EXT) or p.endswith(CERT_EXT)


def make_seal(path, office_name=None, now=None, feedback=None):
    """The seal of a file as a dict (not written): name, size, sha256, t, office, key, signature, url…"""
    seed, pub = key_pair()
    office_name = short(office() if office_name is None else office_name, OFFICE_BYTES)
    name = short(os.path.basename(path), NAME_BYTES)
    size = os.path.getsize(path)
    digest = file_sha256(path, feedback)
    t = int(time.time() if now is None else now)
    sig = sign(seed, message(t, size, bytes.fromhex(digest), pub, name, office_name))
    seal = {"biruni_seal": VERSION, "name": name, "size": size, "sha256": digest, "t": t,
            "sealed_utc": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "office": office_name, "key": b64(pub), "fingerprint": fingerprint(pub), "signature": b64(sig)}
    seal["url"] = PAGE_URL + "#" + pack_link(seal)
    return seal


def seal_file(path, office_name=None, certificate=True, feedback=None):
    """Seals a file: writes <file>.seal.json, <file>.seal.png (QR code of the link) and, if asked,
    <file>.seal.html (a printable certificate). Returns the seal with the written paths."""
    seal = make_seal(path, office_name, feedback=feedback)
    with open(path + SEAL_EXT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(seal, f, ensure_ascii=False, indent=1)
    png = qr_png(seal["url"])
    with open(path + QR_EXT, "wb") as f:
        f.write(png)
    seal["files"] = [path + SEAL_EXT, path + QR_EXT]
    if certificate:
        with open(path + CERT_EXT, "w", encoding="utf-8", newline="\n") as f:
            f.write(certificate_html(seal, png))
        seal["files"].append(path + CERT_EXT)
    return seal


def qr_png(text, scale=6):
    from ..gps.browser import qr_png as make
    from ..vendor import ensure_vendor

    ensure_vendor()  # segno
    return make(text, scale)


# ---- checking ----------------------------------------------------------------------------------------------
def read_seal(source):
    """A seal from a .seal.json file, a link, or the text after '#'. Raises ValueError when unreadable."""
    source = str(source or "").strip()
    if os.path.isfile(source):
        try:
            with open(source, encoding="utf-8-sig") as f:
                seal = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError("not a seal file") from e
        if not isinstance(seal, dict) or seal.get("biruni_seal") != VERSION:
            raise ValueError("not a seal file")
        return seal
    return unpack_link(source)


def check_seal(seal, path, feedback=None):
    """Checks a file against a seal: (status, details). The signature is checked first: a seal whose fields
    were edited is BAD_SIGNATURE whatever the file. With a link seal (only the start of the hash), a file
    with another hash is CHANGED and the signature cannot be checked without the sealed file."""
    try:
        pub, sig = unb64(seal["key"]), unb64(seal["signature"])
        t, size, name, office_name = int(seal["t"]), int(seal["size"]), str(seal["name"]), str(seal["office"])
        full = seal.get("sha256")
        if full is not None:
            full = bytes.fromhex(full)
            if len(full) != 32:
                raise ValueError
    except (KeyError, TypeError, ValueError):
        return BAD_SEAL, {}
    details = {"name": name, "size": size, "office": office_name, "t": t, "fingerprint": fingerprint(pub),
               "sealed_utc": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if full is not None and not verify(pub, sig, message(t, size, full, pub, name, office_name)):
        return BAD_SIGNATURE, details
    got_size = os.path.getsize(path)
    got = file_sha256(path, feedback)
    details.update(file_size=got_size, file_sha256=got)
    if full is None:
        if got_size != size or not got.startswith(seal.get("sha256_start", "-")):
            return CHANGED, details
        if not verify(pub, sig, message(t, size, bytes.fromhex(got), pub, name, office_name)):
            return BAD_SIGNATURE, details
        return UNCHANGED, details
    return (UNCHANGED if got_size == size and got == full.hex() else CHANGED), details


def check_file(path, source=None, feedback=None):
    """check_seal() with the seal read from source (default: <file>.seal.json next to the file)."""
    try:
        seal = read_seal(source or path + SEAL_EXT)
    except ValueError:
        return BAD_SEAL, {}
    return check_seal(seal, path, feedback)


# ---- the printable certificate -----------------------------------------------------------------------------
def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


def certificate_html(seal, png):
    """One printable page next to the file: what was sealed, by which key, and the QR code to check it."""
    from ..i18n import is_rtl, lang, tr

    e = html.escape
    when = dt.datetime.fromtimestamp(seal["t"]).strftime("%Y-%m-%d %H:%M")
    rows = [(tr("File"), seal["name"]), (tr("Size"), f"{human_size(seal['size'])} ({seal['size']:,} bytes)"),
            (tr("Sealed on"), when), (tr("Office"), seal["office"] or "—"),
            (tr("Key fingerprint"), seal["fingerprint"]), ("SHA-256", seal["sha256"])]
    body = "".join(f"<tr><th>{e(k)}</th><td{' class=mono' if k in ('SHA-256', tr('Key fingerprint')) else ''}>"
                   f"<bdi>{e(str(v))}</bdi></td></tr>" for k, v in rows)
    qr = base64.b64encode(png).decode("ascii")
    return f"""<!doctype html><html lang="{e(lang().replace('_', '-'))}" dir="{'rtl' if is_rtl() else 'ltr'}">
<head><meta charset="utf-8"><title>{e(tr("Verification seal"))} - {e(seal["name"])}</title><style>
body{{font:15px/1.5 system-ui,"Segoe UI","Noto Sans Arabic",Tahoma,sans-serif;color:#14181D;background:#F4F6F8;margin:0}}
.page{{max-width:720px;margin:24px auto;background:#fff;border:1px solid #E1E6EB;border-radius:20px;padding:28px 32px}}
h1{{font-size:24px;margin:0 0 4px;color:#12A594}}p{{color:#5F6B76;margin:6px 0 18px}}
.top{{display:flex;gap:24px;align-items:flex-start}}.top img{{width:190px;height:190px;image-rendering:pixelated}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:start;padding:7px 8px;border-bottom:1px solid #E1E6EB;vertical-align:top}}
th{{color:#5F6B76;font-weight:600;white-space:nowrap;width:1%}}.mono{{font-family:Consolas,monospace;word-break:break-all}}
a{{color:#12A594;word-break:break-all;font-size:12px}}.foot{{font-size:12px;color:#5F6B76;margin-top:18px}}
@media print{{body{{background:#fff}}.page{{border:0;margin:0}}}}
</style></head><body><div class="page"><div class="top"><div style="flex:1">
<h1>{e(tr("Verification seal"))}</h1>
<p>{e(tr("Scan the code or open the link to check that the file you received is exactly the one that was "
         "sealed. The check runs in your browser: the file is not uploaded."))}</p></div>
<img alt="QR" src="data:image/png;base64,{qr}"></div>
<table>{body}</table>
<p class="foot">{e(tr("The seal proves that the file is unchanged since it was sealed with this key. The office "
                      "name is written by whoever sealed it: compare the key fingerprint with the one the office "
                      "publishes."))}</p>
<a href="{e(seal["url"])}">{e(seal["url"])}</a>
<p class="foot">{e(tr("Sealed with Biruni GIS"))}</p></div></body></html>
"""
