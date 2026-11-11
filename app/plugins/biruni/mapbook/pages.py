"""The pages of a map book: where each page is, its turn, scale and orientation, its number, name and neighbours.

Pure geometry - no layout here. A page is the map frame of one sheet: centre, ground width and height of the frame
(unrotated) and the turn of the map (degrees counter-clockwise from east, the same value the layout map's rotation
takes, so the page's left-right axis runs along ``angle`` on the ground). ``core`` is the area the page is "about"
(a grid cell, an index polygon, the feature): the key map, the neighbours and the name index use it; the frame shows
a little more around it.
"""
import math

from qgis.core import Qgis, QgsGeometry, QgsPointXY, QgsRectangle, QgsSpatialIndex

PAPER_MM = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0), "A1": (841.0, 594.0),
            "A0": (1189.0, 841.0), "Letter": (279.4, 215.9), "Tabloid": (431.8, 279.4)}
# standard scales a book may use (the denominators)
BOOK_SCALES = [100, 200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 20000, 25000, 50000, 100000, 200000,
               250000, 500000, 1000000, 2000000, 5000000, 10000000]
SIDES = ("up", "right", "down", "left")
MARGIN_MM = 7.0          # paper edge to the neat line
HEADER_MM = 13.0         # title band
GAP_MM = 3.0
GRID_PAD_MM = 6.0        # room for the coordinate labels around the map frame


# ---- the sheet: where the map frame sits on the paper -----------------------------------------------------------
def paper_size(paper, landscape):
    w, h = PAPER_MM.get(paper, PAPER_MM["A4"])
    w, h = max(w, h), min(w, h)
    return (w, h) if landscape else (h, w)


def sheet_parts(paper, landscape, grid=True, rtl=False):
    """Rectangles (x, y, w, h in mm) of a map page: neat line, header, map frame (inside the grid labels), the map
    area with its labels, and the side (landscape) or bottom (portrait) panel."""
    pw, ph = paper_size(paper, landscape)
    m = MARGIN_MM
    neat = (m, m, pw - 2 * m, ph - 2 * m)
    header = (m, m, pw - 2 * m, HEADER_MM)
    top = m + HEADER_MM + GAP_MM
    pad = GRID_PAD_MM if grid else 1.5
    if landscape:
        panel_w = min(92.0, max(60.0, pw * 0.215))
        area_w = pw - 2 * m - panel_w - GAP_MM - 3.0
        area_x = m + 1.5 + (panel_w + GAP_MM if rtl else 0.0)
        area = (area_x, top, area_w, ph - m - 1.5 - top)
        panel = (m + 1.5 if rtl else area_x + area_w + GAP_MM, top, panel_w, area[3])
    else:
        panel_h = min(95.0, max(58.0, ph * 0.21))
        area = (m + 1.5, top, pw - 2 * m - 3.0, ph - m - 1.5 - top - panel_h - GAP_MM)
        panel = (m + 1.5, area[1] + area[3] + GAP_MM, area[2], panel_h)
    frame = (area[0] + pad, area[1] + pad, area[2] - 2 * pad, area[3] - 2 * pad)
    return {"page": (pw, ph), "neat": neat, "header": header, "area": area, "frame": frame, "panel": panel}


def frame_mm(paper, landscape, grid=True):
    f = sheet_parts(paper, landscape, grid)["frame"]
    return f[2], f[3]


