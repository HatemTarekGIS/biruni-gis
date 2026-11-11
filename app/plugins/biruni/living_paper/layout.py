"""The QR code on the print layout: the printed map's frame (4 corners, so a rotated map keeps its shape), its
title and scale, and optionally a few features of one layer, packed into the address of the phone page.

The code is drawn as a vector picture (SVG kept inside the layout, so the project carries it and it prints sharp),
with the address part in the QR code's alphanumeric mode, and sized so a module is never smaller than
MODULE_MM - printed, a phone camera reads it from a hand's length.
"""
import base64
import io

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutItemPage, QgsLayoutItemPicture, QgsLayoutPoint,
                       QgsLayoutSize, QgsPointXY, QgsProject, QgsWkbTypes)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor

from ..i18n import lang as ui_lang
from ..i18n import tr
from .codec import MAX_CHARS, PAGE_URL, encode

QR_ID, CAPTION_ID = "biruni_live_qr", "biruni_live_qr_caption"
PROP = "biruni/living_paper/"
MODULE_MM = 0.38          # smallest printed module
MIN_SIZE_MM = 24.0
BORDER = 2                # quiet zone (modules); the picture's white background adds to it
PAD = 2.5                 # mm from the map frame's edge
CAPTION_PT = 6.5
MAX_FEATURES = 5000       # features read from the layer (the code keeps far fewer)
WGS84 = "EPSG:4326"


# ---- what the printed map shows -------------------------------------------------------------------------------
def map_item(layout):
    """The layout's main map: its reference map, else the largest map frame; None when there is none."""
    ref = layout.referenceMap()
    if ref is not None:
        return ref
    maps = [i for i in layout.items() if isinstance(i, QgsLayoutItemMap)]
    if not maps:
        return None
    return max(maps, key=lambda m: m.sizeWithUnits().width() * m.sizeWithUnits().height())


def _project(layout):
    return layout.project() or QgsProject.instance()


def frame_polygon(mp):
    """The area the map frame shows (a rotated map gives a rotated quadrilateral), in the map's CRS."""
    poly = mp.visibleExtentPolygon()
    pts = [QgsPointXY(p.x(), p.y()) for p in poly]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts[:4]


def frame_corners(mp, project=None):
    """The 4 corners (lon, lat) of what the map frame shows, in order around it."""
    xf = QgsCoordinateTransform(mp.crs(), QgsCoordinateReferenceSystem(WGS84), project or QgsProject.instance())
    out = []
    for p in frame_polygon(mp):
        q = xf.transform(p)
        out.append((q.x(), q.y()))
    return out


def layout_title(layout):
    """The map's title: the one Map Studio gave it, else the largest text on the page, else the layout's name."""
    t = layout.customProperty("biruni/studio/title", "")
    if t:
        return str(t)
    best = None
    for item in layout.items():
        if isinstance(item, QgsLayoutItemLabel) and item.id() != CAPTION_ID:
            text = " ".join((item.currentText() or "").split())
            if not text or len(text) > 120:
                continue
            size = item.textFormat().size()
            if best is None or size > best[0]:
                best = (size, text)
    return best[1] if best else layout.name()


def scale_text(mp):
    s = mp.scale()
    if not s or s <= 0 or mp.crs().isGeographic() and s > 1e9:
        return ""
    return f"1:{round(s):,}"


def _value(v):
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return ""
    return " ".join(str(v).split())


