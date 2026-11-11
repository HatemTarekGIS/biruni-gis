"""The frames of a light transfer: one fountain piece per QR code, and reading them back from a camera image.

A frame (all numbers big-endian), then as QR bytes:

    "BL" | version 1 | flags | transfer id (4) | payload length (4) | K blocks (4) | block size (2)
    | piece index (4) | first 8 bytes of sha256(payload) | piece (block size) | crc32 of all before (4)

Frames of another transfer, of another version or with a bad CRC are dropped; the payload's sha256 is
checked once it is whole, so a wrong piece cannot slip through unnoticed.
"""
import hashlib
import os
import struct
import zlib

import numpy as np

from . import fountain

MAGIC = b"BL"
VERSION = 1
HEAD = struct.Struct(">2sBBIIIHI8s")
OVERHEAD = HEAD.size + 4

# (block bytes per frame, frames per second): what a screen and a camera can carry. A frame of about 1 kB is
# a QR code of version 23 with low error correction: an ordinary laptop webcam at 720p reads it at arm's length
# from a full-screen window.
PRESETS = {
    "weak": (380, 6),       # an old or blurry camera, a small screen: version ~14
    "normal": (1000, 10),
    "strong": (1600, 12),   # a sharp 1080p camera close to a big screen: version ~30
}
MAX_PAYLOAD = 8 * 2**20     # 8 MB: about ten minutes at the normal speed; elimination grows with K squared


def _vendor():
    from ..vendor import ensure_vendor

    ensure_vendor()


class Frame:
    __slots__ = ("tid", "length", "k", "block", "index", "digest", "piece", "flags")

    def __init__(self, tid, length, k, block, index, digest, piece, flags=0):
        self.tid, self.length, self.k, self.block = tid, length, k, block
        self.index, self.digest, self.piece, self.flags = index, digest, piece, flags


def pack(f):
    body = HEAD.pack(MAGIC, VERSION, f.flags, f.tid, f.length, f.k, f.block, f.index, f.digest) + f.piece
    return body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def unpack(raw):
    """The Frame in ``raw``, or None when it is not one of ours or is damaged."""
    raw = bytes(raw)
    if len(raw) < OVERHEAD or raw[:2] != MAGIC:
        return None
    body, crc = raw[:-4], struct.unpack(">I", raw[-4:])[0]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        return None
    magic, ver, flags, tid, length, k, block, index, digest = HEAD.unpack_from(body)
    if ver != VERSION:
        return None
    piece = body[HEAD.size:]
    if len(piece) != block or k < 1 or length > k * block or length > MAX_PAYLOAD:
        return None
    return Frame(tid, length, k, block, index, digest, piece, flags)


class Sender:
    """The endless frames of one payload."""

    def __init__(self, data, block=1000, tid=None):
        data = bytes(data)
        if len(data) > MAX_PAYLOAD:
            raise ValueError("too large")
        self.data = data
        self.digest = hashlib.sha256(data).digest()
        self.tid = tid if tid is not None else struct.unpack(">I", os.urandom(4))[0]
        self.enc = fountain.Encoder(data, block)
        self.k, self.block = self.enc.k, self.enc.block
        self.index = 0

    def frame(self, index=None):
        i = self.index if index is None else index
        if index is None:
            self.index += 1
        return pack(Frame(self.tid, len(self.data), self.k, self.block, i & 0xFFFFFFFF, self.digest[:8],
                          self.enc.piece(i & 0xFFFFFFFF)))


class Receiver:
    """Takes the raw bytes of every QR code read; ``result`` once the payload is whole and checked."""

    def __init__(self):
        self.dec = None
        self.tid = None
        self.digest = None
        self.result = None
        self.error = ""
        self.frames = 0          # frames of this transfer read
        self.foreign = 0         # QR codes that were not frames of this transfer

    def feed(self, raw):
        """-> True when this frame finished the payload."""
        if self.result is not None:
            return False
        f = unpack(raw)
        if f is None:
            self.foreign += 1
            return False
        if self.dec is None:
            self.tid, self.digest = f.tid, f.digest
            self.dec = fountain.Decoder(f.k, f.block, f.length)
        elif (f.tid, f.digest, f.k, f.block, f.length) != (self.tid, self.digest, self.dec.k, self.dec.block,
                                                           self.dec.length):
            self.foreign += 1
            return False
        self.frames += 1
        self.dec.add(f.index, f.piece)
        if self.dec.done:
            data = self.dec.data()
            if hashlib.sha256(data).digest()[:8] != self.digest:
                # a piece got through damaged (the CRC passed by chance) - start this transfer again
                self.error = "checksum"
                self.dec = fountain.Decoder(self.dec.k, self.dec.block, self.dec.length)
                return False
            self.result = data
            return True
        return False

    def progress(self):
        """(fraction 0..1, blocks known, pieces kept, K) - the fraction counts the pieces, since elimination
        may give many blocks at once at the end."""
        if self.dec is None:
            return 0.0, 0, 0, 0
        known, kept, k = self.dec.progress()
        return min(1.0, max(known, kept) / float(k)), known, kept, k


# ---- QR codes ------------------------------------------------------------------------------------------
def qr_matrix(raw):
    """The QR code of ``raw`` as a uint8 array (1 = dark), without the quiet zone. Low error correction:
    a damaged frame is simply skipped (the CRC catches it) and replaced by the next one. The mask is fixed:
    choosing the best of eight takes five times as long and the camera reads either."""
    _vendor()
    import segno

    q = segno.make(bytes(raw), error="l", mode="byte", mask=0, boost_error=False)
    return np.array([list(r) for r in q.matrix], np.uint8)


def qr_gray(raw, scale=4, border=4):
    """The QR code as a grayscale image array (0 black, 255 white) with its quiet zone."""
    m = qr_matrix(raw)
    m = np.pad(m, border)
    img = np.where(m > 0, 0, 255).astype(np.uint8)
    return np.kron(img, np.ones((scale, scale), np.uint8)) if scale > 1 else img


def read_codes(gray):
    """The raw bytes of every QR code found in a grayscale uint8 image array."""
    _vendor()
    import zxingcpp

    try:
        found = zxingcpp.read_barcodes(np.ascontiguousarray(gray), formats=zxingcpp.BarcodeFormat.QRCode)
    except Exception:  # noqa: BLE001 - a bad camera frame is skipped
        return []
    return [bytes(r.bytes) for r in found if r.valid and r.bytes]


def gray_of_qimage(img):
    """A QImage as a grayscale uint8 array (copied)."""
    from qgis.PyQt.QtGui import QImage

    g = img.convertToFormat(QImage.Format.Format_Grayscale8)
    w, h, bpl = g.width(), g.height(), g.bytesPerLine()
    ptr = g.constBits()
    ptr.setsize(h * bpl)
    return np.frombuffer(bytes(ptr), np.uint8).reshape(h, bpl)[:, :w].copy()


def seconds_for(length, block, fps, overhead=1.06):
    """About how long one transfer takes with nothing lost."""
    k = max(1, -(-int(length) // int(block)))
    return k * overhead / float(fps)
