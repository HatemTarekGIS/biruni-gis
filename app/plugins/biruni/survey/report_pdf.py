"""Survey report (تقرير رفع مساحي) as an A4 PDF - one page per parcel, Arabic right to left.

Drawn with QPainter, so Arabic letters are joined and ordered by Qt's text
engine (the same on screen, in a PNG and in the PDF). Layout: title block,
parcel data, the parcel sketch with side lengths, corner numbers and north
arrow, the boundaries table, the corner coordinates, the area in figures and
words, notes, and boxes for the office stamp and the surveyor's signature."""
import math
import os
from dataclasses import dataclass, field

from qgis.PyQt.QtCore import QMarginsF, QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (QColor, QFont, QFontDatabase, QImage, QPageSize, QPainter, QPdfWriter, QPen,
                             QPolygonF, QTextOption)

A4_W, A4_H = 210.0, 297.0
MARGIN = 12.0
INK = QColor("#1b2733")
ACCENT = QColor("#1f4e79")
LIGHT = QColor("#e8eef5")
GRID = QColor("#9aa9b8")
_FAMILY = None


def font_family():
    """A font with Arabic letters: Arial / Tahoma / Segoe UI, registered from the Windows fonts folder when
    Qt does not list it (offscreen runs)."""
    global _FAMILY
    if _FAMILY:
        return _FAMILY
    wanted = ("Arial", "Tahoma", "Segoe UI")
    have = set(QFontDatabase.families())
    for fam in wanted:
        if fam in have:
            _FAMILY = fam
            return fam
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for fn in ("arial.ttf", "tahoma.ttf", "segoeui.ttf"):
        path = os.path.join(fonts, fn)
        if os.path.exists(path):
            fid = QFontDatabase.addApplicationFont(path)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                _FAMILY = fams[0]
                return _FAMILY
    _FAMILY = QFont().family()
    return _FAMILY


@dataclass
class ReportPage:
    title: str = "تقرير رفع مساحي"
    office: str = ""
    info: list = field(default_factory=list)          # [(label, value)] shown right to left, two per row
    pts: list = field(default_factory=list)           # parcel ring (projected), counter-clockwise
    corners: list = field(default_factory=list)       # [(no, x, y)]
    side_labels: dict = field(default_factory=dict)   # {"N": "شمالاً: شارع عرض 15م", ...}
    boundaries: list = field(default_factory=list)    # [(direction, length text, neighbour)]
    area: float = 0.0
    area_words: str = ""
    notes: str = ""
    engineer: str = ""
    qr_text: str = ""
    crs: str = ""
    date: str = ""


class _Pen:
    def __init__(self, painter, dpmm):
        self.p, self.k = painter, dpmm

    def r(self, x, y, w, h):
        return QRectF(x * self.k, y * self.k, w * self.k, h * self.k)

    def pt(self, x, y):
        return QPointF(x * self.k, y * self.k)

    def font(self, size, bold=False):
        f = QFont(font_family())
        f.setPointSizeF(size)
        f.setBold(bold)
        self.p.setFont(f)

    def text(self, x, y, w, h, s, size=9.0, bold=False, align="right", color=INK, rtl=True, wrap=True):
        self.font(size, bold)
        self.p.setPen(QPen(color))
        h_flag = {"right": Qt.AlignmentFlag.AlignRight, "left": Qt.AlignmentFlag.AlignLeft,
                  "center": Qt.AlignmentFlag.AlignHCenter}[align]
        opt = QTextOption(h_flag | Qt.AlignmentFlag.AlignVCenter)
        opt.setTextDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
        opt.setWrapMode(QTextOption.WrapMode.WordWrap if wrap else QTextOption.WrapMode.NoWrap)
        self.p.drawText(self.r(x, y, w, h), str(s), opt)

    def box(self, x, y, w, h, fill=None, color=GRID, width=0.25):
        self.p.setPen(QPen(color, width * self.k))
        self.p.setBrush(fill if fill is not None else Qt.BrushStyle.NoBrush)
        self.p.drawRect(self.r(x, y, w, h))
        self.p.setBrush(Qt.BrushStyle.NoBrush)

    def line(self, x1, y1, x2, y2, color=GRID, width=0.25):
        self.p.setPen(QPen(color, width * self.k))
        self.p.drawLine(self.pt(x1, y1), self.pt(x2, y2))


