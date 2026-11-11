"""A map book: its settings, its pages made from the chosen source, kept in the project, and shown on the map.

A book is a plain dict (``new_book()`` gives the defaults) so it is kept in the project as JSON and exported again
after edits. ``make_pages`` builds the pages from the source; ``finish`` puts them in reading order, numbers and
names them and finds their neighbours. Pages moved, turned or left out by hand stay as they are until the pages are
made again.
"""
import json
import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureRequest,
                       QgsGeometry, QgsProject, QgsRectangle, QgsVectorLayer)

from . import pages as P

ENTRY_SCOPE, ENTRY_KEY = "Biruni", "mapbook"
PREVIEW_PROP = "biruni/mapbook/preview"
SOURCES = ("grid", "index", "strips", "features", "stops")
FORMAT = 1


def new_book(**kw):
    """The settings of a book with their defaults; keyword arguments override them."""
    book = {
        "format": FORMAT, "name": "", "title": "", "subtitle": "", "author": "",
        "source": "grid",            # grid | index | strips | features | stops
        "layer": "",                 # index polygons / route lines / features (layer id)
        "name_field": "",            # page names from this field (index, features)
        "rotation_field": "",        # page turn from this field (index, features), degrees counter-clockwise
        "data_layers": [],           # what counts as data for the automatic grid (empty = the visible vector layers)
        "paper": "A4", "orientation": "auto",      # auto | landscape | portrait
        "scale_mode": "best",        # best (per page) | common (one for the book) | fixed
        "scale": 0,                  # fixed scale / grid scale (0 = chosen for you)
        "max_pages": 24,             # the automatic grid / strips: the largest scale that keeps to this many pages
        "overlap": 0.08,             # share of the page repeated on the next one (grid, strips)
        "fewest": True,              # automatic grid: only cells with data, fewest pages
        "order": "rows",             # rows | snake | columns | route
        "naming": "number",          # number | grid (A1, B2…) | given (field / stop names)
        "page_title": "",            # each page's title: text with [% expression %], {name} {number} {total}
        "lang": "",                  # the book's language ("" = the interface's)
        "arabic_digits": None,       # None = for Arabic books
        "locator": True, "neighbours": True, "legend": True, "grid": True, "qr": True,
        "table_layer": "", "table_fields": [], "table_rows": 12,
        "cover": True, "contents": True, "index_layer": "", "index_field": "", "back": False, "back_text": "",
        "seal": False, "per_page": "",   # "" | png | jpg | pdf
        "dpi": 150, "crs": "", "pages": [], "method": "",
    }
    book.update(kw)
    return book


# ---- keeping books in the project -------------------------------------------------------------------------------
def _all(project):
    text, _ok = project.readEntry(ENTRY_SCOPE, ENTRY_KEY, "")
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("books", {})
    return data


def book_layer(book, project, key):
    """The layer a setting names: a layer given for this run only (``book["_objects"]``, from a Processing tool),
    else the project's layer with that id."""
    obj = (book.get("_objects") or {}).get(key)
    if obj is not None:
        return obj
    return project.mapLayer(book.get(key) or "")


def books(project=None):
    """{name: book} kept in the project."""
    return _all(project or QgsProject.instance())["books"]


def save_book(book, project=None, page_list=None):
    """Keep ``book`` (with ``page_list`` when given) in the project under its name."""
    project = project or QgsProject.instance()
    if page_list is not None:
        book["pages"] = [p.to_dict() for p in page_list]
    book = {k: v for k, v in book.items() if not k.startswith("_")}
    data = _all(project)
    name = book.get("name") or book.get("title") or "Map book"
    book["name"] = name
    data["books"][name] = book
    data["current"] = name
    project.writeEntry(ENTRY_SCOPE, ENTRY_KEY, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return book


def load_book(name=None, project=None):
    data = _all(project or QgsProject.instance())
    name = name or data.get("current")
    b = data["books"].get(name) if name else None
    return new_book(**b) if b else None


def delete_book(name, project=None):
    project = project or QgsProject.instance()
    data = _all(project)
    data["books"].pop(name, None)
    if data.get("current") == name:
        data["current"] = next(iter(data["books"]), None)
    project.writeEntry(ENTRY_SCOPE, ENTRY_KEY, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def page_list(book):
    return [P.Page.from_dict(d) for d in book.get("pages") or []]


# ---- where the book is drawn ------------------------------------------------------------------------------------
def utm_for(lon, lat):
    zone = int(math.floor((lon + 180) / 6)) % 60 + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32700 if lat < 0 else 32600) + zone}")


