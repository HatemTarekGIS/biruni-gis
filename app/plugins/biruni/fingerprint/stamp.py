"""The date of a certificate, proved: an OpenTimestamps stamp.

A certificate's code proves what was delivered, but only from the first time someone saw the code. A stamp proves
the certificate existed before a given time without trusting anyone: only the certificate's fingerprint (32 bytes,
from which nothing of it can be read) is sent to the free public OpenTimestamps calendars, which put it, with many
others, in a Bitcoin transaction within hours. The stamp file (``<certificate>.ots``, the standard format: any
OpenTimestamps tool reads it) then holds the hashes leading from the certificate to that block, and the block's
time is the date proved.

Made in one step (seconds, the stamp is "pending"), completed later (a few hours on, the calendars give the rest of
the path), checked against the block itself (its header from a public block explorer).
"""
import datetime as dt
import hashlib
import io
import os
import secrets
import urllib.error
import urllib.request

MAGIC = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
CALENDARS = ("https://alice.btc.calendar.opentimestamps.org", "https://bob.btc.calendar.opentimestamps.org",
             "https://finney.calendar.eternitywall.com")
EXPLORER = "https://blockstream.info/api"
BITCOIN = bytes.fromhex("0588960d73d71901")
PENDING = bytes.fromhex("83dfe30d2ef90c8e")
SUFFIX = ".ots"

SHA256, SHA1, RIPEMD160, KECCAK = 0x08, 0x02, 0x03, 0x67
APPEND, PREPEND, REVERSE, HEXLIFY = 0xf0, 0xf1, 0xf2, 0xf3


class StampError(Exception):
    pass


# ---- the format ---------------------------------------------------------------------------------------
def _uint(r):
    v, shift = 0, 0
    while True:
        b = r.read(1)
        if not b:
            raise StampError("format")
        v |= (b[0] & 0x7f) << shift
        if not b[0] & 0x80:
            return v
        shift += 7


def _put_uint(w, v):
    while True:
        b = v & 0x7f
        v >>= 7
        if v:
            w.write(bytes([b | 0x80]))
        else:
            w.write(bytes([b]))
            return


def _bytes(r, limit=8192):
    n = _uint(r)
    if n > limit:
        raise StampError("format")
    b = r.read(n)
    if len(b) != n:
        raise StampError("format")
    return b


def _put_bytes(w, b):
    _put_uint(w, len(b))
    w.write(b)


def apply(op, msg):
    tag, arg = op
    if tag == SHA256:
        return hashlib.sha256(msg).digest()
    if tag == SHA1:
        return hashlib.sha1(msg).digest()
    if tag == RIPEMD160:
        try:
            return hashlib.new("ripemd160", msg).digest()
        except ValueError as e:
            raise StampError("ripemd160") from e
    if tag == KECCAK:
        raise StampError("keccak")
    if tag == APPEND:
        return msg + arg
    if tag == PREPEND:
        return arg + msg
    if tag == REVERSE:
        return msg[::-1]
    if tag == HEXLIFY:
        return msg.hex().encode("ascii")
    raise StampError("format")


class Timestamp:
    """What a message leads to: attestations [(tag, payload)] and operations [((tag, arg), Timestamp)]."""

    def __init__(self, msg):
        self.msg = msg
        self.atts = []
        self.ops = []

    def op(self, tag, arg=b""):
        t = Timestamp(apply((tag, arg), self.msg))
        self.ops.append(((tag, arg), t))
        return t

    def merge(self, other):
        if other.msg != self.msg:
            raise StampError("merge")
        for a in other.atts:
            if a not in self.atts:
                self.atts.append(a)
        for op, t in other.ops:
            mine = [x for o, x in self.ops if o == op]
            if mine:
                mine[0].merge(t)
            else:
                self.ops.append((op, t))

    def walk(self):
        """(message, attestation) of every attestation under this one."""
        for a in self.atts:
            yield self.msg, a
        for _op, t in self.ops:
            yield from t.walk()

    def find(self, msg):
        if self.msg == msg:
            return self
        for _op, t in self.ops:
            f = t.find(msg)
            if f is not None:
                return f
        return None


def read_timestamp(r, msg, depth=0):
    if depth > 256:
        raise StampError("format")
    t = Timestamp(msg)

    def one(tag):
        if tag == 0x00:
            kind = r.read(8)
            payload = _bytes(r)
            if len(kind) != 8:
                raise StampError("format")
            t.atts.append((kind, payload))
        else:
            arg = _bytes(r) if tag in (APPEND, PREPEND) else b""
            nxt = apply((tag, arg), msg)
            t.ops.append(((tag, arg), read_timestamp(r, nxt, depth + 1)))

    b = r.read(1)
    while b == b"\xff":
        one(r.read(1)[0])
        b = r.read(1)
    if not b:
        raise StampError("format")
    one(b[0])
    return t


def write_timestamp(w, t):
    items = [("a", a) for a in sorted(t.atts)] + [("o", o) for o in t.ops]
    if not items:
        raise StampError("empty")
    for i, (k, x) in enumerate(items):
        if i < len(items) - 1:
            w.write(b"\xff")
        if k == "a":
            w.write(b"\x00" + x[0])
            _put_bytes(w, x[1])
        else:
            (tag, arg), sub = x
            w.write(bytes([tag]))
            if tag in (APPEND, PREPEND):
                _put_bytes(w, arg)
            write_timestamp(w, sub)