def _table(pen, x, y, w, header, rows, widths, row_h=6.0, size=8.0, rtl_cols=None):
    """Table laid out right to left: the first column is the rightmost. Returns the bottom y."""
    rtl_cols = rtl_cols or [True] * len(header)
    xs, cur = [], x + w
    for frac in widths:
        cw = w * frac
        cur -= cw
        xs.append((cur, cw))
    pen.box(x, y, w, row_h, fill=ACCENT, color=ACCENT)
    for (cx, cw), h in zip(xs, header):
        pen.text(cx + 1, y, cw - 2, row_h, h, size, True, "center", QColor("white"))
    y += row_h
    for i, row in enumerate(rows):
        lines = max(1, max(math.ceil(len(str(v)) * size * 0.19 / max(cw - 2, 1)) for v, (_, cw) in zip(row, xs)))
        rh = row_h * min(lines, 3)
        pen.box(x, y, w, rh, fill=LIGHT if i % 2 else None)
        for (cx, cw), v, rtl in zip(xs, row, rtl_cols):
            pen.text(cx + 1, y, cw - 2, rh, v, size, False, "right" if rtl else "center", rtl=rtl)
        for cx, _ in xs[:-1]:
            pen.line(cx, y, cx, y + rh)
        y += rh
    return y


def _sketch(pen, page, x, y, w, h):
    pen.box(x, y, w, h, color=INK, width=0.35)
    pen.text(x + 2, y + 1, w - 4, 6, "مخطط القطعة", 9, True, "right", ACCENT)
    pts = page.pts
    if len(pts) < 3:
        return
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    margin = 24.0
    s = min((w - 2 * margin) / max(maxx - minx, 1e-6), (h - 2 * margin - 6) / max(maxy - miny, 1e-6))
    cx, cy = x + w / 2, y + 3 + h / 2

    def mp(p):
        return (cx + (p[0] - (minx + maxx) / 2) * s, cy - (p[1] - (miny + maxy) / 2) * s)

    poly = QPolygonF([pen.pt(*mp(p)) for p in pts])
    pen.p.setPen(QPen(INK, 0.5 * pen.k))
    pen.p.setBrush(QColor("#f3f7fb"))
    pen.p.drawPolygon(poly)
    pen.p.setBrush(Qt.BrushStyle.NoBrush)
    # side lengths, along each edge and outside it (the ring is counter-clockwise: outside is to the right)
    for i in range(len(pts)):
        a, b = mp(pts[i]), mp(pts[(i + 1) % len(pts)])
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy)
        if ln < 11:                                      # too short to carry its label: the table has it
            continue
        real = math.hypot(pts[(i + 1) % len(pts)][0] - pts[i][0], pts[(i + 1) % len(pts)][1] - pts[i][1])
        nx, ny = -dy / ln, dx / ln                       # screen y is down: outside of a CCW ring
        mx, my = (a[0] + b[0]) / 2 + nx * 3.2, (a[1] + b[1]) / 2 + ny * 3.2
        ang = math.degrees(math.atan2(dy, dx))
        if ang > 90 or ang < -90:
            ang += 180
        pen.p.save()
        pen.p.translate(pen.pt(mx, my))
        pen.p.rotate(ang)
        pen.font(7.5, True)
        pen.p.setPen(QPen(ACCENT))
        opt = QTextOption(Qt.AlignmentFlag.AlignCenter)
        pen.p.drawText(QRectF(-15 * pen.k, -2.5 * pen.k, 30 * pen.k, 5 * pen.k), f"{real:.2f} م", opt)
        pen.p.restore()
    # corner numbers (from the corner table: nearest ring point)
    for n, X, Y in page.corners:
        px, py = mp((X, Y)) if (X, Y) in pts else mp(min(pts, key=lambda q: math.hypot(q[0] - X, q[1] - Y)))
        pen.p.setPen(QPen(INK, 0.3 * pen.k))
        pen.p.setBrush(QColor("white"))
        pen.p.drawEllipse(pen.pt(px, py), 1.0 * pen.k, 1.0 * pen.k)
        pen.p.setBrush(Qt.BrushStyle.NoBrush)
        ox, oy = px - cx, py - cy
        d = math.hypot(ox, oy) or 1
        pen.text(px + ox / d * 3.5 - 3, py + oy / d * 3.5 - 2, 6, 4, str(n), 7, True, "center", INK, rtl=False)
    # neighbours around the sketch
    lab = page.side_labels
    pen.text(x + 20, y + 7, w - 40, 8, lab.get("N", ""), 8, False, "center")
    pen.text(x + 20, y + h - 9, w - 40, 8, lab.get("S", ""), 8, False, "center")
    for key, lx in (("E", x + w - 22), ("W", x + 2)):
        pen.text(lx, cy - 15, 20, 30, lab.get(key, ""), 7, False, "center")
    # north arrow
    ax, ay = x + 10, y + 12
    arrow = QPolygonF([pen.pt(ax, ay), pen.pt(ax + 2.5, ay + 8), pen.pt(ax, ay + 6), pen.pt(ax - 2.5, ay + 8)])
    pen.p.setPen(QPen(INK, 0.3 * pen.k))
    pen.p.setBrush(INK)
    pen.p.drawPolygon(arrow)
    pen.p.setBrush(Qt.BrushStyle.NoBrush)
    pen.text(ax - 4, ay + 8.5, 8, 4, "ش N", 7, True, "center", rtl=False)
    # approximate scale (the sketch is fitted to the box)
    denom = 1000.0 / s
    nice = next((v for v in (50, 100, 200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 25000, 50000)
                 if v >= denom), round(denom, -3))
    pen.text(x + 2, y + h - 6, 45, 5, f"مقياس تقريبي ‎1:{nice:,.0f}", 7, False, "left")


