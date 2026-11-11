"""Content-addressed storage: files are cut where their CONTENT says (not every N bytes), so an edit in the middle of a
GeoPackage moves only the chunks around it and the rest keep their names; a chunk is named by its BLAKE2b hash and
kept once, however many snapshots hold it.

Cutting: a rolling sum of a random value per byte over a 64-byte window (computed for a whole block at once with
numpy); a cut where the low bits of the sum are zero, kept between MIN and MAX bytes apart.
"""
import hashlib
import os
import sqlite3
import threading
import zlib

import numpy as np

WINDOW = 64
MIN_CHUNK = 64 << 10
AVG_BITS = 18                 # a cut every 2^18 bytes on average after MIN (≈ 320 KiB chunks)
MAX_CHUNK = 1 << 20
BLOCK = 8 << 20               # bytes read at once
_GEAR = np.random.RandomState(0x5EED_B1).randint(0, 2**32, size=256, dtype=np.uint64)
_MASK = np.uint64((1 << AVG_BITS) - 1)


def chunk_id(data):
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def _candidates(buf, base, prev_tail):
    """Positions (absolute, after which a cut may fall) where the window hash hits the mask, in buf."""
    data = np.frombuffer(prev_tail + buf, dtype=np.uint8)
    g = _GEAR[data]
    cs = np.cumsum(g, dtype=np.uint64)
    if len(cs) <= WINDOW:
        return []
    h = cs[WINDOW:] - cs[:-WINDOW]           # sum over the window ending at position i + WINDOW
    hits = np.nonzero((h & _MASK) == 0)[0] + WINDOW + 1  # cut after this byte (in data coordinates)
    off = base - len(prev_tail)
    return (hits + off).tolist()


def cut_points(f, size=None):
    """Chunk boundaries of a file object: [(offset, length), …]."""
    out, start, pos, tail = [], 0, 0, b""
    while True:
        buf = f.read(BLOCK)
        if not buf:
            break
        cands = _candidates(buf, pos, tail)
        pos += len(buf)
        tail = (tail + buf)[-WINDOW:]
        for c in cands:
            if c <= start:
                continue
            while c - start > MAX_CHUNK:
                out.append((start, MAX_CHUNK))
                start += MAX_CHUNK
            if c - start >= MIN_CHUNK:
                out.append((start, c - start))
                start = c
        while pos - start > MAX_CHUNK:     # a long run without a cut
            out.append((start, MAX_CHUNK))
            start += MAX_CHUNK
    if pos > start:
        out.append((start, pos - start))
    return out


def chunk_file(path):
    """Yields (chunk id, bytes) of one file, in order."""
    with open(path, "rb") as f:
        cuts = cut_points(f)
        for off, ln in cuts:
            f.seek(off)
            data = f.read(ln)
            yield chunk_id(data), data


def walk(paths):
    """Files under the given files and folders -> [(absolute path, path as kept)]: a folder keeps its own name as
    the first part (``survey/parcels.gpkg``), a file its name."""
    out, seen = [], set()
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            items = [(p, os.path.basename(p))]
        elif os.path.isdir(p):
            root = os.path.dirname(p.rstrip("\\/"))
            items = []
            for d, dirs, files in os.walk(p):
                dirs.sort()
                for fn in sorted(files):
                    full = os.path.join(d, fn)
                    items.append((full, os.path.relpath(full, root).replace("\\", "/")))
        else:
            continue
        for full, rel in items:
            if rel not in seen:
                seen.add(rel)
                out.append((full, rel))
    return out


def safe_rel(rel):
    """A path kept in a manifest, made safe to write under a folder (no drive, no .., no absolute path)."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".", "..")]
    parts = [p.replace(":", "_") for p in parts]
    if not parts:
        raise ValueError("an empty path")
    return os.path.join(*parts)


class ChunkStore:
    """This computer's own chunks (deflated), one file each under objects/ab/<id>, and an index in SQLite."""

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "objects"), exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(os.path.join(root, "store.sqlite"), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS chunk(id TEXT PRIMARY KEY, size INTEGER, stored INTEGER)")
        self.db.commit()

    def _path(self, cid):
        return os.path.join(self.root, "objects", cid[:2], cid)

    def has(self, cid):
        return self.db.execute("SELECT 1 FROM chunk WHERE id=?", (cid,)).fetchone() is not None

    def put(self, cid, data):
        """-> True when the chunk is new here."""
        with self._lock:
            if self.has(cid):
                return False
            p = self._path(cid)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            z = zlib.compress(data, 1)
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(z)
            os.replace(tmp, p)
            self.db.execute("INSERT INTO chunk VALUES(?,?,?)", (cid, len(data), len(z)))
            self.db.commit()
            return True

    def get(self, cid):
        with open(self._path(cid), "rb") as f:
            data = zlib.decompress(f.read())
        if chunk_id(data) != cid:
            raise ValueError("a chunk on this disk was altered: " + cid[:12])
        return data

    def stats(self):
        n, size, stored = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(stored),0) "
                                          "FROM chunk").fetchone()
        return {"chunks": n, "bytes": size, "stored": stored}

    def close(self):
        self.db.close()
