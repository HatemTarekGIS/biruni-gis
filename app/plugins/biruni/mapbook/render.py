"""The book as a print layout and a PDF: cover with the key map, contents, the map pages, the name index and a back
page - one layout (so every page can still be changed in the layout designer), one PDF whose bookmarks,
neighbour arrows, key-map cells, contents rows and index entries jump to their pages, and one file per page.

Built on the parts Biruni already has: the quick map sheet's texts, scale bar and north arrow, Map Studio's
legend, the coordinate grid, the name index tool, the living paper QR code and the seal.
"""
import datetime as dt
import os
import re

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression, QgsExpressionContext,
                       QgsExpressionContextUtils, QgsFeature, QgsFeatureRequest, QgsFillSymbol, QgsGeometry,
                       QgsLayoutExporter, QgsLayoutFrame, QgsLayoutItemAttributeTable, QgsLayoutItemLabel,
                       QgsLayoutItemMap, QgsLayoutItemMapGrid, QgsLayoutItemMapOverview, QgsLayoutItemPage,
                       QgsLayoutItemPicture, QgsLayoutItemShape, QgsLayoutMeasurement,
                       QgsLayoutPoint, QgsLayoutSize, QgsLayoutTableColumn, QgsLineSymbol, QgsPalLayerSettings,
                       QgsPrintLayout, QgsProject, QgsRectangle, QgsTextBufferSettings,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling, Qgis)
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QColor

from .. import i18n
from .. import mapsheet as MS
from . import book as B
from . import pages as P

ACCENT = "#1D4E89"
INK = "#1E252B"
MUTED = "#5B6670"
RULE = "#C9D0D6"
PANEL_BG = "#F7F9FA"
ARROWS = {"left": "←", "right": "→", "up": "↑", "down": "↓"}

# the book's own language (it may differ from the interface's)
_BOOK = {"strings": None, "lang": "en", "rtl": False, "ad": False, "font": "Segoe UI"}


def tr(text):
    s = _BOOK["strings"]
    return s.get(text, text) if s is not None else i18n.tr(text)


def _set_language(code):
    code = (code or i18n.lang() or "en").replace("-", "_")
    base = code.split("_")[0].lower()
    _BOOK["lang"] = base
    _BOOK["strings"] = {} if base == "en" else i18n.load_strings(code if code != base else _full_code(base))
    _BOOK["rtl"] = i18n.is_rtl(base)
    try:
        from ..map_studio.themes import font_pair

        ar, la = font_pair()
    except Exception:  # noqa: BLE001
        ar, la = "Arial", "Segoe UI"
    _BOOK["font"] = ar if _BOOK["rtl"] else la


def _full_code(base):
    return {"pt": "pt_BR"}.get(base, base)


def _d(text):
    """Digits of the book (Arabic-Indic in Arabic books when chosen)."""
    return MS.digits(text, _BOOK["ad"])


def _num(text):
    """Numbers with separators (1:2,500 - 30/10/2026) keep their order in right-to-left text: a left-to-right mark
    on both sides of every separator, so it binds to the digits around it."""
    if not _BOOK["ad"]:
        return str(text)
    lrm = "\u200e"
    return lrm + re.sub(r"([^0-9\u0660-\u0669])", lrm + r"\1" + lrm, MS.digits(text, True)) + lrm


# ---- small layout helpers ---------------------------------------------------------------------------------------
def _place(item, rect, page):
    item.attemptResize(QgsLayoutSize(rect[2], rect[3]))
    item.attemptMove(QgsLayoutPoint(rect[0], rect[1]), page=page)
    return item


def _fmt(size, bold=False, color=INK, family=None):
    return MS._text_format(size, bold, color, family or _BOOK["font"])


def _text(layout, text, rect, page, size=9.0, bold=False, color=INK, align=None, valign=None, bg=None,
          frame=None, item_id=""):
    lab = QgsLayoutItemLabel(layout)
    lab.setText(text)
    lab.setTextFormat(_fmt(size, bold, color))
    if align == "center":
        lab.setHAlign(Qt.AlignmentFlag.AlignHCenter)
    elif align == "end" or (align is None and _BOOK["rtl"]):
        lab.setHAlign(Qt.AlignmentFlag.AlignRight)
    elif align == "start" and _BOOK["rtl"]:
        lab.setHAlign(Qt.AlignmentFlag.AlignRight)
    elif align == "right":
        lab.setHAlign(Qt.AlignmentFlag.AlignRight)
    else:
        lab.setHAlign(Qt.AlignmentFlag.AlignLeft)
    lab.setVAlign(valign or Qt.AlignmentFlag.AlignVCenter)
    lab.setMarginX(0.6 if bg else 0)
    lab.setMarginY(0)
    if bg:
        lab.setBackgroundEnabled(True)
        lab.setBackgroundColor(QColor(bg))
    if frame:
        lab.setFrameEnabled(True)
        lab.setFrameStrokeColor(QColor(frame))
        lab.setFrameStrokeWidth(QgsLayoutMeasurement(0.25))
    layout.addLayoutItem(lab)
    if item_id:
        lab.setId(item_id)
    return _place(lab, rect, page)


def _rect(layout, rect, page, fill="#FFFFFF", stroke=INK, width=0.35, item_id=""):
    shape = QgsLayoutItemShape(layout)
    shape.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
    props = {"color": fill, "outline_color": stroke, "outline_width": str(width), "outline_width_unit": "MM"}
    if stroke is None:
        props["outline_style"] = "no"
    shape.setSymbol(QgsFillSymbol.createSimple(props))
    layout.addLayoutItem(shape)
    if item_id:
        shape.setId(item_id)
    return _place(shape, rect, page)


def _hrule(layout, x, y, w, page, color=RULE):
    return _rect(layout, (x, y, w, 0.25), page, fill=color, stroke=None)


def _mirror(rect, box):
    """``rect`` flipped left-right inside ``box`` (right-to-left books)."""
    if not _BOOK["rtl"]:
        return rect
    x, y, w, h = rect
    return (box[0] + box[0] + box[2] - x - w, y, w, h)


# ---- the content of the book ------------------------------------------------------------------------------------
def map_layers(project):
    """What the map pages draw: the visible layers, without the book's own preview."""
    from ..map_studio.auto_layout import drawn_layers

    return [l for l in drawn_layers(project) if not l.customProperty(B.PREVIEW_PROP)
            and not l.customProperty("biruni/mapbook/internal")]


