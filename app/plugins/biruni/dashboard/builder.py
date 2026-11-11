"""The dashboard builder: a 12-column grid of widgets to place and size with the mouse, a palette of the 15
widget types, a panel for the selected widget (its layer, what it counts or sums, how it groups, its look),
the dashboard's own settings (theme, dates, privacy threshold, filters), templates built from the layers of
the project, undo/redo - and the live preview: the very page the phones open (page.py), fed in-process by
the DashboardService with the definition being edited.
"""
import copy
import json

from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import QRect, QSize, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen
from qgis.PyQt.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                 QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                                 QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget)

from ..i18n import tr
from . import model

COLUMNS = 12
ROW_PX = 46

TYPE_LABELS = {
    "kpi": "Number", "gauge": "Gauge", "sparkline": "Small trend", "line": "Line chart", "area": "Area chart",
    "bar": "Bar chart", "pie": "Pie chart", "table": "Table", "map": "Map", "choropleth": "Map of zones",
    "progress": "Progress by zone", "leaderboard": "Leaderboard", "feed": "Latest changes", "text": "Text",
    "filter": "Filter",
}
TYPE_HINTS = {
    "kpi": "One number (a count, a sum, an average) with its change.",
    "gauge": "One number on a dial, against a target.",
    "sparkline": "A small line of how a number moved.",
    "line": "How numbers change over time.",
    "area": "How numbers change over time, as filled areas.",
    "bar": "Numbers side by side, one bar per group.",
    "pie": "How a whole is shared between groups.",
    "table": "Rows of features with chosen fields.",
    "map": "The features on a map.",
    "choropleth": "Zones coloured by a number.",
    "progress": "How much is done in each zone.",
    "leaderboard": "Groups ranked by a number (people, teams, districts).",
    "feed": "The latest changes to the data, as they happen.",
    "text": "A heading or a note.",
    "filter": "A choice the viewer makes; the other widgets follow it.",
}
SIZES = {"kpi": (3, 2), "gauge": (3, 3), "sparkline": (3, 2), "line": (6, 4), "area": (6, 4), "bar": (6, 4),
         "pie": (4, 4), "table": (6, 5), "map": (6, 6), "choropleth": (6, 6), "progress": (4, 4),
         "leaderboard": (4, 5), "feed": (4, 5), "text": (12, 1), "filter": (3, 1)}
ICONS = {"kpi": "#", "gauge": "◔", "sparkline": "∿", "line": "📈", "area": "▲", "bar": "▮", "pie": "◕",
         "table": "▦", "map": "🗺", "choropleth": "⬢", "progress": "▰", "leaderboard": "🏆", "feed": "☰",
         "text": "T", "filter": "⏷"}
OP_LABELS = {"count": "Count", "sum": "Sum", "avg": "Average", "min": "Smallest", "max": "Largest",
             "distinct": "Different values", "ratio": "Ratio"}
TIME_LABELS = {"day": "Day", "week": "Week", "month": "Month", "year": "Year"}
THEME_LABELS = {"biruni-light": "Light", "biruni-dark": "Dark", "desert": "Desert", "ocean": "Ocean",
                "high-contrast": "High contrast", "tv-wall": "Wall screen"}
DATE_LABELS = {"both": "Hijri and Gregorian", "greg": "Gregorian", "hijri": "Hijri"}
PALETTE = ("#1565c0", "#2e7d32", "#ef6c00", "#6a1b9a", "#c62828", "#00838f", "#5d4037", "#455a64")


# ---- definitions ----------------------------------------------------------------------------------------------
def free_spot(d, w, h):
    """The first place (x, y) on the grid where a w×h widget fits."""
    taken = [(int(x.get("x", 0)), int(x.get("y", 0)), int(x.get("w", 1)), int(x.get("h", 1)))
             for x in d.get("widgets") or []]
    y = 0
    while True:
        for x in range(0, COLUMNS - w + 1):
            if not any(x < a + c and a < x + w and y < b + e and b < y + h for a, b, c, e in taken):
                return x, y
        y += 1


def source_for(d, layer_id, flt=""):
    """The id of the source reading layer_id (with this filter), made when there is none."""
    sources = d.setdefault("sources", {})
    for sid, s in sources.items():
        if s.get("layer") == layer_id and (s.get("filter") or "") == (flt or ""):
            return sid
    n = 1
    while f"s{n}" in sources:
        n += 1
    sources[f"s{n}"] = {"layer": layer_id, "filter": flt or ""}
    return f"s{n}"


def prune_sources(d):
    used = {w.get("source") for w in d.get("widgets") or []} | {f.get("source") for f in d.get("filters") or []}
    for w in d.get("widgets") or []:
        for L in (w.get("options") or {}).get("layers") or [] if w.get("type") == "map" else []:
            if isinstance(L, dict):
                used.add(L.get("source"))
    d["sources"] = {k: v for k, v in (d.get("sources") or {}).items() if k in used}


def new_widget(d, kind, layer=None, title=None):
    """A widget of this type with sensible settings for the layer (or the first vector layer)."""
    w, h = SIZES.get(kind, (4, 3))
    x, y = free_spot(d, w, h)
    ids = {x_.get("id") for x_ in d.get("widgets") or []}
    n = 1
    while f"w{n}" in ids:
        n += 1
    wd = {"id": f"w{n}", "type": kind, "title": title if title is not None else tr(TYPE_LABELS.get(kind, kind)),
          "x": x, "y": y, "w": w, "h": h, "source": None, "measure": None, "group": None,
          "style": {"color": PALETTE[(n - 1) % len(PALETTE)], "icon": "", "unit": "", "decimals": 0, "target": None,
                    "thresholds": []},
          "options": {}}
    if kind in model.NEEDS_SOURCE:
        layer = layer or _first_vector()
        if layer is not None:
            wd["source"] = source_for(d, layer.id())
        if kind not in ("map", "table"):
            wd["measure"] = {"op": "count", "field": None, "where": ""}
        if kind in model.NEEDS_GROUP or kind == "choropleth":
            if kind == "choropleth":
                zones = _polygon_layers()
                wd["group"] = {"zones": zones[0].id(), "label": _text_field(zones[0])} if zones else None
            elif kind in ("line", "area", "sparkline"):
                df = _date_field(layer) if layer is not None else None
                wd["group"] = {"field": df, "time": "month"} if df else ({"field": _text_field(layer)} if layer else None)
            else:
                f = _text_field(layer) if layer is not None else None
                wd["group"] = {"field": f, "top": 10} if f else None
        if kind == "progress":
            wd["options"]["done"] = _done_expression(layer) if layer is not None else "true"
        if kind == "table" and layer is not None:
            wd["options"]["fields"] = [f.name() for f in layer.fields()][:5]
            wd["options"]["limit"] = 50
        if kind == "map" and layer is not None:
            wd["options"]["layers"] = [map_layer(d, layer)]
            wd["options"]["basemap"] = "project"
    if kind == "text":
        wd["options"]["text"] = tr("Write a heading or a note here")
    if kind == "filter":
        flt = (d.get("filters") or [None])[0]
        if flt is None and layer is None:
            layer = _first_vector()
        if flt is None and layer is not None and _text_field(layer):
            flt = {"id": _next_filter_id(d), "source": source_for(d, layer.id()), "field": _text_field(layer),
                   "label": _text_field(layer)}
            d.setdefault("filters", []).append(flt)
        wd["options"]["filter"] = flt["id"] if flt else None
    d.setdefault("widgets", []).append(wd)
    return wd


MAP_STYLES = {"layer": "As on the map (the layer's own style)", "single": "One colour",
              "categories": "A colour per value", "graduated": "Shades by a number"}


def map_layer(d, layer, flt=""):
    """A map layer entry for ``layer``: its own QGIS style, a label and a few popup fields guessed."""
    label = _field_like(layer, ("name", "no", "number", "num", "label", "رقم", "اسم", "parcel"), None)
    popup = [f.name() for f in layer.fields() if f.name().lower() not in ("fid", "id", "ogc_fid")][:5]
    return {"source": source_for(d, layer.id(), flt), "name": layer.name(), "style": {"kind": "layer"},
            "label": label, "popup": popup}


