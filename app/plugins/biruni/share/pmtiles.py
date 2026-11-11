"""PMTiles version 3, read and written in plain Python: the one-file tile archive that a web page reads in
pieces (HTTP range requests) and that needs no tile server.

* ``Reader``: the header, the directories and any tile of an archive, from a file on the disk or from an
  http(s) address (only the bytes needed are fetched).
* ``extract``: the tiles of an area, from the zoom levels asked for, into a new archive: a city out of a
  planet-wide basemap build, downloading only that city.
* ``write``: an archive from tiles {(z, x, y): bytes}.
* ``to_mbtiles``: the same tiles in an MBTiles file, which the desktop map draws as vector tiles.

Tiles are copied as they are (their own compression kept); the directories are written gzip-compressed.
The specification: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
"""
import gzip
import json
import math
import os
import sqlite3
import struct
import urllib.error
import urllib.parse
import urllib.request

MAGIC = b"PMTiles"
HEADER_LEN = 127
ROOT_MAX = 16384 - HEADER_LEN          # the header and the root directory are read in one 16 kB request
COMPRESSION = {0: "unknown", 1: "none", 2: "gzip", 3: "brotli", 4: "zstd"}
TILE_TYPE = {0: "unknown", 1: "mvt", 2: "png", 3: "jpeg", 4: "webp", 5: "avif"}
FORMAT_OF = {1: "pbf", 2: "png", 3: "jpg", 4: "webp", 5: "avif"}
USER_AGENT = "Biruni-GIS"


class PMTilesError(Exception):
    pass


# ---- tile ids ---------------------------------------------------------------------------------------------
def _rotate(n, xy, rx, ry):
    if ry == 0:
        if rx == 1:
            xy[0] = n - 1 - xy[0]
            xy[1] = n - 1 - xy[1]
        xy[0], xy[1] = xy[1], xy[0]


def zxy_to_id(z, x, y):
    """The tile id of (z, x, y): the tiles of the levels above, then the tile's place on the Hilbert curve."""
    if z > 31:
        raise PMTilesError("zoom")
    n = 1 << z
    if x < 0 or y < 0 or x >= n or y >= n:
        raise PMTilesError("tile")
    acc = ((1 << (2 * z)) - 1) // 3
    xy = [x, y]
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if xy[0] & s else 0
        ry = 1 if xy[1] & s else 0
        d += s * s * ((3 * rx) ^ ry)
        _rotate(s, xy, rx, ry)
        s >>= 1
    return acc + d


