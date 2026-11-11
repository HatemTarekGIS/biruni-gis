"""ArcGIS Pro layouts (CIMLayout in .aprx / .pagx) <-> print layouts.

Read: page size, map frames (extent, scale, linked map, border, grids),
legends, north arrows, scale bars, text (with the common dynamic text tags),
pictures, rectangles / ellipses / polygons / lines and groups.
Write: the same elements back to CIM for the .aprx writer.
Tables, charts, map series and other frames are reported, never dropped
silently."""
import base64
import hashlib
import math
import os
import re

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsLayoutItem,
                       QgsLayoutItemGroup, QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap,
                       QgsLayoutItemMapGrid, QgsLayoutItemPage,
                       QgsLayoutItemPicture, QgsLayoutItemPolygon, QgsLayoutItemPolyline, QgsLayoutItemScaleBar,
                       QgsLayoutItemShape, QgsLayoutMeasurement, QgsLayoutPoint, QgsLayoutSize, QgsLegendRenderer,
                       QgsLegendStyle, QgsPrintLayout, QgsProject, QgsRectangle, QgsRenderContext, QgsUnitTypes)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from qgis.PyQt.QtGui import QColor, QPolygonF, QTextDocument

from ..i18n import tr
from .cim import Converter, color, crs_from_spatial_reference
from .cim_write import MM_TO_PT, SymbolWriter, cim_color, ref, spatial_reference

MM = Qgis.LayoutUnit.Millimeters
# page units (Esri unit WKIDs) -> millimetres
PAGE_UNITS_MM = {1025: 1.0, 1033: 10.0, 109008: 25.4, 109009: 25.4, 9001: 1000.0}
_DIST = {9001: Qgis.DistanceUnit.Meters, 9036: Qgis.DistanceUnit.Kilometers, 9002: Qgis.DistanceUnit.Feet,
         9003: Qgis.DistanceUnit.Feet, 9093: Qgis.DistanceUnit.Miles, 9035: Qgis.DistanceUnit.Miles,
         9030: Qgis.DistanceUnit.NauticalMiles, 1033: Qgis.DistanceUnit.Centimeters,
         1025: Qgis.DistanceUnit.Millimeters, 9096: Qgis.DistanceUnit.Yards}
_DIST_BACK = {Qgis.DistanceUnit.Meters: 9001, Qgis.DistanceUnit.Kilometers: 9036, Qgis.DistanceUnit.Feet: 9002,
              Qgis.DistanceUnit.Miles: 9093, Qgis.DistanceUnit.NauticalMiles: 9030,
              Qgis.DistanceUnit.Centimeters: 1033, Qgis.DistanceUnit.Millimeters: 1025, Qgis.DistanceUnit.Yards: 9096}
# ArcGIS Pro 3.x scale bars: CIMScaleLine (stepped: true/false), CIMSingleFillScaleBar and
# CIMDoubleFillScaleBar with style Alternating / DoubleAlternating / Hollow (older names kept for reading)
_SCALEBARS = {"CIMScaleLine": "Line Ticks Up", "CIMSingleFillScaleBar": "Single Box",
              "CIMDoubleFillScaleBar": "Single Box", "CIMAlternatingScaleBar": "Single Box",
              "CIMDoubleAlternatingScaleBar": "Double Box", "CIMHollowScaleBar": "Hollow",
              "CIMSingleDivisionScaleBar": "Single Box", "CIMSteppedScaleLine": "Stepped Line"}
_FILL_STYLES = {"Alternating": "Single Box", "DoubleAlternating": "Double Box", "Hollow": "Hollow"}
_FILL_STYLES_BACK = {v: k for k, v in _FILL_STYLES.items()}
NORTH_ARROW_SVG = ":/images/north_arrows/layout_default_north_arrow.svg"
# CIM layout elements are listed bottom-most first: the last one draws on top (checked with
# ArcGIS Pro 3.7 rendering two overlapping rectangles); the Contents pane shows them reversed.
ELEMENTS_TOP_FIRST = False


def _norm(uri):
    u = (uri or "").strip()
    if u.upper().startswith("CIMPATH="):
        u = u[8:]
    return u.replace("\\", "/").lower()


def _sym(r):
    if isinstance(r, dict) and r.get("type") == "CIMSymbolReference":
        return r.get("symbol") or {}
    return r or {}


def _points(geom):
    """All plain [x, y] vertices of a CIM geometry (rings / paths / curve rings)."""
    out = []
    for key in ("rings", "paths", "curveRings", "curvePaths"):
        for part in (geom or {}).get(key) or []:
            for p in part:
                if isinstance(p, list) and len(p) >= 2:
                    out.append((float(p[0]), float(p[1])))
                elif isinstance(p, dict):
                    for seg in p.values():
                        if isinstance(seg, list) and seg and isinstance(seg[0], list):
                            out.append((float(seg[0][0]), float(seg[0][1])))
    if not out and "x" in (geom or {}):
        out.append((float(geom["x"]), float(geom["y"])))
    return out


def _bbox(pts):
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _set_manual_legend(leg):
    """Stop the legend following the project (it keeps the layers it shows now)."""
    if hasattr(leg, "setSyncMode") and hasattr(Qgis, "LegendSyncMode"):
        leg.setSyncMode(Qgis.LegendSyncMode.Manual)
    else:
        leg.setAutoUpdateModel(False)


def _legend_auto(leg):
    if hasattr(leg, "syncMode") and hasattr(Qgis, "LegendSyncMode"):
        return leg.syncMode() != Qgis.LegendSyncMode.Manual
    return bool(leg.autoUpdateModel())


def _nice(v):
    """A round grid interval close to v."""
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * e >= v:
            return m * e
    return 10 * e


