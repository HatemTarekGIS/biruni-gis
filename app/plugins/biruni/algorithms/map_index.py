"""Map book index: every name (street, place, parcel) with the pages and grid squares where it appears."""
import html
import os
import re

from qgis.core import (Qgis, QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber, QgsRectangle,
                       QgsSpatialIndex)
from qgis.PyQt.QtCore import QMetaType

from .. import i18n
from ..arabic_text import normalise
from ..i18n import tr
from .base import BiruniAlgorithm


def column_letters(k):
    """0 → A, 25 → Z, 26 → AA."""
    s = ""
    k += 1
    while k:
        k, r = divmod(k - 1, 26)
        s = chr(65 + r) + s
    return s


def grid_cells(rect, columns, rows):
    """Cells of a grid over ``rect``: letters for columns (left to right), numbers for rows (top to bottom).
    Returns [(ref, QgsRectangle)]."""
    w, h = rect.width() / columns, rect.height() / rows
    cells = []
    for r in range(rows):
        for c in range(columns):
            x0 = rect.xMinimum() + c * w
            y1 = rect.yMaximum() - r * h
            cells.append((f"{column_letters(c)}{r + 1}", QgsRectangle(x0, y1 - h, x0 + w, y1)))
    return cells


def auto_grid(rect, target=8):
    """Columns and rows for roughly square cells, about ``target`` along the longer side."""
    if rect.width() >= rect.height():
        cols = target
        rows = max(1, round(target * rect.height() / max(rect.width(), 1e-12)))
    else:
        rows = target
        cols = max(1, round(target * rect.width() / max(rect.height(), 1e-12)))
    return cols, rows


def _natural(v):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(v))]


def _is_arabic(text):
    return any("؀" <= ch <= "ۿ" for ch in str(text))


def sort_key(name, strip_prefixes=True, arabic_first=True):
    """Arabic-aware sorting: hamza forms, ta marbuta and alef maqsura unified, the article "ال" and words like
    "شارع / حي" ignored (optional), numbers in natural order; Arabic names before or after Latin ones."""
    key = normalise(name, strip_prefixes=strip_prefixes) or str(name).lower()
    group = 0 if _is_arabic(name) == arabic_first else 1
    return (group, _natural(key))


def ref_text(refs):
    """{page: {cells}} → "12: B3, C3 · 13: A1" (pages natural order, cells by column then row)."""
    parts = []
    for page in sorted(refs, key=_natural):
        cells = sorted(refs[page], key=lambda c: (re.sub(r"\d", "", c), _natural(c)))
        if page == "":
            parts.append(", ".join(cells))
        elif cells:
            parts.append(f"{page}: {', '.join(cells)}")
        else:
            parts.append(str(page))
    return " · ".join(parts)