def id_to_zxy(tid):
    acc = 0
    for z in range(32):
        num = 1 << (2 * z)
        if acc + num > tid:
            pos = tid - acc
            n = 1 << z
            xy = [0, 0]
            s = 1
            t = pos
            while s < n:
                rx = 1 & (t // 2)
                ry = 1 & (t ^ rx)
                _rotate(s, xy, rx, ry)
                xy[0] += s * rx
                xy[1] += s * ry
                t //= 4
                s *= 2
            return z, xy[0], xy[1]
        acc += num
    raise PMTilesError("tile id")


def lonlat_to_tile(lon, lat, z):
    lat = max(min(lat, 85.0511287798), -85.0511287798)
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tiles_in(bbox, z):
    """(x0, y0, x1, y1) of the tiles of level z covering bbox = (west, south, east, north) in degrees."""
    w, s, e, n = bbox
    x0, y1 = lonlat_to_tile(w, s, z)
    x1, y0 = lonlat_to_tile(e, n, z)
    return x0, y0, x1, y1


# ---- varints and directories --------------------------------------------------------------------------------
def _read_varint(buf, pos):
    v, shift = 0, 0
    while True:
        if pos >= len(buf):
            raise PMTilesError("directory")
        b = buf[pos]
        pos += 1
        v |= (b & 0x7f) << shift
        if not b & 0x80:
            return v, pos
        shift += 7


def _write_varint(out, v):
    while True:
        b = v & 0x7f
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def parse_directory(data):
    """[(tile id, offset, length, run length)]; a run length of 0 points to a leaf directory."""
    n, pos = _read_varint(data, 0)
    ids, runs, lens, offs = [], [], [], []
    last = 0
    for _ in range(n):
        d, pos = _read_varint(data, pos)
        last += d
        ids.append(last)
    for _ in range(n):
        v, pos = _read_varint(data, pos)
        runs.append(v)
    for _ in range(n):
        v, pos = _read_varint(data, pos)
        lens.append(v)
    for i in range(n):
        v, pos = _read_varint(data, pos)
        if v == 0 and i > 0:
            offs.append(offs[i - 1] + lens[i - 1])
        else:
            offs.append(v - 1)
    return list(zip(ids, offs, lens, runs))


def serialize_directory(entries):
    out = bytearray()
    _write_varint(out, len(entries))
    last = 0
    for e in entries:
        _write_varint(out, e[0] - last)
        last = e[0]
    for e in entries:
        _write_varint(out, e[3])
    for e in entries:
        _write_varint(out, e[2])
    for i, e in enumerate(entries):
        if i > 0 and e[1] == entries[i - 1][1] + entries[i - 1][2]:
            _write_varint(out, 0)
        else:
            _write_varint(out, e[1] + 1)
    return bytes(out)


def _decompress(data, kind):
    if kind in (0, 1):
        return data
    if kind == 2:
        return gzip.decompress(data)
    raise PMTilesError(f"compression {COMPRESSION.get(kind, kind)}")


def _gzip(data):
    return gzip.compress(data, compresslevel=9, mtime=0)


# ---- the header ---------------------------------------------------------------------------------------------
def parse_header(b):
    if len(b) < HEADER_LEN or b[:7] != MAGIC:
        raise PMTilesError("not a PMTiles archive")
    if b[7] != 3:
        raise PMTilesError(f"version {b[7]}")
    q = struct.unpack_from("<11Q", b, 8)
    h = dict(zip(("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset",
                  "leaf_length", "data_offset", "data_length", "addressed_tiles", "tile_entries",
                  "tile_contents"), q))
    clustered, internal, tile_comp, tile_type, minz, maxz = struct.unpack_from("<6B", b, 96)
    w, s, e, n = struct.unpack_from("<4i", b, 102)
    cz, = struct.unpack_from("<B", b, 118)
    clon, clat = struct.unpack_from("<2i", b, 119)
    h.update(clustered=bool(clustered), internal_compression=internal, tile_compression=tile_comp,
             tile_type=tile_type, min_zoom=minz, max_zoom=maxz,
             bounds=(w / 1e7, s / 1e7, e / 1e7, n / 1e7), center=(clon / 1e7, clat / 1e7, cz))
    return h


def make_header(h):
    b = bytearray(MAGIC + bytes([3]))
    b += struct.pack("<11Q", h["root_offset"], h["root_length"], h["metadata_offset"], h["metadata_length"],
                     h["leaf_offset"], h["leaf_length"], h["data_offset"], h["data_length"], h["addressed_tiles"],
                     h["tile_entries"], h["tile_contents"])
    b += struct.pack("<6B", 1 if h.get("clustered", True) else 0, h.get("internal_compression", 2),
                     h["tile_compression"], h["tile_type"], h["min_zoom"], h["max_zoom"])
    w, s, e, n = h["bounds"]
    b += struct.pack("<4i", *(int(round(v * 1e7)) for v in (w, s, e, n)))
    clon, clat, cz = h.get("center") or ((w + e) / 2, (s + n) / 2, h["min_zoom"])
    b += struct.pack("<B", int(cz))
    b += struct.pack("<2i", int(round(clon * 1e7)), int(round(clat * 1e7)))
    assert len(b) == HEADER_LEN
    return bytes(b)


# ---- reading ------------------------------------------------------------------------------------------------
class _Source:
    """Bytes of an archive by range: a local file, or an http(s) address."""

    def __init__(self, where, timeout=30):
        self.where = where
        self.timeout = timeout
        self.remote = where.startswith(("http://", "https://"))
        if self.remote:
            self.url = urllib.parse.quote(where, safe=":/?&=%#@+,;~!$'()*[]")
        self.requests = 0
        self.bytes = 0
        if not self.remote:
            self._f = open(where, "rb")

    def read(self, offset, length, exact=True):
        if length <= 0:
            return b""
        self.requests += 1
        if not self.remote:
            self._f.seek(offset)
            data = self._f.read(length)
        else:
            req = urllib.request.Request(self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}",
                                                              "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:     # noqa: S310 - user's address
                    if resp.status != 206 and offset > 0:
                        raise PMTilesError("the server does not send parts of files (no range requests)")
                    data = resp.read(length)
            except urllib.error.HTTPError as e:
                raise PMTilesError(f"not found: {e.code}") from e
            except urllib.error.URLError as e:
                raise PMTilesError(f"offline: {e}") from e
        self.bytes += len(data)
        if exact and len(data) != length:
            raise PMTilesError("the archive is shorter than its header says")
        return data

    def close(self):
        if not self.remote:
            self._f.close()


class Reader:
    def __init__(self, where, timeout=30):
        self.src = _Source(where, timeout)
        head = self.src.read(0, 16384, exact=False) if self.src.remote else self.src.read(0, HEADER_LEN)
        self.header = h = parse_header(head)
        if self.src.remote and h["root_offset"] + h["root_length"] <= len(head):
            raw = head[h["root_offset"]:h["root_offset"] + h["root_length"]]
        else:
            raw = self.src.read(h["root_offset"], h["root_length"])
        self.root = parse_directory(_decompress(raw, h["internal_compression"]))
        self._leaves = {}

    def close(self):
        self.src.close()

    def metadata(self):
        h = self.header
        if not h["metadata_length"]:
            return {}
        raw = _decompress(self.src.read(h["metadata_offset"], h["metadata_length"]), h["internal_compression"])
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _leaf(self, offset, length):
        key = (offset, length)
        if key not in self._leaves:
            raw = self.src.read(self.header["leaf_offset"] + offset, length)
            self._leaves[key] = parse_directory(_decompress(raw, self.header["internal_compression"]))
        return self._leaves[key]

    def find(self, tid):
        """(offset in the tile data, length) of a tile id, or None."""
        entries = self.root
        for _depth in range(4):
            lo, hi = 0, len(entries) - 1
            hit = None
            while lo <= hi:
                mid = (lo + hi) // 2
                if entries[mid][0] <= tid:
                    hit = entries[mid]
                    lo = mid + 1
                else:
                    hi = mid - 1
            if hit is None:
                return None
            eid, off, length, run = hit
            if run == 0:
                entries = self._leaf(off, length)
                continue
            if tid < eid + run:
                return off, length
            return None
        return None

    def tile(self, z, x, y):
        f = self.find(zxy_to_id(z, x, y))
        if f is None:
            return None
        return self.src.read(self.header["data_offset"] + f[0], f[1])

    def entries(self):
        """Every (tile id, offset, length) of the archive (the run lengths spread out)."""
        def walk(entries):
            for eid, off, length, run in entries:
                if run == 0:
                    yield from walk(self._leaf(off, length))
                else:
                    for k in range(run):
                        yield eid + k, off, length
        yield from walk(self.root)


# ---- writing ------------------------------------------------------------------------------------------------
def _directories(entries):
    """(root bytes, leaves bytes) gzip-compressed, the root small enough to come with the header."""
    root = _gzip(serialize_directory(entries))
    if len(root) <= ROOT_MAX:
        return root, b""
    size = 4096
    while True:
        leaves = bytearray()
        root_entries = []
        for i in range(0, len(entries), size):
            chunk = entries[i:i + size]
            data = _gzip(serialize_directory(chunk))
            root_entries.append((chunk[0][0], len(leaves), len(data), 0))
            leaves += data
        root = _gzip(serialize_directory(root_entries))
        if len(root) <= ROOT_MAX:
            return root, bytes(leaves)
        size = int(size * 1.2)


def write(path, tiles, tile_type=1, tile_compression=2, metadata=None, bounds=None, center=None):
    """Write tiles {(z, x, y): bytes} as a PMTiles archive. Identical tiles are kept once."""
    if not tiles:
        raise PMTilesError("no tiles")
    by_id = sorted((zxy_to_id(*k), v) for k, v in tiles.items())
    entries, blobs, seen = [], [], {}
    offset = 0
    for tid, data in by_id:
        key = hash(data), len(data)
        if key in seen and blobs[seen[key][2]] == data:
            off, length, _i = seen[key]
        else:
            off, length = offset, len(data)
            seen[key] = (off, length, len(blobs))
            blobs.append(data)
            offset += length
        if entries and entries[-1][1] == off and entries[-1][0] + entries[-1][3] == tid:
            e = entries[-1]
            entries[-1] = (e[0], e[1], e[2], e[3] + 1)
        else:
            entries.append((tid, off, length, 1))
    root, leaves = _directories(entries)
    meta = _gzip(json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    zs = [k[0] for k in tiles]
    if bounds is None:
        bounds = (-180.0, -85.0511287798, 180.0, 85.0511287798)
    h = {"root_offset": HEADER_LEN, "root_length": len(root)}
    h["metadata_offset"] = h["root_offset"] + len(root)
    h["metadata_length"] = len(meta)
    h["leaf_offset"] = h["metadata_offset"] + len(meta)
    h["leaf_length"] = len(leaves)
    h["data_offset"] = h["leaf_offset"] + len(leaves)
    h["data_length"] = offset
    h.update(addressed_tiles=len(tiles), tile_entries=len(entries), tile_contents=len(blobs), clustered=True,
             internal_compression=2, tile_compression=tile_compression, tile_type=tile_type, min_zoom=min(zs),
             max_zoom=max(zs), bounds=bounds, center=center)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(make_header(h))
        f.write(root)
        f.write(meta)
        f.write(leaves)
        for b in blobs:
            f.write(b)
    os.replace(tmp, path)
    return h


# ---- an area out of an archive --------------------------------------------------------------------------------
class Plan:
    """What extracting an area would take: the tiles found, and their bytes (before anything is downloaded)."""

    def __init__(self):
        self.tiles = {}        # (z, x, y) -> (offset, length)
        self.missing = 0       # tiles of the area the archive does not have (sea, nothing there)

    @property
    def size(self):
        return sum(length for off, length in {v for v in self.tiles.values()})


def plan(reader, bbox, min_zoom=0, max_zoom=None, limit=2_000_000):
    """The tiles of ``reader`` covering bbox (west, south, east, north) from min_zoom to max_zoom."""
    h = reader.header
    max_zoom = h["max_zoom"] if max_zoom is None else min(max_zoom, h["max_zoom"])
    min_zoom = max(min_zoom, h["min_zoom"])
    p = Plan()
    count = 0
    for z in range(min_zoom, max_zoom + 1):
        x0, y0, x1, y1 = tiles_in(bbox, z)
        count += (x1 - x0 + 1) * (y1 - y0 + 1)
        if count > limit:
            raise PMTilesError("too many tiles: choose a smaller area or a lower zoom")
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                f = reader.find(zxy_to_id(z, x, y))
                if f is None:
                    p.missing += 1
                else:
                    p.tiles[(z, x, y)] = f
    return p


def _fetch(reader, plan_, gap=256 * 1024, chunk=8 * 1024 * 1024, progress=None):
    """{(z, x, y): bytes}: the tiles of the plan, fetched in as few ranges as can be."""
    wanted = sorted({v for v in plan_.tiles.values()})
    got = {}
    i = 0
    done = 0
    total = sum(length for _o, length in wanted) or 1
    base = reader.header["data_offset"]
    while i < len(wanted):
        start = wanted[i][0]
        end = start + wanted[i][1]
        j = i + 1
        while j < len(wanted) and wanted[j][0] - end <= gap and wanted[j][0] + wanted[j][1] - start <= chunk:
            end = max(end, wanted[j][0] + wanted[j][1])
            j += 1
        block = reader.src.read(base + start, end - start)
        for off, length in wanted[i:j]:
            got[(off, length)] = block[off - start:off - start + length]
            done += length
        if progress is not None and progress(100.0 * done / total) is False:
            raise PMTilesError("cancelled")
        i = j
    return {k: got[v] for k, v in plan_.tiles.items()}


def extract(where, out, bbox, min_zoom=0, max_zoom=None, progress=None, plan_=None, reader=None):
    """Write the area bbox of the archive at ``where`` (a path or an http(s) address) into ``out``.
    Returns the new header."""
    r = reader or Reader(where)
    try:
        p = plan_ or plan(r, bbox, min_zoom, max_zoom)
        if not p.tiles:
            raise PMTilesError("the archive has no tiles in this area")
        tiles = _fetch(r, p, progress=progress)
        meta = r.metadata()
        h = r.header
        w, s, e, n = bbox
        hb = h["bounds"]
        bounds = (max(w, hb[0]), max(s, hb[1]), min(e, hb[2]), min(n, hb[3]))
        zs = [k[0] for k in tiles]
        if isinstance(meta, dict):
            meta = dict(meta)
            meta["bounds"] = ",".join(str(round(v, 6)) for v in bounds)
            meta["minzoom"], meta["maxzoom"] = min(zs), max(zs)
            meta.setdefault("attribution", "© OpenStreetMap contributors")
        center = ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, min(max(zs), max(min(zs), 12)))
        return write(out, tiles, h["tile_type"], h["tile_compression"], meta, bounds, center)
    finally:
        if reader is None:
            r.close()


# ---- MBTiles --------------------------------------------------------------------------------------------------
def to_mbtiles(where, out, progress=None):
    """The tiles of a PMTiles archive as an MBTiles file (rows counted from the south, as MBTiles does)."""
    r = Reader(where)
    try:
        h = r.header
        meta = r.metadata()
        if os.path.exists(out):
            os.remove(out)
        con = sqlite3.connect(out)
        try:
            con.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
            con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, "
                        "tile_data BLOB)")
            w, s, e, n = h["bounds"]
            cx, cy, cz = h["center"]
            fmt = FORMAT_OF.get(h["tile_type"], "pbf")
            rows = {"name": (meta or {}).get("name", os.path.splitext(os.path.basename(where))[0]),
                    "format": fmt, "minzoom": str(h["min_zoom"]), "maxzoom": str(h["max_zoom"]),
                    "bounds": f"{w},{s},{e},{n}", "center": f"{cx},{cy},{cz}",
                    "attribution": (meta or {}).get("attribution", "")}
            if fmt == "pbf":
                rows["json"] = json.dumps({"vector_layers": (meta or {}).get("vector_layers", [])},
                                          ensure_ascii=False)
            con.executemany("INSERT INTO metadata VALUES (?, ?)", list(rows.items()))
            entries = list(r.entries())
            total = len(entries) or 1
            cache = {}
            batch = []
            for i, (tid, off, length) in enumerate(entries):
                z, x, y = id_to_zxy(tid)
                if (off, length) not in cache:
                    cache.clear()
                    cache[(off, length)] = r.src.read(h["data_offset"] + off, length)
                data = cache[(off, length)]
                if h["tile_compression"] == 1 and fmt == "pbf":
                    data = _gzip(data)            # vector MBTiles are gzip-compressed
                batch.append((z, x, (1 << z) - 1 - y, data))
                if len(batch) >= 2000:
                    con.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", batch)
                    batch = []
                    if progress is not None and progress(100.0 * i / total) is False:
                        raise PMTilesError("cancelled")
            if batch:
                con.executemany("INSERT INTO tiles VALUES (?, ?, ?, ?)", batch)
            con.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
            con.commit()
        finally:
            con.close()
        return len(entries)
    finally:
        r.close()


def info(where):
    """The header and metadata of an archive, for showing."""
    r = Reader(where)
    try:
        h = dict(r.header)
        h["metadata"] = r.metadata()
        h["tile_type_name"] = TILE_TYPE.get(h["tile_type"], "?")
        return h
    finally:
        r.close()


def read_all(where):
    """{(z, x, y): bytes} of a whole (small) archive."""
    r = Reader(where)
    try:
        out = {}
        for tid, off, length in r.entries():
            out[id_to_zxy(tid)] = r.src.read(r.header["data_offset"] + off, length)
        return out
    finally:
        r.close()