def map_layers_of(d, w):
    """The layer entries of a map; a first-shape map (one source, style.by) is turned into one."""
    o = w.setdefault("options", {})
    if isinstance(o.get("layers"), list) and o["layers"]:
        return o["layers"]
    src = (d.get("sources") or {}).get(w.get("source")) or {}
    lyr = QgsProject.instance().mapLayer(src.get("layer")) if src.get("layer") else None
    if lyr is None:
        o["layers"] = []
        return o["layers"]
    by = (w.get("style") or {}).get("by")
    o["layers"] = [{"source": w["source"], "name": lyr.name(),
                    "style": {"kind": "categories", "field": by} if by else {"kind": "layer"},
                    "label": o.get("label"), "popup": list(o.get("fields") or [])[:12]}]
    o.setdefault("basemap", "project")
    return o["layers"]


def _next_filter_id(d):
    ids = {f.get("id") for f in d.get("filters") or []}
    n = 1
    while f"f{n}" in ids:
        n += 1
    return f"f{n}"


def _vector_layers():
    return [lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid()]


def _first_vector():
    lyrs = _vector_layers()
    return lyrs[0] if lyrs else None


def _polygon_layers():
    return [lyr for lyr in _vector_layers() if lyr.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry]


def _point_layers():
    return [lyr for lyr in _vector_layers() if lyr.geometryType() == QgsWkbTypes.GeometryType.PointGeometry]


def _field_like(layer, words, kinds=None):
    if layer is None:
        return None
    for f in layer.fields():
        n = f.name().lower()
        if any(w in n for w in words) and (kinds is None or kinds(f)):
            return f.name()
    return None


def _is_text(f):
    return f.type() in (10,) or f.typeName().lower() in ("string", "text", "varchar")


def _is_number(f):
    return f.isNumeric()


def _is_date(f):
    return f.typeName().lower() in ("date", "datetime", "timestamp") or f.type() in (14, 16)


def _text_field(layer):
    if layer is None:
        return None
    hit = _field_like(layer, ("district", "hay", "حي", "zone", "area_name", "region", "منطقة", "status", "حالة",
                              "type", "نوع", "class"), _is_text)
    if hit:
        return hit
    return next((f.name() for f in layer.fields() if _is_text(f)), None)


def _date_field(layer):
    if layer is None:
        return None
    return next((f.name() for f in layer.fields() if _is_date(f)), None) or _field_like(
        layer, ("date", "time", "تاريخ", "created", "updated"))


def _status_field(layer):
    return _field_like(layer, ("status", "state", "حالة", "progress", "stage", "مرحلة"), _is_text)


DONE_WORDS = ("done", "complete", "completed", "finished", "approved", "منجز", "مكتمل", "تم", "معتمد")


def _done_value(layer, field):
    if layer is None or not field:
        return None
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return None
    for v in layer.uniqueValues(idx, 200):
        s = str(v).strip().lower()
        if s in DONE_WORDS or any(s.startswith(w) for w in DONE_WORDS[:4]):
            return str(v)
    return None


def _quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def _done_expression(layer):
    f = _status_field(layer)
    v = _done_value(layer, f)
    if f and v is not None:
        return f'"{f}" = {_quote(v)}'
    return "true"


# ---- templates ----------------------------------------------------------------------------------------------
TEMPLATES = ("Map dashboard", "Survey progress", "Parcels by district", "Team activity", "Field inspections")


def _place(w, x, y, wd, h):
    w.update(x=x, y=y, w=wd, h=h)
    return w


def map_template(d, main, zones, layers):
    """The map is the page: a large map of the main layer (and the zones' outlines) over the basemap, the key
    numbers beside it, charts that select on the map below."""
    status = _status_field(main)
    done = _done_value(main, status)
    group_f = _text_field(main)
    date_f = _date_field(main)
    m = _place(new_widget(d, "map", main, tr("Map")), 0, 0, 8, 8)
    lys = m["options"]["layers"]
    if status:
        lys[0]["style"] = {"kind": "categories", "field": status}
    if zones is not None:
        z = map_layer(d, zones)
        z["style"] = {"kind": "single", "color": "#9AA5B1", "opacity": 0.08, "outline": "#5C6B7A", "width": 1.5}
        z["label"] = _text_field(zones)
        z["popup"] = [z["label"]] if z["label"] else []
        lys.insert(0, z)
    _place(new_widget(d, "kpi", main, tr("All features")), 8, 0, 4, 2)
    if status and done is not None:
        k = new_widget(d, "kpi", main, tr("Done"))
        k["measure"] = {"op": "count", "field": None, "where": f'"{status}" = {_quote(done)}'}
        _place(k, 8, 2, 4, 2)
        g = new_widget(d, "gauge", main, tr("Share done"))
        g["measure"] = {"op": "ratio", "field": None, "where": f'"{status}" = {_quote(done)}',
                        "denominator": {"op": "count", "field": None, "where": ""}}
        g["style"]["unit"] = "%"
        _place(g, 8, 4, 4, 2)
    if group_f:
        f = new_widget(d, "filter", main, group_f)
        _place(f, 8, 6, 4, 2)
        b = new_widget(d, "bar", main, tr("By {field}").format(field=group_f))
        b["group"] = {"field": group_f, "top": 12}
        _place(b, 0, 8, 4, 4)
    if status:
        p = new_widget(d, "pie", main, tr("By {field}").format(field=status))
        p["group"] = {"field": status, "top": 8}
        _place(p, 4, 8, 4, 4)
    if date_f:
        ln = new_widget(d, "line", main, tr("Over time"))
        ln["group"] = {"field": date_f, "time": "week"}
        _place(ln, 8, 8, 4, 4)
    else:
        _place(new_widget(d, "feed", None, tr("Latest changes")), 8, 8, 4, 4)
    _place(new_widget(d, "table", main, tr("Features")), 0, 12, 12, 4)


def template(name, layers=None):
    """A ready dashboard built from the project's layers. ``layers`` overrides the choice ({"main": layer})."""
    layers = layers or {}
    d = model.blank(tr(name))
    polys, points = _polygon_layers(), _point_layers()
    main = layers.get("main") or (points[0] if name == "Field inspections" and points else None) or (
        polys[0] if polys else _first_vector())
    if main is None:
        t = new_widget(d, "text")
        t["options"]["text"] = tr("Add a layer to the map, then build the dashboard from it.")
        return d
    zones = layers.get("zones") or next((p for p in polys if p.id() != main.id()), None)
    head = new_widget(d, "text", title="")
    head["options"]["text"] = tr(name) + " — " + main.name()
    status = _status_field(main)
    done = _done_value(main, status)
    group_f = _text_field(main)
    date_f = _date_field(main)

    def add(kind, title, **kw):
        w = new_widget(d, kind, main, title)
        for k, v in kw.items():
            w[k] = v
        return w

    if name == "Map dashboard":
        d["widgets"] = []
        map_template(d, main, zones, layers)
    elif name == "Survey progress":
        add("kpi", tr("All features"))
        if status and done is not None:
            add("kpi", tr("Done"), measure={"op": "count", "field": None, "where": f'"{status}" = {_quote(done)}'})
            g = add("gauge", tr("Share done"), measure={"op": "ratio", "field": None,
                                                         "where": f'"{status}" = {_quote(done)}',
                                                         "denominator": {"op": "count", "field": None, "where": ""}})
            g["style"]["unit"] = "%"
            add("bar", tr("By status"), group={"field": status, "top": 12})
        if zones is not None:
            p = add("progress", tr("Progress by zone"), group={"zones": zones.id(), "label": _text_field(zones)})
            p["options"]["done"] = _done_expression(main)
        if date_f:
            add("line", tr("Over time"), group={"field": date_f, "time": "week"})
        add("map", tr("Map"))
    elif name == "Parcels by district":
        add("kpi", tr("Parcels"))
        area = _field_like(main, ("area", "مساحة", "shape_area"), _is_number)
        if area:
            k = add("kpi", tr("Total area"), measure={"op": "sum", "field": area, "where": ""})
            k["style"]["unit"] = "m²"
            add("kpi", tr("Average area"), measure={"op": "avg", "field": area, "where": ""})
        if group_f:
            add("bar", tr("Parcels by {field}").format(field=group_f), group={"field": group_f, "top": 15})
            add("pie", tr("Share by {field}").format(field=group_f), group={"field": group_f, "top": 8})
            f = new_widget(d, "filter", main, group_f)
            d["filters"][-1]["label"] = group_f
        if zones is not None:
            add("choropleth", tr("Parcels per zone"), group={"zones": zones.id(), "label": _text_field(zones)})
        add("table", tr("Parcels"))
    elif name == "Team activity":
        add("kpi", tr("Features"))
        who = _field_like(main, ("editor", "user", "by", "author", "surveyor", "created_by", "مساح", "المستخدم"),
                          _is_text)
        if who:
            add("leaderboard", tr("Most active"), group={"field": who, "top": 10})
        if date_f:
            add("sparkline", tr("This month"), group={"field": date_f, "time": "day"})
            add("area", tr("Changes over time"), group={"field": date_f, "time": "week"})
        new_widget(d, "feed", None, tr("Latest changes"))
        add("map", tr("Where the work is"))
    else:  # Field inspections
        add("kpi", tr("Inspections"))
        if date_f:
            add("kpi", tr("Today"), measure={"op": "count", "field": None,
                                            "where": f'to_date("{date_f}") = to_date(now())'})
            add("line", tr("Inspections per day"), group={"field": date_f, "time": "day"})
        kind = _field_like(main, ("type", "kind", "نوع", "result", "نتيجة", "status", "حالة"), _is_text) or group_f
        if kind:
            add("bar", tr("By {field}").format(field=kind), group={"field": kind, "top": 10})
            add("pie", tr("Share by {field}").format(field=kind), group={"field": kind, "top": 6})
        add("map", tr("Inspections on the map"))
        add("table", tr("Latest inspections"))
    prune_sources(d)
    return d