# ---- one page ---------------------------------------------------------------------------------------------------
class Page:
    """One map page. Plain data; ``to_dict``/``from_dict`` keep it in the project."""

    FIELDS = ("cx", "cy", "w", "h", "angle", "scale", "landscape", "name", "number", "key", "row", "col",
              "excluded", "title", "core", "manual", "chain", "theme")

    def __init__(self, cx, cy, w, h, angle=0.0, scale=1000.0, landscape=True, name="", key="", row=None, col=None,
                 core=None, chain=None):
        self.cx, self.cy, self.w, self.h = float(cx), float(cy), float(w), float(h)
        self.angle = float(angle or 0.0)
        self.scale = float(scale)
        self.landscape = bool(landscape)
        self.name = str(name or "")
        self.number = None          # 1..N in reading order; None when the page is left out
        self.key = str(key or "")   # what made it: feature id, stop id, grid cell
        self.row, self.col = row, col
        self.excluded = False
        self.title = ""             # the page's own title (from the title expression), filled when rendered
        self.core = core            # WKT of the area the page is about; None = the frame
        self.manual = False         # moved / turned / resized by hand
        self.chain = chain          # (start, end) along the route for strip pages
        self.theme = ""             # a map theme the page shows (Map Stops that remember their layers)
        self.neighbours = {}        # direction -> index into the book's page list

    # geometry
    def corners(self):
        """The frame's corners on the ground, clockwise from the top-left of the page."""
        a = math.radians(self.angle)
        ca, sa = math.cos(a), math.sin(a)
        out = []
        for dx, dy in ((-1, 1), (1, 1), (1, -1), (-1, -1)):
            x, y = dx * self.w / 2, dy * self.h / 2
            out.append(QgsPointXY(self.cx + x * ca - y * sa, self.cy + x * sa + y * ca))
        return out

    def polygon(self):
        pts = self.corners()
        return QgsGeometry.fromPolygonXY([pts + [pts[0]]])

    def core_geometry(self):
        if self.core:
            g = QgsGeometry.fromWkt(self.core)
            if g is not None and not g.isEmpty():
                return g
        return self.polygon()

    def to_page(self, x, y):
        """Ground point → page axes (u to the page's right, v to its top), from the centre."""
        a = math.radians(self.angle)
        dx, dy = x - self.cx, y - self.cy
        return dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)

    def from_page(self, u, v):
        a = math.radians(self.angle)
        return QgsPointXY(self.cx + u * math.cos(a) - v * math.sin(a), self.cy + u * math.sin(a) + v * math.cos(a))

    def unrotated_extent(self):
        return QgsRectangle(self.cx - self.w / 2, self.cy - self.h / 2, self.cx + self.w / 2, self.cy + self.h / 2)

    # keeping
    def to_dict(self):
        d = {k: getattr(self, k) for k in self.FIELDS}
        d["chain"] = list(self.chain) if self.chain else None
        return d

    @classmethod
    def from_dict(cls, d):
        p = cls(d["cx"], d["cy"], d["w"], d["h"], d.get("angle", 0.0), d.get("scale", 1000.0),
                d.get("landscape", True), d.get("name", ""), d.get("key", ""), d.get("row"), d.get("col"),
                d.get("core"), tuple(d["chain"]) if d.get("chain") else None)
        p.number = d.get("number")
        p.excluded = bool(d.get("excluded", False))
        p.title = d.get("title", "") or ""
        p.manual = bool(d.get("manual", False))
        p.theme = d.get("theme", "") or ""
        return p

    def copy(self):
        return Page.from_dict(self.to_dict())


def frame_for(page, paper, grid=True):
    """Set the page's ground size from its paper, orientation and scale."""
    fw, fh = frame_mm(paper, page.landscape, grid)
    page.w, page.h = fw * page.scale / 1000.0, fh * page.scale / 1000.0
    return page


# ---- scale and orientation -------------------------------------------------------------------------------------
def standard_scale(denominator, scales=None):
    """The smallest standard scale that is at least ``denominator`` (i.e. the largest scale that still fits)."""
    for s in scales or BOOK_SCALES:
        if s >= denominator - 1e-6:
            return s
    return (scales or BOOK_SCALES)[-1]


def best_fit(width, height, paper, orientation="auto", grid=True, margin=0.08, scales=None):
    """(landscape?, scale) that shows a ``width`` × ``height`` area (ground units, along the page axes) with a
    margin around it at the largest standard scale. ``orientation``: auto | portrait | landscape."""
    best = None
    for landscape in (True, False):
        if orientation == "portrait" and landscape or orientation == "landscape" and not landscape:
            continue
        fw, fh = frame_mm(paper, landscape, grid)
        need = max(width * (1 + 2 * margin) / fw, height * (1 + 2 * margin) / fh, 1e-9) * 1000.0
        scale = standard_scale(need, scales)
        fill = need / scale  # how much of the frame the area fills at that scale
        key = (scale, -fill)
        if best is None or key < best[0]:
            best = (key, landscape, scale)
    return best[1], best[2]


def extent_along(geom, angle):
    """Width, height and centre of ``geom`` measured along a page turned by ``angle``."""
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    us, vs = [], []
    for v in geom.vertices():
        us.append(v.x() * ca + v.y() * sa)
        vs.append(-v.x() * sa + v.y() * ca)
    if not us:
        c = geom.centroid().asPoint()
        return 0.0, 0.0, c
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    uc, vc = (u0 + u1) / 2, (v0 + v1) / 2
    centre = QgsPointXY(uc * ca - vc * sa, uc * sa + vc * ca)
    return u1 - u0, v1 - v0, centre