def _qr_image(text):
    try:
        from .. import vendor
        vendor.ensure_vendor()
        import io

        import segno
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="png", scale=6, border=2)
        img = QImage()
        img.loadFromData(buf.getvalue())
        return None if img.isNull() else img
    except Exception:  # noqa: BLE001 - the QR code is optional
        return None


def draw_page(painter, page, dpmm, page_no=1, pages=1):
    pen = _Pen(painter, dpmm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    x0, w = MARGIN, A4_W - 2 * MARGIN
    # title block
    y = MARGIN
    pen.box(x0, y, w, 22, fill=LIGHT, color=ACCENT, width=0.4)
    pen.text(x0 + w - 70, y + 2, 68, 8, page.office or "المكتب الهندسي", 11, True, "right", ACCENT)
    pen.text(x0 + w - 70, y + 11, 68, 8, f"‎{page.date}‎", 8, False, "right")
    pen.text(x0 + 40, y + 3, w - 110, 10, page.title, 15, True, "center", INK)
    pen.text(x0 + 40, y + 13, w - 110, 7, page.crs, 7.5, False, "center", INK, rtl=False)
    qr = _qr_image(page.qr_text) if page.qr_text else None
    if qr is not None:
        painter.drawImage(pen.r(x0 + 2, y + 1, 20, 20), qr)
    y += 25
    # parcel data, two pairs per row
    info = list(page.info)
    rows = [info[i:i + 2] for i in range(0, len(info), 2)]
    half = w / 2
    for row in rows:
        for j, (label, value) in enumerate(row):
            bx = x0 + w - (j + 1) * half
            pen.box(bx, y, half, 6.5)
            pen.box(bx + half - 30, y, 30, 6.5, fill=LIGHT)
            pen.text(bx + half - 29, y, 28, 6.5, label, 8, True, "right")
            pen.text(bx + 1, y, half - 32, 6.5, value, 8.5, False, "right")
        y += 6.5
    y += 3
    # sketch: whatever height is left once the tables below have their room
    n_rows = len(page.corners)
    per_block = n_rows if n_rows <= 8 else math.ceil(n_rows / 2)
    coord_row = 5.5 if n_rows <= 16 else 4.6
    below = (6 + 6.0 * (1 + len(page.boundaries)) + 3) + (6 + coord_row * (1 + per_block) + 3) + 16 + 30
    sketch_h = max(45.0, min(100.0, A4_H - MARGIN - 6 - y - 3 - below))
    _sketch(pen, page, x0, y, w, sketch_h)
    y += sketch_h + 3
    # boundaries
    pen.text(x0, y, w, 6, "بيان الحدود والأطوال", 9.5, True, "right", ACCENT)
    y = _table(pen, x0, y + 6, w, ["الاتجاه", "الطول (م)", "الحد / المجاور"], page.boundaries,
               [0.14, 0.18, 0.68], 6.0, 8.5, [True, False, True]) + 3
    # corner coordinates, in one or two blocks
    pen.text(x0, y, w, 6, "جدول إحداثيات نقاط القطعة", 9.5, True, "right", ACCENT)
    y += 6
    rows = [(str(n), f"{X:,.3f}", f"{Y:,.3f}") for n, X, Y in page.corners]
    blocks = [rows] if len(rows) <= 8 else [rows[:math.ceil(len(rows) / 2)], rows[math.ceil(len(rows) / 2):]]
    bw = w if len(blocks) == 1 else (w - 4) / 2
    row_h = coord_row
    bottoms = []
    for i, b in enumerate(blocks):
        bx = x0 + w - (i + 1) * bw - (4 * i if i else 0)
        bottoms.append(_table(pen, bx, y, bw, ["النقطة", "الشرقي (E)", "الشمالي (N)"], b, [0.2, 0.4, 0.4],
                              row_h, 8.0, [False, False, False]))
    y = max(bottoms) + 3
    # area
    pen.box(x0, y, w, 13, fill=LIGHT, color=ACCENT, width=0.35)
    pen.text(x0 + 2, y + 0.5, w - 4, 6, f"المساحة: {page.area:,.2f} م²", 10, True, "right", INK)
    pen.text(x0 + 2, y + 6.5, w - 4, 6, page.area_words, 8.5, False, "right", INK)
    y += 16
    # signature (right), notes (middle), office stamp (left)
    foot_top = min(max(y, A4_H - MARGIN - 34), A4_H - MARGIN - 6 - 18)
    foot_h = A4_H - MARGIN - 6 - foot_top
    sig_w, stamp_w = 72.0, 48.0
    pen.box(x0 + w - sig_w, foot_top, sig_w, foot_h, color=GRID)
    pen.text(x0 + w - sig_w + 1, foot_top + 1, sig_w - 2, 6, "المهندس المساح: " + (page.engineer or ""), 8.5,
             True, "right")
    pen.text(x0 + w - sig_w + 1, foot_top + 9, sig_w - 2, 6, "التوقيع: ..............................", 8.5,
             False, "right")
    pen.text(x0 + w - sig_w + 1, foot_top + 17, sig_w - 2, 6, f"التاريخ: ‎{page.date}‎", 8.5, False,
             "right")
    notes_w = w - sig_w - stamp_w - 4
    pen.box(x0 + stamp_w + 2, foot_top, notes_w, foot_h, color=GRID)
    pen.text(x0 + stamp_w + 3, foot_top + 1, notes_w - 2, 5, "ملاحظات", 8, True, "right", ACCENT)
    if page.notes:
        pen.text(x0 + stamp_w + 3, foot_top + 6, notes_w - 2, foot_h - 7, page.notes, 7.5, False, "right")
    pen.box(x0, foot_top, stamp_w, foot_h, color=GRID)
    pen.text(x0, foot_top + 1, stamp_w, 5, "ختم المكتب", 8, True, "center", GRID)
    pen.line(x0, A4_H - MARGIN - 4, x0 + w, A4_H - MARGIN - 4)
    pen.text(x0, A4_H - MARGIN - 4, w / 2, 4, f"{page_no} / {pages}", 7, False, "left", GRID, rtl=False)
    pen.text(x0 + w / 2, A4_H - MARGIN - 4, w / 2, 4, "أُعدّ ببرنامج Biruni GIS", 7, False, "right", GRID)


def write_pdf(path, pages, title="", creator="Biruni GIS"):
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setResolution(300)
    writer.setTitle(title or (pages[0].title if pages else ""))
    writer.setCreator(creator)
    painter = QPainter(writer)
    try:
        dpmm = writer.width() / A4_W
        for i, page in enumerate(pages):
            if i:
                writer.newPage()
            draw_page(painter, page, dpmm, i + 1, len(pages))
    finally:
        painter.end()
    return path


def render_png(path, page, dpi=110):
    """The same page as a picture (previews and checks)."""
    dpmm = dpi / 25.4
    img = QImage(int(A4_W * dpmm), int(A4_H * dpmm), QImage.Format.Format_RGB32)
    img.setDotsPerMeterX(int(dpmm * 1000))
    img.setDotsPerMeterY(int(dpmm * 1000))
    img.fill(QColor("white"))
    painter = QPainter(img)
    try:
        draw_page(painter, page, dpmm)
    finally:
        painter.end()
    img.save(path)
    return path