def _key_layer(page_list, crs, project, labels=True, size=9.0):
    lyr = B.pages_layer(page_list, crs, "Biruni map book key", core=True, include_excluded=False)
    lyr.setCustomProperty("biruni/mapbook/internal", True)
    lyr.renderer().setSymbol(QgsFillSymbol.createSimple({
        "color": "29,78,137,18" if labels else "255,255,255,0", "outline_color": ACCENT if labels else "#7D8A96",
        "outline_width": "0.35" if labels else "0.18"}))
    if labels:
        pal = QgsPalLayerSettings()
        pal.fieldName = "name"
        pal.placement = Qgis.LabelPlacement.OverPoint
        fmt = _fmt(size, True, ACCENT)
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.9)
        buf.setColor(QColor("white"))
        fmt.setBuffer(buf)
        pal.setFormat(fmt)
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        lyr.setLabelsEnabled(True)
    project.addMapLayer(lyr, False)
    return lyr


def page_title(book, page, total, project, layer=None):
    """The page's title from the book's title text: {name} {number} {total} {title} and [% expressions %] on the
    feature the page was made from."""
    template = book.get("page_title") or ""
    title = book.get("title") or ""
    if not template:
        return title
    text = (template.replace("{name}", page.name).replace("{number}", _d(page.number or ""))
            .replace("{total}", _d(total)).replace("{title}", title))
    if "[%" in text:
        ctx = QgsExpressionContext()
        scopes = (QgsExpressionContextUtils.globalProjectLayerScopes(layer) if layer is not None else
                  [QgsExpressionContextUtils.globalScope(), QgsExpressionContextUtils.projectScope(project)])
        ctx.appendScopes(scopes)
        if layer is not None and page.key.lstrip("-").isdigit():
            f = layer.getFeature(int(page.key))
            if f.isValid():
                ctx.setFeature(f)
        text = QgsExpression.replaceExpressionText(text, ctx)
    return text


def page_features(layer, page, crs, fields, limit=50, project=None):
    """Rows of the features of ``layer`` on the page (inside its frame), with the chosen fields."""
    poly = page.polygon()
    xf = QgsCoordinateTransform(crs, layer.crs(), project or QgsProject.instance())
    poly.transform(xf)
    req = QgsFeatureRequest().setFilterRect(poly.boundingBox())
    engine = QgsGeometry.createGeometryEngine(poly.constGet())
    engine.prepareGeometry()
    rows = []
    for f in layer.getFeatures(req):
        g = f.geometry()
        if g.isNull() or not engine.intersects(g.constGet()):
            continue
        rows.append([f[n] for n in fields])
        if len(rows) >= limit:
            break
    return rows


def _filter_expression(layer, page, crs, project):
    poly = page.polygon()
    poly.transform(QgsCoordinateTransform(crs, layer.crs(), project))
    return f"intersects($geometry, geom_from_wkt('{poly.asWkt(3)}'))"


def name_index(book, page_list, crs, project):
    """The name index of the book (the map book index tool, run on the book's pages): ([entries], squares?).
    entries: {"name", "pages", "squares", "letter"}; squares: grid squares are given (the pages are not turned)."""
    layer = B.book_layer(book, project, "index_layer")
    field = book.get("index_field") or ""
    if not isinstance(layer, QgsVectorLayer) or layer.fields().indexOf(field) < 0:
        return [], False
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProcessingUtils

    from ..algorithms.map_index import MapIndex

    auth = crs.authid() or crs.toWkt()
    geom_kind = {Qgis.GeometryType.Point: "MultiPoint", Qgis.GeometryType.Line: "MultiLineString"}.get(
        layer.geometryType(), "MultiPolygon")
    names = QgsVectorLayer(f"{geom_kind}?crs={auth}&field=name:string", "names", "memory")
    xf = QgsCoordinateTransform(layer.crs(), crs, project)
    feats = []
    for f in layer.getFeatures():
        g = QgsGeometry(f.geometry())
        if g.isNull() or g.isEmpty() or f[field] is None:
            continue
        g.transform(xf)
        g.convertToMultiType()
        nf = QgsFeature(names.fields())
        nf.setGeometry(g)
        nf.setAttributes([str(f[field])])
        feats.append(nf)
    names.dataProvider().addFeatures(feats)
    live = [p for p in page_list if not p.excluded]
    turned = any(abs(p.angle) > 1e-6 for p in live)
    plyr = QgsVectorLayer(f"Polygon?crs={auth}&field=pname:string", "pages", "memory")
    pf = []
    for p in live:
        f = QgsFeature(plyr.fields())
        f.setGeometry(p.polygon())
        f.setAttributes([p.name])
        pf.append(f)
    plyr.dataProvider().addFeatures(pf)
    alg = MapIndex()
    alg.initAlgorithm({})
    ctx = QgsProcessingContext()
    ctx.setProject(project)
    params = {"INPUT": names, "FIELD": "name", "PAGES": plyr, "PAGE_FIELD": "pname", "USE_GRID": not turned,
              "COLUMNS": 0, "ROWS": 0, "STRIP": True, "OUTPUT": "memory:"}
    res, ok = alg.run(params, ctx, QgsProcessingFeedback())
    if not ok or not res:
        return [], False
    out = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx)
    entries = []
    for f in sorted(out.getFeatures(), key=lambda f: f["sort_order"]):
        entries.append({"name": f["name"], "pages": f["pages"] or "", "squares": f["squares"] or "",
                        "letter": f["letter"] or ""})
    return entries, not turned