# ---- the grid --------------------------------------------------------------------------------------------------
class GridCanvas(QWidget):
    """The widgets on the 12-column grid: click to select, drag to move, drag the corner to resize."""
    selected = pyqtSignal(str)
    changed = pyqtSignal()

    HANDLE = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.d = model.blank()
        self.current = None
        self._drag = None
        self.setMouseTracking(True)
        self.setMinimumSize(480, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_definition(self, d):
        self.d = d
        if self.current not in {w["id"] for w in d.get("widgets") or []}:
            self.current = None
        self._fit()
        self.update()

    def _rows(self):
        return max([int(w.get("y", 0)) + int(w.get("h", 1)) for w in self.d.get("widgets") or []] + [6]) + 3

    def _fit(self):
        self.setMinimumHeight(self._rows() * ROW_PX + 2)

    def sizeHint(self):
        return QSize(720, self._rows() * ROW_PX)

    def col_px(self):
        return max(10.0, (self.width() - 2) / COLUMNS)

    def rect_of(self, w):
        c = self.col_px()
        return QRect(int(int(w.get("x", 0)) * c) + 3, int(w.get("y", 0)) * ROW_PX + 3,
                     int(int(w.get("w", 1)) * c) - 6, int(w.get("h", 1)) * ROW_PX - 6)

    def widget_at(self, pos):
        for w in reversed(self.d.get("widgets") or []):
            if self.rect_of(w).contains(pos):
                return w
        return None

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        base = pal.base().color()
        p.fillRect(self.rect(), base)
        grid = QColor(pal.mid().color())
        grid.setAlpha(60)
        p.setPen(QPen(grid, 1, Qt.PenStyle.DotLine))
        c = self.col_px()
        for i in range(COLUMNS + 1):
            p.drawLine(int(i * c), 0, int(i * c), self.height())
        for j in range(self._rows() + 1):
            p.drawLine(0, j * ROW_PX, self.width(), j * ROW_PX)
        for w in self.d.get("widgets") or []:
            r = self.rect_of(w)
            col = QColor((w.get("style") or {}).get("color") or "#607d8b")
            fill = QColor(col)
            fill.setAlpha(38)
            p.setBrush(fill)
            p.setPen(QPen(col, 3 if w["id"] == self.current else 1.4))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(pal.text().color())
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            label = (ICONS.get(w["type"], "") + "  " + (w.get("title") or tr(TYPE_LABELS.get(w["type"], ""))))
            p.drawText(r.adjusted(10, 6, -10, -6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                       | Qt.TextFlag.TextWordWrap, label)
            p.setFont(self.font())
            p.setPen(pal.placeholderText().color())
            p.drawText(r.adjusted(10, 6, -10, -6), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       tr(TYPE_LABELS.get(w["type"], w["type"])))
            if w["id"] == self.current:
                h = self.HANDLE
                p.fillRect(QRect(r.right() - h, r.bottom() - h, h, h), col)

    def mousePressEvent(self, e):  # noqa: N802
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        w = self.widget_at(pos)
        self.current = w["id"] if w else None
        if w is not None:
            r = self.rect_of(w)
            resize = pos.x() >= r.right() - self.HANDLE - 4 and pos.y() >= r.bottom() - self.HANDLE - 4
            self._drag = {"id": w["id"], "resize": resize, "start": pos, "orig": (w["x"], w["y"], w["w"], w["h"]),
                          "moved": False}
        self.selected.emit(self.current or "")
        self.update()

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        if not self._drag:
            w = self.widget_at(pos)
            if w is not None and w["id"] == self.current:
                r = self.rect_of(w)
                corner = pos.x() >= r.right() - self.HANDLE - 4 and pos.y() >= r.bottom() - self.HANDLE - 4
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if corner else Qt.CursorShape.SizeAllCursor)
            else:
                self.unsetCursor()
            return
        w = next((x for x in self.d["widgets"] if x["id"] == self._drag["id"]), None)
        if w is None:
            return
        dx = round((pos.x() - self._drag["start"].x()) / self.col_px())
        dy = round((pos.y() - self._drag["start"].y()) / ROW_PX)
        x0, y0, w0, h0 = self._drag["orig"]
        if self._drag["resize"]:
            nw, nh = max(1, min(COLUMNS - x0, w0 + dx)), max(1, h0 + dy)
            new = (x0, y0, nw, nh)
        else:
            new = (max(0, min(COLUMNS - w0, x0 + dx)), max(0, y0 + dy), w0, h0)
        if new != (w["x"], w["y"], w["w"], w["h"]):
            w["x"], w["y"], w["w"], w["h"] = new
            self._drag["moved"] = True
            self._fit()
            self.update()

    def mouseReleaseEvent(self, _e):  # noqa: N802
        if self._drag and self._drag["moved"]:
            self.changed.emit()
        self._drag = None

    def keyPressEvent(self, e):  # noqa: N802
        if self.current and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            w = next((x for x in self.d["widgets"] if x["id"] == self.current), None)
            if w is None:
                return
            grow = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            dx = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(e.key(), 0)
            dy = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(e.key(), 0)
            if grow:
                w["w"] = max(1, min(COLUMNS - w["x"], w["w"] + dx))
                w["h"] = max(1, w["h"] + dy)
            else:
                w["x"] = max(0, min(COLUMNS - w["w"], w["x"] + dx))
                w["y"] = max(0, w["y"] + dy)
            self._fit()
            self.update()
            self.changed.emit()
        else:
            super().keyPressEvent(e)


# ---- the selected widget's settings -----------------------------------------------------------------------------
class ColorButton(QPushButton):
    picked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.color = "#1565c0"
        self.clicked.connect(self._pick)
        self.setFixedWidth(64)

    def set_color(self, c):
        self.color = c or "#1565c0"
        self.setStyleSheet(f"background:{self.color}; border:1px solid #888; border-radius:4px; min-height:20px")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, tr("Colour"))
        if c.isValid():
            self.set_color(c.name())
            self.picked.emit(self.color)


