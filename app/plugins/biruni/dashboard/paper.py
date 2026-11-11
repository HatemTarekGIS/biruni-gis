"""The live paper («اللوحة على الورق»): a map printed from Biruni that comes alive under a phone camera.

A paper is the map widget of a dashboard drawn as a plan (parcels, streets, names) on an A4/A3 page, framed by
square fiducial markers of our own (6x6 cells: a black border and 16 bits, a dictionary in which every code and
its rotations differ from all others in at least 3 bits). The phone finds the markers in the camera picture, works
out where the page is (a homography from page millimetres to image pixels) and draws the live state of every
parcel exactly on top of the printed one (ext_paper.py).

Marker ids 0-3 sit in the corners and 4-7 at the middles of the edges of every paper; one more marker (an id from
FIXED on) is the paper's own code, so the phone knows which paper it is looking at.

The paper's passport says where the map is on the page: the frame in millimetres and the EPSG:3857 extent printed
in it, the markers' places, the dashboard and the map widget. It is signed with this PC's dashboard key (cards.py)
and kept in the project; the phone gets the passports from the PC over its card's channel, never from the paper,
so a paper made elsewhere cannot point the phone at another place. The QR code on the paper holds the passport's
id, code and signature: the phone may check that the paper in its hands is the one this PC signed.
"""
import json
import math
import os
import secrets
import time

from ..secure import crypto
from . import cards

R_EARTH = 6378137.0
PAGES = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}     # landscape, millimetres
FIXED = 8                  # marker ids every paper has (corners, then the middles of the edges)
CELLS = 6
ENTRY = "papers"
QUIET = "#FFFFFF"


# ---- the marker dictionary ----------------------------------------------------------------------------------------
def rot(code):
    """The 4x4 bits of a code turned a quarter clockwise."""
    out = 0
    for r in range(4):
        for c in range(4):
            if code >> (15 - (r * 4 + c)) & 1:
                # (r, c) goes to (c, 3 - r)
                out |= 1 << (15 - (c * 4 + (3 - r)))
    return out


def _pop(x):
    return bin(x).count("1")


def make_dictionary(n=40, dmin=3):
    """n codes; each differs from its own rotations and from every rotation of the others in >= dmin bits, has
    between 5 and 11 black bits and no fully black or white row (they read badly)."""
    out, rots = [], []
    x = 0x2F1B
    tried = 0
    while len(out) < n and tried < 200000:
        tried += 1
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        code = (x >> 7) & 0xFFFF
        if not 5 <= _pop(code) <= 11:
            continue
        rows = [(code >> (12 - 4 * i)) & 0xF for i in range(4)]
        if any(r in (0, 15) for r in rows):
            continue
        rs = [code, rot(code), rot(rot(code)), rot(rot(rot(code)))]
        if min(_pop(code ^ r) for r in rs[1:]) < dmin:
            continue
        if any(_pop(r ^ o) < dmin for r in rs for o in rots):
            continue
        out.append(code)
        rots.extend(rs)
    return out


DICT = make_dictionary()


def marker_cells(mid):
    """6x6 rows of 1 (black) / 0 (white) for marker id mid."""
    code = DICT[mid]
    g = [[1] * CELLS for _ in range(CELLS)]
    for r in range(4):
        for c in range(4):
            g[r + 1][c + 1] = (code >> (15 - (r * 4 + c))) & 1
    return g


# ---- the page --------------------------------------------------------------------------------------------------------
def page_size(size="A4", orient="landscape"):
    w, h = PAGES.get(size, PAGES["A4"])
    return (w, h) if orient == "landscape" else (h, w)


def layout(w, h, code):
    """Markers [[id, x, y, side]] (top-left of the black square, millimetres) and the map frame [x0, y0, x1, y1]."""
    s = 16.0 if min(w, h) <= 215 else 22.0
    m = 8.0
    mk = [[0, m, m, s], [1, w - m - s, m, s], [2, w - m - s, h - m - s, s], [3, m, h - m - s, s],
          [4, (w - s) / 2, m, s], [5, w - m - s, (h - s) / 2, s], [6, (w - s) / 2, h - m - s, s],
          [7, m, (h - s) / 2, s], [code, round(w * 0.27 - s / 2, 2), h - m - s, s]]
    g = 4.0
    frame = [m + s + g, m + s + g, w - m - s - g, h - m - s - g]
    return mk, frame