def fit_pages(items, paper, orientation="auto", scale_mode="best", fixed_scale=0, grid=True, margin=0.08):
    """Pages that each show one geometry: ``items`` = [(geometry, key, name, angle)].
    scale_mode: best (each page its own largest scale and orientation) | common (one scale, the one the largest
    item needs, for the whole book) | fixed (``fixed_scale``)."""
    fits = []
    for geom, key, name, angle in items:
        if geom is None or geom.isEmpty():
            continue
        w, h, c = extent_along(geom, angle or 0.0)
        w, h = max(w, 1e-6), max(h, 1e-6)
        landscape, scale = best_fit(w, h, paper, orientation, grid, margin)
        fits.append([geom, key, name, angle or 0.0, w, h, c, landscape, scale])
    if not fits:
        return []
    if scale_mode == "common":
        common = max(f[8] for f in fits)
        for f in fits:
            f[8] = common
    elif scale_mode == "fixed" and fixed_scale > 0:
        for f in fits:
            f[8] = float(fixed_scale)
            if orientation == "auto":
                f[7] = f[4] >= f[5]
    pages = []
    for geom, key, name, angle, w, h, c, landscape, scale in fits:
        p = Page(c.x(), c.y(), 0, 0, angle, scale, landscape, name, key)
        frame_for(p, paper, grid)
        if geom.type() == Qgis.GeometryType.Polygon:
            simple = geom if geom.constGet().nCoordinates() <= 400 else geom.simplify(max(w, h) / 400.0)
            p.core = simple.asWkt(3)
        pages.append(p)
    return pages


# ---- the automatic grid ----------------------------------------------------------------------------------------
class DataCover:
    """Where the data is: a spatial index over the geometries, with exact tests on demand."""

    def __init__(self, geometries):
        self.geoms = [g for g in geometries if g is not None and not g.isEmpty()]
        self.index = QgsSpatialIndex()
        self.extent = QgsRectangle()
        for k, g in enumerate(self.geoms):
            bb = g.boundingBox()
            if bb.width() == 0 and bb.height() == 0:
                bb = QgsRectangle(bb.xMinimum() - 1e-6, bb.yMinimum() - 1e-6, bb.xMaximum() + 1e-6,
                                  bb.yMaximum() + 1e-6)
            self.index.addFeature(k, bb)
            if self.extent.isEmpty():
                self.extent = QgsRectangle(bb)
            else:
                self.extent.combineExtentWith(bb)

    def hits(self, rect):
        """True when some data falls inside ``rect`` (a QgsRectangle)."""
        cell = None
        for k in self.index.intersects(rect):
            g = self.geoms[k]
            bb = g.boundingBox()
            if rect.contains(bb):
                return True
            cell = cell or QgsGeometry.fromRect(rect)
            if g.intersects(cell):
                return True
        return False

    def spans(self, band, horizontal=True):
        """The stretches of a band (QgsRectangle) that hold data: merged [(start, end)] along x (or y)."""
        out = []
        cut = None
        for k in self.index.intersects(band):
            g = self.geoms[k]
            bb = g.boundingBox()
            if not band.contains(bb):
                cut = cut or QgsGeometry.fromRect(band)
                piece = g.intersection(cut)
                if piece.isEmpty():
                    continue
                bb = piece.boundingBox()
            out.append((bb.xMinimum(), bb.xMaximum()) if horizontal else (bb.yMinimum(), bb.yMaximum()))
        out.sort()
        merged = []
        for a, b in out:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [tuple(m) for m in merged]


def _cover_line(spans, length):
    """Fewest windows of ``length`` covering every span (greedy - optimal on a line). Returns window starts."""
    starts = []
    reach = -math.inf
    for a, b in spans:
        if b <= reach:
            continue
        x = max(a, reach)
        while True:
            starts.append(x)
            reach = x + length
            if reach >= b:
                break
            x = reach
    return starts


def plain_grid(extent, cell_w, cell_h):
    """Cells of a plain grid over the whole extent (the count a simple grid needs)."""
    cols = max(1, math.ceil(extent.width() / cell_w - 1e-9))
    rows = max(1, math.ceil(extent.height() / cell_h - 1e-9))
    return cols, rows