# =============================================================================
#  CIM -> print layout
# =============================================================================
class LayoutReader:
    """Builds QgsPrintLayouts from CIMLayout definitions.

    maps: {normalised map uri: {"name", "layers": [QgsMapLayer] | None, "crs", "extent"}}
    layers: {normalised layer uri: QgsMapLayer}
    binary: callable(uri) -> bytes | None (pictures stored in the document)"""

    def __init__(self, project, maps, layers, binary, warn):
        self.project = project
        self.maps = maps
        self.layers = layers
        self.binary = binary
        self._warn = warn
        self.conv = Converter()
        self.frames = {}
        self.name = ""

    def warn(self, text):
        self._warn(f"{self.name}: {text}")

    # -- page geometry --
    def _setup_page(self, cim):
        page = cim.get("page") or {}
        uw = int(((page.get("units") or {}).get("uwkid")) or 0)
        w, h = float(page.get("width", 0) or 0), float(page.get("height", 0) or 0)
        f = PAGE_UNITS_MM.get(uw)
        if f is None:
            f = 25.4 if 0 < max(w, h) <= 60 else 1.0
            if w and h:
                self.warn(tr("unknown page units: the page size was guessed"))
        if not (w and h):
            w, h, f = 297.0, 210.0, 1.0
        self.f, self.H = f, h
        return w * f, h * f

    def rect(self, pts):
        """CIM page points -> (left, top, width, height) in layout millimetres."""
        b = _bbox(pts)
        if b is None:
            return None
        x0, y0, x1, y1 = b
        return x0 * self.f, (self.H - y1) * self.f, (x1 - x0) * self.f, (y1 - y0) * self.f

    def point(self, x, y):
        return QPointF(x * self.f, (self.H - y) * self.f)

    @staticmethod
    def place(item, r):
        if r is None:
            return
        item.attemptMove(QgsLayoutPoint(r[0], r[1], MM))
        if r[2] > 0 and r[3] > 0:
            item.attemptResize(QgsLayoutSize(r[2], r[3], MM))

    def _frame_style(self, item, el, background=False):
        gf = el.get("graphicFrame") or {}
        border = _sym(gf.get("borderSymbol"))
        stroke = None
        for sl in border.get("symbolLayers") or []:
            if sl.get("type") == "CIMSolidStroke" and sl.get("enable", True):
                stroke = sl
                break
        c = color(stroke.get("color")) if stroke else None
        if c is not None and c.alpha() > 0:
            item.setFrameEnabled(True)
            item.setFrameStrokeColor(c)
            item.setFrameStrokeWidth(QgsLayoutMeasurement(float(stroke.get("width", 1)) / MM_TO_PT, MM))
        else:
            item.setFrameEnabled(False)
        fill = None
        for sl in _sym(gf.get("backgroundSymbol")).get("symbolLayers") or []:
            if sl.get("type") == "CIMSolidFill" and sl.get("enable", True):
                fill = color(sl.get("color"))
                break
        if fill is not None and fill.alpha() > 0:
            item.setBackgroundEnabled(True)
            item.setBackgroundColor(fill)
        elif not background:
            item.setBackgroundEnabled(False)

    # -- the layout --
    def build(self, cim, name=None):
        self.name = name or cim.get("name") or tr("Layout")
        layout = QgsPrintLayout(self.project)
        layout.initializeDefaults()
        layout.setName(self._unique(self.name))
        w, h = self._setup_page(cim)
        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(w, h, MM))
        self.layout = layout
        self.frames = {}
        elements = list(cim.get("elements") or [])
        # map frames first, so legends / scale bars / north arrows can link to them
        for el in self._flat(elements):
            if el.get("type") == "CIMMapFrame":
                self.frames[el.get("name") or ""] = QgsLayoutItemMap(layout)
        order = list(reversed(elements)) if ELEMENTS_TOP_FIRST else elements
        for el in order:
            self._element(el)
        return layout

    def _unique(self, name):
        mgr = self.project.layoutManager()
        out, k = name, 2
        while mgr.layoutByName(out) is not None:
            out, k = f"{name} ({k})", k + 1
        return out

    def _flat(self, els):
        for e in els or []:
            yield e
            if e.get("type") == "CIMGroupElement":
                yield from self._flat(e.get("elements"))

    def _element(self, el):
        """One CIM element -> list of layout items."""
        t = el.get("type")
        name = el.get("name") or ""
        try:
            if t == "CIMGroupElement":
                children = list(el.get("elements") or [])
                items = []
                for child in (reversed(children) if ELEMENTS_TOP_FIRST else children):
                    items += self._element(child)
                if len(items) > 1:
                    group = self.layout.groupItems(items)
                    if group is not None:
                        group.setId(name)
                        group.setVisibility(bool(el.get("visible", True)))
                return items
            if t == "CIMMapFrame":
                items = [self._map(el)]
            elif t == "CIMLegend":
                items = [self._legend(el)]
            elif t in ("CIMMarkerNorthArrow", "CIMNorthArrow"):
                items = [self._north(el)]
            elif t in _SCALEBARS or (t or "").endswith("ScaleBar") or (t or "").endswith("ScaleLine"):
                items = [self._scalebar(el)]
            elif t == "CIMGraphicElement":
                items = self._graphic(el)
            else:
                self.warn(tr("{name} ({kind}) was not converted").format(name=name or "?", kind=t or "?"))
                return []
        except Exception as e:  # noqa: BLE001 - one element must not lose the layout
            self.warn(tr("{name} ({kind}) was not converted").format(name=name or "?", kind=t or "?") + f": {e}")
            return []
        items = [i for i in items if i is not None]
        for it in items:
            if name and not it.id():
                it.setId(name)
            if el.get("visible") is False:
                it.setVisibility(False)
            if el.get("rotation") and not isinstance(it, QgsLayoutItemLabel):
                it.setItemRotation(-float(el["rotation"]))
        return items

    # -- map frames --
    def _map(self, el):
        item = self.frames.get(el.get("name") or "") or QgsLayoutItemMap(self.layout)
        self.layout.addLayoutItem(item)
        self.layout.moveItemToTop(item)  # created early (for links): stack it where ArcGIS Pro has it
        r = self.rect(_points(el.get("frame")))
        self.place(item, r)
        uri = _norm(el.get("uRI") or (el.get("view") or {}).get("viewableObjectPath"))
        info = self.maps.get(uri)
        crs = info.get("crs") if info else None
        if crs is None or not crs.isValid():
            crs = self.project.crs()
        item.setCrs(crs)
        if info is None:
            self.warn(tr("map frame {name}: its map was not opened, it shows the current map")
                      .format(name=el.get("name") or "?"))
        elif info.get("layers") is not None:
            item.setLayers(info["layers"])
            item.setKeepLayerSet(True)
        cam = (el.get("view") or {}).get("camera") or {}
        done = False
        try:
            x, y, scale = float(cam["x"]), float(cam["y"]), float(cam["scale"])
            if scale > 0 and all(abs(v) < 1e300 for v in (x, y)) and r is not None:
                half_w = r[2] / 1000 * scale / 2
                half_h = r[3] / 1000 * scale / 2
                if crs.isGeographic():
                    half_w, half_h = half_w / 111320, half_h / 111320
                item.zoomToExtent(QgsRectangle(x - half_w, y - half_h, x + half_w, y + half_h))
                item.setScale(scale)
                done = True
        except (KeyError, TypeError, ValueError):
            pass
        if not done:
            ext = info.get("extent") if info else None
            if ext is not None:
                tr_ = QgsCoordinateTransform(ext.crs(), crs, self.project)
                item.zoomToExtent(tr_.transformBoundingBox(ext))
            else:
                item.zoomToExtent(item.extent() if not item.extent().isEmpty() else QgsRectangle(0, 0, 1, 1))
        if cam.get("heading"):
            item.setMapRotation(-float(cam["heading"]))
        self._frame_style(item, el, background=True)
        for g in el.get("grids") or []:
            self._grid(item, g, crs)
        return item

    def _grid(self, item, g, map_crs):
        t = g.get("type")
        if t not in ("CIMMeasuredGrid", "CIMGraticule", "CIMReferenceGrid", "CIMMGRSGrid"):
            self.warn(tr("{kind} map grids are not converted").format(kind=t or "?"))
            return
        if t in ("CIMReferenceGrid", "CIMMGRSGrid"):
            self.warn(tr("{kind} map grids are drawn as plain coordinate grids").format(kind=t))
        grid = QgsLayoutItemMapGrid(g.get("name") or tr("Grid"), item)
        if t == "CIMGraticule":
            crs = crs_from_spatial_reference(g.get("geographicCoordinateSystem"))
            if not crs.isValid():
                crs = QgsCoordinateReferenceSystem("EPSG:4326")
        else:
            crs = crs_from_spatial_reference(g.get("projectedCoordinateSystem"))
            if not crs.isValid():
                crs = map_crs
        ix = iy = None
        line = None
        for gl in g.get("gridLines") or []:
            iv = (gl.get("pattern") or {}).get("interval")
            orient = gl.get("gridLineOrientation")
            if iv:
                if orient == "NorthSouth":
                    ix = float(iv)
                elif orient == "EastWest":
                    iy = float(iv)
            sym = _sym(gl.get("symbol"))
            if line is None and sym.get("symbolLayers"):
                line = self.conv.line_symbol(sym)
        if not ix and not iy:
            ext = QgsCoordinateTransform(map_crs, crs, self.project).transformBoundingBox(item.extent())
            ix = iy = _nice(max(ext.width(), ext.height()) / 5)
        grid.setCrs(crs)
        grid.setIntervalX(ix or iy)
        grid.setIntervalY(iy or ix)
        grid.setEnabled(bool(g.get("isVisible", True)))
        grid.setStyle(QgsLayoutItemMapGrid.GridStyle.Solid)
        grid.setAnnotationEnabled(True)
        if line is not None:
            grid.setLineSymbol(line)
        item.grids().addGrid(grid)

    def _linked(self, el):
        mf = self.frames.get(el.get("mapFrame") or "")
        if mf is None and self.frames:
            mf = next(iter(self.frames.values()))
        return mf

    # -- surrounds --
    def _legend(self, el):
        leg = QgsLayoutItemLegend(self.layout)
        self.layout.addLayoutItem(leg)
        mf = self._linked(el)
        if mf is not None:
            leg.setLinkedMap(mf)
        leg.setTitle((el.get("title") or "") if el.get("showTitle") else "")
        ts = _sym(el.get("titleSymbol"))
        if ts:
            leg.rstyle(QgsLegendStyle.Style.Title).setTextFormat(self.conv._text_format(ts))
        leg.setColumnCount(max(1, int(el.get("columns", 1) or 1)))
        listed = []
        for it in el.get("items") or []:
            lyr = self.layers.get(_norm(it.get("layer")))
            if lyr is not None and it.get("isVisible", True):
                listed.append(lyr)
        if listed:
            _set_manual_legend(leg)
            root = leg.model().rootGroup()
            for node in list(root.findLayers()):
                if node.layer() not in listed:
                    node.parent().removeChildNode(node)
        elif el.get("items"):
            self.warn(tr("legend: its layers were not opened"))
        self._frame_style(leg, el)
        leg.setResizeToContents(True)
        leg.adjustBoxSize()
        r = self.rect(_points(el.get("frame")))
        if r is not None:
            leg.attemptMove(QgsLayoutPoint(r[0], r[1], MM))
        return leg

    def _north(self, el):
        pic = QgsLayoutItemPicture(self.layout)
        self.layout.addLayoutItem(pic)
        pic.setPicturePath(NORTH_ARROW_SVG, Qgis.PictureFormat.SVG)
        mf = self._linked(el)
        if mf is not None:
            pic.setLinkedMap(mf)
        pic.setNorthMode(QgsLayoutItemPicture.NorthMode.GridNorth if el.get("northType") == "GridNorth"
                         else QgsLayoutItemPicture.NorthMode.TrueNorth)
        self._frame_style(pic, el)
        self.place(pic, self.rect(_points(el.get("frame"))))
        return pic

    def _scalebar(self, el):
        t = el.get("type")
        sb = QgsLayoutItemScaleBar(self.layout)
        self.layout.addLayoutItem(sb)
        mf = self._linked(el)
        if mf is not None:
            sb.setLinkedMap(mf)
        sb.applyDefaultSettings()
        style = _SCALEBARS.get(t)
        if t == "CIMDoubleFillScaleBar":
            style = _FILL_STYLES.get(el.get("style"), "Single Box")
        elif t == "CIMScaleLine" and el.get("stepped"):
            style = "Stepped Line"
        if style is None:
            style = "Single Box"
            self.warn(tr("{kind} scale bars are drawn as boxes").format(kind=t))
        sb.setStyle(style)
        uw = int(((el.get("units") or {}).get("uwkid")) or 0)
        units = _DIST.get(uw) or (sb.guessUnits() if mf is not None else Qgis.DistanceUnit.Meters)
        sb.setUnits(units)
        sb.setUnitLabel(el.get("unitLabel") or QgsUnitTypes.toAbbreviatedString(units))
        sb.setNumberOfSegments(max(1, int(el.get("divisions", 2) or 2)))
        sb.setNumberOfSegmentsLeft(max(0, int(el.get("divisionsBeforeZero", 0) or 0)))
        sb.setNumberOfSubdivisions(max(1, int(el.get("subdivisions", 1) or 1)))
        ls = _sym(el.get("labelSymbol"))
        if ls:
            sb.setTextFormat(self.conv._text_format(ls))
        r = self.rect(_points(el.get("frame")))
        if r is not None:
            sb.setSegmentSizeMode(Qgis.ScaleBarSegmentSizeMode.FitWidth)
            sb.setMinimumBarWidth(max(r[2] * 0.5, 1))
            sb.setMaximumBarWidth(max(r[2], 2))
            sb.attemptMove(QgsLayoutPoint(r[0], r[1], MM))
        sb.update()
        return sb

    # -- graphics --
    def _graphic(self, el):
        g = el.get("graphic") or {}
        gt = g.get("type")
        if gt in ("CIMTextGraphic", "CIMParagraphTextGraphic"):
            return [self._text(el, g)]
        if gt == "CIMPictureGraphic":
            return [self._picture(el, g)]
        if gt == "CIMPolygonGraphic":
            return [self._polygon(el, g)]
        if gt == "CIMLineGraphic":
            return [self._line(el, g)]
        self.warn(tr("{name} ({kind}) was not converted").format(name=el.get("name") or "?", kind=gt or "?"))
        return []

    def _dyn(self, m):
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(0)))
        kind = attrs.get("type", "").lower()
        prop = attrs.get("property", "").lower()
        pre, post = attrs.get("preStr", ""), attrs.get("postStr", "")
        if kind == "date":
            out = "[% format_date(now(), 'dd/MM/yyyy') %]"
        elif kind == "time":
            out = "[% format_date(now(), 'HH:mm') %]"
        elif kind == "mapframe" and prop == "scale":
            frame = (attrs.get("name") or "").replace("'", "''")
            out = f"[% format_number(map_get(item_variables('{frame}'), 'map_scale'), 0) %]"
        elif kind in ("document", "project") and prop in ("name", "title", ""):
            out = "[% @project_title %]"
        elif kind == "layout" and prop in ("name", ""):
            out = "[% @layout_name %]"
        elif kind == "user":
            out = "[% @user_full_name %]"
        elif kind == "page" and "number" in prop:
            out = "[% @layout_page %]"
        else:
            self.warn(tr("dynamic text '{kind}' was removed").format(kind=kind or "?"))
            return ""
        return pre + out + post

    def text_content(self, text):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"<dyn\b[^>]*?/?>", self._dyn, text, flags=re.IGNORECASE)
        return re.sub(r"</?[A-Za-z][A-Za-z0-9]*(\s[^<>]*)?/?>", "", text)

    def _text(self, el, g):
        ts = _sym(g.get("symbol"))
        lbl = QgsLayoutItemLabel(self.layout)
        self.layout.addLayoutItem(lbl)
        lbl.setText(self.text_content(g.get("text")))
        lbl.setTextFormat(self.conv._text_format(ts))
        lbl.setMarginX(0)
        lbl.setMarginY(0)
        h = ts.get("horizontalAlignment", "Left")
        v = ts.get("verticalAlignment", "Bottom")
        lbl.setHAlign({"Center": Qt.AlignmentFlag.AlignHCenter, "Right": Qt.AlignmentFlag.AlignRight,
                       "Justify": Qt.AlignmentFlag.AlignJustify}.get(h, Qt.AlignmentFlag.AlignLeft))
        shape = g.get("shape") or {}
        if "x" in shape and "y" in shape:
            lbl.adjustSizeToText()
            col = {"Center": 1, "Right": 2}.get(h, 0)
            row = {"Top": 0, "Center": 1}.get(v, 2)
            rp = [[lbl.ReferencePoint.UpperLeft, lbl.ReferencePoint.UpperMiddle, lbl.ReferencePoint.UpperRight],
                  [lbl.ReferencePoint.MiddleLeft, lbl.ReferencePoint.Middle, lbl.ReferencePoint.MiddleRight],
                  [lbl.ReferencePoint.LowerLeft, lbl.ReferencePoint.LowerMiddle, lbl.ReferencePoint.LowerRight]]
            lbl.setReferencePoint(rp[row][col])
            p = self.point(float(shape["x"]), float(shape["y"]))
            lbl.attemptMove(QgsLayoutPoint(p.x(), p.y(), MM))
        else:
            self.place(lbl, self.rect(_points(shape) or _points(el.get("frame"))))
            lbl.setVAlign({"Top": Qt.AlignmentFlag.AlignTop, "Center": Qt.AlignmentFlag.AlignVCenter}
                          .get(v, Qt.AlignmentFlag.AlignBottom))
        if el.get("rotation"):
            lbl.setItemRotation(-float(el["rotation"]))
        return lbl

    def _picture(self, el, g):
        pic = QgsLayoutItemPicture(self.layout)
        self.layout.addLayoutItem(pic)
        src = g.get("sourceURL") or ""
        data = None if (src and os.path.isfile(src)) else self.binary(g.get("referenceURI"))
        if src and os.path.isfile(src):
            pic.setPicturePath(src)
        elif data:
            svg = data.lstrip()[:5] in (b"<svg ", b"<?xml") or data.lstrip().startswith(b"<svg")
            pic.setPicturePath("base64:" + base64.b64encode(data).decode(),
                               Qgis.PictureFormat.SVG if svg else Qgis.PictureFormat.Raster)
        else:
            self.warn(tr("picture {name}: the image is not in the file").format(name=el.get("name") or "?"))
        pic.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
        box = g.get("box")
        if box:
            pts = [(box["xmin"], box["ymin"]), (box["xmax"], box["ymax"])]
        else:
            pts = _points(g.get("shape")) or _points(el.get("frame"))
        self._frame_style(pic, {"graphicFrame": g.get("frame") or el.get("graphicFrame")})
        self.place(pic, self.rect(pts))
        return pic

    def _polygon(self, el, g):
        geom = g.get("polygon") or g.get("shape") or {}
        sym = self.conv.fill_symbol(_sym(g.get("symbol")))
        pts = _points(geom)
        r = self.rect(pts)
        ring = (geom.get("rings") or [[]])[0]
        axis = len(ring) == 5 and len({round(p[0], 6) for p in ring}) == 2 and len({round(p[1], 6) for p in ring}) == 2
        if "curveRings" in geom or axis:
            item = QgsLayoutItemShape(self.layout)
            item.setShapeType(QgsLayoutItemShape.Shape.Ellipse if "curveRings" in geom
                              else QgsLayoutItemShape.Shape.Rectangle)
            self.layout.addLayoutItem(item)
            self.place(item, r)
        else:
            item = QgsLayoutItemPolygon(QPolygonF([self.point(x, y) for x, y in pts]), self.layout)
            self.layout.addLayoutItem(item)
        item.setSymbol(sym)
        return item

    def _line(self, el, g):
        geom = g.get("line") or g.get("shape") or {}
        item = QgsLayoutItemPolyline(QPolygonF([self.point(x, y) for x, y in _points(geom)]), self.layout)
        self.layout.addLayoutItem(item)
        item.setSymbol(self.conv.line_symbol(_sym(g.get("symbol"))))
        return item


