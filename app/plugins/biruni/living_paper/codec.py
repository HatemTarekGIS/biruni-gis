"""The map inside the QR code: a compact, versioned encoding of what the phone page shows.

Everything travels after the "#" of the address, so the browser never sends it to any server:

    https://hatemtarekgis.github.io/biruni-gis/m/#B<data>

The first letter is the format: "A" = the packed bytes as they are, "B" = the same bytes compressed with
raw deflate (the page uses DecompressionStream('deflate-raw')). The bytes are written with 43 characters
that are all safe in an address and all in the QR code's alphanumeric set (0-9 A-Z $ * + - . / :), two bytes
in three characters, so the QR code can carry the data in its compact alphanumeric mode (5.5 bits a character)
instead of 8 bits a character - about a quarter fewer modules than base64 at the same length.

Packed bytes (version 1), integers as unsigned varints, signed ones zigzag-coded, coordinates as whole numbers of
10^-decimals degrees, each point a difference from the one before (the first from corner 1):
    decimals:u8  lang:str  title:str  scale:str
    4 corners (lon, lat) of the printed map frame - a rotated map keeps its true shape
    n:varint  then n points (dlon, dlat, label:str)
    n:varint  then n shapes (kind:u8 1=line 2=polygon, count:varint, count × (dlon, dlat))
str = byte length:varint + UTF-8.

No QGIS here: the page's JavaScript decoder (page.py) mirrors decode() and the tests check them against each
other.
"""
import math
import zlib

PAGE_URL = "https://hatemtarekgis.github.io/biruni-gis/m/"
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$*+-./:"
FORMAT_VERSION = 1
MAX_CHARS = 1500          # the whole address; ~ QR version 27-29 (M), still readable printed ~4 cm wide
MAX_POINTS = 40
MAX_SHAPES = 12
MAX_LABEL = 40
SHORT_LABEL = 18
MAX_TITLE = 120
MAX_SCALE = 32
LINE, POLYGON = 1, 2
KINDS = {"line": LINE, "polygon": POLYGON, LINE: LINE, POLYGON: POLYGON}
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