def aligned_cells(cover, extent, cell_w, cell_h, ox=0.0, oy=0.0, keep_empty=False):
    """Cells (row, col, rect) of a grid whose origin is shifted by (ox, oy) cells, data cells only."""
    x0 = extent.xMinimum() - ox * cell_w
    y1 = extent.yMaximum() + oy * cell_h
    cols = max(1, math.ceil((extent.xMaximum() - x0) / cell_w - 1e-9))
    rows = max(1, math.ceil((y1 - extent.yMinimum()) / cell_h - 1e-9))
    out = []
    for r in range(rows):
        for c in range(cols):
            rect = QgsRectangle(x0 + c * cell_w, y1 - (r + 1) * cell_h, x0 + (c + 1) * cell_w, y1 - r * cell_h)
            if keep_empty or cover.hits(rect):
                out.append((r, c, rect))
    return out


def brick_cells(cover, extent, cell_w, cell_h, oy=0.0, horizontal=True):
    """Rows (or columns) of cells, each row placed on its own to cover its data with the fewest cells."""
    out = []
    if horizontal:
        y1 = extent.yMaximum() + oy * cell_h
        rows = max(1, math.ceil((y1 - extent.yMinimum()) / cell_h - 1e-9))
        for r in range(rows):
            band = QgsRectangle(extent.xMinimum() - cell_w, y1 - (r + 1) * cell_h, extent.xMaximum() + cell_w,
                                y1 - r * cell_h)
            for x in _cover_line(cover.spans(band, True), cell_w):
                rect = QgsRectangle(x, band.yMinimum(), x + cell_w, band.yMaximum())
                out.append((r, round((x - extent.xMinimum()) / cell_w), rect))
    else:
        x0 = extent.xMinimum() - oy * cell_w
        cols = max(1, math.ceil((extent.xMaximum() - x0) / cell_w - 1e-9))
        for c in range(cols):
            band = QgsRectangle(x0 + c * cell_w, extent.yMinimum() - cell_h, x0 + (c + 1) * cell_w,
                                extent.yMaximum() + cell_h)
            for y in _cover_line(cover.spans(band, False), cell_h):
                rect = QgsRectangle(band.xMinimum(), y, band.xMaximum(), y + cell_h)
                out.append((round((extent.yMaximum() - y - cell_h) / cell_h), c, rect))
    # the data hit test removes cells that only touched a span's end
    return [c for c in out if cover.hits(c[2])]


def grid_cells(cover, cell_w, cell_h, fewest=True, keep_empty=False, tries=4):
    """The grid cells for a book: (method, [(row, col, rect)], plain grid count).
    ``fewest``: try shifted grid origins and rows/columns placed on their own, keep the fewest cells (ties go to
    the plain aligned grid, whose neighbours line up)."""
    extent = cover.extent
    if extent.isEmpty():
        return "plain", [], 0
    cols, rows = plain_grid(extent, cell_w, cell_h)
    plain = aligned_cells(cover, extent, cell_w, cell_h, 0, 0, keep_empty)
    best = ("aligned", plain)
    if keep_empty or not fewest:
        return best[0], best[1], cols * rows
    for i in range(tries):
        for j in range(tries):
            if i == j == 0:
                continue
            cells = aligned_cells(cover, extent, cell_w, cell_h, i / tries, j / tries)
            if len(cells) < len(best[1]):
                best = ("aligned", cells)
    for horizontal in (True, False):
        for k in range(tries):
            cells = brick_cells(cover, extent, cell_w, cell_h, k / tries, horizontal)
            if cells and len(cells) < len(best[1]):
                best = ("rows" if horizontal else "columns", cells)
    return best[0], best[1], cols * rows


def grid_pages(cover, paper, landscape, scale, overlap=0.0, grid=True, fewest=True, keep_empty=False):
    """Pages of the automatic grid at ``scale``: the cell is the frame less the overlap; every page shows its cell
    and a strip of the next cells around it."""
    fw, fh = frame_mm(paper, landscape, grid)
    W, H = fw * scale / 1000.0, fh * scale / 1000.0
    cw, ch = W * (1 - overlap), H * (1 - overlap)
    method, cells, plain = grid_cells(cover, cw, ch, fewest, keep_empty)
    pages = []
    for r, c, rect in cells:
        ctr = rect.center()
        p = Page(ctr.x(), ctr.y(), W, H, 0.0, scale, landscape, key=f"{r},{c}", row=r, col=c,
                 core=QgsGeometry.fromRect(rect).asWkt(3))
        pages.append(p)
    return pages, method, plain


