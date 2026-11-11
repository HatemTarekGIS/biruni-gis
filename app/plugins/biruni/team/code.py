"""Room codes and the keys made from them.

A room code is 16 characters of Crockford base32 (80 random bits), shown as four groups:
``K7QM-2D9X-HR4P-8WZT``. Letters that look alike are read as one (O->0, I/L->1), case and
spaces do not matter, and the dashes are optional. Everything a room needs comes from it:

* the room key (scrypt of the code): every message between the computers is sealed with it
  (AES-256-GCM), so a relay, a tunnel or anyone on the network sees unreadable bytes;
* the room tag: a short public name that lets a computer answer only the rooms it holds;
* the address topic: where the host leaves its sealed address for members outside the office.
"""
import hashlib
import secrets

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_READ = {"O": "0", "I": "1", "L": "1", "U": "V"}
LENGTH = 16


def new_code():
    n = secrets.randbits(5 * LENGTH)
    raw = "".join(ALPHABET[(n >> (5 * i)) & 31] for i in range(LENGTH))
    return pretty(raw)


def pretty(raw):
    return "-".join(raw[i:i + 4] for i in range(0, LENGTH, 4))


def normalize(text):
    """The code's 16 characters from what the user typed or pasted, or None."""
    s = "".join(_READ.get(c, c) for c in (text or "").upper() if c.isalnum())
    if len(s) != LENGTH or any(c not in ALPHABET for c in s):
        return None
    return s


class RoomKeys:
    """The keys of one room code."""

    def __init__(self, code):
        raw = normalize(code)
        if raw is None:
            raise ValueError("not a room code")
        self.code = pretty(raw)
        # scrypt makes guessing codes slow; the parameters are fixed so every member derives the same key
        self.key = hashlib.scrypt(raw.encode("ascii"), salt=b"biruni-team-room-1", n=1 << 14, r=8, p=1,
                                  maxmem=64 << 20, dklen=32)
        self.tag = hashlib.sha256(b"biruni-team-tag:" + self.key).hexdigest()[:16]
        self.topic = "biruniteam" + hashlib.sha256(b"biruni-team-topic:" + self.key).hexdigest()[:36]