def work_crs(project, layers=()):
    """The project's CRS when it measures in metres or feet; otherwise the UTM zone of the data."""
    crs = project.crs()
    if crs.isValid() and not crs.isGeographic() and crs.authid() not in ("EPSG:3857", "EPSG:900913"):
        return crs
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    ext = QgsRectangle()
    for lyr in layers:
        try:
            e = QgsCoordinateTransform(lyr.crs(), wgs, project).transformBoundingBox(lyr.extent())
        except Exception:  # noqa: BLE001
            continue
        if not e.isFinite() or (e.width() == 0 and e.height() == 0 and e.xMinimum() == 0):
            continue
        if ext.isEmpty():
            ext = QgsRectangle(e)
        else:
            ext.combineExtentWith(e)
    c = ext.center() if not ext.isEmpty() else None
    if c is None:
        return crs if crs.isValid() else QgsCoordinateReferenceSystem("EPSG:3857")
    return utm_for(c.x(), c.y())


def book_crs(book, project):
    crs = QgsCoordinateReferenceSystem(book.get("crs") or "")
    return crs if crs.isValid() else work_crs(project, data_layers(book, project))


def data_layers(book, project):
    """The vector layers that count as data (the book's choice, else the visible vector layers)."""
    if book.get("_data"):
        return [l for l in book["_data"] if isinstance(l, QgsVectorLayer) and l.isValid()]
    out = [project.mapLayer(i) for i in book.get("data_layers") or []]
    out = [l for l in out if isinstance(l, QgsVectorLayer) and l.isValid()]
    if out:
        return out
    root = project.layerTreeRoot()
    for lyr in root.layerOrder():
        node = root.findLayer(lyr.id())
        if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and node is not None and node.isVisible() \
                and not lyr.customProperty(PREVIEW_PROP):
            out.append(lyr)
    return out


def _features(layer, crs, project, limit=200000):
    xf = QgsCoordinateTransform(layer.crs(), crs, project)
    req = QgsFeatureRequest()
    req.setLimit(limit)
    for f in layer.getFeatures(req):
        g = QgsGeometry(f.geometry())
        if g.isNull() or g.isEmpty():
            continue
        try:
            g.transform(xf)
        except Exception:  # noqa: BLE001 - a feature off the projection's world
            continue
        yield f, g


def _value(f, field):
    if not field or f.fields().indexOf(field) < 0:
        return None
    v = f[field]
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return v


