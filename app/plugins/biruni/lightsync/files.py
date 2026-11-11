"""A file as the bytes of one light transfer: a small header (name, size, checksum), then the content -
deflated, or sealed with a passphrase (AES-256-GCM, the key from Argon2id/scrypt) when the user gives one.

    "BLF1" | header length (4, big-endian) | header JSON | body
"""
import hashlib
import json
import os
import struct
import zlib

from ..secure import crypto

MAGIC = b"BLF1"
AAD = b"biruni-light-file-1"


class FileError(Exception):
    """Not a file sent by light, a wrong passphrase, or damaged content."""


def pack(name, content, passphrase=None):
    content = bytes(content)
    head = {"name": os.path.basename(str(name)) or "file", "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}
    if passphrase:
        kdf = crypto.new_kdf()
        body = crypto.seal(crypto.derive(passphrase, kdf), content, AAD, compress=True)
        head["kdf"] = kdf
    else:
        z = zlib.compress(content, 6)
        head["z"] = len(z) < len(content)
        body = z if head["z"] else content
    h = json.dumps(head, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return MAGIC + struct.pack(">I", len(h)) + h + body


def header(data):
    """The header of packed bytes (raises FileError)."""
    data = bytes(data)
    if data[:4] != MAGIC or len(data) < 8:
        raise FileError("format")
    n = struct.unpack(">I", data[4:8])[0]
    try:
        head = json.loads(data[8:8 + n].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FileError("format") from None
    if not isinstance(head, dict) or "size" not in head:
        raise FileError("format")
    head["_body"] = 8 + n
    return head


def sealed(data):
    return bool(header(data).get("kdf"))


def unpack(data, passphrase=None):
    """(file name, content). FileError("passphrase") for a sealed file without the right passphrase."""
    head = header(data)
    body = bytes(data)[head["_body"]:]
    size = int(head["size"])
    if head.get("kdf"):
        if not passphrase:
            raise FileError("passphrase")
        try:
            content = crypto.unseal(crypto.derive(passphrase, head["kdf"]), body, AAD, max_size=size)
        except crypto.CryptoError:
            raise FileError("passphrase") from None
    elif head.get("z"):
        d = zlib.decompressobj()
        try:
            content = d.decompress(body, size + 1)
        except zlib.error:
            raise FileError("damaged") from None
    else:
        content = body
    if len(content) != size or hashlib.sha256(content).hexdigest() != head.get("sha256"):
        raise FileError("damaged")
    name = os.path.basename(str(head.get("name") or "file").replace("\\", "/")) or "file"
    return name, content