class ExpressionEdit(QWidget):
    """A QGIS expression with the expression builder one click away."""
    edited = pyqtSignal()

    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.editingFinished.connect(self.edited)
        self.btn = QToolButton()
        self.btn.setText("ε")
        self.btn.setToolTip(tr("Expression builder"))
        self.btn.clicked.connect(self._build)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.btn)
        self.layer = None

    def text(self):
        return self.edit.text().strip()

    def setText(self, t):  # noqa: N802
        self.edit.setText(t or "")

    def _build(self):
        from qgis.gui import QgsExpressionBuilderDialog

        dlg = QgsExpressionBuilderDialog(self.layer, self.text(), self)
        if dlg.exec():
            self.setText(dlg.expressionText())
            self.edited.emit()


class WidgetPanel(QScrollArea):
    """What the selected widget shows and how it looks. Every edit goes straight into the definition."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.d = None
        self.w = None
        self._loading = False
        body = QWidget()
        self.setWidget(body)
        root = QVBoxLayout(body)
        self.empty = QLabel(tr("Select a widget on the grid, or add one from the list."))
        self.empty.setWordWrap(True)
        root.addWidget(self.empty)

        self.box = QWidget()
        root.addWidget(self.box)
        root.addStretch(1)
        lay = QVBoxLayout(self.box)
        lay.setContentsMargins(0, 0, 0, 0)

        g = QGroupBox(tr("Widget"))
        f = QFormLayout(g)
        self.kind = QLabel()
        self.title = QLineEdit()
        self.title.editingFinished.connect(self._save)
        f.addRow(tr("Type"), self.kind)
        f.addRow(tr("Title"), self.title)
        self.text = QPlainTextEdit()
        self.text.setMaximumHeight(80)
        self.text.textChanged.connect(self._save_soon)
        self.text_label = QLabel(tr("Text"))
        f.addRow(self.text_label, self.text)
        lay.addWidget(g)

        g = QGroupBox(tr("Data"))
        self.data_box = g
        f = QFormLayout(g)
        self.layer = QComboBox()
        self.layer.currentIndexChanged.connect(self._layer_changed)
        f.addRow(tr("Layer"), self.layer)
        self.only = ExpressionEdit(tr("all features"))
        self.only.edited.connect(self._save)
        f.addRow(tr("Only features where"), self.only)
        self.op = QComboBox()
        for k in model.OPS:
            self.op.addItem(tr(OP_LABELS[k]), k)
        self.op.currentIndexChanged.connect(self._save)
        self.op_label = QLabel(tr("Measure"))
        f.addRow(self.op_label, self.op)
        self.field = QComboBox()
        self.field.currentIndexChanged.connect(self._save)
        self.field_label = QLabel(tr("Of the field"))
        f.addRow(self.field_label, self.field)
        self.where = ExpressionEdit(tr("every feature of the layer"))
        self.where.edited.connect(self._save)
        self.where_label = QLabel(tr("Counting only"))
        f.addRow(self.where_label, self.where)
        self.done = ExpressionEdit(tr("which features are done"))
        self.done.edited.connect(self._save)
        self.done_label = QLabel(tr("Done when"))
        f.addRow(self.done_label, self.done)
        self.fields = QLineEdit()
        self.fields.setPlaceholderText(tr("field names, separated by commas"))
        self.fields.editingFinished.connect(self._save)
        self.fields_label = QLabel(tr("Columns"))
        f.addRow(self.fields_label, self.fields)
        self.filter_pick = QComboBox()
        self.filter_pick.currentIndexChanged.connect(self._save)
        self.filter_label = QLabel(tr("Filter"))
        f.addRow(self.filter_label, self.filter_pick)
        lay.addWidget(g)

        g = QGroupBox(tr("Map layers"))
        self.map_box = g
        v = QVBoxLayout(g)
        self.mlist = QListWidget()
        self.mlist.setMaximumHeight(110)
        self.mlist.currentRowChanged.connect(self._map_pick)
        v.addWidget(self.mlist)
        row = QHBoxLayout()
        for text, tip, fn in (("+", "Add a layer", self._map_add), ("−", "Remove the layer", self._map_remove),
                              ("↑", "Draw it higher", lambda: self._map_move(1)),
                              ("↓", "Draw it lower", lambda: self._map_move(-1))):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tr(tip))
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        f = QFormLayout()
        v.addLayout(f)
        self.ml_layer = QComboBox()
        self.ml_layer.currentIndexChanged.connect(self._map_layer_changed)
        f.addRow(tr("Layer"), self.ml_layer)
        self.ml_where = ExpressionEdit(tr("all features"))
        self.ml_where.edited.connect(self._map_save)
        f.addRow(tr("Only features where"), self.ml_where)
        self.ml_kind = QComboBox()
        for k, label in MAP_STYLES.items():
            self.ml_kind.addItem(tr(label), k)
        self.ml_kind.currentIndexChanged.connect(self._map_kind)
        f.addRow(tr("Style"), self.ml_kind)
        self.ml_field = QComboBox()
        self.ml_field.currentIndexChanged.connect(self._map_save)
        self.ml_field_label = QLabel(tr("Field"))
        f.addRow(self.ml_field_label, self.ml_field)
        self.ml_classes = QSpinBox()
        self.ml_classes.setRange(2, 9)
        self.ml_classes.setValue(5)
        self.ml_classes.valueChanged.connect(self._map_save)
        self.ml_classes_label = QLabel(tr("Classes"))
        f.addRow(self.ml_classes_label, self.ml_classes)
        self.ml_color = ColorButton()
        self.ml_color.picked.connect(self._map_save)
        self.ml_color_label = QLabel(tr("Colour"))
        f.addRow(self.ml_color_label, self.ml_color)
        self.ml_opacity = QSpinBox()
        self.ml_opacity.setRange(5, 100)
        self.ml_opacity.setSuffix(" %")
        self.ml_opacity.setValue(75)
        self.ml_opacity.valueChanged.connect(self._map_save)
        f.addRow(tr("Fill opacity"), self.ml_opacity)
        self.ml_label = QComboBox()
        self.ml_label.currentIndexChanged.connect(self._map_save)
        f.addRow(tr("Label"), self.ml_label)
        self.ml_popup = QLineEdit()
        self.ml_popup.setPlaceholderText(tr("field names, separated by commas"))
        self.ml_popup.editingFinished.connect(self._map_save)
        f.addRow(tr("Shown on a tap"), self.ml_popup)
        self.ml_basemap = QCheckBox(tr("The project's basemap under the map (orthophoto, tiles)"))
        self.ml_basemap.toggled.connect(self._map_save)
        v.addWidget(self.ml_basemap)
        lay.addWidget(g)

        g = QGroupBox(tr("Groups"))
        self.group_box = g
        f = QFormLayout(g)
        self.gkind = QComboBox()
        for k, label in (("", "No groups"), ("field", "By the values of a field"), ("time", "By time"),
                         ("zones", "By zones (polygons)")):
            self.gkind.addItem(tr(label), k)
        self.gkind.currentIndexChanged.connect(self._group_kind)
        f.addRow(tr("Group"), self.gkind)
        self.gfield = QComboBox()
        self.gfield.currentIndexChanged.connect(self._save)
        self.gfield_label = QLabel(tr("Field"))
        f.addRow(self.gfield_label, self.gfield)
        self.gtime = QComboBox()
        for k in model.TIMES:
            self.gtime.addItem(tr(TIME_LABELS[k]), k)
        self.gtime.currentIndexChanged.connect(self._save)
        self.gtime_label = QLabel(tr("Every"))
        f.addRow(self.gtime_label, self.gtime)
        self.zones = QComboBox()
        self.zones.currentIndexChanged.connect(self._zones_changed)
        self.zones_label = QLabel(tr("Zones layer"))
        f.addRow(self.zones_label, self.zones)
        self.zlabel = QComboBox()
        self.zlabel.currentIndexChanged.connect(self._save)
        self.zlabel_label = QLabel(tr("Zone name"))
        f.addRow(self.zlabel_label, self.zlabel)
        self.top = QSpinBox()
        self.top.setRange(0, 200)
        self.top.setSpecialValueText(tr("all"))
        self.top.valueChanged.connect(self._save)
        f.addRow(tr("Largest groups only"), self.top)
        lay.addWidget(g)

        g = QGroupBox(tr("Look"))
        f = QFormLayout(g)
        self.color = ColorButton()
        self.color.picked.connect(self._save)
        f.addRow(tr("Colour"), self.color)
        self.icon = QLineEdit()
        self.icon.setPlaceholderText(tr("an emoji or a short sign"))
        self.icon.setMaxLength(8)
        self.icon.editingFinished.connect(self._save)
        f.addRow(tr("Icon"), self.icon)
        self.unit = QLineEdit()
        self.unit.setMaxLength(16)
        self.unit.editingFinished.connect(self._save)
        f.addRow(tr("Unit"), self.unit)
        self.decimals = QSpinBox()
        self.decimals.setRange(0, 6)
        self.decimals.valueChanged.connect(self._save)
        f.addRow(tr("Decimals"), self.decimals)
        self.has_target = QCheckBox(tr("Target"))
        self.target = QDoubleSpinBox()
        self.target.setRange(-1e12, 1e12)
        self.target.setDecimals(2)
        self.has_target.toggled.connect(self._save)
        self.target.valueChanged.connect(self._save)
        row = QHBoxLayout()
        row.addWidget(self.has_target)
        row.addWidget(self.target, 1)
        f.addRow(tr("Goal"), row)
        self.thresholds = QLineEdit()
        self.thresholds.setPlaceholderText("900:#f9a825; 1100:#c62828")
        self.thresholds.setToolTip(tr("From this value on, the number takes this colour. Separate the steps with ;"))
        self.thresholds.editingFinished.connect(self._save)
        f.addRow(tr("Colour steps"), self.thresholds)
        lay.addWidget(g)

        self.box.hide()
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.setInterval(500)
        self._soon.timeout.connect(self._save)

    # ---- show -------------------------------------------------------------------------------------------------
    def show_widget(self, d, wid):
        self.d = d
        self.w = next((w for w in d.get("widgets") or [] if w["id"] == wid), None) if wid else None
        self.empty.setVisible(self.w is None)
        self.box.setVisible(self.w is not None)
        if self.w is None:
            return
        self._loading = True
        try:
            self._fill()
        finally:
            self._loading = False

    def _layer_obj(self):
        lid = self.layer.currentData()
        return QgsProject.instance().mapLayer(lid) if lid else None

    def _fill(self):
        w, d = self.w, self.d
        t = w["type"]
        self.kind.setText(tr(TYPE_LABELS.get(t, t)))
        self.title.setText(w.get("title") or "")
        is_text = t == "text"
        self.text.setVisible(is_text)
        self.text_label.setVisible(is_text)
        if is_text:
            self.text.setPlainText((w.get("options") or {}).get("text") or "")
        uses_data = t in model.NEEDS_SOURCE or t == "filter"
        self.data_box.setVisible(uses_data)
        src = (d.get("sources") or {}).get(w.get("source")) or {}
        self.layer.clear()
        for lyr in _vector_layers():
            self.layer.addItem(lyr.name(), lyr.id())
        i = self.layer.findData(src.get("layer"))
        self.layer.setCurrentIndex(max(0, i))
        lyr = self._layer_obj()
        self.only.layer = self.where.layer = self.done.layer = lyr
        self.only.setText(src.get("filter") or "")
        m = w.get("measure") or {}
        has_measure = t in model.NEEDS_SOURCE and t not in ("map", "table")
        for x in (self.op, self.op_label, self.field, self.field_label, self.where, self.where_label):
            x.setVisible(has_measure)
        self.op.setCurrentIndex(max(0, self.op.findData(m.get("op") or "count")))
        self._fields(self.field, lyr, m.get("field"), blank=True)
        self.where.setText(m.get("where") or "")
        self.done.setVisible(t == "progress")
        self.done_label.setVisible(t == "progress")
        self.done.setText((w.get("options") or {}).get("done") or "")
        self.fields.setVisible(t == "table")
        self.fields_label.setVisible(t == "table")
        self.fields.setText(", ".join((w.get("options") or {}).get("fields") or []))
        is_filter = t == "filter"
        self.filter_pick.setVisible(is_filter)
        self.filter_label.setVisible(is_filter)
        for x in (self.layer, self.only):
            x.setVisible(not is_filter)
        self.filter_pick.clear()
        for flt in d.get("filters") or []:
            self.filter_pick.addItem(flt.get("label") or flt.get("field"), flt["id"])
        self.filter_pick.setCurrentIndex(max(0, self.filter_pick.findData((w.get("options") or {}).get("filter"))))

        is_map = t == "map"
        self.map_box.setVisible(is_map)
        if is_map:
            self.data_box.setVisible(False)
            self._map_fill()
        grouped = t in model.NEEDS_GROUP or t in ("choropleth", "kpi", "gauge", "table", "map")
        self.group_box.setVisible(grouped and t not in ("map", "table", "kpi", "gauge"))
        g = w.get("group") or {}
        kind = "zones" if g.get("zones") else "time" if g.get("time") else "field" if g.get("field") else ""
        self.gkind.setCurrentIndex(max(0, self.gkind.findData(kind)))
        self._fields(self.gfield, lyr, g.get("field"))
        self.gtime.setCurrentIndex(max(0, self.gtime.findData(g.get("time") or "month")))
        self.zones.clear()
        for z in _polygon_layers():
            self.zones.addItem(z.name(), z.id())
        self.zones.setCurrentIndex(max(0, self.zones.findData(g.get("zones"))))
        zl = QgsProject.instance().mapLayer(self.zones.currentData()) if self.zones.currentData() else None
        self._fields(self.zlabel, zl, g.get("label"))
        self.top.setValue(int(g.get("top") or 0))
        self._group_rows(kind)

        s = w.get("style") or {}
        self.color.set_color(s.get("color"))
        self.icon.setText(s.get("icon") or "")
        self.unit.setText(s.get("unit") or "")
        self.decimals.setValue(int(s.get("decimals") or 0))
        self.has_target.setChecked(s.get("target") is not None)
        self.target.setValue(float(s.get("target") or 0))
        self.thresholds.setText("; ".join(f"{_num(x.get('at'))}:{x.get('color')}" for x in s.get("thresholds") or []))

    # ---- the map's layers ------------------------------------------------------------------------------------
    def _map_entries(self):
        return map_layers_of(self.d, self.w)

    def _map_fill(self, row=0):
        entries = self._map_entries()
        self.mlist.blockSignals(True)
        self.mlist.clear()
        for L in reversed(entries):   # the top of the list is drawn on top
            self.mlist.addItem(L.get("name") or "?")
        self.mlist.blockSignals(False)
        self.ml_basemap.setChecked((self.w.get("options") or {}).get("basemap") == "project")
        if entries:
            self.mlist.setCurrentRow(max(0, min(row, len(entries) - 1)))
        self._map_pick(self.mlist.currentRow())

    def _map_index(self, row=None):
        entries = self._map_entries()
        row = self.mlist.currentRow() if row is None else row
        return len(entries) - 1 - row if 0 <= row < len(entries) else -1

    def _map_pick(self, row):
        entries = self._map_entries()
        i = self._map_index(row)
        on = i >= 0
        for x in (self.ml_layer, self.ml_where, self.ml_kind, self.ml_field, self.ml_classes, self.ml_color,
                  self.ml_opacity, self.ml_label, self.ml_popup):
            x.setEnabled(on)
        if not on:
            return
        L = entries[i]
        was = self._loading
        self._loading = True
        try:
            src = (self.d.get("sources") or {}).get(L.get("source")) or {}
            self.ml_layer.clear()
            for lyr in _vector_layers():
                self.ml_layer.addItem(lyr.name(), lyr.id())
            self.ml_layer.setCurrentIndex(max(0, self.ml_layer.findData(src.get("layer"))))
            lyr = QgsProject.instance().mapLayer(self.ml_layer.currentData()) if self.ml_layer.currentData() else None
            self.ml_where.layer = lyr
            self.ml_where.setText(L.get("where") or "")
            st = L.get("style") or {}
            self.ml_kind.setCurrentIndex(max(0, self.ml_kind.findData(st.get("kind") or "layer")))
            self._fields(self.ml_field, lyr, st.get("field"))
            self.ml_classes.setValue(int(st.get("classes") or 5))
            self.ml_color.set_color(st.get("color"))
            self.ml_opacity.setValue(int(round(100 * float(st.get("opacity") if st.get("opacity") is not None
                                                           else 0.75))))
            self._fields(self.ml_label, lyr, L.get("label"), blank=True)
            self.ml_popup.setText(", ".join(L.get("popup") or []))
            self._map_rows()
        finally:
            self._loading = was

    def _map_rows(self):
        k = self.ml_kind.currentData()
        for x in (self.ml_field, self.ml_field_label):
            x.setVisible(k in ("categories", "graduated"))
        for x in (self.ml_classes, self.ml_classes_label):
            x.setVisible(k == "graduated")
        for x in (self.ml_color, self.ml_color_label):
            x.setVisible(k == "single")

    def _map_kind(self):
        self._map_rows()
        self._map_save()

    def _map_layer_changed(self):
        if self._loading:
            return
        lyr = QgsProject.instance().mapLayer(self.ml_layer.currentData()) if self.ml_layer.currentData() else None
        self._loading = True
        try:
            self.ml_where.layer = lyr
            self._fields(self.ml_field, lyr, _status_field(lyr) or _text_field(lyr))
            self._fields(self.ml_label, lyr, None, blank=True)
            self.ml_popup.setText(", ".join(f.name() for f in lyr.fields())[:200] if lyr is not None else "")
        finally:
            self._loading = False
        self._map_save()

    def _map_save(self, *_a):
        if self._loading or self.w is None or self.w.get("type") != "map":
            return
        entries = self._map_entries()
        o = self.w.setdefault("options", {})
        o["basemap"] = "project" if self.ml_basemap.isChecked() else "none"
        i = self._map_index()
        if i >= 0:
            lid = self.ml_layer.currentData()
            lyr = QgsProject.instance().mapLayer(lid) if lid else None
            L = entries[i]
            if lyr is not None:
                L["source"] = source_for(self.d, lid)
                L["name"] = lyr.name()
            where = self.ml_where.text()
            if where:
                L["where"] = where
            else:
                L.pop("where", None)
            k = self.ml_kind.currentData() or "layer"
            st = {"kind": k, "opacity": round(self.ml_opacity.value() / 100.0, 2)}
            if k in ("categories", "graduated"):
                st["field"] = self.ml_field.currentData()
            if k == "graduated":
                st["classes"] = self.ml_classes.value()
            if k == "single":
                st["color"] = self.ml_color.color
            old = L.get("style") or {}
            for keep in ("outline", "width", "size", "palette", "colors"):
                if keep in old:
                    st[keep] = old[keep]
            L["style"] = st
            L["label"] = self.ml_label.currentData()
            L["popup"] = [x.strip() for x in self.ml_popup.text().split(",") if x.strip()][:12]
            item = self.mlist.item(self.mlist.currentRow())
            if item is not None:
                item.setText(L.get("name") or "?")
        if entries:
            self.w["source"] = entries[0]["source"]
        prune_sources(self.d)
        self.changed.emit()

    def _map_add(self):
        if self.w is None:
            return
        entries = self._map_entries()
        have = {((self.d.get("sources") or {}).get(L.get("source")) or {}).get("layer") for L in entries}
        lyr = next((x for x in _vector_layers() if x.id() not in have), None) or _first_vector()
        if lyr is None:
            return
        entries.append(map_layer(self.d, lyr))
        self._map_fill(0)
        self._map_save()

    def _map_remove(self):
        entries = self._map_entries()
        i = self._map_index()
        if i < 0 or len(entries) <= 1:
            return
        entries.pop(i)
        self._map_fill(0)
        self._map_save()

    def _map_move(self, step):
        entries = self._map_entries()
        i = self._map_index()
        j = i + step
        if i < 0 or not 0 <= j < len(entries):
            return
        entries[i], entries[j] = entries[j], entries[i]
        self._map_fill(len(entries) - 1 - j)
        self._map_save()

    @staticmethod
    def _fields(combo, layer, current, blank=False):
        combo.blockSignals(True)
        combo.clear()
        if blank:
            combo.addItem("—", None)
        if layer is not None:
            for f in layer.fields():
                combo.addItem(f.name(), f.name())
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.blockSignals(False)

    def _group_rows(self, kind):
        for x in (self.gfield, self.gfield_label):
            x.setVisible(kind in ("field", "time"))
        for x in (self.gtime, self.gtime_label):
            x.setVisible(kind == "time")
        for x in (self.zones, self.zones_label, self.zlabel, self.zlabel_label):
            x.setVisible(kind == "zones")

    # ---- edit -------------------------------------------------------------------------------------------------
    def _layer_changed(self):
        if self._loading or self.w is None:
            return
        lyr = self._layer_obj()
        self.only.layer = self.where.layer = self.done.layer = lyr
        self._loading = True
        try:
            self._fields(self.field, lyr, None, blank=True)
            self._fields(self.gfield, lyr, _text_field(lyr))
        finally:
            self._loading = False
        self._save()

    def _zones_changed(self):
        if self._loading:
            return
        zl = QgsProject.instance().mapLayer(self.zones.currentData()) if self.zones.currentData() else None
        self._loading = True
        try:
            self._fields(self.zlabel, zl, _text_field(zl))
        finally:
            self._loading = False
        self._save()

    def _group_kind(self):
        self._group_rows(self.gkind.currentData())
        self._save()

    def _save_soon(self):
        if not self._loading:
            self._soon.start()

    def _save(self, *_a):
        if self._loading or self.w is None:
            return
        w, d = self.w, self.d
        t = w["type"]
        w["title"] = self.title.text().strip()
        opts = dict(w.get("options") or {})
        if t == "text":
            opts["text"] = self.text.toPlainText()[:4000]
        if t in model.NEEDS_SOURCE and t != "map":
            lid = self.layer.currentData()
            if lid:
                w["source"] = source_for(d, lid, self.only.text())
            if t not in ("map", "table"):
                m = {"op": self.op.currentData() or "count", "field": self.field.currentData(),
                     "where": self.where.text()}
                if m["op"] == "ratio":
                    m["denominator"] = (w.get("measure") or {}).get("denominator") or {"op": "count", "field": None,
                                                                                          "where": ""}
                w["measure"] = m
            kind = self.gkind.currentData()
            if self.group_box.isVisible() or t in model.NEEDS_GROUP or t == "choropleth":
                g = None
                if kind == "field" and self.gfield.currentData():
                    g = {"field": self.gfield.currentData()}
                elif kind == "time" and self.gfield.currentData():
                    g = {"field": self.gfield.currentData(), "time": self.gtime.currentData()}
                elif kind == "zones" and self.zones.currentData():
                    g = {"zones": self.zones.currentData(), "label": self.zlabel.currentData()}
                if g is not None and self.top.value():
                    g["top"] = self.top.value()
                w["group"] = g
        if t == "progress":
            opts["done"] = self.done.text() or "true"
        if t == "table":
            opts["fields"] = [x.strip() for x in self.fields.text().split(",") if x.strip()][:30]
        if t == "filter":
            opts["filter"] = self.filter_pick.currentData()
        w["options"] = opts
        s = dict(w.get("style") or {})
        s.update(color=self.color.color, icon=self.icon.text().strip(), unit=self.unit.text().strip(),
                 decimals=self.decimals.value(),
                 target=self.target.value() if self.has_target.isChecked() else None,
                 thresholds=parse_thresholds(self.thresholds.text()))
        w["style"] = s
        prune_sources(d)
        self.changed.emit()


def parse_thresholds(text):
    out = []
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        at, color = part.split(":", 1)
        try:
            v = float(at.strip())
        except ValueError:
            continue
        c = color.strip()
        if QColor(c).isValid():
            out.append({"at": v, "color": QColor(c).name()})
    return sorted(out, key=lambda x: x["at"])


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else str(f)


# ---- the dashboard's filters ------------------------------------------------------------------------------------
class FiltersDialog(QDialog):
    def __init__(self, d, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Filters"))
        self.d = d
        self.rows = copy.deepcopy(d.get("filters") or [])
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("A filter is a choice the viewer makes (a district, a status); every widget of the "
                                "same layer follows it.")))
        self.list = QListWidget()
        lay.addWidget(self.list)
        f = QFormLayout()
        self.layer = QComboBox()
        for lyr in _vector_layers():
            self.layer.addItem(lyr.name(), lyr.id())
        self.layer.currentIndexChanged.connect(self._fields)
        self.field = QComboBox()
        self.label = QLineEdit()
        f.addRow(tr("Layer"), self.layer)
        f.addRow(tr("Field"), self.field)
        f.addRow(tr("Label"), self.label)
        lay.addLayout(f)
        row = QHBoxLayout()
        add = QPushButton(tr("Add"))
        add.clicked.connect(self._add)
        rem = QPushButton(tr("Remove"))
        rem.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fields()
        self._refresh()

    def _fields(self):
        lyr = QgsProject.instance().mapLayer(self.layer.currentData()) if self.layer.currentData() else None
        WidgetPanel._fields(self.field, lyr, _text_field(lyr))

    def _refresh(self):
        self.list.clear()
        for r in self.rows:
            src = (self.d.get("sources") or {}).get(r.get("source")) or {}
            lyr = QgsProject.instance().mapLayer(src.get("layer") or "")
            self.list.addItem(f"{r.get('label') or r.get('field')}  —  {lyr.name() if lyr else '?'} · {r.get('field')}")

    def _add(self):
        if not self.layer.currentData() or not self.field.currentData():
            return
        ids = {r["id"] for r in self.rows}
        n = 1
        while f"f{n}" in ids:
            n += 1
        sid = source_for(self.d, self.layer.currentData())
        self.rows.append({"id": f"f{n}", "source": sid, "field": self.field.currentData(),
                          "label": self.label.text().strip() or self.field.currentData()})
        self.label.clear()
        self._refresh()

    def _remove(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.rows):
            del self.rows[i]
            self._refresh()

    def result_filters(self):
        return self.rows


# ---- the live preview ------------------------------------------------------------------------------------------
class Preview(QWidget):
    """The live preview: the very page the phones open, fed the definition being edited (unsaved), on this
    PC's browser or on a phone on the same network (its QR code). The page runs in the browser, never inside
    Biruni: a browser fault cannot take Biruni down (as the bridge's hidden page, which has its own process)."""

    def __init__(self, sharing_provider, lang="en", parent=None):
        super().__init__(parent)
        self.sharing_provider = sharing_provider
        self.lang = lang
        self.sharing = None
        self.key = None
        self._def = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        head = QLabel(tr("Live preview"))
        f = QFont(head.font())
        f.setBold(True)
        head.setFont(f)
        lay.addWidget(head)
        note = QLabel(tr("The page the viewers get, with the dashboard as you edit it (not saved yet). Every change "
                         "shows at once."))
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QHBoxLayout()
        self.open_b = QPushButton(tr("Open the preview in the browser"))
        self.open_b.clicked.connect(self.open_browser)
        self.phone_b = QPushButton(tr("Preview on a phone…"))
        self.phone_b.clicked.connect(self.show_phone)
        row.addWidget(self.open_b)
        row.addWidget(self.phone_b)
        row.addStretch(1)
        lay.addLayout(row)
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.qr)
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        lay.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._tick)

    def start(self):
        try:
            self.sharing = self.sharing_provider()
            self.key = self.sharing.new_preview(copy.deepcopy(self._def) if self._def else model.blank())
        except Exception as e:  # noqa: BLE001 - no port: the builder still works, without a preview
            self.sharing = None
            self.state.setText(tr("The preview cannot start: {why}").format(why=e))
            self.open_b.setEnabled(False)
            self.phone_b.setEnabled(False)
            return
        self.timer.start()
        self._tick()

    def show_definition(self, d):
        self._def = copy.deepcopy(d)
        if self.sharing is not None and self.key:
            self.sharing.update_preview(self.key, self._def)

    def links(self):
        return self.sharing.preview_links(self.key) if self.sharing is not None and self.key else {}

    def open_browser(self):
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices

        url = self.links().get("local")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def show_phone(self):
        lan = self.links().get("lan") or []
        if not lan:
            self.state.setText(tr("This PC is not on a network: the preview opens on this PC only."))
            return
        from .dock import qr_pixmap

        self.qr.setPixmap(qr_pixmap(lan[0], scale=4))
        self.qr.setToolTip(lan[0])

    def previewers(self):
        return self.sharing.previewers(self.key) if self.sharing is not None and self.key else 0

    def _tick(self):
        n = self.previewers()
        self.state.setText(tr("Previews open now: {n}").format(n=n) if n else
                           tr("No preview is open. The page opens in any browser."))

    def stop(self):
        self.timer.stop()
        if self.sharing is not None and self.key:
            _quiet(self.sharing.end_preview, self.key)
        self.key = None


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001
        pass