def layer_features(source, mp, label_field=None, project=None):
    """(points, shapes) of a layer (QgsVectorLayer or a Processing feature source) inside the map frame, in WGS84,
    as codec.encode() takes them. Lines and polygons are cut to the frame and come largest first; with a label
    field they also give a labelled point each."""
    project = project or QgsProject.instance()
    crs = source.sourceCrs()
    to_layer = QgsCoordinateTransform(mp.crs(), crs, project)
    frame = QgsGeometry.fromPolygonXY([frame_polygon(mp)])
    try:
        frame.transform(to_layer)
    except Exception:  # noqa: BLE001 - frame outside the layer CRS's area
        return [], []
    to_wgs = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem(WGS84), project)
    gtype = QgsWkbTypes.geometryType(source.wkbType())
    names = source.fields().names()
    field = label_field if label_field and label_field in names else None
    req = QgsFeatureRequest().setFilterRect(frame.boundingBox()).setLimit(MAX_FEATURES)
    if field is None:
        req.setNoAttributes()
    points, shapes = [], []
    engine = QgsGeometry.createGeometryEngine(frame.constGet())
    engine.prepareGeometry()
    for f in source.getFeatures(req):
        g = f.geometry()
        if g is None or g.isNull() or g.isEmpty() or not engine.intersects(g.constGet()):
            continue
        label = _value(f[field]) if field else ""
        if gtype == Qgis.GeometryType.Point:
            for part in g.asGeometryCollection() if g.isMultipart() else [g]:
                p = QgsGeometry(part)
                if not engine.intersects(p.constGet()):
                    continue
                p.transform(to_wgs)
                q = p.asPoint()
                points.append((q.x(), q.y(), label))
            continue
        cut = g.intersection(frame)
        if cut.isEmpty():
            continue
        if label:
            lp = cut.pointOnSurface() if gtype == Qgis.GeometryType.Polygon else cut.interpolate(cut.length() / 2)
            if lp is not None and not lp.isEmpty():
                lp.transform(to_wgs)
                q = lp.asPoint()
                points.append((q.x(), q.y(), label))
        for part in cut.asGeometryCollection():
            if gtype == Qgis.GeometryType.Polygon and part.type() == Qgis.GeometryType.Polygon:
                size = part.area()
                part.transform(to_wgs)
                ring = part.asPolygon()[0] if not part.isMultipart() else part.asMultiPolygon()[0][0]
                shapes.append((size, "polygon", [(p.x(), p.y()) for p in ring]))
            elif gtype == Qgis.GeometryType.Line and part.type() == Qgis.GeometryType.Line:
                size = part.length()
                part.transform(to_wgs)
                line = part.asPolyline() if not part.isMultipart() else part.asMultiPolyline()[0]
                shapes.append((size, "line", [(p.x(), p.y()) for p in line]))
    shapes.sort(key=lambda s: -s[0])
    return points, [(k, c) for _s, k, c in shapes[:200]]


# ---- the QR code ----------------------------------------------------------------------------------------------
def qr_code(url):
    """A segno QR code for the address: the page part as bytes, the map part (after "#") in alphanumeric mode."""
    from ..vendor import ensure_vendor

    ensure_vendor()  # the QR code library
    import segno

    if "#" in url:
        head, data = url.split("#", 1)
        return segno.make([head + "#", data], error="m")
    return segno.make(url, error="m")


def qr_svg(qr):
    buf = io.BytesIO()
    qr.save(buf, kind="svg", border=BORDER, dark="#111111", light="#ffffff", xmldecl=False)
    return buf.getvalue()


def _block_size(qr, size_mm=None):
    modules = qr.symbol_size(border=BORDER)[0]
    side = size_mm or max(MIN_SIZE_MM, modules * MODULE_MM)
    cap_h = CAPTION_PT * 0.3528 * 2.4
    return side, cap_h


def _other_rects(layout, mp):
    out = []
    for item in layout.items():
        if item is mp or not hasattr(item, "sizeWithUnits") or item.id() in (QR_ID, CAPTION_ID):
            continue
        if isinstance(item, QgsLayoutItemPage) or not item.isVisible():
            continue
        try:
            pos, size = item.positionWithUnits(), item.sizeWithUnits()
        except Exception:  # noqa: BLE001 - pages and odd items
            continue
        if size.width() <= 0 or size.height() <= 0:
            continue
        out.append(QRectF(pos.x(), pos.y(), size.width(), size.height()))
    return out


def best_corner(layout, mp, w, h, rtl=False):
    """Top-left (page mm) of the inner corner of the map frame where a w × h block covers the least: other items
    first (legend, scale bar…), then the map's data. Bottom corners on the reading side's end come first."""
    pos, size = mp.positionWithUnits(), mp.sizeWithUnits()
    fx, fy, fw, fh = pos.x(), pos.y(), size.width(), size.height()
    right, left = (fx + fw - PAD - w, fx + PAD)
    top, bottom = (fy + PAD, fy + fh - PAD - h)
    order = [(right, bottom), (left, bottom), (right, top), (left, top)]
    if rtl:
        order = [(left, bottom), (right, bottom), (left, top), (right, top)]
    others = _other_rects(layout, mp)
    occ = None
    try:
        from ..map_studio.placement import Occupancy

        occ = Occupancy(mp)
    except Exception:  # noqa: BLE001 - no data render: items alone decide
        occ = None
    best = None
    for rank, (x, y) in enumerate(order):
        r = QRectF(x, y, w, h)
        hit = sum((r & o).width() * (r & o).height() for o in others if r.intersects(o)) / max(w * h, 1e-6)
        data = 0.0
        if occ is not None:
            try:
                data = occ.share(QRectF(x - fx, y - fy, w, h))
            except Exception:  # noqa: BLE001
                data = 0.0
        score = hit * 10 + data + rank * 0.004
        if best is None or score < best[0]:
            best = (score, x, y)
    return best[1], best[2]