# ---- the sources ------------------------------------------------------------------------------------------------
def make_pages(book, project=None, feedback=None):
    """Pages from the book's source, in the book's CRS (not yet ordered or numbered). Returns the list; the book
    gets its CRS and, for the grid, the method that gave the fewest pages."""
    project = project or QgsProject.instance()
    crs = book_crs(book, project)
    book["crs"] = crs.authid() or crs.toWkt()
    src = book.get("source", "grid")
    paper, orient, grid = book.get("paper", "A4"), book.get("orientation", "auto"), bool(book.get("grid", True))
    layer = book_layer(book, project, "layer")
    if src in ("index", "features", "strips") and not isinstance(layer, QgsVectorLayer):
        raise ValueError("choose the layer")
    if src == "grid":
        geoms = [g for lyr in data_layers(book, project) for _f, g in _features(lyr, crs, project)]
        if not geoms:
            raise ValueError("no data")
        cover = P.DataCover(geoms)
        overlap = float(book.get("overlap", 0.0) or 0.0)
        best = None
        for landscape in ((True, False) if orient == "auto" else (orient == "landscape",)):
            scale = float(book.get("scale") or 0) or P.auto_grid_scale(cover, paper, landscape,
                                                                         int(book.get("max_pages", 24)), overlap,
                                                                         grid)
            pages, method, plain = P.grid_pages(cover, paper, landscape, scale, overlap, grid,
                                                bool(book.get("fewest", True)))
            key = (scale, len(pages))
            if best is None or key < best[0]:
                best = (key, pages, method, plain)
        _key, pages, book["method"], book["plain_pages"] = best
        return pages
    if src in ("index", "features"):
        items = []
        nf, rf = book.get("name_field", ""), book.get("rotation_field", "")
        for f, g in _features(layer, crs, project):
            name = _value(f, nf)
            angle = _value(f, rf) if rf else 0.0
            try:
                angle = float(angle or 0.0)
            except (TypeError, ValueError):
                angle = 0.0
            items.append((g, str(f.id()), "" if name is None else str(name), angle))
        margin = 0.02 if src == "index" else 0.08
        return P.fit_pages(items, paper, orient, book.get("scale_mode", "best"), float(book.get("scale") or 0),
                           grid, margin)
    if src == "strips":
        lines = [g for _f, g in _features(layer, crs, project) if g.type() == Qgis.GeometryType.Line]
        if not lines:
            raise ValueError("no lines")
        line = QgsGeometry.unaryUnion(lines).mergeLines()
        landscape = orient != "portrait"
        overlap = float(book.get("overlap", 0.0) or 0.0)
        rtl_view = bool(book.get("rtl_view", False))
        scale = float(book.get("scale") or 0)
        if not scale:
            # the largest standard scale that keeps the book to max_pages pages
            for s in P.BOOK_SCALES:
                fw, _fh = P.frame_mm(paper, landscape, grid)
                if line.length() / (fw * s / 1000.0) > int(book.get("max_pages", 24)) * 1.6:
                    continue
                trial = P.strip_pages(line, paper, landscape, s, overlap * fw * s / 1000.0, grid,
                                      reverse_view=rtl_view)
                if len(trial) <= int(book.get("max_pages", 24)):
                    scale = s
                    break
            scale = scale or P.BOOK_SCALES[-1]
        fw, _fh = P.frame_mm(paper, landscape, grid)
        return P.strip_pages(line, paper, landscape, scale, overlap * fw * scale / 1000.0, grid,
                             reverse_view=rtl_view)
    if src == "stops":
        return stop_pages(book, project, crs)
    raise ValueError(src)


def stop_pages(book, project, crs):
    """One page per Map Stop of the project: its view (centre, size, turn), its scale when it kept one, the layers
    it remembers."""
    from ..stops.model import PROJECT, store_for

    paper, orient, grid = book.get("paper", "A4"), book.get("orientation", "auto"), bool(book.get("grid", True))
    out = []
    for stop in store_for(project=project).stops(PROJECT):
        try:
            fr = stop.frame.transformed(crs, project)
        except ValueError:
            continue
        w, h = max(fr.w, 1e-6), max(fr.h, 1e-6)
        if book.get("scale_mode") == "fixed" and book.get("scale"):
            landscape, scale = (w >= h if orient == "auto" else orient == "landscape"), float(book["scale"])
        else:
            landscape, scale = P.best_fit(w, h, paper, orient, grid, margin=0.0)
            if stop.scale > 0:
                scale = float(stop.scale)
        p = P.Page(fr.cx, fr.cy, 0, 0, fr.rot, scale, landscape, stop.name, stop.id)
        P.frame_for(p, paper, grid)
        p.theme = stop.theme or ""
        out.append(p)
    if book.get("scale_mode") == "common" and out:
        common = max(p.scale for p in out)
        for p in out:
            p.scale = common
            P.frame_for(p, paper, grid)
    return out


