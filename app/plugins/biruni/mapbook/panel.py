"""The Map Book panel: choose where the pages come from, the paper, the scale and what each page carries; see the
pages on the map as numbered rectangles, move, turn, resize, leave out and renumber them by hand; export the book.
The book is kept in the project, so it can be exported again after the data changes."""
import math
import os

from qgis.core import Qgis, QgsMapLayerProxyModel, QgsPointXY, QgsProject, QgsSettings
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog, QFormLayout,
                                 QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ..i18n import tr
from . import book as B
from . import pages as P

KEY = "biruni/mapbook/"
_PANELS = {}


def open_panel(plugin):
    d = _PANELS.get(id(plugin))
    if d is None:
        d = MapBookPanel(plugin)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        _PANELS[id(plugin)] = d
    d.show()
    d.raise_()
    return d


def scale_text(s):
    return f"1:{int(round(s)):,}"


def parse_scale(text):
    t = str(text).replace(",", "").replace(" ", "").replace("٬", "")
    if ":" in t:
        t = t.split(":", 1)[1]
    try:
        v = float(t)
    except ValueError:
        return 0.0
    return v if v > 0 else 0.0


class PageTool(QgsMapTool):
    """Click a page to pick it; drag to move it, Shift+drag to turn it, the wheel (or Ctrl+drag) to change its
    scale - one standard scale per step."""

    def __init__(self, canvas, panel):
        super().__init__(canvas)
        self.panel = panel
        self.drag = None
        self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
        self.band.setColor(QColor(214, 69, 69, 200))
        self.band.setFillColor(QColor(214, 69, 69, 40))
        self.band.setWidth(2)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _ground(self, e):
        pt = self.toMapCoordinates(e.pos())
        return self.panel.to_book(pt)

    def page_at(self, pt):
        best = None
        for k, p in enumerate(self.panel.pages):
            u, v = p.to_page(pt.x(), pt.y())
            if abs(u) <= p.w / 2 and abs(v) <= p.h / 2:
                d = math.hypot(u / p.w, v / p.h)
                if best is None or d < best[0]:
                    best = (d, k)
        return None if best is None else best[1]

    def canvasPressEvent(self, e):  # noqa: N802
        pt = self._ground(e)
        k = self.page_at(pt)
        if k is None:
            return
        self.panel.select(k)
        p = self.panel.pages[k]
        mods = e.modifiers()
        mode = "turn" if mods & Qt.KeyboardModifier.ShiftModifier else \
            "size" if mods & Qt.KeyboardModifier.ControlModifier else "move"
        self.drag = (k, mode, pt, p.copy())

    def canvasMoveEvent(self, e):  # noqa: N802
        if self.drag is None:
            return
        self.apply(self.drag, self._ground(e))
        self.show_band(self.panel.pages[self.drag[0]])

    def apply(self, drag, pt):
        """Change the dragged page for the pointer at ``pt`` (book CRS)."""
        k, mode, start, before = drag
        p = self.panel.pages[k]
        if mode == "move":
            p.cx, p.cy = before.cx + pt.x() - start.x(), before.cy + pt.y() - start.y()
        elif mode == "turn":
            a0 = math.atan2(start.y() - before.cy, start.x() - before.cx)
            a1 = math.atan2(pt.y() - before.cy, pt.x() - before.cx)
            p.angle = P._norm(before.angle + math.degrees(a1 - a0))
        else:
            r0 = max(math.hypot(start.x() - before.cx, start.y() - before.cy), 1e-9)
            r1 = math.hypot(pt.x() - before.cx, pt.y() - before.cy)
            p.scale = P.standard_scale(before.scale * r1 / r0)
            P.frame_for(p, self.panel.book.get("paper", "A4"), bool(self.panel.book.get("grid", True)))
        p.manual = True

    def canvasReleaseEvent(self, e):  # noqa: N802
        if self.drag is not None:
            self.drag = None
            self.band.reset(Qgis.GeometryType.Polygon)
            self.panel.pages_changed()

    def wheelEvent(self, e):  # noqa: N802
        k = self.panel.current()
        if k is None:
            e.ignore()
            return
        self.panel.step_scale(k, -1 if e.angleDelta().y() > 0 else 1)
        e.accept()

    def show_band(self, p):
        self.band.reset(Qgis.GeometryType.Polygon)
        for c in self.panel.from_book_points(p.corners()):
            self.band.addPoint(c, False)
        self.band.closePoints(True)

    def deactivate(self):
        self.band.reset(Qgis.GeometryType.Polygon)
        super().deactivate()