def auto_grid_scale(cover, paper, landscape, max_pages=24, overlap=0.0, grid=True):
    """The largest standard scale whose grid needs no more than ``max_pages`` pages."""
    fw, fh = frame_mm(paper, landscape, grid)
    ext = cover.extent
    for s in BOOK_SCALES:
        W, H = fw * s / 1000.0 * (1 - overlap), fh * s / 1000.0 * (1 - overlap)
        cols, rows = plain_grid(ext, W, H)
        if cols * rows > max_pages * 4:
            continue
        if len(aligned_cells(cover, ext, W, H)) <= max_pages:
            return s
    return BOOK_SCALES[-1]


# ---- strips along a route --------------------------------------------------------------------------------------
def _substring(line, start, end):
    g = line.constGet()
    return QgsGeometry(g.curveSubstring(start, end))


def _chord_angle(line, start, end):
    a = line.interpolate(start).asPoint()
    b = line.interpolate(end).asPoint()
    if a.distance(b) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(b.y() - a.y(), b.x() - a.x()))


def _fits(line, start, end, W, H, pad):
    piece = _substring(line, start, end)
    ang = _chord_angle(line, start, end)
    w, h, _c = extent_along(piece, ang)
    return w <= W * (1 - 2 * pad) + 1e-9 and h <= H * (1 - 2 * pad) + 1e-9


def strip_pages(line, paper, landscape, scale, overlap_m=0.0, grid=True, pad=0.06, reverse_view=False):
    """Pages along a route: each page turned so the route runs along it (left to right; right to left when
    ``reverse_view``), as long a stretch per page as the frame holds, ``overlap_m`` of route repeated on the next
    page. ``line``: a single-part line geometry in a metric CRS."""
    if line.isMultipart():
        merged = line.mergeLines()
        line = merged if not merged.isMultipart() else QgsGeometry.fromPolylineXY(
            max(merged.asMultiPolyline(), key=lambda pl: QgsGeometry.fromPolylineXY(pl).length()))
    fw, fh = frame_mm(paper, landscape, grid)
    W, H = fw * scale / 1000.0, fh * scale / 1000.0
    total = line.length()
    step = max(W / 60.0, total / 4000.0, 1e-6)
    pages = []
    s = 0.0
    guard = 0
    while s < total - 1e-6 and guard < 2000:
        guard += 1
        if _fits(line, s, total, W, H, pad):
            e = total
        else:
            lo, hi = s, None
            e = s + step
            while e < total:
                if _fits(line, s, e, W, H, pad):
                    lo = e
                    e += step
                else:
                    hi = e
                    break
            hi = hi if hi is not None else total
            for _ in range(30):
                mid = (lo + hi) / 2
                if _fits(line, s, mid, W, H, pad):
                    lo = mid
                else:
                    hi = mid
            e = max(lo, s + step)
        ang = _chord_angle(line, s, e)
        view = ang + 180.0 if reverse_view else ang
        piece = _substring(line, s, e)
        _w, _h, c = extent_along(piece, view)
        pages.append(Page(c.x(), c.y(), W, H, _norm(view), scale, landscape, key=f"{s:.3f}", chain=(s, e)))
        if e >= total - 1e-6:
            break
        s = max(e - overlap_m, s + step)
    return pages


def _norm(angle):
    a = (angle + 180.0) % 360.0 - 180.0
    return 180.0 if abs(a + 180.0) < 1e-9 else a