def merc(lon, lat):
    lat = max(-85.0, min(85.0, lat))
    return (R_EARTH * math.radians(lon), R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def unmerc(x, y):
    return (math.degrees(x / R_EARTH), math.degrees(2 * math.atan(math.exp(y / R_EARTH)) - math.pi / 2))


def fit_extent(bbox4326, frame, pad=0.04):
    """The EPSG:3857 extent printed in the frame: the bbox with a margin, widened on one side to keep the frame's
    proportions (so the scale is the same across and down)."""
    x0, y0 = merc(bbox4326[0], bbox4326[1])
    x1, y1 = merc(bbox4326[2], bbox4326[3])
    w, h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    x0, x1, y0, y1 = x0 - w * pad, x1 + w * pad, y0 - h * pad, y1 + h * pad
    w, h = x1 - x0, y1 - y0
    fa = (frame[2] - frame[0]) / (frame[3] - frame[1])
    if w / h < fa:
        nw = h * fa
        x0, x1 = x0 - (nw - w) / 2, x1 + (nw - w) / 2
    else:
        nh = w / fa
        y0, y1 = y0 - (nh - h) / 2, y1 + (nh - h) / 2
    return [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)]


def to_page(p, lon, lat):
    """Page millimetres of a point (lon, lat) on paper p."""
    x, y = merc(lon, lat)
    f, e = p["frame"], p["merc"]
    return (f[0] + (x - e[0]) / (e[2] - e[0]) * (f[2] - f[0]), f[1] + (e[3] - y) / (e[3] - e[1]) * (f[3] - f[1]))


# ---- passports -------------------------------------------------------------------------------------------------------
def _body(p):
    return crypto.canonical({k: v for k, v in p.items() if k != "sig"})


def qr_side(w, h):
    """The side (mm) of the paper's QR code when it holds an address: big enough for a phone at ~40 cm."""
    return 26.0 if min(w, h) <= 215 else 32.0


def make_passport(dash_id, widget_id, title, bbox4326, size="A4", orient="landscape", code=None, seed=None,
                  url_fn=None):
    """url_fn(paper id) -> the address the paper's QR code opens (the dashboard page with #p=<id>); None keeps the
    old QR code (the passport token only)."""
    w, h = page_size(size, orient)
    code = FIXED if code is None else int(code)
    if not FIXED <= code < len(DICT):
        raise ValueError("paper code out of range")
    mk, frame = layout(w, h, code)
    pid = "p_" + secrets.token_hex(5)
    url = url_fn(pid) if url_fn else None
    if url:   # a bigger QR code: the map frame gives up the room above the bottom markers
        frame[3] = min(frame[3], h - 8.0 - qr_side(w, h) - 4.0)
    seed = seed or cards.seed()
    p = {"v": 1, "id": pid, "code": code, "dash": dash_id, "widget": widget_id,
         "title": str(title or "")[:120], "size": size, "orient": orient, "page": [w, h],
         "frame": [round(v, 3) for v in frame], "merc": fit_extent(bbox4326, frame), "m": mk,
         "iat": int(time.time()), "pub": crypto.public_key(seed).hex()}
    if url:
        p["url"] = str(url)[:600]
    p["sig"] = crypto.sign(seed, _body(p)).hex()
    return p


def verify_passport(p, pub=None):
    """True when the passport is whole and signed by pub (default: the key it names, which the caller compares
    with this PC's own)."""
    try:
        key = bytes.fromhex(pub or p["pub"])
        return crypto.verify(key, bytes.fromhex(p["sig"]), _body(p))
    except (KeyError, ValueError, TypeError):
        return False


def qr_token(p):
    """What the paper's QR code holds: id, code and the passport's signature."""
    return "BLP1.%s.%d.%s" % (p["id"], p["code"], cards.b64u(bytes.fromhex(p["sig"])))


def _project(project=None):
    from qgis.core import QgsProject

    return project or QgsProject.instance()


def saved(project=None):
    from . import model

    return model._entries(_project(project), ENTRY)


def save_passport(p, project=None):
    from . import model

    project = _project(project)
    allp = model._entries(project, ENTRY)
    allp[p["id"]] = p
    model._put_entries(project, ENTRY, allp)