# ---- 43 letters ------------------------------------------------------------------------------------------------
def b43encode(data):
    out = []
    for i in range(0, len(data) - 1, 2):
        v = data[i] * 256 + data[i + 1]
        out.append(ALPHABET[v % 43] + ALPHABET[v // 43 % 43] + ALPHABET[v // 1849])
    if len(data) % 2:
        v = data[-1]
        out.append(ALPHABET[v % 43] + ALPHABET[v // 43])
    return "".join(out)


def b43decode(text):
    if len(text) % 3 == 1:
        raise ValueError("bad length")
    try:
        d = [_INDEX[c] for c in text]
    except KeyError:
        raise ValueError("bad character") from None
    out = bytearray()
    for i in range(0, len(d) - 2, 3):
        v = d[i] + d[i + 1] * 43 + d[i + 2] * 1849
        if v > 0xFFFF:
            raise ValueError("bad group")
        out += bytes((v >> 8, v & 0xFF))
    if len(d) % 3 == 2:
        v = d[-2] + d[-1] * 43
        if v > 0xFF:
            raise ValueError("bad group")
        out.append(v)
    return bytes(out)


# ---- packing ---------------------------------------------------------------------------------------------------
def _uvar(n, out):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _svar(n, out):
    _uvar(n * 2 if n >= 0 else -n * 2 - 1, out)


def _str(s, out):
    b = s.encode("utf-8")
    _uvar(len(b), out)
    out += b


class _Reader:
    def __init__(self, data):
        self.data, self.i = data, 0

    def u8(self):
        if self.i >= len(self.data):
            raise ValueError("truncated")
        self.i += 1
        return self.data[self.i - 1]

    def uvar(self):
        n, shift = 0, 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                return n
            shift += 7
            if shift > 63:
                raise ValueError("bad number")

    def svar(self):
        n = self.uvar()
        return n // 2 if n % 2 == 0 else -(n + 1) // 2

    def str(self):
        n = self.uvar()
        if self.i + n > len(self.data):
            raise ValueError("truncated")
        s = self.data[self.i:self.i + n].decode("utf-8")
        self.i += n
        return s


def _cut(text, n):
    """At most n characters; a cut text ends with an ellipsis."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:max(1, n - 1)].rstrip() + "…"


def pack(decimals, title, scale, corners, points, shapes, lang=""):
    f = 10 ** decimals
    out = bytearray([decimals])
    _str(lang, out)
    _str(title, out)
    _str(scale, out)
    prev = [0, 0]

    def xy(lon, lat):
        q = (round(lon * f), round(lat * f))
        _svar(q[0] - prev[0], out)
        _svar(q[1] - prev[1], out)
        prev[0], prev[1] = q

    for lon, lat in corners:
        xy(lon, lat)
    prev[:] = (round(corners[0][0] * f), round(corners[0][1] * f))  # points and shapes start from corner 1
    _uvar(len(points), out)
    for lon, lat, label in points:
        xy(lon, lat)
        _str(label, out)
    _uvar(len(shapes), out)
    for kind, coords in shapes:
        out.append(kind)
        _uvar(len(coords), out)
        for lon, lat in coords:
            xy(lon, lat)
    return bytes(out)


def unpack(data):
    r = _Reader(data)
    decimals = r.u8()
    if not 0 <= decimals <= 9:
        raise ValueError("bad precision")
    f = 10.0 ** decimals
    d = {"decimals": decimals, "lang": r.str(), "title": r.str(), "scale": r.str()}
    prev = [0, 0]

    def xy():
        prev[0] += r.svar()
        prev[1] += r.svar()
        return [prev[0] / f, prev[1] / f]

    d["corners"] = [xy()]
    first = list(prev)
    d["corners"] += [xy() for _ in range(3)]
    prev[:] = first  # points and shapes start from corner 1
    n = r.uvar()
    if n > 10000:
        raise ValueError("too many points")
    d["points"] = []
    for _ in range(n):
        lon, lat = xy()
        d["points"].append([lon, lat, r.str()])
    n = r.uvar()
    if n > 1000:
        raise ValueError("too many shapes")
    d["shapes"] = []
    for _ in range(n):
        kind = r.u8()
        if kind not in (LINE, POLYGON):
            raise ValueError("bad shape")
        m = r.uvar()
        if m > 100000:
            raise ValueError("too many vertices")
        d["shapes"].append({"kind": "line" if kind == LINE else "polygon", "coords": [xy() for _ in range(m)]})
    for lon, lat in d["corners"]:
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            raise ValueError("bad corner")
    return d


def _deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def _inflate(data):
    d = zlib.decompressobj(-15)
    out = d.decompress(data, 1 << 20)
    if d.unconsumed_tail:
        raise ValueError("too large")
    return out


def wrap(data):
    """The text after "#": the shorter of the raw and the compressed form."""
    raw = "A" + b43encode(bytes([FORMAT_VERSION]) + data)
    packed = "B" + b43encode(_deflate(bytes([FORMAT_VERSION]) + data))
    return packed if len(packed) < len(raw) else raw


# ---- geometry helpers (degrees; distances in metres on a local flat approximation) ----------------------------
def _metres(lat):
    return 111320.0 * math.cos(math.radians(lat)), 110540.0


def diagonal_m(corners):
    lat0 = sum(c[1] for c in corners) / len(corners)
    kx, ky = _metres(lat0)
    return max(math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * ky) for a in corners for b in corners)


def simplify(coords, tol_m, closed=False):
    """Douglas-Peucker in metres. A polygon ring keeps at least 3 points, a line 2."""
    pts = [tuple(p[:2]) for p in coords]
    if closed and len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) <= (3 if closed else 2) or tol_m <= 0:
        return pts
    lat0 = sum(p[1] for p in pts) / len(pts)
    kx, ky = _metres(lat0)
    xy = [(p[0] * kx, p[1] * ky) for p in pts]
    if closed:  # split the ring at the point farthest from the first one, so both halves are lines
        far = max(range(len(xy)), key=lambda i: (xy[i][0] - xy[0][0]) ** 2 + (xy[i][1] - xy[0][1]) ** 2)
        keep = _dp(xy, 0, far, tol_m) | _dp(xy + [xy[0]], far, len(xy), tol_m)
        keep.discard(len(xy))
    else:
        keep = _dp(xy, 0, len(xy) - 1, tol_m)
    out = [pts[i] for i in sorted(keep)]
    if closed and len(out) < 3:
        return []
    return out


def _dp(xy, a, b, tol):
    keep = {a, b}
    stack = [(a, b)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j % len(xy)]
        dx, dy = x2 - x1, y2 - y1
        n2 = dx * dx + dy * dy
        best, at = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k][0] - x1, xy[k][1] - y1
            if n2 == 0:
                dist = math.hypot(px, py)
            else:
                t = max(0.0, min(1.0, (px * dx + py * dy) / n2))
                dist = math.hypot(px - t * dx, py - t * dy)
            if dist > best:
                best, at = dist, k
        if at >= 0 and best > tol:
            keep.add(at)
            stack += [(i, at), (at, j)]
    return keep


def spread_order(points, centre, limit=None):
    """Points ordered so that any first n of them are spread over the map (farthest point first, starting near
    the centre): dropping from the end keeps the coverage. limit: stop after that many."""
    if not points:
        return []
    kx, ky = _metres(centre[1])
    left = list(points)
    first = min(left, key=lambda p: ((p[0] - centre[0]) * kx) ** 2 + ((p[1] - centre[1]) * ky) ** 2)
    order = [first]
    left.remove(first)
    near = [((p[0] - first[0]) * kx) ** 2 + ((p[1] - first[1]) * ky) ** 2 for p in left]
    while left and (limit is None or len(order) < limit):
        k = max(range(len(left)), key=near.__getitem__)
        p = left.pop(k)
        near.pop(k)
        order.append(p)
        near = [min(d, ((q[0] - p[0]) * kx) ** 2 + ((q[1] - p[1]) * ky) ** 2) for q, d in zip(left, near)]
    return order


# ---- public ----------------------------------------------------------------------------------------------------
def _check_corners(corners):
    corners = [(float(c[0]), float(c[1])) for c in corners]
    if len(corners) != 4:
        raise ValueError("the map frame needs 4 corners")
    for lon, lat in corners:
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("a corner is not a longitude/latitude")
    return corners


def _decimals(diag_m):
    """Coordinates to ~1/10000 of the map's size: 6 decimals (11 cm) for a site plan, 4 (11 m) for a region."""
    want = max(diag_m, 1.0) / 10000.0
    for d in (4, 5):
        if 111320.0 / 10 ** d <= want:
            return d
    return 6


def encode(title, corners, scale="", points=(), shapes=(), lang="", max_chars=MAX_CHARS, base=PAGE_URL):
    """(url, report). corners: the 4 corners (lon, lat) of the printed map frame, in order around it.
    points: (lon, lat, label); shapes: (kind "line"/"polygon", [(lon, lat), ...]) - a polygon is its outer ring.
    When everything does not fit in max_chars, labels are shortened, then the shapes simplified, then the points
    thinned (keeping them spread over the map), then the shapes simplified further and dropped, then the title
    shortened. report says what was kept."""
    corners = _check_corners(corners)
    diag = diagonal_m(corners)
    decimals = _decimals(diag)
    centre = (sum(c[0] for c in corners) / 4, sum(c[1] for c in corners) / 4)
    pts = [(float(p[0]), float(p[1]), str(p[2]) if len(p) > 2 and p[2] is not None else "") for p in points]
    pts = [p for p in pts if math.isfinite(p[0]) and math.isfinite(p[1]) and -180 <= p[0] <= 180
           and -90 <= p[1] <= 90]
    given_points = len(pts)
    pts = spread_order(pts, centre, MAX_POINTS)
    shp = []
    for kind, coords in shapes:
        k = KINDS.get(kind)
        coords = [(float(c[0]), float(c[1])) for c in coords]
        if k and len(coords) >= (3 if k == POLYGON else 2):
            shp.append((k, coords))
    given_shapes = len(shp)
    shp = shp[:MAX_SHAPES]
    title_full = " ".join(str(title or "").split())
    scale = _cut(scale, MAX_SCALE)
    room = max_chars - len(base) - 1

    def attempt(n_pts, label_len, tol, n_shapes, title_len):
        simple = []
        for k, coords in shp[:n_shapes]:
            s = simplify(coords, tol, closed=k == POLYGON)
            if len(s) >= (3 if k == POLYGON else 2):
                simple.append((k, s))
        data = pack(decimals, _cut(title_full, title_len), scale, corners,
                    [(lon, lat, _cut(lb, label_len)) for lon, lat, lb in pts[:n_pts]], simple, lang)
        return wrap(data), simple

    tol0 = diag / 2000.0
    tols = [tol0 * 2 ** k for k in range(0, 9) if tol0 * 2 ** k <= diag / 8] or [tol0]
    mid = [t for t in tols if t <= diag / 100] or tols[:1]
    plan = [(len(pts), MAX_LABEL, tol0, len(shp), MAX_TITLE), (len(pts), SHORT_LABEL, tol0, len(shp), MAX_TITLE)]
    plan += [(len(pts), SHORT_LABEL, t, len(shp), MAX_TITLE) for t in mid[1:]]
    found = None
    for step in plan:
        text, simple = attempt(*step)
        if len(text) <= room:
            found = (step, text, simple)
            break
    if found is None:  # thin the points: the most that still fit
        lo, hi, best = 0, len(pts) - 1, None
        while lo <= hi:
            n = (lo + hi) // 2
            text, simple = attempt(n, SHORT_LABEL, mid[-1], len(shp), MAX_TITLE)
            if len(text) <= room:
                best, lo = ((n, SHORT_LABEL, mid[-1], len(shp), MAX_TITLE), text, simple), n + 1
            else:
                hi = n - 1
        found = best
    if found is None:  # no points: simplify the shapes more, then drop them from the end
        for n_shapes in range(len(shp), -1, -1):
            for t in (tols if n_shapes else tols[:1]):
                text, simple = attempt(0, SHORT_LABEL, t, n_shapes, MAX_TITLE)
                if len(text) <= room:
                    found = ((0, SHORT_LABEL, t, n_shapes, MAX_TITLE), text, simple)
                    break
            if found:
                break
    if found is None:
        for title_len in (60, 30, 12, 0):
            text, simple = attempt(0, SHORT_LABEL, tol0, 0, title_len)
            if len(text) <= room:
                found = ((0, SHORT_LABEL, tol0, 0, title_len), text, simple)
                break
    if found is None:
        raise ValueError("the address cannot be made that short")
    (n_pts, label_len, tol, _n_shapes, title_len), text, simple = found
    report = {"chars": len(base) + 1 + len(text), "max_chars": max_chars, "points": n_pts,
              "points_given": given_points, "shapes": len(simple), "shapes_given": given_shapes,
              "vertices": sum(len(c) for _k, c in simple),
              "vertices_given": sum(len(c) for _k, c in shp), "tolerance_m": round(tol, 2) if simple else 0.0,
              "labels_shortened": label_len < MAX_LABEL and any(len(p[2]) > label_len for p in pts[:n_pts]),
              "title_shortened": len(title_full) > title_len, "decimals": decimals, "compressed": text[0] == "B"}
    return base + "#" + text, report


def decode(url_or_hash):
    """The map from an address made by encode() (or just the part after "#"): a dict with title, scale, lang,
    corners [[lon, lat]×4], points [[lon, lat, label]], shapes [{"kind", "coords"}], decimals. ValueError when
    it is not one."""
    text = str(url_or_hash or "").strip()
    if "#" in text:
        text = text.split("#", 1)[1]
    text = text.upper() if text[:1] in ("a", "b") else text   # a scanner that lower-cased the alphanumeric part
    if len(text) < 2 or text[0] not in "AB":
        raise ValueError("not a Biruni map address")
    data = b43decode(text[1:])
    if text[0] == "B":
        try:
            data = _inflate(data)
        except zlib.error as e:
            raise ValueError("damaged address") from e
    if not data or data[0] != FORMAT_VERSION:
        raise ValueError("made by a newer Biruni GIS")
    try:
        return unpack(data[1:])
    except UnicodeDecodeError as e:
        raise ValueError("damaged address") from e