# ---- reading order, numbers, names -----------------------------------------------------------------------------
def _rows(pages):
    """Pages grouped in rows from the top (grid rows when known)."""
    if all(p.row is not None for p in pages):
        rows = {}
        for k, p in enumerate(pages):
            rows.setdefault(p.row, []).append(k)
        return [rows[r] for r in sorted(rows)]
    hs = sorted(p.h for p in pages) or [1.0]
    tol = hs[len(hs) // 2] * 0.5
    order = sorted(range(len(pages)), key=lambda k: -pages[k].cy)
    rows = []
    for k in order:
        if rows and abs(pages[rows[-1][0]].cy - pages[k].cy) <= tol:
            rows[-1].append(k)
        else:
            rows.append([k])
    return rows


def reading_order(pages, mode="rows", rtl=False):
    """Indices of ``pages`` in reading order. mode: rows (top to bottom, each row in the reading direction) |
    snake (rows, every other row back the other way) | columns | route / as_is (the order they were made in)."""
    if mode in ("route", "as_is") or len(pages) < 2:
        return list(range(len(pages)))
    if mode == "columns":
        cols = sorted(range(len(pages)), key=lambda k: (pages[k].cx if not rtl else -pages[k].cx, -pages[k].cy))
        return cols
    out = []
    for n, row in enumerate(_rows(pages)):
        row.sort(key=lambda k: pages[k].cx * (-1 if rtl else 1))
        if mode == "snake" and n % 2 == 1:
            row.reverse()
        out += row
    return out


def column_letters(k):
    """0 → A, 25 → Z, 26 → AA (as the name index)."""
    s = ""
    k += 1
    while k:
        k, r = divmod(k - 1, 26)
        s = chr(65 + r) + s
    return s


def number_pages(pages, naming="number", names=None, start=1):
    """Give the pages that are not left out numbers from ``start`` in list order, and names:
    number (the page number) | grid (column letter + row number: A1, B1 … from the grid) | given (``names`` or
    the names they already have, the number when empty)."""
    n = start
    cols = sorted({p.col for p in pages if p.col is not None})
    rows = sorted({p.row for p in pages if p.row is not None})
    for k, p in enumerate(pages):
        if p.excluded:
            p.number = None
            continue
        p.number = n
        n += 1
        if naming == "grid" and p.row is not None and p.col is not None:
            p.name = f"{column_letters(cols.index(p.col))}{rows.index(p.row) + 1}"
        elif naming == "given":
            given = names[k] if names and k < len(names) else p.name
            p.name = str(given) if given not in (None, "") else str(p.number)
        elif naming == "number" or not p.name:
            p.name = str(p.number)
    return pages


# ---- neighbours ------------------------------------------------------------------------------------------------
def _edge_strip(page, direction, depth):
    """The band just outside one edge of the page (ground polygon)."""
    hw, hh = page.w / 2, page.h / 2
    if direction == "right":
        u0, u1, v0, v1 = hw, hw + depth, -hh * 0.9, hh * 0.9
    elif direction == "left":
        u0, u1, v0, v1 = -hw - depth, -hw, -hh * 0.9, hh * 0.9
    elif direction == "up":
        u0, u1, v0, v1 = -hw * 0.9, hw * 0.9, hh, hh + depth
    else:
        u0, u1, v0, v1 = -hw * 0.9, hw * 0.9, -hh - depth, -hh
    pts = [page.from_page(u, v) for u, v in ((u0, v1), (u1, v1), (u1, v0), (u0, v0), (u0, v1))]
    return QgsGeometry.fromPolygonXY([pts])


def find_neighbours(pages, sequence=False, reverse=False):
    """For every page that is in the book: the page across each edge (up, right, down, left), as list indices.
    The neighbour is the page that covers most of the band just outside that edge - the next grid cell, the next
    sheet along a strip, the touching index polygon. ``sequence``: strips - left is the page before and right the
    page after (in page order), as the route runs along the page (the other way round with ``reverse``, for strips
    whose route runs from right to left)."""
    live = [k for k, p in enumerate(pages) if not p.excluded]
    for p in pages:
        p.neighbours = {}
    if sequence:
        ordered = sorted(live, key=lambda k: pages[k].number or 0)
        back, ahead = ("right", "left") if reverse else ("left", "right")
        for i, k in enumerate(ordered):
            if i > 0:
                pages[k].neighbours[back] = ordered[i - 1]
            if i + 1 < len(ordered):
                pages[k].neighbours[ahead] = ordered[i + 1]
        return pages
    cores = {k: pages[k].core_geometry() for k in live}
    index = QgsSpatialIndex()
    for k in live:
        index.addFeature(k, cores[k].boundingBox())
    for k in live:
        p = pages[k]
        core = cores[k]
        # the core's own extent along the page (a grid cell is smaller than the frame)
        cw, chh, cc = extent_along(core, p.angle)
        probe = Page(cc.x(), cc.y(), max(cw, 1e-9), max(chh, 1e-9), p.angle)
        depth = 0.25 * min(probe.w, probe.h)
        for d in SIDES:
            strip = _edge_strip(probe, d, depth)
            best, best_a = None, 0.0
            for j in index.intersects(strip.boundingBox()):
                if j == k:
                    continue
                inter = cores[j].intersection(strip)
                a = inter.area() if not inter.isEmpty() else 0.0
                if a > best_a + 1e-9:
                    best, best_a = j, a
            if best is not None and best_a > 0.08 * strip.area():
                p.neighbours[d] = best
    return pages