def papers_for(dash_id, project=None):
    """The papers of a dashboard, newest first, whose signature is this PC's."""
    mine = cards.public_hex()
    out = [p for p in saved(project).values() if p.get("dash") == dash_id and p.get("pub") == mine
           and verify_passport(p, mine)]
    return sorted(out, key=lambda p: -int(p.get("iat") or 0))


def next_code(project=None):
    n = len(saved(project))
    return FIXED + n % (len(DICT) - FIXED)


# ---- drawing ---------------------------------------------------------------------------------------------------------
_FAMILY = []


def font_family():
    """A family with Arabic letters (Segoe UI is loaded from the Windows fonts when Qt does not know it - an
    offscreen Qt knows no system fonts)."""
    if _FAMILY:
        return _FAMILY[0]
    from qgis.PyQt.QtGui import QFontDatabase

    fam = None
    if "Segoe UI" in QFontDatabase.families():
        fam = "Segoe UI"
    else:
        for path in (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\tahoma.ttf"):
            if os.path.exists(path):
                i = QFontDatabase.addApplicationFont(path)
                if i >= 0 and QFontDatabase.applicationFontFamilies(i):
                    fam = QFontDatabase.applicationFontFamilies(i)[0]
                    if path.endswith("segoeui.ttf"):
                        QFontDatabase.addApplicationFont(r"C:\Windows\Fonts\segoeuib.ttf")
                    break
    _FAMILY.append(fam or "Sans Serif")
    return _FAMILY[0]


TEXTS = {
    "ar": {"hint": "وجّه كاميرا الهاتف إلى هذه الورقة لتراها حيّة", "kind": "ورقة حيّة من Biruni GIS",
           "check": "رمز التحقق"},
    "en": {"hint": "Point a phone camera at this paper to see it live", "kind": "A live paper from Biruni GIS",
           "check": "Check code"},
}


def _geoms(f):
    g = f.get("geometry") or {}
    t, c = g.get("type"), g.get("coordinates")
    if t == "Polygon":
        return "fill", [c]
    if t == "MultiPolygon":
        return "fill", c
    if t == "LineString":
        return "line", [c]
    if t == "MultiLineString":
        return "line", c
    if t == "Point":
        return "point", [c]
    if t == "MultiPoint":
        return "point", c
    return None, []


def draw(painter, p, layers, ppm, lang="ar", basemap=None, when=None):
    """Draw paper p at ppm device pixels per millimetre: the plan of the map layers, the markers, the texts and
    the QR code. basemap: an optional QImage of the frame's extent, drawn faded under the plan."""
    from qgis.PyQt.QtCore import QLineF, QPointF, QRectF, Qt
    from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPainterPath, QPen, QPolygonF

    tx = TEXTS.get(lang, TEXTS["en"])
    W, H = p["page"]
    fam = font_family()

    def X(mm):
        return mm * ppm

    def font(mm, bold=False):
        f = QFont(fam)
        f.setPixelSize(max(1, int(round(mm * ppm))))
        f.setBold(bold)
        return f

    def pt(lon, lat):
        x, y = to_page(p, lon, lat)
        return QPointF(X(x), X(y))

    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing)
    painter.setRenderHint(painter.RenderHint.TextAntialiasing)
    painter.fillRect(QRectF(0, 0, X(W), X(H)), QColor("#FFFFFF"))
    fr = p["frame"]
    frame = QRectF(X(fr[0]), X(fr[1]), X(fr[2] - fr[0]), X(fr[3] - fr[1]))
    painter.fillRect(frame, QColor("#FBFAF6"))
    painter.save()
    painter.setClipRect(frame)
    if basemap is not None and not basemap.isNull():
        painter.setOpacity(0.42)
        painter.drawImage(frame, basemap)
        painter.setOpacity(1.0)
    labels = []
    # fills first (districts drawn as boundaries, parcels as a light plan), then lines, then points
    order = sorted(layers or [], key=lambda L: {"fill": 0, "line": 1, "point": 2}.get(L.get("kind"), 3))
    for L in order:
        kind = L.get("kind")
        st = L.get("style") or {}
        faint = kind == "fill" and float(st.get("opacity", 1) if st.get("opacity") is not None else 1) < 0.2
        for f in (L.get("geojson") or {}).get("features") or []:
            k, parts = _geoms(f)
            props = f.get("properties") or {}
            if k == "fill":
                for poly in parts:
                    path = QPainterPath()
                    for ring in poly:
                        if len(ring) < 3:
                            continue
                        path.addPolygon(QPolygonF([pt(*c[:2]) for c in ring]))
                        path.closeSubpath()
                    if faint:
                        painter.setPen(QPen(QColor("#5B6770"), X(0.55), Qt.PenStyle.DashLine))
                        painter.setBrush(Qt.BrushStyle.NoBrush)
                    else:
                        painter.setPen(QPen(QColor("#7B848C"), X(0.16)))
                        painter.setBrush(QBrush(QColor("#FFFFFF")))
                    painter.drawPath(path)
                if props.get("_l"):
                    ring = parts[0][0] if parts and parts[0] else []
                    if ring:
                        cx = sum(c[0] for c in ring[:-1] or ring) / max(1, len(ring[:-1] or ring))
                        cy = sum(c[1] for c in ring[:-1] or ring) / max(1, len(ring[:-1] or ring))
                        labels.append(("area" if faint else "parcel", str(props["_l"]), pt(cx, cy), None))
            elif k == "line":
                main = str(props.get("_c", "")).upper() not in ("#FFFFFF", "#FFF", "")
                for line in parts:
                    poly = QPolygonF([pt(*c[:2]) for c in line])
                    painter.setPen(QPen(QColor("#A9A08A"), X(4.6 if main else 2.8), Qt.PenStyle.SolidLine,
                                        Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
                    painter.drawPolyline(poly)
                    painter.setPen(QPen(QColor("#FFF3C2" if main else "#FFFFFF"), X(3.8 if main else 2.1),
                                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
                    painter.drawPolyline(poly)
                    if props.get("_l") and len(line) >= 2:
                        a, b = pt(*line[0][:2]), pt(*line[-1][:2])
                        labels.append(("street", str(props["_l"]).strip(), QPointF((a.x() + b.x()) / 2,
                                                                                   (a.y() + b.y()) / 2),
                                       QLineF(a, b).angle()))
            elif k == "point":
                for c in parts:
                    q = pt(*c[:2])
                    big = bool(props.get("_l"))
                    r = X(1.6 if big else 0.55)
                    painter.setPen(QPen(QColor("#FFFFFF"), X(0.3)))
                    painter.setBrush(QBrush(QColor(props.get("_c") or "#555555")))
                    painter.drawEllipse(q, r, r)
                    if big:
                        labels.append(("poi", str(props["_l"]), QPointF(q.x(), q.y() - X(2.6)), None))
    # labels: districts large, streets along the street, parcel numbers small
    for kind, text, q, ang in sorted(labels, key=lambda t: {"parcel": 0, "street": 1, "poi": 2, "area": 3}[t[0]]):
        size = {"parcel": 1.55, "street": 1.9, "poi": 2.1, "area": 4.2}[kind]
        painter.save()
        painter.translate(q)
        if ang is not None:
            a = -ang
            if a < -90 or a > 90:
                a += 180
            if a > 180:
                a -= 360
            painter.rotate(a)
        painter.setFont(font(size, kind in ("area", "poi")))
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        box = QRectF(-tw / 2 - 2, -fm.height() / 2, tw + 4, fm.height())
        if kind == "area":
            painter.setPen(QPen(QColor(255, 255, 255, 230), X(0.9)))
            path = QPainterPath()
            path.addText(QPointF(box.left() + 2, box.top() + fm.ascent()), painter.font(), text)
            painter.drawPath(path)
            painter.setPen(QColor("#37474F"))
        else:
            painter.setPen(QColor("#4A5560" if kind == "parcel" else "#3E4A52"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
    painter.restore()
    painter.setPen(QPen(QColor("#37474F"), X(0.35)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(frame)
    # north arrow and scale bar, inside the frame's corner
    nx, ny = fr[0] + 7, fr[3] - 18
    path = QPainterPath()
    path.moveTo(X(nx), X(ny - 5))
    path.lineTo(X(nx + 2.6), X(ny + 3.5))
    path.lineTo(X(nx), X(ny + 2))
    path.lineTo(X(nx - 2.6), X(ny + 3.5))
    path.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#37474F"))
    painter.drawPath(path)
    e = p["merc"]
    m_per_mm = (e[2] - e[0]) / (fr[2] - fr[0]) * math.cos(math.radians(unmerc(0, (e[1] + e[3]) / 2)[1]))
    nice = [10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000]
    metres = min(nice, key=lambda v: abs(v / m_per_mm - 30))
    bar = metres / m_per_mm
    sx, sy = fr[0] + 4, fr[3] - 6
    painter.setPen(QPen(QColor("#37474F"), X(0.5)))
    painter.drawLine(QPointF(X(sx), X(sy)), QPointF(X(sx + bar), X(sy)))
    for xx in (sx, sx + bar):
        painter.drawLine(QPointF(X(xx), X(sy - 1.2)), QPointF(X(xx), X(sy)))
    painter.setFont(font(2.1))
    painter.drawText(QRectF(X(sx), X(sy - 4.4), X(bar), X(3.2)), Qt.AlignmentFlag.AlignCenter,
                     ("%d m" % metres) if metres < 1000 else ("%g km" % (metres / 1000)))
    # the markers, with their white quiet zone
    for mid, mx, my, ms in p["m"]:
        cell = ms / CELLS
        painter.fillRect(QRectF(X(mx - cell), X(my - cell), X(ms + 2 * cell), X(ms + 2 * cell)), QColor(QUIET))
        g = marker_cells(mid)
        for r in range(CELLS):
            for c in range(CELLS):
                if g[r][c]:
                    painter.fillRect(QRectF(X(mx + c * cell), X(my + r * cell), X(cell) + 0.6, X(cell) + 0.6),
                                     QColor("#000000"))
    # texts: the title (top, right), what this is (top, left), the hint and the QR code (bottom)
    s = p["m"][0][3]
    top = QRectF(X(fr[0]), X(8), X(fr[2] - fr[0]), X(s))
    seg_r = QRectF(X((W + s) / 2 + 4), top.top(), X(W - 8 - s - 4) - X((W + s) / 2 + 4), top.height())
    seg_l = QRectF(X(8 + s + 4), top.top(), X((W - s) / 2 - 4) - X(8 + s + 4), top.height())
    painter.setPen(QColor("#1F2A33"))
    painter.setFont(font(4.4, True))
    painter.drawText(seg_r, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                     painter.fontMetrics().elidedText(p.get("title") or "", Qt.TextElideMode.ElideLeft,
                                                      int(seg_r.width())))
    painter.setFont(font(2.8))
    painter.setPen(QColor("#52606B"))
    stamp = time.strftime("%Y-%m-%d", time.localtime(when or p.get("iat") or time.time()))
    if lang in ("ar", "fa"):   # a right-to-left line: the kind first (on the right), then the date
        painter.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    painter.drawText(seg_l, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tx["kind"] + "  ·  " + stamp)
    painter.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    code_x = next(x for i, x, y, z in p["m"] if i == p["code"])
    by = H - 8 - s
    hint = QRectF(X(code_x + s + 5), X(by), X((W - s) / 2 - 5) - X(code_x + s + 5), X(s))
    painter.setFont(font(3.4, True))
    painter.setPen(QColor("#0B4F4A"))
    painter.drawText(hint, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, tx["hint"])
    # QR code with the passport's id, code and signature
    try:
        from .. import vendor

        vendor.ensure_vendor()
        import segno

        q = segno.make(p.get("url") or qr_token(p), error="m" if p.get("url") else "m", micro=False)
        mat = [list(row) for row in q.matrix]
        n = len(mat)
        qs = qr_side(W, H) if p.get("url") else s
        qx = (W + s) / 2 + ((W - 8 - s) - (W + s) / 2) / 2 - qs / 2
        by = H - 8 - qs
        mod = qs / n
        painter.fillRect(QRectF(X(qx - 1.5), X(by - 1.5), X(qs + 3), X(qs + 3)), QColor("#FFFFFF"))
        for yy in range(n):
            for xx in range(n):
                if mat[yy][xx]:
                    painter.fillRect(QRectF(X(qx + xx * mod), X(by + yy * mod), X(mod) + 0.5, X(mod) + 0.5),
                                     QColor("#000000"))
        painter.setFont(font(2.2))
        painter.setPen(QColor("#52606B"))
        painter.drawText(QRectF(X(qx - 30), X(by + qs + 0.6), X(qs + 60), X(3)), Qt.AlignmentFlag.AlignCenter,
                         tx["check"])
    except Exception:  # noqa: BLE001 - a paper without its QR code still works (the markers carry its code)
        pass
    painter.restore()


def render_png(p, layers, path=None, ppm=4.0, lang="ar", basemap=None):
    """The paper as a QImage (and a PNG when path is given)."""
    from qgis.PyQt.QtGui import QImage, QPainter

    w, h = p["page"]
    img = QImage(int(round(w * ppm)), int(round(h * ppm)), QImage.Format.Format_RGB32)
    painter = QPainter(img)
    draw(painter, p, layers, ppm, lang, basemap)
    painter.end()
    if path:
        img.save(path, "PNG")
    return img


def render_pdf(p, layers, path, lang="ar", basemap=None, dpi=300):
    """The paper as a PDF page of its own size (vector drawing)."""
    from qgis.PyQt.QtCore import QMarginsF, QSizeF
    from qgis.PyQt.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter

    w, h = p["page"]
    pdf = QPdfWriter(path)
    pdf.setResolution(dpi)
    ps = QPageSize(QSizeF(w, h), QPageSize.Unit.Millimeter, "", QPageSize.SizeMatchPolicy.ExactMatch)
    pdf.setPageLayout(QPageLayout(ps, QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0),
                                  QPageLayout.Unit.Millimeter))
    pdf.setTitle(p.get("title") or "Biruni live paper")
    pdf.setCreator("Biruni GIS")
    painter = QPainter(pdf)
    draw(painter, p, layers, dpi / 25.4, lang, basemap)
    painter.end()
    return path


# ---- the map of a dashboard ------------------------------------------------------------------------------------------
def map_widgets(d):
    return [w for w in d.get("widgets") or [] if w.get("type") == "map"]


def map_layers(value):
    """The layers of a map widget's value (a first-shape map becomes one layer)."""
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("layers"), list):
        return value["layers"]
    if value.get("geojson"):
        return [{"id": "L0", "name": "", "kind": "fill", "geojson": value["geojson"], "style": {}}]
    return []


def bbox_of(layers):
    xs, ys = [], []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        elif isinstance(c, (list, tuple)):
            for x in c:
                walk(x)
    for L in layers:
        for f in (L.get("geojson") or {}).get("features") or []:
            walk((f.get("geometry") or {}).get("coordinates"))
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def basemap_image(p, project, ppm=6.0):
    """The project's basemap layers drawn for the paper's frame (a QImage), or None."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsMapRendererSequentialJob, QgsMapSettings, QgsRectangle
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor

    from .basemap import basemap_layers

    layers = basemap_layers(project)
    if not layers:
        return None
    fr = p["frame"]
    ms = QgsMapSettings()
    ms.setLayers(layers)
    ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    ms.setOutputSize(QSize(int((fr[2] - fr[0]) * ppm), int((fr[3] - fr[1]) * ppm)))
    ms.setBackgroundColor(QColor("#FFFFFF"))
    ms.setExtent(QgsRectangle(*p["merc"]))
    ms.setTransformContext(project.transformContext())
    job = QgsMapRendererSequentialJob(ms)
    job.start()
    job.waitForFinished()
    return job.renderedImage()


def paper_url(where, sharing=None, lang="ar", lan_ip=None):
    """url_fn for make_passport: "anywhere" (the public page, the bridge key of this PC) or "lan" (this PC's
    address on the office network). sharing: the dock's Sharing (started as needed)."""
    if where == "lan":
        if sharing is None:
            raise ValueError("the dashboard is not shared on this network")
        sharing.start()
        ip = lan_ip or (sharing.addresses() or ["127.0.0.1"])[0]
        base = f"https://{ip}:{sharing.lan.port}/d/{sharing.lan.token}/"
        return lambda pid: f"{base}#p={pid}&l={lang}"
    from .host import PAGE_URL

    key = None
    if sharing is not None:
        sharing.start_anywhere()
        key = sharing.anywhere.key
    if not key:
        from . import accounts
        from ..field_photos import bridge

        key = accounts.stable("bridge", bridge.new_key)
    from ..field_photos.bridge import pin

    i = pin(key)   # the PC's identity pin: new papers check it, papers printed before carry none and still open
    return lambda pid: f"{PAGE_URL}#k={key}&i={i}&p={pid}&l={lang}"


def make_paper(d, widget_id=None, size="A4", orient="landscape", project=None, pdf_path=None, png_path=None,
               lang="ar", with_basemap=True, url_fn=None):
    """Print a live paper of dashboard d: compute its map widget, sign and keep the passport, write the PDF
    (and a PNG). -> the passport."""
    from . import data

    project = _project(project)
    maps = map_widgets(d)
    if not maps:
        raise ValueError("the dashboard has no map")
    w = next((m for m in maps if m.get("id") == widget_id), maps[0])
    res = data.Computer(d, project).compute(card=None, filters=None)
    layers = map_layers(res["widgets"].get(w["id"]))
    box = bbox_of(layers)
    if box is None:
        raise ValueError("the map is empty")
    p = make_passport(d.get("id"), w["id"], d.get("title") or w.get("title") or "", box, size, orient,
                      next_code(project), url_fn=url_fn)
    save_passport(p, project)
    bm = basemap_image(p, project) if with_basemap else None
    if pdf_path:
        render_pdf(p, layers, pdf_path, lang, bm)
    if png_path:
        render_png(p, layers, png_path, 4.0, lang, bm)
    return p


# ---- the panel's action ----------------------------------------------------------------------------------------------
def print_ui(plugin, dock=None):
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtGui import QDesktopServices
    from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel,
                                     QMessageBox)

    from ..i18n import tr

    parent = dock if dock is not None else getattr(getattr(plugin, "iface", None), "mainWindow", lambda: None)()
    d = dock.current() if dock is not None and hasattr(dock, "current") else None
    if d is None:
        QMessageBox.information(parent, tr("Live paper"), tr("Choose a dashboard in the list first."))
        return None
    maps = map_widgets(d)
    if not maps:
        QMessageBox.information(parent, tr("Live paper"), tr("This dashboard has no map to print."))
        return None
    dlg = QDialog(parent)
    dlg.setWindowTitle(tr("Print a live paper"))
    form = QFormLayout(dlg)
    form.addRow(QLabel(tr("A map printed with markers: point a phone at it and the parcels come alive with the "
                          "dashboard's live state.")))
    wbox = QComboBox()
    for m in maps:
        wbox.addItem(m.get("title") or m["id"], m["id"])
    sbox = QComboBox()
    for k in ("A4", "A3"):
        sbox.addItem(k, k)
    obox = QComboBox()
    obox.addItem(tr("Landscape"), "landscape")
    obox.addItem(tr("Portrait"), "portrait")
    if len(maps) > 1:
        form.addRow(tr("Map"), wbox)
    form.addRow(tr("Paper size"), sbox)
    form.addRow(tr("Orientation"), obox)
    qbox = QComboBox()
    qbox.addItem(tr("From anywhere (the public page)"), "anywhere")
    qbox.addItem(tr("On this office network only"), "lan")
    qbox.addItem(tr("No address (markers only)"), "")
    form.addRow(tr("The QR code opens"), qbox)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    form.addRow(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    path, _f = QFileDialog.getSaveFileName(parent, tr("Save the live paper"),
                                           (d.get("title") or "live-paper") + ".pdf", "PDF (*.pdf)")
    if not path:
        return None
    try:
        from ..i18n import lang as ui_lang

        lang = "ar" if str(ui_lang()).startswith(("ar", "fa")) else "en"
    except Exception:  # noqa: BLE001
        lang = "ar"
    if qbox.currentData() == "anywhere":
        from ..secure.door import door_ok
        from .host import PAGE_URL

        if not door_ok(parent, PAGE_URL):
            return None
    try:
        where = qbox.currentData()
        url_fn = paper_url(where, getattr(dock, "sharing", None) if where == "lan" else
                           (getattr(dock, "sharing", None) or None), lang) if where else None
        p = make_paper(d, wbox.currentData(), sbox.currentData(), obox.currentData(), pdf_path=path, lang=lang,
                       url_fn=url_fn)
    except ValueError as e:
        QMessageBox.warning(parent, tr("Live paper"), str(e))
        return None
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    return p