class MapBookPanel(QDockWidget):
    def __init__(self, plugin, project=None):
        super().__init__(tr("Map Book"), plugin.iface.mainWindow() if plugin.iface else None)
        self.setObjectName("BiruniMapBook")
        self.plugin = plugin
        self.iface = plugin.iface
        self.project = project or QgsProject.instance()
        self.book = B.load_book(project=self.project) or B.new_book(title=self.project.title() or "")
        self.pages = B.page_list(self.book)
        self.preview = None
        self.tool = None
        self._build()
        self._load_into_widgets()
        self.refresh_list()

    # ---- the form -----------------------------------------------------------------------------------------------
    def _build(self):
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8, 8, 8, 8)
        intro = QLabel(tr("A book of map pages with a key map, contents and a name index, in one PDF whose arrows "
                          "and bookmarks jump to their pages."))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        g = QGroupBox(tr("Pages from"))
        f = QFormLayout(g)
        self.source = QComboBox()
        for key, text in (("grid", tr("An automatic grid over the data")),
                          ("index", tr("An index layer (one page per polygon)")),
                          ("strips", tr("Strips along a line (road, pipe, river)")),
                          ("features", tr("One page per feature (parcels…)")),
                          ("stops", tr("The project's Map Stops"))):
            self.source.addItem(text, key)
        self.source.currentIndexChanged.connect(self._source_changed)
        f.addRow(tr("Source"), self.source)
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        self.layer.layerChanged.connect(self._layer_changed)
        f.addRow(tr("Layer"), self.layer)
        self.name_field = QgsFieldComboBox()
        self.name_field.setAllowEmptyFieldName(True)
        f.addRow(tr("Page names from"), self.name_field)
        lay.addWidget(g)

        g = QGroupBox(tr("Paper and scale"))
        f = QFormLayout(g)
        self.paper = QComboBox()
        self.paper.addItems(list(P.PAPER_MM))
        f.addRow(tr("Paper"), self.paper)
        self.orientation = QComboBox()
        for key, text in (("auto", tr("Best fit for each page")), ("landscape", tr("Landscape")),
                          ("portrait", tr("Portrait"))):
            self.orientation.addItem(text, key)
        f.addRow(tr("Orientation"), self.orientation)
        self.scale_mode = QComboBox()
        for key, text in (("best", tr("The largest scale that fits each page")),
                          ("common", tr("One scale for the whole book")), ("fixed", tr("This scale:"))):
            self.scale_mode.addItem(text, key)
        f.addRow(tr("Scale"), self.scale_mode)
        self.scale = QComboBox()
        self.scale.setEditable(True)
        self.scale.addItem(tr("Chosen for you"), 0)
        for s in P.BOOK_SCALES[2:16]:
            self.scale.addItem(scale_text(s), s)
        f.addRow("", self.scale)
        self.max_pages = QSpinBox()
        self.max_pages.setRange(1, 999)
        f.addRow(tr("At most (pages)"), self.max_pages)
        self.overlap = QSpinBox()
        self.overlap.setRange(0, 45)
        self.overlap.setSuffix(" %")
        f.addRow(tr("Overlap with the next page"), self.overlap)
        self.fewest = QCheckBox(tr("Only cells with data, the fewest pages"))
        f.addRow("", self.fewest)
        lay.addWidget(g)

        g = QGroupBox(tr("Order and names"))
        f = QFormLayout(g)
        self.order = QComboBox()
        for key, text in (("rows", tr("Rows, in reading order")), ("snake", tr("Snake (every other row back)")),
                          ("columns", tr("Columns"))):
            self.order.addItem(text, key)
        f.addRow(tr("Page order"), self.order)
        self.naming = QComboBox()
        for key, text in (("number", tr("Page numbers")), ("grid", tr("Grid letters (A1, B1…)")),
                          ("given", tr("From the names field or the stops"))):
            self.naming.addItem(text, key)
        f.addRow(tr("Page names"), self.naming)
        self.title = QLineEdit()
        f.addRow(tr("Book title"), self.title)
        self.subtitle = QLineEdit()
        f.addRow(tr("Subtitle"), self.subtitle)
        self.author = QLineEdit()
        f.addRow(tr("Prepared by"), self.author)
        self.page_title = QLineEdit()
        self.page_title.setPlaceholderText(tr("{title} - {name}  or  [% \"district\" %]"))
        self.page_title.setToolTip(tr("Each page's title: {title} {name} {number} {total}, and expressions in "
                                      "[% %] on the feature the page was made from."))
        f.addRow(tr("Page title"), self.page_title)
        self.lang = QComboBox()
        for code, text in (("", tr("The interface's language")), ("ar", "العربية"), ("en", "English"),
                           ("fr", "Français"), ("es", "Español"), ("pt_BR", "Português"), ("tr", "Türkçe"),
                           ("fa", "فارسی")):
            self.lang.addItem(text, code)
        f.addRow(tr("Book language"), self.lang)
        lay.addWidget(g)

        g = QGroupBox(tr("On every page"))
        v = QVBoxLayout(g)
        self.opt = {}
        for key, text in (("locator", tr("Key map with this page")), ("neighbours", tr("Arrows to the next pages")),
                          ("legend", tr("Legend of what the page shows")), ("grid", tr("Coordinate grid")),
                          ("qr", tr("QR code: the page's area on a phone"))):
            cb = QCheckBox(text)
            self.opt[key] = cb
            v.addWidget(cb)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Table of")))
        self.table_layer = QgsMapLayerComboBox()
        self.table_layer.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        self.table_layer.setAllowEmptyLayer(True)
        row.addWidget(self.table_layer, 1)
        v.addLayout(row)
        self.table_fields = QLineEdit()
        self.table_fields.setPlaceholderText(tr("Fields, separated by commas (empty = the first three)"))
        v.addWidget(self.table_fields)
        lay.addWidget(g)

        g = QGroupBox(tr("Book pages"))
        v = QVBoxLayout(g)
        for key, text in (("cover", tr("Cover with the key map")), ("contents", tr("Contents"))):
            cb = QCheckBox(text)
            self.opt[key] = cb
            v.addWidget(cb)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Index of names from")))
        self.index_layer = QgsMapLayerComboBox()
        self.index_layer.setFilters(QgsMapLayerProxyModel.Filter.VectorLayer)
        self.index_layer.setAllowEmptyLayer(True)
        row.addWidget(self.index_layer, 1)
        self.index_field = QgsFieldComboBox()
        self.index_layer.layerChanged.connect(self.index_field.setLayer)
        row.addWidget(self.index_field, 1)
        v.addLayout(row)
        self.opt["back"] = QCheckBox(tr("Back page with notes"))
        v.addWidget(self.opt["back"])
        self.back_text = QLineEdit()
        v.addWidget(self.back_text)
        lay.addWidget(g)

        self.make_btn = QPushButton(tr("Make the pages"))
        self.make_btn.clicked.connect(self.make_pages)
        lay.addWidget(self.make_btn)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        g = QGroupBox(tr("Pages"))
        v = QVBoxLayout(g)
        hint = QLabel(tr("Tick a page to keep it. With «Edit on the map»: drag a page to move it, Shift+drag to "
                         "turn it, the mouse wheel to change its scale."))
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.list = QListWidget()
        self.list.setMinimumHeight(150)
        self.list.itemChanged.connect(self._item_changed)
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemDoubleClicked.connect(lambda _i: self.show_page(self.current()))
        v.addWidget(self.list)
        row = QHBoxLayout()
        for text, fn in ((tr("Up"), lambda: self.move_page(-1)), (tr("Down"), lambda: self.move_page(1)),
                         (tr("Renumber"), self.renumber), (tr("Show"), lambda: self.show_page(self.current()))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        self.edit_btn = QPushButton(tr("Edit on the map"))
        self.edit_btn.setCheckable(True)
        self.edit_btn.toggled.connect(self._edit_toggled)
        v.addWidget(self.edit_btn)
        lay.addWidget(g)

        g = QGroupBox(tr("Export"))
        f = QFormLayout(g)
        self.per_page = QComboBox()
        for key, text in (("", tr("One PDF only")), ("png", tr("…and a PNG picture per page")),
                          ("jpg", tr("…and a JPG picture per page")), ("pdf", tr("…and a PDF per page"))):
            self.per_page.addItem(text, key)
        f.addRow(tr("Files"), self.per_page)
        self.opt["seal"] = QCheckBox(tr("Seal the PDF (proof it was not changed)"))
        f.addRow("", self.opt["seal"])
        self.export_btn = QPushButton(tr("Export the book…"))
        self.export_btn.clicked.connect(self.export)
        f.addRow(self.export_btn)
        lay.addWidget(g)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        self.setWidget(scroll)
        try:
            from ..tracking import panel_qss

            inner.setStyleSheet(panel_qss(self.plugin.theme.tokens()))
        except Exception:  # noqa: BLE001 - no theme in tests and scripts
            pass

    def _source_changed(self, *_):
        src = self.source.currentData()
        filters = {"index": QgsMapLayerProxyModel.Filter.PolygonLayer, "strips": QgsMapLayerProxyModel.Filter.LineLayer,
                   "features": QgsMapLayerProxyModel.Filter.VectorLayer}
        self.layer.setFilters(filters.get(src, QgsMapLayerProxyModel.Filter.VectorLayer))
        self.layer.setEnabled(src in filters)
        self.name_field.setEnabled(src in ("index", "features"))
        self.fewest.setEnabled(src == "grid")
        self.max_pages.setEnabled(src in ("grid", "strips"))
        self.overlap.setEnabled(src in ("grid", "strips"))
        self.order.setEnabled(src in ("grid", "index", "features"))

    def _layer_changed(self, layer):
        self.name_field.setLayer(layer)

    def _set_combo(self, combo, value):
        k = combo.findData(value)
        if k >= 0:
            combo.setCurrentIndex(k)

    def _load_into_widgets(self):
        b = self.book
        self._set_combo(self.source, b.get("source", "grid"))
        lyr = self.project.mapLayer(b.get("layer") or "")
        if lyr is not None:
            self.layer.setLayer(lyr)
        self.name_field.setLayer(self.layer.currentLayer())
        self.name_field.setField(b.get("name_field", ""))
        self.paper.setCurrentText(b.get("paper", "A4"))
        self._set_combo(self.orientation, b.get("orientation", "auto"))
        self._set_combo(self.scale_mode, b.get("scale_mode", "best"))
        if b.get("scale"):
            self.scale.setCurrentText(scale_text(b["scale"]))
        self.max_pages.setValue(int(b.get("max_pages", 24)))
        self.overlap.setValue(int(round(100 * float(b.get("overlap", 0.08)))))
        self.fewest.setChecked(bool(b.get("fewest", True)))
        self._set_combo(self.order, b.get("order", "rows"))
        self._set_combo(self.naming, b.get("naming", "number"))
        self.title.setText(b.get("title", ""))
        self.subtitle.setText(b.get("subtitle", ""))
        self.author.setText(b.get("author", ""))
        self.page_title.setText(b.get("page_title", ""))
        self._set_combo(self.lang, b.get("lang", ""))
        for key, cb in self.opt.items():
            cb.setChecked(bool(b.get(key, key not in ("back", "seal"))))
        self.table_layer.setLayer(self.project.mapLayer(b.get("table_layer") or ""))
        self.table_fields.setText(", ".join(b.get("table_fields") or []))
        self.index_layer.setLayer(self.project.mapLayer(b.get("index_layer") or ""))
        self.index_field.setLayer(self.index_layer.currentLayer())
        self.index_field.setField(b.get("index_field", ""))
        self.back_text.setText(b.get("back_text", ""))
        self._set_combo(self.per_page, b.get("per_page", ""))
        self._source_changed()

    def read_widgets(self):
        """The form into the book's settings."""
        b = self.book
        b["source"] = self.source.currentData()
        lyr = self.layer.currentLayer()
        b["layer"] = lyr.id() if lyr is not None else ""
        b["name_field"] = self.name_field.currentField()
        b["paper"] = self.paper.currentText()
        b["orientation"] = self.orientation.currentData()
        b["scale_mode"] = self.scale_mode.currentData()
        b["scale"] = parse_scale(self.scale.currentText()) if self.scale.currentIndex() != 0 or \
            parse_scale(self.scale.currentText()) else 0
        b["max_pages"] = self.max_pages.value()
        b["overlap"] = self.overlap.value() / 100.0
        b["fewest"] = self.fewest.isChecked()
        b["order"] = self.order.currentData()
        b["naming"] = self.naming.currentData()
        b["title"] = self.title.text().strip()
        b["name"] = b.get("name") or b["title"] or tr("Map book")
        b["subtitle"] = self.subtitle.text().strip()
        b["author"] = self.author.text().strip()
        b["page_title"] = self.page_title.text().strip()
        b["lang"] = self.lang.currentData()
        for key, cb in self.opt.items():
            b[key] = cb.isChecked()
        tl = self.table_layer.currentLayer()
        b["table_layer"] = tl.id() if tl is not None else ""
        b["table_fields"] = [x.strip() for x in self.table_fields.text().split(",") if x.strip()]
        il = self.index_layer.currentLayer()
        b["index_layer"] = il.id() if il is not None else ""
        b["index_field"] = self.index_field.currentField()
        b["back_text"] = self.back_text.text().strip()
        b["per_page"] = self.per_page.currentData()
        return b

    # ---- coordinates between the canvas and the book ------------------------------------------------------------
    def _xf(self, back=False):
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

        crs = QgsCoordinateReferenceSystem(self.book.get("crs") or "")
        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs() if self.iface else self.project.crs()
        if not crs.isValid() or crs == canvas_crs:
            return None
        return QgsCoordinateTransform(crs, canvas_crs, self.project) if back else \
            QgsCoordinateTransform(canvas_crs, crs, self.project)

    def to_book(self, pt):
        xf = self._xf()
        return QgsPointXY(xf.transform(pt)) if xf else QgsPointXY(pt)

    def from_book_points(self, pts):
        xf = self._xf(back=True)
        return [QgsPointXY(xf.transform(p)) if xf else QgsPointXY(p) for p in pts]

    # ---- pages --------------------------------------------------------------------------------------------------
    def make_pages(self):
        self.read_widgets()
        from .. import i18n

        rtl = i18n.is_rtl(self.book.get("lang") or None)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.pages = B.build(self.book, self.project, rtl)
        except ValueError as e:
            self.status.setText(self._why(str(e)))
            return False
        finally:
            QApplication.restoreOverrideCursor()
        B.save_book(self.book, self.project, self.pages)
        self.refresh_list()
        self.show_preview()
        text = tr("{n} pages.").format(n=len(self.pages))
        if self.book.get("source") == "grid" and self.book.get("plain_pages"):
            text += " " + tr("A plain grid over the same area needs {n}.").format(n=self.book["plain_pages"])
        self.status.setText(text)
        return True

    def _why(self, code):
        return {"choose the layer": tr("Choose the layer the pages come from."),
                "no data": tr("No visible data to make the pages from."),
                "no lines": tr("The layer has no lines.")}.get(code, tr("The pages could not be made: {e}").format(
                    e=code))

    def refresh_list(self):
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.pages:
            num = "–" if p.number is None else str(p.number)
            text = f"{num}.  {p.name}  ·  {scale_text(p.scale)}"
            if abs(p.angle) > 0.05:
                text += f"  ·  {P._norm(p.angle):.0f}°"
            if p.manual:
                text += "  ✎"
            it = QListWidgetItem(text)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if p.excluded else Qt.CheckState.Checked)
            self.list.addItem(it)
        self.list.blockSignals(False)

    def current(self):
        k = self.list.currentRow()
        return k if 0 <= k < len(self.pages) else None

    def select(self, k):
        self.list.setCurrentRow(k)

    def _row_changed(self, k):
        if self.tool is not None and 0 <= k < len(self.pages):
            self.tool.show_band(self.pages[k])

    def _item_changed(self, item):
        k = self.list.row(item)
        self.set_excluded(k, item.checkState() != Qt.CheckState.Checked)

    def set_excluded(self, k, excluded):
        self.pages[k].excluded = excluded
        self.pages_changed()

    def move_page(self, step):
        k = self.current()
        if k is None or not 0 <= k + step < len(self.pages):
            return
        self.pages[k], self.pages[k + step] = self.pages[k + step], self.pages[k]
        self.pages_changed()
        self.select(k + step)

    def renumber(self):
        """Numbers again in reading order (pages moved by hand keep their place on the map)."""
        from .. import i18n

        B.finish(self.book, self.pages, i18n.is_rtl(self.book.get("lang") or None), renumber=True)
        self.pages_changed(relink=False)

    def step_scale(self, k, step):
        p = self.pages[k]
        scales = P.BOOK_SCALES
        i = min(range(len(scales)), key=lambda j: abs(scales[j] - p.scale))
        p.scale = scales[max(0, min(len(scales) - 1, i + step))]
        P.frame_for(p, self.book.get("paper", "A4"), bool(self.book.get("grid", True)))
        p.manual = True
        self.pages_changed()

    def pages_changed(self, relink=True):
        if relink:
            B.relink(self.book, self.pages)
        else:
            self.book["pages"] = [p.to_dict() for p in self.pages]
        B.save_book(self.book, self.project)
        k = self.current()
        self.refresh_list()
        if k is not None:
            self.select(k)
        self.show_preview()

    def show_preview(self):
        """The pages as numbered rectangles on the map (a temporary layer, replaced on every change)."""
        from qgis.core import QgsCoordinateReferenceSystem

        crs = QgsCoordinateReferenceSystem(self.book.get("crs") or "")
        crs = crs if crs.isValid() else self.project.crs()
        old = self.preview
        lyr = B.style_preview(B.pages_layer(self.pages, crs, tr("Map book pages")))
        if old is not None and self.project.mapLayer(old) is not None:
            self.project.removeMapLayer(old)
        for other in list(self.project.mapLayers().values()):
            if other.customProperty(B.PREVIEW_PROP):
                self.project.removeMapLayer(other.id())
        self.project.addMapLayer(lyr, False)
        self.project.layerTreeRoot().insertLayer(0, lyr)
        self.preview = lyr.id()
        return lyr

    def show_page(self, k):
        if k is None or self.iface is None:
            return
        pts = self.from_book_points(self.pages[k].corners())
        from qgis.core import QgsRectangle

        r = QgsRectangle(pts[0], pts[2])
        for p in pts:
            r.combineExtentWith(p)
        r.scale(1.1)
        self.iface.mapCanvas().setExtent(r)
        self.iface.mapCanvas().refresh()

    def _edit_toggled(self, on):
        if self.iface is None:
            return
        canvas = self.iface.mapCanvas()
        if on:
            self.tool = PageTool(canvas, self)
            canvas.setMapTool(self.tool)
        elif self.tool is not None:
            canvas.unsetMapTool(self.tool)
            self.tool = None

    # ---- export -------------------------------------------------------------------------------------------------
    def export(self, path=None):
        self.read_widgets()
        if not self.pages and not self.make_pages():
            return None
        if not path:
            last = QgsSettings().value(KEY + "folder", os.path.expanduser("~"))
            from ..map_studio.outputs import safe_name

            path, _f = QFileDialog.getSaveFileName(self, tr("Save the map book"),
                                                   os.path.join(last, safe_name(self.book.get("title") or
                                                                                tr("Map book")) + ".pdf"),
                                                   "PDF (*.pdf)")
            if not path:
                return None
            QgsSettings().setValue(KEY + "folder", os.path.dirname(path))
        from . import render

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            res = render.export_book(self.book, self.pages, path, self.project, per_page=self.book.get("per_page"),
                                     seal=bool(self.book.get("seal")))
        except Exception as e:  # noqa: BLE001 - shown to the user
            QApplication.restoreOverrideCursor()
            self.status.setText(tr("The book could not be written: {e}").format(e=e))
            return None
        QApplication.restoreOverrideCursor()
        B.save_book(self.book, self.project, self.pages)
        msg = tr("Map book saved: {n} map pages, {links} page links.").format(n=res["pages"], links=res["links"])
        self.status.setText(msg + "\n" + path)
        if self.iface is not None:
            try:
                self.iface.messageBar().pushSuccess(tr("Map Book"), msg + " " + path)
            except Exception:  # noqa: BLE001
                pass
        return res