def finish(book, page_list, rtl=False, renumber=True):
    """Reading order, numbers, names and neighbours."""
    src = book.get("source", "grid")
    if renumber:
        order = P.reading_order(page_list, "route" if src in ("strips", "stops") else book.get("order", "rows"),
                                rtl)
        page_list[:] = [page_list[k] for k in order]
    naming = book.get("naming", "number")
    if naming == "grid" and src != "grid":
        naming = "number"
    if naming == "given" and src not in ("index", "features", "stops"):
        naming = "number"
    P.number_pages(page_list, naming)
    P.find_neighbours(page_list, sequence=src == "strips", reverse=bool(book.get("rtl_view")))
    return page_list


def build(book, project=None, rtl=False):
    """make_pages + finish; the pages are kept in the book."""
    project = project or QgsProject.instance()
    pl = make_pages(book, project)
    finish(book, pl, rtl)
    book["pages"] = [p.to_dict() for p in pl]
    return pl


def relink(book, page_list):
    """After a page was moved, turned, left out or put back: numbers and neighbours again (order kept)."""
    finish(book, page_list, renumber=False)
    book["pages"] = [p.to_dict() for p in page_list]
    return page_list


# ---- the pages on the map ---------------------------------------------------------------------------------------
def pages_layer(page_list, crs, name="Map book pages", core=False, include_excluded=True):
    """A temporary polygon layer of the pages (their frames, or the areas they are about with ``core``)."""
    auth = crs.authid() or crs.toWkt()
    lyr = QgsVectorLayer(f"Polygon?crs={auth}&field=idx:integer&field=number:integer&field=name:string"
                         f"&field=label:string&field=excluded:integer&field=scale:integer&field=angle:double",
                         name, "memory")
    feats = []
    for k, p in enumerate(page_list):
        if p.excluded and not include_excluded:
            continue
        f = QgsFeature(lyr.fields())
        f.setGeometry(p.core_geometry() if core else p.polygon())
        label = p.name if p.number is None or p.name == str(p.number) else f"{p.name} · {p.number}"
        f.setAttributes([k, p.number, p.name, label, 1 if p.excluded else 0, int(round(p.scale)),
                         round(p.angle, 3)])
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def style_preview(lyr):
    """Pages as numbered outlines; pages left out dashed and grey."""
    from qgis.core import (QgsFillSymbol, QgsPalLayerSettings, QgsRuleBasedRenderer, QgsTextBufferSettings,
                           QgsTextFormat, QgsVectorLayerSimpleLabeling)
    from qgis.PyQt.QtGui import QColor, QFont

    root = QgsRuleBasedRenderer.Rule(None)
    on = QgsRuleBasedRenderer.Rule(QgsFillSymbol.createSimple({
        "color": "31,111,178,28", "outline_color": "#1F6FB2", "outline_width": "0.6"}), filterExp='"excluded" = 0')
    off = QgsRuleBasedRenderer.Rule(QgsFillSymbol.createSimple({
        "color": "0,0,0,0", "outline_color": "#8A949C", "outline_width": "0.4", "outline_style": "dash"}),
        filterExp='"excluded" = 1')
    root.appendChild(on)
    root.appendChild(off)
    lyr.setRenderer(QgsRuleBasedRenderer(root))
    pal = QgsPalLayerSettings()
    pal.fieldName = "label"
    pal.placement = Qgis.LabelPlacement.OverPoint
    fmt = QgsTextFormat()
    font = QFont("Segoe UI")
    font.setBold(True)
    fmt.setFont(font)
    fmt.setSize(13)
    fmt.setColor(QColor("#1F6FB2"))
    buf = QgsTextBufferSettings()
    buf.setEnabled(True)
    buf.setSize(1.2)
    buf.setColor(QColor("white"))
    fmt.setBuffer(buf)
    pal.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    lyr.setLabelsEnabled(True)
    lyr.setCustomProperty(PREVIEW_PROP, True)
    return lyr