# =============================================================================
#  print layout -> CIM
# =============================================================================
def _text_symbol_default(size=10, family="Tahoma"):
    return {"type": "CIMTextSymbol", "fontFamilyName": family, "fontStyleName": "Regular", "fontType": "Unspecified",
            "height": size, "horizontalAlignment": "Left", "verticalAlignment": "Bottom",
            "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [
                {"type": "CIMSolidFill", "enable": True, "color": {"type": "CIMRGBColor", "values": [0, 0, 0, 100]}}]}}


class LayoutWriter:
    """QgsPrintLayout -> CIMLayout dict + embedded pictures."""

    def __init__(self, project, map_uri, map_crs, layer_uris):
        self.project = project or QgsProject.instance()
        self.map_uri = map_uri
        self.map_crs = map_crs
        self.layer_uris = layer_uris  # layer id -> uri
        self.sw = SymbolWriter()
        self.binaries = {}
        self.warnings = []

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    def convert(self, layout):
        self.layout = layout
        self.names = {}
        self.map_names = {}
        pages = layout.pageCollection()
        page = pages.page(0)
        if page is None:
            raise ValueError("empty layout")
        pr = page.rect()
        self.W, self.H = pr.width(), pr.height()
        if pages.pageCount() > 1:
            self.warn(tr("only the first page is written"))
        items = [i for i in layout.items() if isinstance(i, QgsLayoutItem)
                 and not isinstance(i, (QgsLayoutItemPage, QgsLayoutItemGroup))]
        items.sort(key=lambda i: i.zValue(), reverse=ELEMENTS_TOP_FIRST)
        for it in items:  # names first: legends and scale bars refer to their map frame by name
            if isinstance(it, QgsLayoutItemMap):
                self.map_names[it.uuid()] = self._name(it)
        elements = []
        for it in items:
            if it.page() != 0:
                self.warn(tr("{name}: items on other pages are not written").format(name=self._label(it)))
                continue
            try:
                el = self._item(it)
            except Exception as e:  # noqa: BLE001
                self.warn(tr("{name} was not written").format(name=self._label(it)) + f": {e}")
                el = None
            if el is not None:
                elements.append(el)
        for w in self.sw.warnings:
            self.warn(w)
        cim = {"type": "CIMLayout", "name": layout.name(), "uRI": "", "useSourceMetadata": True,
               "elements": elements,
               "page": {"type": "CIMPage", "height": round(self.H, 4), "width": round(self.W, 4),
                        "units": {"uwkid": 1025}, "showRulers": True, "showGuides": True,
                        "smallestRulerDivision": 0.1, "showMargin": True},
               "colorModel": "RGB", "rGBColorProfile": "sRGB IEC61966-2.1",
               "cMYKColorProfile": "U.S. Web Coated (SWOP) v2"}
        return cim

    # -- helpers --
    @staticmethod
    def _label(it):
        return it.displayName() or it.id() or "?"

    def _name(self, it):
        if it.uuid() in self.names:
            return self.names[it.uuid()]
        base = it.id() or it.displayName() or "Element"
        name, k = base, 2
        while name in self.names.values():
            name, k = f"{base} {k}", k + 1
        self.names[it.uuid()] = name
        return name

    def _page_pt(self, scene_pt):
        return [round(scene_pt.x(), 4), round(self.H - scene_pt.y(), 4)]

    def _base(self, cim_type, it):
        r = it.rect()
        c = it.mapToScene(r.center())
        w, h = r.width(), r.height()
        x0, x1 = c.x() - w / 2, c.x() + w / 2
        y0, y1 = self.H - (c.y() + h / 2), self.H - (c.y() - h / 2)
        ring = [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]
        d = {"type": cim_type, "anchor": "BottomLeftCorner", "name": self._name(it), "visible": bool(it.isVisible()),
             "rotationCenter": {"x": round(c.x(), 4), "y": round(self.H - c.y(), 4)},
             "frame": {"rings": [[[round(a, 4), round(b, 4)] for a, b in ring]]},
             "graphicFrame": self._graphic_frame(it)}
        if cim_type == "CIMGraphicElement":  # graphics carry their own geometry (as ArcGIS Pro writes them)
            d["_ring"] = d.pop("frame")["rings"][0]
            d.pop("graphicFrame")
        if it.itemRotation():
            d["rotation"] = -float(it.itemRotation())
        return d

    def _graphic_frame(self, it):
        gf = {"type": "CIMGraphicFrame"}
        if it.frameEnabled():
            w = it.frameStrokeWidth()
            mm = w.length() if w.units() == MM else w.length() * {Qgis.LayoutUnit.Centimeters: 10,
                                                                    Qgis.LayoutUnit.Points: 25.4 / 72}.get(w.units(), 1)
            gf["borderSymbol"] = ref({"type": "CIMLineSymbol", "symbolLayers": [
                {"type": "CIMSolidStroke", "enable": True, "capStyle": "Round", "joinStyle": "Round",
                 "lineStyle3D": "Strip", "miterLimit": 10, "width": round(mm * MM_TO_PT, 3),
                 "color": cim_color(it.frameStrokeColor())}]})
        else:
            gf["borderSymbol"] = ref({"type": "CIMLineSymbol", "symbolLayers": [
                {"type": "CIMSolidStroke", "enable": True, "width": 1, "color": cim_color(QColor(0, 0, 0, 0))}]})
        if it.hasBackground():
            gf["backgroundSymbol"] = ref({"type": "CIMPolygonSymbol", "symbolLayers": [
                {"type": "CIMSolidFill", "enable": True, "color": cim_color(it.backgroundColor())}]})
        return gf

    def _item(self, it):
        d = self._item_cim(it)
        if d is not None:
            d.pop("_ring", None)
        return d

    def _item_cim(self, it):
        if isinstance(it, QgsLayoutItemMap):
            return self._map(it)
        if isinstance(it, QgsLayoutItemLegend):
            return self._legend(it)
        if isinstance(it, QgsLayoutItemScaleBar):
            return self._scalebar(it)
        if isinstance(it, QgsLayoutItemPicture):
            return self._picture(it)
        if isinstance(it, QgsLayoutItemLabel):
            return self._text(it)
        if isinstance(it, QgsLayoutItemShape):
            return self._shape(it)
        if isinstance(it, (QgsLayoutItemPolygon, QgsLayoutItemPolyline)):
            return self._nodes(it)
        self.warn(tr("{name} ({kind}) was not written").format(name=self._label(it),
                                                                kind=type(it).__name__.replace("QgsLayoutItem", "")))
        return None

    def _map_frame_name(self, map_item):
        if map_item is not None and map_item.uuid() in self.map_names:
            return self.map_names[map_item.uuid()]
        return next(iter(self.map_names.values()), "")

    # -- elements --
    def _map(self, it):
        d = self._base("CIMMapFrame", it)
        ext = it.extent()
        center = ext.center()
        crs = it.crs()
        if self.map_crs is not None and self.map_crs.isValid() and crs.isValid() and crs != self.map_crs:
            center = QgsCoordinateTransform(crs, self.map_crs, self.project).transform(center)
        cam = {"type": "CIMViewCamera", "pitch": -90, "scale": round(it.scale(), 4), "x": center.x(),
               "y": center.y(), "z": "NaN", "viewportHeight": -1, "viewportWidth": -1}
        if it.mapRotation():
            cam["heading"] = -float(it.mapRotation())
        d.update({"uRI": self.map_uri,
                  "view": {"type": "CIMMapView", "viewableObjectPath": self.map_uri, "viewingMode": "Map",
                           "camera": cam, "verticalExaggerationScaleFactor": 1},
                  "autoCamera": {"type": "CIMAutoCamera", "autoCameraType": "None", "marginType": "Percent",
                                 "source": "None"},
                  "extentIndicatorsExpanded": True, "mapGridsExpanded": True})
        if it.keepLayerSet() or it.followVisibilityPreset():
            self.warn(tr("map frame {name}: ArcGIS Pro shows all the layers of the map in it")
                      .format(name=d["name"]))
        grids = []
        for g in it.grids().asList():
            if not g.enabled():
                continue
            gcrs = g.crs() if g.crs().isValid() else crs
            if g.units() != QgsLayoutItemMapGrid.GridUnit.MapUnit:
                self.warn(tr("map grids measured on the page are written with map intervals"))
            line = self.sw.line_symbol(g.lineSymbol()) if g.lineSymbol() else None
            lines = []
            for orient, iv in (("NorthSouth", g.intervalX()), ("EastWest", g.intervalY())):
                gl = {"type": "CIMGridLine", "name": orient, "elementType": "Line", "gridLineOrientation": orient,
                      "pattern": {"type": "CIMGridPattern", "interval": iv}}
                if line:
                    gl["symbol"] = ref(line)
                lines.append(gl)
            gd = {"type": "CIMGraticule" if gcrs.isGeographic() else "CIMMeasuredGrid", "name": g.name(),
                  "isVisible": True, "gridLines": lines, "useMapClipShape": True}
            sr = spatial_reference(gcrs)
            if sr:
                gd["geographicCoordinateSystem" if gcrs.isGeographic() else "projectedCoordinateSystem"] = sr
            grids.append(gd)
        if grids:
            d["grids"] = grids
        return d

    def _legend(self, it):
        d = self._base("CIMLegend", it)
        w, h = it.rect().width(), it.rect().height()
        if w < 1 or h < 1:  # a legend never drawn yet has no size: ArcGIS Pro would show it empty
            try:
                ctx = QgsRenderContext.fromQPainter(None)
                ctx.setScaleFactor(self.layout.renderContext().dpi() / 25.4)
                size = QgsLegendRenderer(it.model(), it.legendSettings()).minimumSize(ctx)
                w, h = max(w, size.width(), 10), max(h, size.height(), 10)
            except Exception:  # noqa: BLE001
                w, h = max(w, 40), max(h, 60)
            tl = it.mapToScene(QPointF(0, 0))
            x0, y1 = round(tl.x(), 4), round(self.H - tl.y(), 4)
            x1, y0 = round(x0 + w, 4), round(y1 - h, 4)
            d["frame"] = {"rings": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]]}
        d["anchor"] = "TopLeftCorner"
        title_fmt = it.rstyle(QgsLegendStyle.Style.Title).textFormat()
        label_ts = self.sw.text_symbol(it.rstyle(QgsLegendStyle.Style.SymbolLabel).textFormat())
        label_ts.update(horizontalAlignment="Left", verticalAlignment="Center")
        name_ts = self.sw.text_symbol(it.rstyle(QgsLegendStyle.Style.Subgroup).textFormat())
        name_ts.update(horizontalAlignment="Left", verticalAlignment="Bottom")
        items = []
        for node in it.model().rootGroup().findLayers():
            lyr = node.layer()
            if lyr is None or lyr.id() not in self.layer_uris or not lyr.isSpatial():
                continue
            items.append({"type": "CIMHorizontalLegendItem", "name": node.name() or lyr.name(),
                          "layer": self.layer_uris[lyr.id()], "showLayerName": True, "showHeading": True,
                          "showLabels": True, "showDescription": False, "isVisible": True,
                          "layerNameSymbol": ref(name_ts), "headingSymbol": ref(name_ts),
                          "labelSymbol": ref(label_ts), "descriptionSymbol": ref(label_ts),
                          "autoVisibility": False, "keepTogetherOption": "None", "manualColumn": -1,
                          "arrangement": "PatchLabelDescription"})
        title_ts = self.sw.text_symbol(title_fmt)
        title_ts.update(horizontalAlignment="Left", verticalAlignment="Bottom")
        d.update({"mapFrame": self._map_frame_name(it.linkedMap()), "autoAdd": _legend_auto(it),
                  "autoFonts": True, "autoReorder": False, "autoVisibility": False, "title": it.title(),
                  "showTitle": bool(it.title()), "titleSymbol": ref(title_ts), "columns": it.columnCount(),
                  "fittingStrategy": "AdjustColumnsAndSize", "makeColumnsSameWidth": True, "items": items,
                  "defaultPatchHeight": 12, "defaultPatchWidth": 24, "groupGap": 5, "headingGap": 5,
                  "horizontalItemGap": 5, "itemGap": 5, "classGap": 5, "layerNameGap": 5, "patchGap": 5,
                  "textGap": 5, "titleGap": 5, "minFontSize": 4, "scaleSymbols": True})
        return d

    def _scalebar(self, it):
        style = it.style()
        if style == "Numeric":
            d = self._base("CIMGraphicElement", it)
            ts = self.sw.text_symbol(it.textFormat())
            r = it.rect()
            p = it.mapToScene(QPointF(0, r.height()))
            frame = self._map_frame_name(it.linkedMap()).replace('"', "'")
            d["graphic"] = {"type": "CIMTextGraphic", "symbol": ref(dict(ts, horizontalAlignment="Left",
                                                                           verticalAlignment="Bottom")),
                            "text": f'<dyn type="mapFrame" name="{frame}" property="scale" preStr="1:"/>',
                            "shape": {"x": round(p.x(), 4), "y": round(self.H - p.y(), 4)},
                            "placement": "Unspecified", "blendingMode": "Alpha"}
            return d
        fill_style = _FILL_STYLES_BACK.get(style)
        cim_type = "CIMDoubleFillScaleBar" if fill_style else "CIMScaleLine"
        if not fill_style and not style.startswith("Line Ticks") and style != "Stepped Line":
            self.warn(tr("{style} scale bars are written as scale lines").format(style=style))
        d = self._base(cim_type, it)
        ts = self.sw.text_symbol(it.textFormat())
        ts.update(horizontalAlignment="Left", verticalAlignment="Bottom")
        units = it.units()
        line = {"type": "CIMLineSymbol", "symbolLayers": [
            {"type": "CIMSolidStroke", "enable": True, "capStyle": "Butt", "joinStyle": "Miter",
             "lineStyle3D": "Strip", "miterLimit": 10, "width": 1, "color": cim_color(QColor("black"))}]}
        d.update({"mapFrame": self._map_frame_name(it.linkedMap()),
                  "units": {"uwkid": _DIST_BACK.get(units, 9001)}, "unitLabel": it.unitLabel(),
                  "division": it.unitsPerSegment(), "divisions": it.numberOfSegments(),
                  "divisionsBeforeZero": it.numberOfSegmentsLeft(), "subdivisions": it.numberOfSubdivisions(),
                  "fittingStrategy": "AdjustDivision", "labelFrequency": "Divisions", "labelGap": 3,
                  "labelPosition": "Above", "labelSymbol": ref(ts), "unitLabelGap": 3,
                  "unitLabelPosition": "AfterLabels", "unitLabelSymbol": ref(ts),
                  "numberFormat": {"type": "CIMNumericFormat", "alignmentOption": "esriAlignLeft",
                                   "alignmentWidth": 12, "roundingOption": "esriRoundNumberOfDecimals",
                                   "roundingValue": 2},
                  "barHeight": round(it.height() * MM_TO_PT, 3), "divisionMarkHeight": 7,
                  "subdivisionMarkHeight": 5, "midpointMarkHeight": 5, "markPosition": "Above",
                  "divisionMarkSymbol": ref(line), "subdivisionMarkSymbol": ref(line)})
        if fill_style:
            stroke = line["symbolLayers"][0]

            def fill(c):
                return ref({"type": "CIMPolygonSymbol", "symbolLayers": [
                    stroke, {"type": "CIMSolidFill", "enable": True, "color": cim_color(c)}]})

            c1 = it.fillSymbol().color() if it.fillSymbol() else QColor("black")
            c2 = it.alternateFillSymbol().color() if it.alternateFillSymbol() else QColor("white")
            d.update({"style": fill_style, "fillSymbol1": fill(c1), "fillSymbol2": fill(c2)})
        else:
            d.update({"lineSymbol": ref(line), "markFrequency": "Divisions"})
            if style == "Stepped Line":
                d["stepped"] = True
        return d

    def _picture_bytes(self, it):
        path = it.evaluatedPath() if hasattr(it, "evaluatedPath") else it.picturePath()
        path = path or it.picturePath()
        if path.startswith("base64:"):
            return base64.b64decode(path[7:]), None
        if path.lower().endswith(".svg") or path.startswith(":/") and path.lower().endswith(".svg"):
            img, _ = QgsApplication.svgCache().svgAsImage(path, 600, it.svgFillColor(), it.svgStrokeColor(),
                                                          it.svgStrokeWidth(), 1)
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            return bytes(ba), path
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read(), path
        return None, path

    def _picture(self, it):
        if it.linkedMap() is not None or "north" in (it.picturePath() or "").lower():
            d = self._base("CIMMarkerNorthArrow", it)
            d.update({"anchor": "CenterPoint", "lockedAspectRatio": True,
                      "mapFrame": self._map_frame_name(it.linkedMap()),
                      "northType": "GridNorth" if it.northMode() == QgsLayoutItemPicture.NorthMode.GridNorth
                      else "TrueNorth",
                      "pointSymbol": ref({"type": "CIMPointSymbol", "symbolLayers": [
                          {"type": "CIMCharacterMarker", "enable": True, "anchorPointUnits": "Relative",
                           "dominantSizeAxis3D": "Z", "size": 24, "billboardMode3D": "FaceNearPlane",
                           "characterIndex": 78, "fontFamilyName": "Courier New", "fontStyleName": "Regular",
                           "fontType": "Unspecified", "scaleX": 1, "respectFrame": True,
                           "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [
                               {"type": "CIMSolidFill", "enable": True, "color": cim_color(QColor("black"))}]}},
                          {"type": "CIMCharacterMarker", "enable": True, "anchorPointUnits": "Relative",
                           "dominantSizeAxis3D": "Z", "size": 72, "billboardMode3D": "FaceNearPlane",
                           "characterIndex": 8593, "fontFamilyName": "Courier New", "fontStyleName": "Regular",
                           "fontType": "Unspecified", "scaleX": 1, "respectFrame": True,
                           "symbol": {"type": "CIMPolygonSymbol", "symbolLayers": [
                               {"type": "CIMSolidFill", "enable": True, "color": cim_color(QColor("black"))}]}}],
                          "haloSize": 1, "scaleX": 1, "angleAlignment": "Display"})})
            return d
        data, path = self._picture_bytes(it)
        if not data:
            self.warn(tr("picture {name}: the image could not be read").format(name=self._label(it)))
            return None
        ext = "png" if data[:4] == b"\x89PNG" else "jpg" if data[:2] == b"\xff\xd8" else "png"
        uri = f"CIMPATH=Pictures/{hashlib.md5(data).hexdigest()}.{ext}"
        self.binaries[uri] = data
        d = self._base("CIMGraphicElement", it)
        d["lockedAspectRatio"] = True
        ring = d["_ring"]
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        g = {"type": "CIMPictureGraphic", "blendingMode": "Alpha", "placement": "Unspecified",
             "box": {"xmin": min(xs), "ymin": min(ys), "xmax": max(xs), "ymax": max(ys)},
             "referenceURI": uri, "shape": {"rings": [ring]}}
        if path and os.path.isfile(path) and not path.lower().endswith(".svg"):
            g["sourceURL"] = os.path.abspath(path)
        d["graphic"] = g
        return d

    def _text(self, it):
        d = self._base("CIMGraphicElement", it)
        raw = it.text()
        text = it.currentText()
        if it.mode() == QgsLayoutItemLabel.Mode.ModeHtml:
            doc = QTextDocument()
            doc.setHtml(text)
            text = doc.toPlainText()
            self.warn(tr("HTML text is written as plain text"))
        if "[%" in raw:
            self.warn(tr("{name}: dynamic text is written with its current value").format(name=d["name"]))
        ts = self.sw.text_symbol(it.textFormat())
        ha = it.hAlign()
        h = "Center" if ha & Qt.AlignmentFlag.AlignHCenter else "Right" if ha & Qt.AlignmentFlag.AlignRight else "Left"
        ts.update(horizontalAlignment=h, verticalAlignment="Top")
        r = it.rect()
        mx, my = it.marginX(), it.marginY()
        x = {"Left": mx, "Center": r.width() / 2, "Right": r.width() - mx}[h]
        p = it.mapToScene(QPointF(x, max(my, 0)))
        d["graphic"] = {"type": "CIMTextGraphic", "symbol": ref(ts), "text": text,
                        "shape": {"x": round(p.x(), 4), "y": round(self.H - p.y(), 4)},
                        "placement": "Unspecified", "blendingMode": "Alpha"}
        d["lockedAspectRatio"] = True
        return d

    def _shape(self, it):
        d = self._base("CIMGraphicElement", it)
        r = it.rect()
        st = it.shapeType()
        if st == QgsLayoutItemShape.Shape.Ellipse:
            pts = [QPointF(r.center().x() + r.width() / 2 * math.cos(math.radians(a)),
                           r.center().y() + r.height() / 2 * math.sin(math.radians(a))) for a in range(0, 360, 10)]
        elif st == QgsLayoutItemShape.Shape.Triangle:
            pts = [QPointF(r.width() / 2, 0), QPointF(r.width(), r.height()), QPointF(0, r.height())]
        else:
            pts = [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]
            if it.cornerRadius().length():
                self.warn(tr("rounded corners are written square"))
        ring = [self._page_pt(it.mapToScene(p)) for p in pts]
        ring.append(ring[0])
        d["graphic"] = {"type": "CIMPolygonGraphic", "symbol": ref(self.sw.fill_symbol(it.symbol())),
                        "blendingMode": "Alpha", "placement": "Unspecified", "polygon": {"rings": [ring]}}
        return d

    def _nodes(self, it):
        d = self._base("CIMGraphicElement", it)
        pts = [self._page_pt(it.mapToScene(p)) for p in it.nodes()]
        if isinstance(it, QgsLayoutItemPolygon):
            pts.append(pts[0])
            d["graphic"] = {"type": "CIMPolygonGraphic", "symbol": ref(self.sw.fill_symbol(it.symbol())),
                            "blendingMode": "Alpha", "placement": "Unspecified", "polygon": {"rings": [pts]}}
        else:
            if it.startMarker() != QgsLayoutItemPolyline.MarkerMode.NoMarker or \
                    it.endMarker() != QgsLayoutItemPolyline.MarkerMode.NoMarker:
                self.warn(tr("arrow heads on lines are not written"))
            d["graphic"] = {"type": "CIMLineGraphic", "symbol": ref(self.sw.line_symbol(it.symbol())),
                            "blendingMode": "Alpha", "placement": "Unspecified", "line": {"paths": [pts]}}
        return d