# ---- the book layout --------------------------------------------------------------------------------------------
class BookLayout:
    """Builds the layout of a book and knows where every link goes."""

    def __init__(self, book, page_list, project=None):
        self.book = book
        self.project = project or QgsProject.instance()
        self.all_pages = page_list
        self.pages = [p for p in page_list if not p.excluded and p.number is not None]
        self.crs = B.book_crs(book, self.project)
        self.layers = map_layers(self.project)
        if book.get("source") == "index":  # the sheet polygons are the pages: drawn on the key maps, not over the data
            sheets = B.book_layer(book, self.project, "layer")
            self.layers = [l for l in self.layers if sheets is None or l.id() != sheets.id()]
        self.links = []        # (layout page, (x, y, w, h), target layout page)
        self.outline = []
        self.sizes = []        # layout page sizes (mm)
        self.layout = None
        self.map_items = {}    # book page number -> layout map
        self.legends = {}
        self.tables = {}
        self.qr_urls = {}
        self.kinds = []        # what each layout page is: cover / contents / map / index / back
        self.index_entries = []

    # -- the frame of the whole book
    def build(self, name=None):
        book = self.book
        _set_language(book.get("lang"))
        ad = book.get("arabic_digits")
        _BOOK["ad"] = (_BOOK["lang"] == "ar") if ad is None else bool(ad)
        if not self.pages:
            raise ValueError("no pages")
        layout = QgsPrintLayout(self.project)
        layout.initializeDefaults()
        layout.setName(name or tr("Map book"))
        self.layout = layout
        paper = book.get("paper", "A4")
        land = sum(1 for p in self.pages if p.landscape) * 2 >= len(self.pages)
        self.book_size = P.paper_size(paper, land)
        self.key_layer = _key_layer(self.pages, self.crs, self.project, True, 9.0)
        self.loc_layer = _key_layer(self.pages, self.crs, self.project, False)
        for lyr in (self.key_layer, self.loc_layer):  # removed with the layout when the book is made again
            lyr.setCustomProperty("biruni/mapbook/layout", layout.name())

        index_entries, self.squares = (name_index(book, self.all_pages, self.crs, self.project)
                                       if B.book_layer(book, self.project, "index_layer") is not None else ([], False))
        self.index_entries = index_entries
        # the plan of the layout pages
        plan = []
        if book.get("cover", True):
            plan.append(("cover", None))
        contents_rows = self._contents_rows()
        if book.get("contents", True):
            per = self._contents_per_page()
            for k in range(0, max(1, len(contents_rows)), per):
                plan.append(("contents", contents_rows[k:k + per]))
        first_map = len(plan)
        for p in self.pages:
            plan.append(("map", p))
        index_chunks = self._index_chunks(index_entries) if index_entries else []
        first_index = len(plan)
        for chunk in index_chunks:
            plan.append(("index", chunk))
        if book.get("back"):
            plan.append(("back", None))
        self.plan = plan
        self.page_of = {p.number: first_map + k for k, p in enumerate(self.pages)}
        self.first_index = first_index if index_chunks else None
        pc = layout.pageCollection()
        for k, (kind, _x) in enumerate(plan):
            size = P.paper_size(paper, _x.landscape) if kind == "map" else self.book_size
            page = pc.page(0) if k == 0 else QgsLayoutItemPage(layout)
            page.setPageSize(QgsLayoutSize(size[0], size[1]))
            if k > 0:
                pc.addPage(page)
            self.sizes.append(size)
            self.kinds.append(kind)
        n_contents = 0
        for k, (kind, data) in enumerate(plan):
            if kind == "cover":
                self._cover(k)
            elif kind == "contents":
                self._contents(k, data, n_contents)
                n_contents += 1
            elif kind == "map":
                self._map_page(k, data)
            elif kind == "index":
                self._index_page(k, data, k - first_index)
            elif kind == "back":
                self._back(k)
        self._outline(first_map)
        for key, value in (("book", book.get("name") or ""), ("pages", str(len(self.pages)))):
            layout.setCustomProperty("biruni/mapbook/" + key, value)
        return layout

    def _outline(self, first_map):
        out = []
        kinds = self.kinds
        if "cover" in kinds:
            out.append((tr("Cover and key map"), kinds.index("cover"), []))
        if "contents" in kinds:
            out.append((tr("Contents"), kinds.index("contents"), []))
        children = []
        for p in self.pages:
            label = tr("Page {n}").format(n=_d(p.number))
            if p.name != str(p.number):
                label += f" · {p.name}"
            if p.title and p.title != self.book.get("title"):
                label += f" - {p.title}"
            children.append((label, self.page_of[p.number], []))
        out.append((tr("Map pages"), first_map, children))
        if "index" in kinds:
            out.append((tr("Index of names"), kinds.index("index"), []))
        if "back" in kinds:
            out.append((tr("Notes"), kinds.index("back"), []))
        self.outline = out

    # -- common page furniture
    def _frame_and_heading(self, k, heading):
        """Neat line, the book's title band and a heading; returns the content box (x, y, w, h)."""
        pw, ph = self.sizes[k]
        m = P.MARGIN_MM
        _rect(self.layout, (m, m, pw - 2 * m, ph - 2 * m), k, width=0.5)
        _rect(self.layout, (m, m, pw - 2 * m, 11.0), k, fill=ACCENT, stroke=None)
        _text(self.layout, self.book.get("title") or tr("Map book"), (m + 4, m, pw - 2 * m - 8, 11.0), k, 11, True,
              "#FFFFFF")
        _text(self.layout, heading, (m + 4, m + 15, pw - 2 * m - 8, 10), k, 16, True, ACCENT)
        _hrule(self.layout, m + 4, m + 26, pw - 2 * m - 8, k)
        foot = tr("Page {n} of {total}").format(n=_d(k + 1), total=_d(len(self.plan)))
        _text(self.layout, foot, (m + 4, ph - m - 7, pw - 2 * m - 8, 5), k, 7.5, False, MUTED, align="center")
        return (m + 4, m + 29, pw - 2 * m - 8, ph - 2 * m - 29 - 9)

    # -- cover with the key map
    def _cover(self, k):
        L = self.layout
        pw, ph = self.sizes[k]
        m = P.MARGIN_MM
        book = self.book
        _rect(L, (m, m, pw - 2 * m, ph - 2 * m), k, width=0.6)
        band_h = 30.0
        _rect(L, (m, m, pw - 2 * m, band_h), k, fill=ACCENT, stroke=None)
        _text(L, book.get("title") or tr("Map book"), (m + 6, m + 3, pw - 2 * m - 12, 15), k, 22, True, "#FFFFFF")
        sub = book.get("subtitle") or tr("Map book of {n} pages").format(n=_d(len(self.pages)))
        _text(L, sub, (m + 6, m + 18, pw - 2 * m - 12, 9), k, 11, False, "#DCE6F2")
        # facts line at the bottom
        scales = sorted({int(round(p.scale)) for p in self.pages})
        scale_txt = (tr("Scale {s}").format(s=_num(f"1:{scales[0]:,}")) if len(scales) == 1
                     else tr("Scales {a} to {b}").format(a=_num(f"1:{scales[0]:,}"), b=_num(f"1:{scales[-1]:,}")))
        facts = [tr("{n} map pages").format(n=_d(len(self.pages))), scale_txt, self.book.get("paper", "A4"),
                 _num(dt.date.today().strftime("%d/%m/%Y")) if _BOOK["ad"] else dt.date.today().isoformat()]
        if book.get("author"):
            facts.append(tr("Prepared by: {a}").format(a=book["author"]))
        foot_h = 16.0
        _hrule(L, m + 6, ph - m - foot_h, pw - 2 * m - 12, k)
        _text(L, "  ·  ".join(facts), (m + 6, ph - m - foot_h + 2, pw - 2 * m - 12, 6), k, 9, False, INK)
        _text(L, tr("Made with Biruni GIS") + "  ·  " + self.crs.description(),
              (m + 6, ph - m - foot_h + 8.5, pw - 2 * m - 12, 5), k, 7, False, MUTED)
        # the key map
        top = m + band_h + 9
        _text(L, tr("Key map - click a page to open it"), (m + 6, top - 7, pw - 2 * m - 12, 6), k, 9, True, ACCENT)
        box = (m + 6, top, pw - 2 * m - 12, ph - m - foot_h - 3 - top)
        mp = self._key_map(k, box, self.key_layer, self.layers, margin=1.04)
        mp.setId("mapbook-key-map")
        ext = mp.extent()
        for p in self.pages:
            bb = p.core_geometry().boundingBox()
            if abs(p.angle) > 1e-6:
                c = p.core_geometry().pointOnSurface().asPoint()
                bb = QgsRectangle(c.x() - bb.width() * 0.2, c.y() - bb.height() * 0.2, c.x() + bb.width() * 0.2,
                                  c.y() + bb.height() * 0.2)
            r = self._ground_to_mm(bb, ext, box)
            if r is not None:
                self.links.append((k, r, self.page_of[p.number]))
        self._north(k, (box[0] + box[2] - 13, box[1] + 2, 10, 13), mp)

    def _key_map(self, k, box, layer, context, margin=1.05, item_id=""):
        mp = QgsLayoutItemMap(self.layout)
        self.layout.addLayoutItem(mp)
        _place(mp, box, k)
        mp.setCrs(self.crs)
        mp.setLayers([layer] + list(context))
        mp.setKeepLayerSet(True)
        mp.setFrameEnabled(True)
        mp.setFrameStrokeWidth(QgsLayoutMeasurement(0.3))
        ext = QgsRectangle()
        for p in self.pages:
            bb = p.polygon().boundingBox()
            if ext.isEmpty():
                ext = QgsRectangle(bb)
            else:
                ext.combineExtentWith(bb)
        ext.scale(margin)
        mp.zoomToExtent(ext)
        if item_id:
            mp.setId(item_id)
        return mp

    @staticmethod
    def _ground_to_mm(bb, ext, box):
        if ext.width() <= 0 or ext.height() <= 0:
            return None
        x0 = box[0] + (bb.xMinimum() - ext.xMinimum()) / ext.width() * box[2]
        x1 = box[0] + (bb.xMaximum() - ext.xMinimum()) / ext.width() * box[2]
        y0 = box[1] + (ext.yMaximum() - bb.yMaximum()) / ext.height() * box[3]
        y1 = box[1] + (ext.yMaximum() - bb.yMinimum()) / ext.height() * box[3]
        x0, x1 = max(box[0], x0), min(box[0] + box[2], x1)
        y0, y1 = max(box[1], y0), min(box[1] + box[3], y1)
        if x1 - x0 < 0.5 or y1 - y0 < 0.5:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    # -- contents
    def _contents_rows(self):
        rows = [(p, self.page_of_number(p)) for p in self.pages]
        return rows

    def page_of_number(self, p):
        return p.number

    def _contents_per_page(self):
        _pw, ph = self.book_size
        usable = ph - 2 * P.MARGIN_MM - 29 - 9 - 8
        return max(5, int(usable // 6.4))

    def _contents(self, k, rows, n):
        L = self.layout
        box = self._frame_and_heading(k, tr("Contents") if n == 0 else tr("Contents (continued)"))
        x, y, w, _h = box
        book_title = self.book.get("title") or ""
        named = any(p.name != str(p.number) for p in self.pages)
        titled = any((p.title or book_title) != book_title for p in self.pages)
        heads, ratios = [tr("Page")], [0.12]
        if named:
            heads.append(tr("Name"))
            ratios.append(0.2 if titled else 0.5)
        if titled:
            heads.append(tr("Title"))
            ratios.append(0.5 if named else 0.7)
        heads.append(tr("Scale"))
        ratios.append(1.0 - sum(ratios))
        cols = self._cols(box, ratios)
        for (cx, cw), head in zip(cols, heads):
            _text(L, head, (cx, y + 1, cw, 5), k, 8, True, MUTED)
        y += 7
        for p, _num_page in rows:
            vals = [_d(p.number)] + ([p.name] if named else []) + ([p.title or book_title] if titled else []) \
                + [_num(f"1:{int(round(p.scale)):,}")]
            if (p.number or 0) % 2 == 0:
                _rect(L, (x, y - 0.4, w, 6.2), k, fill=PANEL_BG, stroke=None)
            for (cx, cw), v in zip(cols, vals):
                _text(L, v, (cx, y, cw, 5.4), k, 9, cx == cols[0][0], INK)
            self.links.append((k, (x, y - 0.4, w, 6.2), self.page_of[p.number]))
            y += 6.4
        if self.first_index is not None and rows and rows[-1][0] is self.pages[-1]:
            y += 3
            _text(L, tr("Index of names") + "  →", (x, y, w, 6), k, 9, True, ACCENT)
            self.links.append((k, (x, y, w, 6), self.first_index))

    def _cols(self, box, ratios):
        x, _y, w, _h = box
        out, cx = [], x
        for r in ratios:
            out.append((cx, w * r - 1.5))
            cx += w * r
        if _BOOK["rtl"]:
            out = [(x + x + w - c - cw, cw) for c, cw in out]
        return out

    # -- the map pages
    def _map_page(self, k, p):
        L, book = self.layout, self.book
        rtl = _BOOK["rtl"]
        grid = bool(book.get("grid", True))
        parts = P.sheet_parts(book.get("paper", "A4"), p.landscape, grid, rtl)
        neat, header, frame, panel = parts["neat"], parts["header"], parts["frame"], parts["panel"]
        _rect(L, neat, k, width=0.5)
        # header: the page badge (back to the key map), the title, page n of N
        badge_text = p.name if len(p.name) <= 5 else _d(p.number)
        bw = max(16.0, 5.5 + 3.6 * len(badge_text))
        badge = _mirror((header[0], header[1], bw, header[3]), header)
        _rect(L, badge, k, fill=ACCENT, stroke=None)
        _text(L, badge_text, badge, k, 15, True, "#FFFFFF", align="center", item_id=f"mapbook-badge-{p.number}")
        if "cover" in self.kinds:
            self.links.append((k, badge, self.kinds.index("cover")))
        total = len(self.pages)
        title = p.title or book.get("title") or ""
        if p.name and badge_text != p.name and p.name not in title:
            title = f"{title} - {p.name}" if title else p.name
        tw = header[2] - bw - 62
        _text(L, title, _mirror((header[0] + bw + 4, header[1], tw, header[3] - 4.5), header), k, 13, True, INK)
        sub = book.get("subtitle") or ""
        if sub:
            _text(L, sub, _mirror((header[0] + bw + 4, header[1] + header[3] - 5, tw, 4.5), header), k, 7.5,
                  False, MUTED)
        side = _mirror((header[0] + header[2] - 61, header[1], 58, header[3]), header)
        _text(L, tr("Page {n} of {total}").format(n=_d(p.number), total=_d(total)),
              (side[0], side[1] + 1, side[2], 6), k, 10, True, ACCENT, align="left" if rtl else "right")
        _text(L, tr("Scale {s}").format(s=_num(f"1:{int(round(p.scale)):,}")),
              (side[0], side[1] + 7, side[2], 5), k, 8, False, MUTED, align="left" if rtl else "right")
        _hrule(L, neat[0], header[1] + header[3] + 0.6, neat[2], k, ACCENT)

        # the map
        mp = QgsLayoutItemMap(L)
        L.addLayoutItem(mp)
        _place(mp, frame, k)
        mp.setId(f"mapbook-map-{p.number}")
        mp.setCrs(self.crs)
        layers = self.layers
        if p.theme and self.project.mapThemeCollection().hasMapTheme(p.theme):
            mp.setFollowVisibilityPreset(True)
            mp.setFollowVisibilityPresetName(p.theme)
        else:
            mp.setLayers(layers)
            mp.setKeepLayerSet(True)
        mp.setMapRotation(p.angle)
        mp.setExtent(p.unrotated_extent())
        mp.setScale(p.scale)
        mp.setFrameEnabled(True)
        mp.setFrameStrokeWidth(QgsLayoutMeasurement(0.4))
        self.map_items[p.number] = mp
        if grid:
            from .. import map_grid

            style = "geographic" if self.crs.isGeographic() else "projected"
            try:
                map_grid.add_grid(mp, style, _BOOK["lang"], size=5.5)
            except Exception:  # noqa: BLE001 - the page is still useful without its grid labels
                pass
        if self.squares and abs(p.angle) < 1e-6:
            self._squares(k, mp, frame, p)
        if book.get("neighbours", True):
            self._neighbour_tabs(k, frame, p)
        self._panel(k, p, mp, panel, frame)

    def _squares(self, k, mp, frame, p):
        """The name index's grid squares on the page: light lines and the letters / numbers inside the frame."""
        from ..algorithms.map_index import auto_grid, column_letters

        ext = p.unrotated_extent()
        cols, rows = auto_grid(ext, 6)
        g = QgsLayoutItemMapGrid("mapbook-squares", mp)
        mp.grids().addGrid(g)
        g.setUnits(QgsLayoutItemMapGrid.GridUnit.MapUnit)
        g.setIntervalX(ext.width() / cols)
        g.setIntervalY(ext.height() / rows)
        g.setOffsetX(ext.xMinimum())
        g.setOffsetY(ext.yMinimum())
        g.setStyle(QgsLayoutItemMapGrid.GridStyle.Solid)
        g.setLineSymbol(QgsLineSymbol.createSimple({"color": "29,78,137,90", "width": "0.15", "line_style": "dash"}))
        g.setAnnotationEnabled(False)
        g.setFrameStyle(QgsLayoutItemMapGrid.FrameStyle.NoFrame)
        cw, ch = frame[2] / cols, frame[3] / rows
        for c in range(cols):
            _text(self.layout, column_letters(c), (frame[0] + c * cw + cw / 2 - 2.5, frame[1] + 0.6, 5, 3.6), k, 7,
                  True, ACCENT, align="center", bg="#CCFFFFFF")
        for r in range(rows):
            _text(self.layout, str(r + 1), (frame[0] + 0.6, frame[1] + r * ch + ch / 2 - 1.8, 5, 3.6), k, 7, True,
                  ACCENT, align="center", bg="#CCFFFFFF")

    def _neighbour_tabs(self, k, frame, p):
        x, y, w, h = frame
        for d in P.SIDES:
            j = p.neighbours.get(d)
            if j is None or j >= len(self.all_pages):
                continue
            nb = self.all_pages[j]
            if nb.excluded or nb.number is None:
                continue
            label = nb.name if nb.name == str(nb.number) else f"{nb.name} ({_d(nb.number)})"
            label = _d(label) if nb.name == str(nb.number) else label
            text = f"{ARROWS[d]} {label}" if d in ("left", "up", "down") else f"{label} {ARROWS[d]}"
            tw, th = 6.0 + 1.9 * len(text), 6.0
            if d == "up":
                r = (x + w / 2 - tw / 2, y + 0.8, tw, th)
            elif d == "down":
                r = (x + w / 2 - tw / 2, y + h - th - 0.8, tw, th)
            elif d == "left":
                r = (x + 0.8, y + h / 2 - th / 2, tw, th)
            else:
                r = (x + w - tw - 0.8, y + h / 2 - th / 2, tw, th)
            _text(self.layout, text, r, k, 9, True, ACCENT, align="center", bg="#FFFFFF", frame=ACCENT,
                  item_id=f"mapbook-next-{p.number}-{d}")
            self.links.append((k, r, self.page_of[nb.number]))

    def _north(self, k, rect, mp):
        arrow = QgsLayoutItemPicture(self.layout)
        svg = MS._north_arrow_svg()
        if svg:
            arrow.setPicturePath(svg)
        arrow.setLinkedMap(mp)
        self.layout.addLayoutItem(arrow)
        return _place(arrow, rect, k)

    def _panel(self, k, p, mp, panel, frame):
        L, book = self.layout, self.book
        x, y, w, h = panel
        _rect(L, panel, k, fill=PANEL_BG, stroke=RULE, width=0.25)
        x, y, w, h = x + 3, y + 3, w - 6, h - 6
        if p.landscape:
            boxes = {"locator": (x, y, w, w * 0.62 + 5), "north": (x, y + w * 0.62 + 7, w, 19)}
            rest_y = boxes["north"][1] + boxes["north"][3] + 2
            boxes["bottom"] = (x, y + h - 30, w, 30)
            boxes["legend"] = (x, rest_y, w, boxes["bottom"][1] - rest_y - 2)
        else:
            cw = min(h * 1.25, w * 0.32)
            rw = 54.0
            boxes = {"locator": (x, y, cw, h), "north": (x + w - rw, y, rw, 19),
                     "bottom": (x + w - rw, y + h - 30, rw, 30),
                     "legend": (x + cw + 4, y, w - cw - rw - 8, h)}
            if _BOOK["rtl"]:
                boxes = {key: _mirror(b, (x, y, w, h)) for key, b in boxes.items()}
        # key map with this page shown
        if book.get("locator", True):
            lx, ly, lw, lh = boxes["locator"]
            _text(L, tr("Key map"), (lx, ly, lw, 4), k, 7, True, MUTED)
            loc = self._key_map(k, (lx, ly + 5, lw, lh - 5), self.loc_layer, self.layers, 1.08,
                                f"mapbook-locator-{p.number}")
            ov = QgsLayoutItemMapOverview("this page", loc)
            loc.overviews().addOverview(ov)
            ov.setLinkedMap(mp)
            ov.setFrameSymbol(QgsFillSymbol.createSimple({"color": "214,69,69,110", "outline_color": "#C62828",
                                                          "outline_width": "0.45"}))
        # north arrow and scale bar
        nx, ny, nw, nh = boxes["north"]
        self._north(k, _mirror((nx, ny, 11, 14), boxes["north"]), mp)
        from ..map_studio.auto_layout import _scale_units

        units = _scale_units(p.scale, "")
        bar = MS._scale_bar(L, mp, (units[0], tr(units[1])), nx + 14 if not _BOOK["rtl"] else nx, ny + 1,
                            nw - 15, f"mapbook-scalebar-{p.number}")
        bar.attemptMove(QgsLayoutPoint(nx + 14 if not _BOOK["rtl"] else nx, ny + 1), page=k)
        bar.setTextFormat(_fmt(6.5))
        note = tr("Scale {s}").format(s=_num(f"1:{int(round(p.scale)):,}"))
        if abs(p.angle) > 0.05:
            note += "  ·  " + tr("turned {deg}°").format(deg=_d(int(round(P._norm(p.angle)))))
        _text(L, note, _mirror((nx + 14, ny + 12, nw - 14, 5), boxes["north"]), k, 7, False, MUTED)
        # legend (only what this page shows) and the features table
        lx, ly, lw, lh = boxes["legend"]
        legend_box = (lx, ly, lw, lh)
        tlayer = B.book_layer(book, self.project, "table_layer")
        if isinstance(tlayer, QgsVectorLayer):
            rows_max = int(book.get("table_rows", 12) or 12)
            want = min(rows_max, len(page_features(tlayer, p, self.crs, [], rows_max, self.project)))
            if want and p.landscape:  # the table under the legend
                table_h = min(lh * 0.6 if book.get("legend", True) else lh, 4.4 * (want + 1) + 7)
                self._table(k, tlayer, p, (lx, ly + lh - table_h, lw, table_h))
                legend_box = (lx, ly, lw, lh - table_h - 2)
            elif want:  # portrait: legend and table side by side
                tw = lw * 0.55 if book.get("legend", True) else lw
                table_box = _mirror((lx + lw - tw, ly, tw, lh), boxes["legend"])
                self._table(k, tlayer, p, table_box)
                legend_box = _mirror((lx, ly, lw - tw - 3, lh), boxes["legend"])
        if book.get("legend", True):
            self._legend(k, mp, legend_box, p)
        # QR code and details
        bx, by, bw, bh = boxes["bottom"]
        qr_w = 0.0
        if book.get("qr", True):
            qr_w = self._qr(k, p, mp, _mirror((bx, by, 24, 28), boxes["bottom"]))
        details = [self.crs.authid() or self.crs.description(),
                   _num(dt.date.today().strftime("%d/%m/%Y")) if _BOOK["ad"] else dt.date.today().isoformat()]
        if book.get("author"):
            details.append(tr("Prepared by: {a}").format(a=book["author"]))
        details.append(tr("Made with Biruni GIS"))
        _text(L, "\n".join(details), _mirror((bx + qr_w + 4, by + 2, bw - qr_w - 4, bh - 2), boxes["bottom"]), k,
              6.5, False, MUTED, valign=Qt.AlignmentFlag.AlignBottom)

    def _legend(self, k, mp, box, p):
        from ..cartography import fit_legend
        from ..map_studio import intents
        from ..map_studio.auto_layout import _legend

        x, y, w, h = box
        if h < 12:
            return None
        _text(self.layout, tr("Legend"), (x, y, w, 4.5), k, 8, True, INK)
        leg = _legend(self.layout, mp, self.layers, intents.get("atlas"), _BOOK["rtl"], _BOOK["font"])
        if leg is None:
            return None
        leg.setId(f"mapbook-legend-{p.number}")
        leg.setBackgroundEnabled(False)
        leg.setFrameEnabled(False)
        leg.setResizeToContents(False)
        _place(leg, (x, y + 5, w, h - 5), k)
        try:
            fit_legend(leg, w, h - 5)
        except Exception:  # noqa: BLE001
            pass
        _place(leg, (x, y + 5, w, h - 5), k)
        self.legends[p.number] = leg
        return leg

    def _table(self, k, layer, p, box):
        x, y, w, h = box
        fields = [f for f in self.book.get("table_fields") or [] if layer.fields().indexOf(f) >= 0]
        if not fields:
            fields = [f.name() for f in layer.fields()][:3]
        _text(self.layout, tr("Features on this page"), (x, y, w, 4.5), k, 8, True, INK)
        table = QgsLayoutItemAttributeTable.create(self.layout)
        table.setVectorLayer(layer)
        cols = []
        for name in (reversed(fields) if _BOOK["rtl"] else fields):
            c = QgsLayoutTableColumn(layer.attributeDisplayName(layer.fields().indexOf(name)))
            c.setAttribute(name)
            if _BOOK["rtl"]:
                c.setHAlignment(Qt.AlignmentFlag.AlignRight)
            cols.append(c)
        table.setColumns(cols)
        table.setFilterFeatures(True)
        table.setFeatureFilter(_filter_expression(layer, p, self.crs, self.project))
        table.setMaximumNumberOfFeatures(int(self.book.get("table_rows", 12) or 12))
        table.setContentTextFormat(_fmt(6.5))
        table.setHeaderTextFormat(_fmt(6.5, True))
        table.setCellMargin(0.7)
        table.setGridStrokeWidth(0.12)
        table.setGridColor(QColor(RULE))
        self.layout.addMultiFrame(table)
        fr = QgsLayoutFrame(self.layout, table)
        _place(fr, (x, y + 5, w, h - 5), k)
        table.addFrame(fr)
        table.refreshAttributes()
        self.tables[p.number] = table
        return table

    def _qr(self, k, p, mp, rect):
        """The living paper QR code for this page: its area (four corners, turned pages keep their shape),
        title and scale, opened on a phone."""
        import base64

        from ..living_paper import codec
        from ..living_paper.layout import qr_code, qr_svg

        xf = QgsCoordinateTransform(self.crs, QgsCoordinateReferenceSystem("EPSG:4326"), self.project)
        try:
            corners = [(q.x(), q.y()) for q in (xf.transform(c) for c in p.corners())]
            title = p.title or self.book.get("title") or ""
            if p.name != str(p.number):
                title = f"{title} - {p.name}" if title else p.name
            url, _report = codec.encode(title, corners, f"1:{int(round(p.scale)):,}", lang=_BOOK["lang"])
            qr = qr_code(url)
        except Exception:  # noqa: BLE001 - no QR library or an area off the world: the page goes without
            return 0.0
        x, y, w, h = rect
        pic = QgsLayoutItemPicture(self.layout)
        pic.setPicturePath("base64:" + base64.b64encode(qr_svg(qr)).decode("ascii"),
                           QgsLayoutItemPicture.Format.FormatSVG)
        pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
        pic.setBackgroundEnabled(True)
        pic.setBackgroundColor(QColor("#FFFFFF"))
        self.layout.addLayoutItem(pic)
        pic.setId(f"mapbook-qr-{p.number}")
        pic.setCustomProperty("biruni/living_paper/url", url)
        _place(pic, (x, y, w, w), k)
        _text(self.layout, tr("Scan: this page on your phone"), (x, y + w + 0.3, w, 5), k, 5.5, True, INK,
              align="center")
        self.qr_urls[p.number] = url
        return w

    # -- index of names
    INDEX_GAP = 7.0        # between columns
    NAME_H, REF_H, LETTER_H = 4.2, 3.4, 7.0

    def _index_geometry(self):
        pw, ph = self.book_size
        cols = 3 if pw > ph else 2
        w, h = pw - 2 * P.MARGIN_MM - 8, ph - 2 * P.MARGIN_MM - 29 - 9
        return cols, (w - (cols - 1) * self.INDEX_GAP) / cols, h - 2

    def _index_blocks(self, entries):
        """Letter headings and entries with their heights: an entry is its name and, under it, its pages and grid
        squares wrapped to the column."""
        from ..thesis.figures import wrap

        _cols, cw, _h = self._index_geometry()
        blocks, letter = [], None
        for e in entries:
            if e["letter"] != letter:
                letter = e["letter"]
                blocks.append(("letter", letter, self.LETTER_H, None))
            refs = e["squares"] or e["pages"]
            refs = _d(refs) if _BOOK["ad"] else refs
            lines = wrap(refs, cw - 4, _BOOK["font"], 6.8) if refs else []
            blocks.append(("entry", e, self.NAME_H + self.REF_H * len(lines) + 0.8, lines))
        return blocks

    def _index_chunks(self, entries):
        """Index pages: columns filled top to bottom; a letter heading never ends a column."""
        cols, _cw, h = self._index_geometry()
        pages, page, col, used = [], [], 0, 0.0
        blocks = self._index_blocks(entries)
        for i, b in enumerate(blocks):
            need = b[2] + (blocks[i + 1][2] if b[0] == "letter" and i + 1 < len(blocks) else 0)
            if used + need > h and used > 0:
                col, used = col + 1, 0.0
                if col >= cols:
                    pages.append(page)
                    page, col = [], 0
            page.append((col, used, b))
            used += b[2]
        if page:
            pages.append(page)
        return pages

    def _index_page(self, k, chunk, n):
        L = self.layout
        box = self._frame_and_heading(k, tr("Index of names") if n == 0 else tr("Index of names (continued)"))
        _cols, cw, _h = self._index_geometry()
        x, y, w, _hh = box
        targets = {p.name: self.page_of[p.number] for p in self.pages}
        for col, top, (kind, data, height, lines) in chunk:
            cx = x + col * (cw + self.INDEX_GAP)
            if _BOOK["rtl"]:
                cx = x + w - (col + 1) * cw - col * self.INDEX_GAP
            cy = y + top
            if kind == "letter":
                _text(L, data, (cx, cy + 1.2, cw, 4.6), k, 10, True, ACCENT)
                _hrule(L, cx, cy + 6.0, cw, k, ACCENT)
                continue
            _text(L, data["name"], (cx, cy, cw, self.NAME_H), k, 7.8, True, INK)
            for j, line in enumerate(lines):
                r = _mirror((cx + 3, cy + self.NAME_H + j * self.REF_H, cw - 3, self.REF_H), (cx, cy, cw, height))
                _text(L, line, r, k, 6.8, False, MUTED)
            first = (data["pages"] or "").split(",")[0].strip()
            if first in targets:
                self.links.append((k, (cx, cy, cw, height - 0.8), targets[first]))

    # -- back page
    def _back(self, k):
        box = self._frame_and_heading(k, tr("Notes"))
        x, y, w, h = box
        text = self.book.get("back_text") or ""
        from ..map_studio.auto_layout import credits

        lines = [text] if text else []
        found = credits(self.layers)
        if found:
            lines.append(tr("Sources: {s}").format(s="; ".join(found)))
        lines.append(tr("Coordinate system: {crs}").format(crs=f"{self.crs.description()} ({self.crs.authid()})"))
        lines.append(tr("Made with Biruni GIS"))
        _text(self.layout, "\n\n".join(lines), (x, y + 2, w, h - 4), k, 10, False, INK,
              valign=Qt.AlignmentFlag.AlignTop)


# ---- exporting --------------------------------------------------------------------------------------------------
def unique_layout_name(project, name, replace=True):
    manager = project.layoutManager()
    old = manager.layoutByName(name)
    if old is not None and replace and old.customProperty("biruni/mapbook/book") is not None:
        manager.removeLayout(old)
        for lyr in list(project.mapLayers().values()):
            if lyr.customProperty("biruni/mapbook/layout") == name:
                project.removeMapLayer(lyr.id())
        return name
    base, k = name, 2
    while manager.layoutByName(name) is not None:
        name, k = f"{base} ({k})", k + 1
    return name


def build_layout(book, page_list, project=None, add_to_manager=True):
    """The book's layout (kept in the project's layouts so it can be changed in the layout designer)."""
    project = project or QgsProject.instance()
    total = len([p for p in page_list if not p.excluded])
    src = B.book_layer(book, project, "layer") if book.get("source") in ("index", "features") else None
    _set_language(book.get("lang"))
    _BOOK["ad"] = (_BOOK["lang"] == "ar") if book.get("arabic_digits") is None else bool(book["arabic_digits"])
    for p in page_list:
        if not p.excluded:
            p.title = page_title(book, p, total, project, src)
    bl = BookLayout(book, page_list, project)
    name = tr("Map book") + " - " + (book.get("title") or book.get("name") or "")
    if add_to_manager:
        name = unique_layout_name(project, name)
    layout = bl.build(name)
    if add_to_manager:
        project.layoutManager().addLayout(layout)
    return bl


def file_names(page_list, ext):
    """One file name per page, from the page names (numbers padded so files sort in page order)."""
    from ..map_studio.outputs import safe_name

    live = [p for p in page_list if not p.excluded and p.number is not None]
    width = max(2, len(str(len(live))))
    out, seen = {}, set()
    for p in live:
        base = f"{p.number:0{width}d}" if p.name == str(p.number) else safe_name(p.name)
        name, n = base, 2
        while name.lower() in seen:
            name, n = f"{base} ({n})", n + 1
        seen.add(name.lower())
        out[p.number] = f"{name}.{ext}"
    return out


def export_pages(bl, folder, kind="png", dpi=150):
    """One file per map page (PNG / JPG image or PDF), named from the page names. Returns the paths."""
    os.makedirs(folder, exist_ok=True)
    names = file_names(bl.all_pages, kind)
    L = bl.layout
    exporter = QgsLayoutExporter(L)
    out = []
    pc = L.pageCollection()
    for p in bl.pages:
        k = bl.page_of[p.number]
        path = os.path.join(folder, names[p.number])
        if kind == "pdf":
            from ..cartography import pdf_settings

            for i in range(pc.pageCount()):
                pc.page(i).setExcludeFromExports(i != k)
            try:
                ok = exporter.exportToPdf(path, pdf_settings(True, 200)) == QgsLayoutExporter.ExportResult.Success
            finally:
                for i in range(pc.pageCount()):
                    pc.page(i).setExcludeFromExports(False)
        else:
            img = exporter.renderPageToImage(k, QSize(), dpi)
            ok = not img.isNull() and img.save(path, None, 92)
        if ok:
            out.append(path)
    return out


def export_book(book, page_list, pdf_path, project=None, per_page="", per_page_folder=None, seal=False,
                add_to_manager=True, feedback=None):
    """Write the book: one PDF with bookmarks and page links, optionally one file per page and the seal.
    Returns {"pdf", "layout", "links", "bookmarks", "pages", "files", "seal"}."""
    from ..cartography import export_pdf

    project = project or QgsProject.instance()
    if feedback:
        feedback(0, tr("Laying out the pages…"))
    bl = build_layout(book, page_list, project, add_to_manager)
    if feedback:
        feedback(30, tr("Writing the PDF…"))
    os.makedirs(os.path.dirname(os.path.abspath(pdf_path)), exist_ok=True)
    ok, _web = export_pdf(bl.layout, pdf_path, lean=True, links=True, dpi=200)
    if not ok:
        raise RuntimeError(tr("The PDF could not be written."))
    from .pdf import add_navigation

    n_links, n_marks = add_navigation(pdf_path, bl.links, bl.outline, bl.sizes)
    files = []
    if per_page:
        if feedback:
            feedback(70, tr("One file per page…"))
        folder = per_page_folder or os.path.join(os.path.dirname(os.path.abspath(pdf_path)),
                                                 os.path.splitext(os.path.basename(pdf_path))[0])
        files = export_pages(bl, folder, per_page, int(book.get("dpi", 150) or 150))
    sealed = None
    if seal:
        from ..seal.core import seal_file

        sealed = seal_file(pdf_path, certificate=True)
    if feedback:
        feedback(100, tr("Done"))
    return {"pdf": pdf_path, "layout": bl.layout, "builder": bl, "links": n_links, "bookmarks": n_marks,
            "pages": len(bl.pages), "pdf_pages": len(bl.plan), "files": files, "seal": sealed}


def render_page_png(bl, number, path, dpi=110, layout_page=False):
    """One page of the book as a picture (for a look before printing): map page ``number``, or the layout's page
    ``number`` (0 = the cover) with ``layout_page``."""
    k = int(number) if layout_page else bl.page_of[number]
    img = QgsLayoutExporter(bl.layout).renderPageToImage(k, QSize(), dpi)
    return img.save(path)