class MapIndex(BiruniAlgorithm):
    ID = "mapindex"
    NAME = "Map book index (names → pages and grid squares)"
    GROUP = "Cartography"
    GROUP_ID = "cartography"
    HELP = ("Makes the index at the back of a map book or a large plan: every street, place or parcel name with the "
            "page numbers and grid squares (A1, B3…) where it appears. Pages come from an atlas coverage layer (one "
            "polygon per page); without one, a lettered grid over the whole area is used. Names written in several "
            "pieces are listed once. Sorting understands Arabic (hamza forms, ta marbuta, the article “ال” and words "
            "like “شارع” or “حي” can be ignored). Results: a table, an Excel sheet with a heading per letter, an "
            "HTML page to print, and the grid as a layer to draw on the map.")

    INPUT, FIELD, PAGES, PAGE_FIELD = "INPUT", "FIELD", "PAGES", "PAGE_FIELD"
    USE_GRID, COLUMNS, ROWS, STRIP = "USE_GRID", "COLUMNS", "ROWS", "STRIP"
    OUTPUT, GRID, XLSX, HTML = "OUTPUT", "GRID", "XLSX", "HTML"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Named features (streets, places, parcels…)"), [Qgis.ProcessingSourceType.VectorAnyGeometry]))
        self.addParameter(QgsProcessingParameterField(
            self.FIELD, tr("Name field"), parentLayerParameterName=self.INPUT))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PAGES, tr("Pages (atlas coverage layer, optional)"), [Qgis.ProcessingSourceType.VectorPolygon],
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.PAGE_FIELD, tr("Page number field (empty = 1, 2, 3… in layer order)"),
            parentLayerParameterName=self.PAGES, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.USE_GRID, tr("Add grid squares (A1, B2…)"), True))
        self.addParameter(QgsProcessingParameterNumber(
            self.COLUMNS, tr("Grid columns (0 = automatic)"), Qgis.ProcessingNumberParameterType.Integer, 0,
            minValue=0, maxValue=200))
        self.addParameter(QgsProcessingParameterNumber(
            self.ROWS, tr("Grid rows (0 = automatic)"), Qgis.ProcessingNumberParameterType.Integer, 0,
            minValue=0, maxValue=200))
        self.addParameter(QgsProcessingParameterBoolean(
            self.STRIP, tr("Sort without “ال” and words like “شارع”, “حي”"), True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Index"), Qgis.ProcessingSourceType.Vector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.GRID, tr("Grid squares"), Qgis.ProcessingSourceType.VectorPolygon, optional=True,
            createByDefault=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.XLSX, tr("Index as Excel"), "Excel (*.xlsx)", optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.HTML, tr("Index as a printable page (HTML)"), "HTML (*.html)", optional=True,
            createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        name_field = self.parameterAsString(parameters, self.FIELD, context)
        ni = source.fields().indexOf(name_field)
        if ni < 0:
            raise QgsProcessingException(tr("Choose the name field."))
        pages_src = self.parameterAsSource(parameters, self.PAGES, context)
        page_field = self.parameterAsString(parameters, self.PAGE_FIELD, context)
        use_grid = self.parameterAsBoolean(parameters, self.USE_GRID, context)
        cols = self.parameterAsInt(parameters, self.COLUMNS, context)
        rows = self.parameterAsInt(parameters, self.ROWS, context)
        strip = self.parameterAsBoolean(parameters, self.STRIP, context)
        crs = source.sourceCrs()

        # pages: [(label, geometry in the features' CRS)]
        pages = []
        if pages_src is not None:
            xf = QgsCoordinateTransform(pages_src.sourceCrs(), crs, context.transformContext())
            pi = pages_src.fields().indexOf(page_field) if page_field else -1
            for k, pf in enumerate(pages_src.getFeatures(), start=1):
                g = QgsGeometry(pf.geometry())
                if g.isEmpty():
                    continue
                g.transform(xf)
                label = pf.attributes()[pi] if pi >= 0 else k
                if isinstance(label, float) and label.is_integer():
                    label = int(label)
                pages.append((str(label), g))
            if not pages:
                raise QgsProcessingException(tr("The pages layer has no polygons."))

        feats = []
        extent = QgsRectangle()
        for f in source.getFeatures():
            g = f.geometry()
            name = f.attributes()[ni]
            if g.isEmpty() or name is None or str(name).strip() == "":
                continue
            feats.append((str(name).strip(), g))
            extent.combineExtentWith(g.boundingBox())
        if not feats:
            raise QgsProcessingException(tr("No named features were found."))

        # cells: per page (grid inside each page's frame) or one grid over everything
        cell_sets = []  # (page label, [(ref, rect)])
        if pages:
            for label, g in pages:
                box = g.boundingBox()
                c, r = (cols, rows) if cols and rows else auto_grid(box, 6)
                cell_sets.append((label, grid_cells(box, cols or c, rows or r) if use_grid else []))
        else:
            box = QgsRectangle(extent)
            if box.width() == 0 or box.height() == 0:
                box.grow(max(box.width(), box.height(), 1.0) * 0.5)
            c, r = auto_grid(box, 8)
            cell_sets.append(("", grid_cells(box, cols or c, rows or r)))

        # cell geometries kept alive for the whole run (constGet() of a temporary geometry would dangle)
        cell_geoms = [[(ref, rect, QgsGeometry.fromRect(rect)) for ref, rect in cells] for _label, cells in cell_sets]
        page_index = QgsSpatialIndex()
        for k, (_label, g) in enumerate(pages):
            page_index.addFeature(k, g.boundingBox())

        entries = {}   # key -> {"names": {spelling: count}, "refs": {page: set(cells)}, "count": n}
        for n, (name, g) in enumerate(feats):
            if feedback.isCanceled():
                return {}
            key = normalise(name) or name
            e = entries.setdefault(key, {"names": {}, "refs": {}, "count": 0})
            e["names"][name] = e["names"].get(name, 0) + 1
            e["count"] += 1
            engine = QgsGeometry.createGeometryEngine(g.constGet())
            engine.prepareGeometry()
            targets = [(k, pages[k]) for k in page_index.intersects(g.boundingBox())] if pages else [(0, ("", None))]
            for k, (label, pg) in targets:
                if pg is not None and not engine.intersects(pg.constGet()):
                    continue
                hit = e["refs"].setdefault(label, set())
                for ref, rect, cell_geom in cell_geoms[k]:
                    if rect.intersects(g.boundingBox()) and engine.intersects(cell_geom.constGet()):
                        hit.add(ref)
            feedback.setProgress(80 * (n + 1) / len(feats))

        arabic_first = i18n.is_rtl()
        rows_out = []
        for key, e in entries.items():
            refs = e["refs"]
            if pages and not refs:
                continue  # outside every page
            name = max(e["names"].items(), key=lambda kv: (kv[1], -len(kv[0])))[0]  # the usual spelling
            sk = sort_key(name, strip, arabic_first)
            letter_src = normalise(name, strip_prefixes=strip) or name
            rows_out.append({
                "name": name, "pages": ", ".join(sorted((p for p in refs if p), key=_natural)),
                "squares": ref_text(refs) if use_grid else "",
                "refs": ref_text(refs) if use_grid else ", ".join(sorted((p for p in refs if p), key=_natural)),
                "count": e["count"], "letter": letter_src[:1].upper(), "sort": sk})
        rows_out.sort(key=lambda r: r["sort"])

        of = QgsFields()
        for fname, ftype in (("name", QMetaType.Type.QString), ("pages", QMetaType.Type.QString),
                             ("squares", QMetaType.Type.QString), ("index_entry", QMetaType.Type.QString),
                             ("parts", QMetaType.Type.Int), ("letter", QMetaType.Type.QString),
                             ("sort_order", QMetaType.Type.Int)):
            of.append(QgsField(fname, ftype))
        sink, oid = self.parameterAsSink(parameters, self.OUTPUT, context, of, Qgis.WkbType.NoGeometry, crs)
        for k, r in enumerate(rows_out, start=1):
            f = QgsFeature(of)
            f.setAttributes([r["name"], r["pages"], r["squares"], f"{r['name']} … {r['refs']}", r["count"],
                             r["letter"], k])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        gid = None
        if use_grid:
            gf = QgsFields()
            gf.append(QgsField("page", QMetaType.Type.QString))
            gf.append(QgsField("square", QMetaType.Type.QString))
            gsink, gid = self.parameterAsSink(parameters, self.GRID, context, gf, Qgis.WkbType.Polygon, crs)
            if gsink is not None:
                for label, cells in cell_sets:
                    for ref, rect in cells:
                        f = QgsFeature(gf)
                        f.setGeometry(QgsGeometry.fromRect(rect))
                        f.setAttributes([label, ref])
                        gsink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

        xlsx = self.parameterAsFileOutput(parameters, self.XLSX, context)
        if xlsx:
            write_xlsx(xlsx, rows_out, i18n.is_rtl(), bool(pages), use_grid)
        page = self.parameterAsFileOutput(parameters, self.HTML, context)
        if page:
            write_html(page, rows_out, i18n.is_rtl())
        feedback.pushInfo(tr("{n} names in the index.").format(n=len(rows_out)))
        out = {self.OUTPUT: oid, "NAME_COUNT": len(rows_out)}
        if gid:
            out[self.GRID] = gid
        if xlsx:
            out[self.XLSX] = xlsx
        if page:
            out[self.HTML] = page
        return out


def write_xlsx(path, rows, rtl, with_pages, with_grid):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = tr("Index")[:31]
    ws.sheet_view.rightToLeft = rtl
    heads = [tr("Name")]
    if with_pages:
        heads.append(tr("Pages"))
    if with_grid:
        heads.append(tr("Grid squares"))
    ws.append(heads)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1E6F5C")
    letter = None
    for r in rows:
        if r["letter"] != letter:
            letter = r["letter"]
            ws.append([letter])
            cell = ws.cell(row=ws.max_row, column=1)
            cell.font = Font(bold=True, size=13, color="1E6F5C")
        line = [r["name"]]
        if with_pages:
            line.append(r["pages"])
        if with_grid:
            line.append(r["squares"])
        ws.append(line)
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 34
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(horizontal="right" if rtl else "left", vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    wb.save(path)


def write_html(path, rows, rtl):
    parts = [f"<!DOCTYPE html><html lang='{'ar' if rtl else 'en'}' dir='{'rtl' if rtl else 'ltr'}'><head>"
             "<meta charset='utf-8'><title>" + html.escape(tr("Index")) + "</title><style>"
             "body{font-family:'Segoe UI',Tahoma,sans-serif;margin:16mm;color:#1E252B}"
             ".cols{column-count:3;column-gap:8mm;font-size:9pt}"
             "h2{font-size:12pt;color:#1E6F5C;margin:6pt 0 2pt;break-after:avoid}"
             ".e{display:flex;gap:4pt;break-inside:avoid}.e .n{flex:1}.e .r{white-space:nowrap;color:#5B6670}"
             "@media print{body{margin:0}}</style></head><body>",
             "<h1>" + html.escape(tr("Index")) + "</h1><div class='cols'>"]
    letter = None
    for r in rows:
        if r["letter"] != letter:
            letter = r["letter"]
            parts.append(f"<h2>{html.escape(letter)}</h2>")
        parts.append(f"<div class='e'><span class='n'>{html.escape(r['name'])}</span>"
                     f"<span class='r'>{html.escape(r['refs'])}</span></div>")
    parts.append("</div></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return os.path.getsize(path)