def _remove_old(layout):
    """The QR code already on this layout (made before): removed, its place kept."""
    old = None
    for iid in (QR_ID, CAPTION_ID):
        item = layout.itemById(iid)
        if item is not None:
            if iid == QR_ID:
                p = item.positionWithUnits()
                old = (p.x(), p.y())
            layout.removeLayoutItem(item)
    return old


def add_live_qr(layout, layer=None, label_field=None, title=None, mp=None, max_chars=MAX_CHARS, size_mm=None,
                lang=None, base=PAGE_URL):
    """Put the live QR code (and its caption) on a print layout. Returns (url, report, picture item).
    report is codec.encode()'s, plus "qr_version" and "size_mm". Running it again replaces the code, in the place
    it was moved to."""
    from ..i18n import is_rtl
    from ..mapsheet import _text_format

    mp = mp or map_item(layout)
    if mp is None:
        raise ValueError(tr("The layout has no map."))
    project = _project(layout)
    corners = frame_corners(mp, project)
    points, shapes = layer_features(layer, mp, label_field, project) if layer is not None else ([], [])
    lang = lang or ui_lang()
    url, report = encode(title if title is not None else layout_title(layout), corners, scale_text(mp), points,
                         shapes, lang=lang, max_chars=max_chars, base=base)
    qr = qr_code(url)
    side, cap_h = _block_size(qr, size_mm)
    report["qr_version"] = qr.version
    report["size_mm"] = round(side, 1)

    old = _remove_old(layout)
    pic = QgsLayoutItemPicture(layout)
    pic.setId(QR_ID)
    pic.setPicturePath("base64:" + base64.b64encode(qr_svg(qr)).decode("ascii"),
                       QgsLayoutItemPicture.Format.FormatSVG)
    pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
    pic.setBackgroundEnabled(True)
    pic.setBackgroundColor(QColor("#FFFFFF"))
    layout.addLayoutItem(pic)
    pic.attemptResize(QgsLayoutSize(side, side))

    cap = QgsLayoutItemLabel(layout)
    cap.setId(CAPTION_ID)
    cap.setText(tr("Scan: this map on your phone"))
    cap.setTextFormat(_text_format(CAPTION_PT, True, "#1E252B"))
    cap.setHAlign(Qt.AlignmentFlag.AlignHCenter)
    cap.setVAlign(Qt.AlignmentFlag.AlignVCenter)
    cap.setMarginX(0.5)
    cap.setMarginY(0)
    cap.setBackgroundEnabled(True)
    cap.setBackgroundColor(QColor("#FFFFFF"))
    layout.addLayoutItem(cap)
    cap.attemptResize(QgsLayoutSize(side, cap_h))

    x, y = old if old else best_corner(layout, mp, side, side + cap_h, is_rtl())
    pic.attemptMove(QgsLayoutPoint(x, y))
    cap.attemptMove(QgsLayoutPoint(x, y + side))
    pic.setCustomProperty(PROP + "url", url)
    layout.setCustomProperty(PROP + "url", url)
    return url, report, pic


def describe(report):
    """What the code carries, in a sentence or two of the interface language."""
    parts = [tr("The code carries the printed area and the title.")]
    if report.get("points_given"):
        parts.append(tr("Points: {kept} of {total}.").format(kept=report["points"], total=report["points_given"]))
    if report.get("shapes_given"):
        if report["shapes"]:
            parts.append(tr("Shapes: {kept} of {total}, simplified to {m} m.").format(
                kept=report["shapes"], total=report["shapes_given"], m=report["tolerance_m"]))
        else:
            parts.append(tr("Shapes: none of {total} fitted.").format(total=report["shapes_given"]))
    if report.get("labels_shortened"):
        parts.append(tr("Some labels were shortened."))
    if report.get("title_shortened"):
        parts.append(tr("The title was shortened."))
    if report.get("qr_version"):
        parts.append(tr("Address: {n} characters; QR code {mm} mm wide.").format(n=report["chars"],
                                                                                 mm=report["size_mm"]))
    return " ".join(parts)