def read_file(data):
    """(file digest, Timestamp) of a detached stamp."""
    r = io.BytesIO(data)
    if r.read(len(MAGIC)) != MAGIC or _uint(r) != 1:
        raise StampError("format")
    if r.read(1)[0] != SHA256:
        raise StampError("format")
    d = r.read(32)
    t = read_timestamp(r, d)
    if r.read(1):
        raise StampError("format")
    return d, t


def write_file(digest, t):
    w = io.BytesIO()
    w.write(MAGIC)
    _put_uint(w, 1)
    w.write(bytes([SHA256]))
    w.write(digest)
    write_timestamp(w, t)
    return w.getvalue()


# ---- the network -----------------------------------------------------------------------------------------------
def http(url, data=None, timeout=15):
    """GET (or POST ``data``): the body, or None when the server has nothing (404) or cannot be reached."""
    req = urllib.request.Request(url, data=data, headers={
        "Accept": "application/vnd.opentimestamps.v1", "User-Agent": "Biruni-GIS",
        **({"Content-Type": "application/x-www-form-urlencoded"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 - fixed https addresses
            return resp.read(1 << 20)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def stamp(path, fetch=http, calendars=CALENDARS):
    """Stamp the file at ``path``: ``path + .ots`` written, pending until the calendars put it in a block.
    Returns the number of calendars that took it."""
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).digest()
    root = Timestamp(digest)
    c = root.op(APPEND, secrets.token_bytes(16)).op(SHA256)
    took = 0
    for cal in calendars:
        body = fetch(cal.rstrip("/") + "/digest", c.msg)
        if not body:
            continue
        try:
            c.merge(read_timestamp(io.BytesIO(body), c.msg))
            took += 1
        except StampError:
            continue
    if not took:
        raise StampError("offline")
    with open(path + SUFFIX, "wb") as f:
        f.write(write_file(digest, root))
    return took


def _height(payload):
    return _uint(io.BytesIO(payload))


def _uri(payload):
    try:
        return _bytes(io.BytesIO(payload), 1000).decode("utf-8", "replace").rstrip("/")
    except StampError:
        return ""


def upgrade(t, fetch=http):
    """Ask the calendars of the pending attestations for the rest of the path. True when something was added."""
    added = False
    for msg, (kind, payload) in list(t.walk()):
        if kind != PENDING:
            continue
        uri = _uri(payload)
        if not uri.startswith("https://"):
            continue
        body = fetch(f"{uri}/timestamp/{msg.hex()}")
        if not body:
            continue
        try:
            got = read_timestamp(io.BytesIO(body), msg)
        except StampError:
            continue
        node = t.find(msg)
        if node is not None and any(k == BITCOIN for _m, (k, _p) in got.walk()):
            node.merge(got)
            added = True
    return added


def block_header(height, fetch=http):
    h = fetch(f"{EXPLORER}/block-height/{height}")
    if not h:
        return None
    head = fetch(f"{EXPLORER}/block/{h.decode('ascii').strip()}/header")
    if not head:
        return None
    try:
        b = bytes.fromhex(head.decode("ascii").strip())
    except ValueError:
        return None
    return b if len(b) == 80 else None


class Result:
    def __init__(self):
        self.ok = False          # the file is the stamped one, and the stamp is in a block checked
        self.why = ""            # "format", "other file", "pending", "offline", "block"
        self.height = None
        self.time = None         # datetime (UTC) of the block: the file existed before it
        self.pending = []        # calendars still to put it in a block
        self.upgraded = False


def verify(path, stamp_path=None, fetch=http):
    """Check the stamp of the file at ``path`` (completing it first when it is still pending)."""
    r = Result()
    sp = stamp_path or path + SUFFIX
    try:
        with open(sp, "rb") as f:
            digest, t = read_file(f.read())
    except (OSError, StampError, IndexError):
        r.why = "format"
        return r
    with open(path, "rb") as f:
        if hashlib.sha256(f.read()).digest() != digest:
            r.why = "other file"
            return r
    if not any(k == BITCOIN for _m, (k, _p) in t.walk()):
        if upgrade(t, fetch):
            r.upgraded = True
            with open(sp, "wb") as f:
                f.write(write_file(digest, t))
    best = None
    for msg, (kind, payload) in t.walk():
        if kind == PENDING:
            r.pending.append(_uri(payload))
        elif kind == BITCOIN:
            hgt = _height(payload)
            if best is None or hgt < best[0]:
                best = (hgt, msg)
    if best is None:
        r.why = "pending"
        return r
    r.height = best[0]
    head = block_header(best[0], fetch)
    if head is None:
        r.why = "offline"
        return r
    if head[36:68] != best[1]:
        r.why = "block"
        return r
    r.time = dt.datetime.fromtimestamp(int.from_bytes(head[68:72], "little"), dt.timezone.utc)
    r.ok = True
    return r


def has_stamp(path):
    return os.path.isfile(path + SUFFIX)