def _themes():
    """The looks the page can draw (page.py), the engine's own first."""
    try:
        from .page import THEMES
    except ImportError:
        THEMES = ()
    return tuple(dict.fromkeys(tuple(model.THEMES) + tuple(THEMES)))


def _service():
    from .service import DashboardService

    return DashboardService(QgsProject.instance())


_OWN = [None]


def own_sharing():
    """The builder's previews need the dashboard server: the panel's when it is open, else one of the builder's
    own (started now; it stops with Biruni)."""
    try:
        from .dock import _DOCK

        if _DOCK[0] is not None:
            if _DOCK[0].sharing is None:
                _DOCK[0].sharing = _DOCK[0]._make_sharing()
            return _DOCK[0].sharing
    except Exception:  # noqa: BLE001
        pass
    if _OWN[0] is None:
        from ..i18n import lang
        from .sharing import Sharing

        _OWN[0] = Sharing(_service(), lang=lang() or "en")
    return _OWN[0]


# ---- the builder -----------------------------------------------------------------------------------------------
class BuilderDialog(QDialog):
    saved = pyqtSignal(str)

    def __init__(self, definition=None, parent=None, lang="en", preview=True, sharing_provider=own_sharing):
        super().__init__(parent)
        self.setWindowTitle(tr("Dashboard builder"))
        self.resize(1280, 820)
        self.d = copy.deepcopy(definition) if definition else model.blank(tr("Dashboard"))
        self._undo, self._redo = [], []
        self._last = json.dumps(self.d, sort_keys=True)
        self.dirty = False

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.title = QLineEdit(self.d.get("title") or "")
        self.title.setPlaceholderText(tr("Dashboard title"))
        self.title.editingFinished.connect(self._settings)
        top.addWidget(QLabel(tr("Title")))
        top.addWidget(self.title, 2)
        self.theme = QComboBox()
        for k in _themes():
            self.theme.addItem(tr(THEME_LABELS.get(k, k)), k)
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.d.get("theme"))))
        self.theme.currentIndexChanged.connect(self._settings)
        top.addWidget(QLabel(tr("Theme")))
        top.addWidget(self.theme)
        self.dates = QComboBox()
        for k in ("both", "greg", "hijri"):
            self.dates.addItem(tr(DATE_LABELS[k]), k)
        self.dates.setCurrentIndex(max(0, self.dates.findData(self.d.get("dates") or "both")))
        self.dates.currentIndexChanged.connect(self._settings)
        top.addWidget(QLabel(tr("Dates")))
        top.addWidget(self.dates)
        self.k_min = QSpinBox()
        self.k_min.setRange(0, 1000)
        self.k_min.setSpecialValueText(tr("off"))
        self.k_min.setValue(int(self.d.get("k_min") or 0))
        self.k_min.setToolTip(tr("Groups with fewer features than this are hidden from viewers, so no one can be "
                                 "picked out of a small group."))
        self.k_min.valueChanged.connect(self._settings)
        top.addWidget(QLabel(tr("Hide groups under")))
        top.addWidget(self.k_min)
        flt = QPushButton(tr("Filters…"))
        flt.clicked.connect(self._filters)
        top.addWidget(flt)
        tpl = QPushButton(tr("Templates"))
        menu = QMenu(tpl)
        for name in TEMPLATES:
            menu.addAction(tr(name), lambda n=name: self.apply_template(n))
        tpl.setMenu(menu)
        top.addWidget(tpl)
        self.undo_b = QToolButton()
        self.undo_b.setText("↶")
        self.undo_b.setToolTip(tr("Undo"))
        self.undo_b.clicked.connect(self.undo)
        self.redo_b = QToolButton()
        self.redo_b.setText("↷")
        self.redo_b.setToolTip(tr("Redo"))
        self.redo_b.clicked.connect(self.redo)
        top.addWidget(self.undo_b)
        top.addWidget(self.redo_b)
        root.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel(tr("Add a widget")))
        self.palette = QListWidget()
        for k in model.TYPES:
            it = QListWidgetItem(f"{ICONS.get(k, '')}  {tr(TYPE_LABELS[k])}")
            it.setData(Qt.ItemDataRole.UserRole, k)
            it.setToolTip(tr(TYPE_HINTS[k]))
            self.palette.addItem(it)
        self.palette.itemDoubleClicked.connect(lambda it: self.add(it.data(Qt.ItemDataRole.UserRole)))
        ll.addWidget(self.palette, 1)
        addb = QPushButton(tr("Add"))
        addb.clicked.connect(lambda: self.palette.currentItem() and self.add(
            self.palette.currentItem().data(Qt.ItemDataRole.UserRole)))
        ll.addWidget(addb)
        row = QHBoxLayout()
        dup = QPushButton(tr("Duplicate"))
        dup.clicked.connect(self.duplicate)
        rem = QPushButton(tr("Remove"))
        rem.clicked.connect(self.remove)
        row.addWidget(dup)
        row.addWidget(rem)
        ll.addLayout(row)
        split.addWidget(left)

        mid = QSplitter(Qt.Orientation.Vertical)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.grid = GridCanvas()
        self.grid.set_definition(self.d)
        self.grid.selected.connect(self._select)
        self.grid.changed.connect(self._changed)
        scroll.setWidget(self.grid)
        mid.addWidget(scroll)
        self.preview = Preview(sharing_provider, lang) if preview else None
        if self.preview is not None:
            mid.addWidget(self.preview)
            mid.setSizes([420, 360])
        split.addWidget(mid)

        self.panel = WidgetPanel()
        self.panel.changed.connect(self._changed)
        split.addWidget(self.panel)
        split.setSizes([200, 760, 320])
        root.addWidget(split, 1)

        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet("color:#c62828")
        root.addWidget(self.problems)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.setInterval(400)
        self._soon.timeout.connect(self._refresh_preview)
        self._buttons()
        if self.preview is not None:
            self.preview.show_definition(self.d)
            self.preview.start()
        self._check()

    # ---- edits ------------------------------------------------------------------------------------------------
    def _remember(self):
        now = json.dumps(self.d, sort_keys=True)
        if now != self._last:
            self._undo.append(self._last)
            del self._undo[:-100]
            self._redo.clear()
            self._last = now
            self.dirty = True
        self._buttons()

    def _buttons(self):
        self.undo_b.setEnabled(bool(self._undo))
        self.redo_b.setEnabled(bool(self._redo))

    def _changed(self):
        self._remember()
        self.grid.set_definition(self.d)
        self._check()
        self._soon.start()

    def _settings(self, *_a):
        self.d["title"] = self.title.text().strip()
        self.d["theme"] = self.theme.currentData()
        self.d["dates"] = self.dates.currentData()
        self.d["k_min"] = self.k_min.value()
        self._changed()

    def _restore(self, raw):
        self.d = json.loads(raw)
        self._last = raw
        self.title.setText(self.d.get("title") or "")
        for combo, key in ((self.theme, "theme"), (self.dates, "dates")):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(self.d.get(key))))
            combo.blockSignals(False)
        self.k_min.blockSignals(True)
        self.k_min.setValue(int(self.d.get("k_min") or 0))
        self.k_min.blockSignals(False)
        self.grid.set_definition(self.d)
        self.panel.show_widget(self.d, self.grid.current)
        self._buttons()
        self._check()
        self._soon.start()

    def undo(self):
        if self._undo:
            self._redo.append(self._last)
            self._restore(self._undo.pop())
            self.dirty = True

    def redo(self):
        if self._redo:
            self._undo.append(self._last)
            self._restore(self._redo.pop())
            self.dirty = True

    def add(self, kind):
        w = new_widget(self.d, kind)
        self.grid.current = w["id"]
        self._changed()
        self.panel.show_widget(self.d, w["id"])
        return w

    def duplicate(self):
        w = next((x for x in self.d["widgets"] if x["id"] == self.grid.current), None)
        if w is None:
            return
        c = copy.deepcopy(w)
        ids = {x["id"] for x in self.d["widgets"]}
        n = 1
        while f"w{n}" in ids:
            n += 1
        c["id"] = f"w{n}"
        c["x"], c["y"] = free_spot(self.d, c["w"], c["h"])
        self.d["widgets"].append(c)
        self.grid.current = c["id"]
        self._changed()
        self.panel.show_widget(self.d, c["id"])

    def remove(self):
        self.d["widgets"] = [x for x in self.d["widgets"] if x["id"] != self.grid.current]
        self.grid.current = None
        prune_sources(self.d)
        self._changed()
        self.panel.show_widget(self.d, None)

    def _select(self, wid):
        self.panel.show_widget(self.d, wid or None)

    def _filters(self):
        dlg = FiltersDialog(self.d, self)
        if dlg.exec():
            self.d["filters"] = dlg.result_filters()
            ids = {f["id"] for f in self.d["filters"]}
            for w in self.d["widgets"]:
                if w["type"] == "filter" and (w.get("options") or {}).get("filter") not in ids:
                    w.setdefault("options", {})["filter"] = next(iter(ids), None)
            prune_sources(self.d)
            self._changed()

    def apply_template(self, name):
        if self.d.get("widgets") and QMessageBox.question(
                self, tr("Templates"), tr("Replace the widgets of this dashboard with the template?")) != \
                QMessageBox.StandardButton.Yes:
            return
        t = template(name)
        keep = self.d.get("id")
        self.d = t
        self.d["id"] = keep or t["id"]
        self.title.setText(self.d["title"])
        self.grid.current = None
        self._changed()
        self.panel.show_widget(self.d, None)

    # ---- check, preview, save ---------------------------------------------------------------------------------
    def problems_now(self):
        return model.validate(self.d)

    def _check(self):
        p = self.problems_now()
        self.problems.setText((tr("To fix before saving: ") + "; ".join(p[:4]) + (" …" if len(p) > 4 else ""))
                              if p else "")

    def _refresh_preview(self):
        if self.preview is not None:
            self.preview.show_definition(self.d)

    def save(self, warn=True):
        p = self.problems_now()
        if p:
            if not warn:
                return False
            QMessageBox.warning(self, tr("Dashboard builder"), tr("The dashboard cannot be saved yet:") + "\n\n"
                                + "\n".join(p[:12]))
            return False
        self.d = model.save(QgsProject.instance(), self.d)
        self.dirty = False
        self.saved.emit(self.d["id"])
        return True

    def done(self, r):  # noqa: D401 - QDialog.done
        if self.preview is not None:
            self.preview.stop()
        super().done(r)


def builder_ui(plugin, dash_id=None, parent=None):
    """Open the builder for a dashboard of the project (a new one when dash_id is None)."""
    from ..i18n import lang

    d = model.get(QgsProject.instance(), dash_id) if dash_id else None
    dlg = BuilderDialog(d, parent or plugin.iface.mainWindow(), lang=lang() or "en")
    dlg.exec()
    return dlg
